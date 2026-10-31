"""Funnel tests. Run with:  python3 -m unittest discover -s tests

End-to-end tests run the real CLI in a throwaway HOME with a fake ``wine`` on
PATH, so they need no display, no Wine, and never touch the real home folder.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funnel.archive import _safe_member_name  # noqa: E402
from funnel.detect import _slugify, find_main_exe  # noqa: E402
from funnel.runtime import RuntimeKind, RuntimeSpec, render_launcher, runtime_environment  # noqa: E402


def _exe(path: Path, size: int = 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\0" * size)
    return path


class ArchiveSafety(unittest.TestCase):
    def test_rejects_traversal_and_absolute_members(self):
        for bad in ("../evil.exe", "a/../../evil", "/etc/passwd", "C:\\Windows\\x.exe", "a\\..\\..\\x", ""):
            self.assertFalse(_safe_member_name(bad), bad)

    def test_accepts_normal_members(self):
        for good in ("Game/Game.exe", "data\\level1.pak", "readme.txt", "dots..in..name.txt"):
            self.assertTrue(_safe_member_name(good), good)


class Detection(unittest.TestCase):
    def test_prefers_game_over_uninstaller_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Cool Game"
            _exe(root / "unins000.exe", 5_000_000)
            _exe(root / "Config.exe", 5_000_000)
            game = _exe(root / "CoolGame.exe", 2_000)
            self.assertEqual(find_main_exe(root), game)

    def test_prefers_top_level_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Thing"
            top = _exe(root / "Thing.exe")
            _exe(root / "bin" / "Huge.exe", 9_000_000)
            self.assertEqual(find_main_exe(root), top)

    def test_slugify(self):
        self.assertEqual(_slugify("  Cool Game: Deluxe!  "), "cool-game-deluxe")
        self.assertEqual(_slugify("???"), "windows-app")


class Runtime(unittest.TestCase):
    spec = RuntimeSpec(RuntimeKind.WINE, Path("/usr/bin/wine"), Path("/tmp/prefix"))

    def test_menu_builder_always_disabled(self):
        self.assertEqual(runtime_environment(self.spec)["WINEDLLOVERRIDES"], "winemenubuilder.exe=d")
        merged = runtime_environment(self.spec, {"WINEDLLOVERRIDES": "winhttp=n,b"})
        self.assertEqual(merged["WINEDLLOVERRIDES"], "winhttp=n,b;winemenubuilder.exe=d")

    def test_launcher_quotes_hostile_paths(self):
        script = render_launcher(self.spec, Path("/games/it's $(rm -rf ~)/g.exe"), slug="g")
        self.assertIn("""GAME='/games/it'"'"'s $(rm -rf ~)/g.exe'""", script)
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class EndToEnd(unittest.TestCase):
    """Drive the real CLI against a fake home and a fake runtime."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        fake_wine = self.bin / "wine"
        fake_wine.write_text('#!/bin/sh\necho "fake-wine $*"\n')
        fake_wine.chmod(0o755)
        self.game = self.tmp / "games" / "Cool Game"
        _exe(self.game / "CoolGame.exe", 300_000)
        _exe(self.game / "unins000.exe")

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, path_first: Path | None = None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FUNNEL_", "XDG_", "WINE"))}
        env.update(HOME=str(self.home), PYTHONPATH=str(ROOT),
                   PATH=f"{path_first or self.bin}:{self.bin}:/usr/bin:/bin")
        return env

    def funnel(self, *args: str, path_first: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "funnel.cli", *args], cwd=self.tmp,
                              env=self.env(path_first), text=True, capture_output=True, timeout=60)

    def created(self, slug: str) -> list[Path]:
        return [
            self.home / f".local/bin/funnel-{slug}",
            self.home / f".local/share/applications/funnel-{slug}.desktop",
            self.home / f"Desktop/funnel-{slug}.desktop",
            self.home / f".funnel/state/{slug}.json",
        ]

    def test_convert_list_redrop_remove(self):
        first = self.funnel(str(self.game))
        self.assertEqual(first.returncode, 0, first.stderr)
        for path in self.created("cool-game"):
            self.assertTrue(path.is_file(), path)

        state = json.loads((self.home / ".funnel/state/cool-game.json").read_text())
        self.assertIn("winemenubuilder.exe=d", state["environment"]["WINEDLLOVERRIDES"])

        launched = subprocess.run([str(self.home / ".local/bin/funnel-cool-game")],
                                  env=self.env(), text=True, capture_output=True)
        self.assertIn("fake-wine", launched.stdout)
        self.assertIn("CoolGame.exe", launched.stdout)

        again = self.funnel(str(self.game))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("ALREADY CONVERTED", again.stdout)
        self.assertFalse((self.home / ".local/bin/funnel-cool-game-2").exists())

        listing = self.funnel("list")
        self.assertIn("cool-game", listing.stdout)
        self.assertIn("Cool Game", listing.stdout)

        removed = self.funnel("remove", "Cool Game")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        for path in self.created("cool-game"):
            self.assertFalse(path.exists(), path)
        self.assertTrue((self.game / "CoolGame.exe").is_file(), "original game must be untouched")
        self.assertIn("No games converted", self.funnel("list").stdout)

    def test_new_flag_makes_a_second_copy(self):
        self.assertEqual(self.funnel(str(self.game)).returncode, 0)
        second = self.funnel(str(self.game), "--new")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue((self.home / ".local/bin/funnel-cool-game-2").is_file())

    def test_purge_removes_prefix_only_with_flag(self):
        self.assertEqual(self.funnel(str(self.game)).returncode, 0)
        prefix = self.home / ".local/share/funnel/prefixes/cool-game"
        prefix.mkdir(parents=True)
        (prefix / "save.dat").write_text("progress")

        self.assertEqual(self.funnel(str(self.game), "--new").returncode, 0)
        kept = self.funnel("remove", "cool-game")
        self.assertIn("kept prefix", kept.stdout)
        self.assertTrue(prefix.is_dir())

        self.assertEqual(self.funnel("remove", "cool-game-2", "--purge").returncode, 0)
        self.assertTrue(prefix.is_dir(), "a different game's prefix must survive")

    def test_purge_deletes_own_prefix(self):
        self.assertEqual(self.funnel(str(self.game)).returncode, 0)
        prefix = self.home / ".local/share/funnel/prefixes/cool-game"
        prefix.mkdir(parents=True)
        result = self.funnel("remove", "cool-game", "--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(prefix.exists())

    def test_remove_unknown_name(self):
        result = self.funnel("remove", "nope")
        self.assertEqual(result.returncode, 1)
        self.assertIn("funnel list", result.stderr)

    def test_failed_extraction_leaves_no_cache_folder(self):
        broken = self.tmp / "broken-bin"
        broken.mkdir()
        fake_7z = broken / "7z"
        fake_7z.write_text(textwrap.dedent("""\
            #!/bin/sh
            [ "$1" = l ] && exit 0
            echo "simulated extraction failure" >&2
            exit 2
        """))
        fake_7z.chmod(0o755)
        archive = self.tmp / "game.zip"
        archive.write_bytes(b"PK\x03\x04 not really a zip")
        result = self.funnel(str(archive), path_first=broken)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("simulated extraction failure", result.stderr)
        self.assertEqual(list((self.home / ".funnel/cache/games").iterdir()), [])

    def test_doctor_word_still_converts_a_folder_named_doctor(self):
        doctor_game = self.tmp / "doctor"
        _exe(doctor_game / "Doctor.exe")
        result = self.funnel("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK: doctor", result.stdout)


if __name__ == "__main__":
    unittest.main()
