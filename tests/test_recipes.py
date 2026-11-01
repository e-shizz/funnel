"""Recipes reproduce Funnel's game-specific behaviour from data, and user files work."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funnel.detect import detect_input  # noqa: E402
from funnel.recipes import RecipeError, apply_recipes, load_file, load_recipes  # noqa: E402


def _game(root: Path, exe: str = "Game.exe", files: tuple[str, ...] = ()) -> Path:
    (root / exe).parent.mkdir(parents=True, exist_ok=True)
    (root / exe).write_bytes(b"MZ" + b"\0" * 64)
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    return root


class BuiltinRecipes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Keep the developer's own ~/.config/funnel/recipes.json out of these tests.
        self._env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "config")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def detect(self, name: str, **kwargs):
        return detect_input(_game(self.tmp / name, **kwargs))

    def test_builtin_file_is_valid(self):
        self.assertGreater(len(load_recipes()), 5)

    def test_plain_game_has_no_tweaks(self):
        det = self.detect("Plain Game")
        self.assertEqual(det.recipe, "default")
        self.assertEqual(det.env, {})
        self.assertIsNone(det.category)

    def test_unity(self):
        det = self.detect("U", files=("UnityPlayer.dll",))
        self.assertIn("engine:unity", det.hints)
        self.assertIn("unity", det.recipe)

    def test_kirikiri_is_japanese(self):
        det = self.detect("VN", files=("data.xp3",))
        self.assertIn("engine:kirikiri", det.hints)
        self.assertEqual(det.env.get("LANG"), "ja_JP.UTF-8")

    def test_japanese_folder_name(self):
        det = self.detect("ゲーム")
        self.assertIn("locale:ja_JP", det.hints)
        self.assertEqual(det.env.get("LC_ALL"), "ja_JP.UTF-8")

    def test_gamemaker_needs_both_files(self):
        self.assertNotIn("engine:gamemaker", self.detect("GM1").hints)
        self.assertIn("engine:gamemaker", self.detect("GM2", files=("data.win",)).hints)

    def test_alicesoft_by_exe_or_ain_and_movies(self):
        self.assertIn("engine:alicesoft", self.detect("A1", exe="System40.exe").hints)
        det = self.detect("A2", files=("Rance.ain", "Movie/op.alm", "DLL/PlayMovie.dll"))
        self.assertIn("engine:alicesoft", det.hints)
        self.assertIn("media:alm-mpeg", det.hints)
        self.assertIn("media:playmovie-dshow", det.hints)

    def test_koikatsu_overrides_merge_with_menu_builder(self):
        from funnel.runtime import RuntimeKind, RuntimeSpec, runtime_environment

        det = self.detect("Koikatsu Party", exe="Koikatu.exe")
        self.assertEqual(det.env["WINEDLLOVERRIDES"], "winhttp=n,b")
        self.assertEqual(det.env["LANG"], "ja_JP.UTF-8")
        spec = RuntimeSpec(RuntimeKind.WINE, Path("/usr/bin/wine"), Path("/tmp/p"))
        merged = runtime_environment(spec, det.env)["WINEDLLOVERRIDES"]
        self.assertEqual(merged, "winhttp=n,b;winemenubuilder.exe=d")

    def test_nwjs(self):
        self.assertIn("engine:nwjs", self.detect("N", files=("nw.dll",)).hints)

    def test_codex_msix(self):
        root = _game(self.tmp / "pkg", exe="app/Codex.exe")
        (root / "AppxManifest.xml").write_text(
            '<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10">'
            '<Identity Name="OpenAI.Codex"/><Applications>'
            '<Application Executable="app\\Codex.exe"/></Applications></Package>'
        )
        det = detect_input(root)
        self.assertEqual(det.slug, "codex")
        self.assertEqual(det.category, "Development")
        self.assertEqual(det.fixed_args, ("--disable-gpu", "--disable-gpu-compositing"))


class UserRecipes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, recipes) -> Path:
        path = self.tmp / "config" / "funnel" / "recipes.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"recipes": recipes}))
        return path

    def test_user_recipe_runs_after_builtins_and_can_override(self):
        self.write([{
            "name": "my-vn", "when": {"files_any": ["*.xp3"]},
            "env": {"LANG": "en_US.UTF-8"}, "args": ["-windowed"], "category": "Game",
        }])
        game = _game(self.tmp / "VN", files=("data.xp3",))
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "config")}):
            det = detect_input(game)
        self.assertTrue(det.recipe.endswith("+my-vn"))
        self.assertEqual(det.env["LANG"], "en_US.UTF-8")
        self.assertEqual(det.fixed_args, ("-windowed",))
        self.assertEqual(det.category, "Game")

    def test_bad_recipe_is_a_clear_error(self):
        for recipes, message in (
            ([{"name": "x", "when": {"filez": ["a"]}}], "unknown 'when' keys"),
            ([{"name": "x", "when": {"text_regex": "("}}], "bad text_regex"),
            ([{"when": {"files_any": ["a"]}}], "'name' is required"),
            ([{"name": "x", "when": {"files_any": "a"}}], "must be a list"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(RecipeError, message):
                    load_file(self.write(recipes))

    def test_any_and_hint_chaining(self):
        recipes = [
            {"name": "first", "when": {"any": [{"exe_names": ["nope.exe"]}, {"files_any": ["*.pak"]}]},
             "hints": ["engine:custom"]},
            {"name": "second", "when": {"hints_any": ["engine:*"]}, "env": {"A": "1"}},
        ]
        game = _game(self.tmp / "G", files=("data.pak",))
        applied = apply_recipes(app_dir=game, exe=game / "Game.exe", text="g", hints=[], recipes=recipes)
        self.assertEqual(applied.names, ["first", "second"])
        self.assertEqual(applied.env, {"A": "1"})


if __name__ == "__main__":
    unittest.main()
