"""Read and manage the games Funnel has already converted.

Every conversion writes ``~/.funnel/state/<slug>.json``. That record lists every
file Funnel created, so it is the single source of truth for listing, re-drop
detection, and removal. Removal only ever deletes paths inside Funnel's own
locations; the user's original game files are never touched.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# Records written before an installer runs; they describe a prefix, not a launcher.
_INSTALLER_RECORD = "installer-recovery"


@dataclass
class Record:
    slug: str
    data: dict
    state_path: Path

    @property
    def display_name(self) -> str:
        return str(self.data.get("display_name") or self.slug)

    @property
    def is_launcher(self) -> bool:
        return self.data.get("kind") != _INSTALLER_RECORD and bool(self.data.get("launch"))

    def path(self, key: str) -> Path | None:
        value = self.data.get(key)
        return Path(str(value)) if value else None

    def installed(self) -> bool:
        """True when the launcher and menu entry still exist on disk."""
        launch, menu = self.path("launch"), self.path("desktop")
        return bool(launch and menu and launch.is_file() and menu.is_file())


@dataclass
class RemoveResult:
    record: Record
    removed: list[Path] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def load_records() -> list[Record]:
    records: list[Record] = []
    for state in sorted(paths.STATE.glob("*.json")):
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            records.append(Record(str(data.get("slug") or state.stem), data, state))
    return records


def launchers() -> list[Record]:
    return [record for record in load_records() if record.is_launcher]


def find_by_source(source: Path) -> list[Record]:
    """Return installed launchers previously created from exactly this input."""
    wanted = str(Path(source).expanduser().resolve())
    return [
        record for record in launchers()
        if record.data.get("source") == wanted and record.installed()
    ]


def find(name: str) -> list[Record]:
    """Match a slug exactly, otherwise a display name case-insensitively."""
    records = launchers()
    exact = [record for record in records if record.slug == name]
    if exact:
        return exact
    folded = name.casefold()
    return [record for record in records if record.display_name.casefold() == folded]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return path.resolve() != root.resolve()


def _unlink(path: Path | None, root: Path, result: RemoveResult) -> None:
    if path is None:
        return
    if not _inside(path, root):
        result.kept.append(f"skipped (outside {root}): {path}")
        return
    if path.is_file() or path.is_symlink():
        path.unlink()
        result.removed.append(path)


def _shared(key: str, value: Path, record: Record) -> bool:
    """True when another record still points at the same prefix or cache."""
    for other in load_records():
        # Installer bookkeeping records do not keep a prefix alive on their own.
        if other.state_path == record.state_path or not other.is_launcher:
            continue
        other_value = other.path(key)
        if other_value and other_value.resolve() == value.resolve():
            return True
    return False


def _extracted_root(app_dir: Path | None) -> Path | None:
    """Top-level cache folder an archive was extracted into, if any."""
    if app_dir is None or not _inside(app_dir, paths.GAMES_CACHE):
        return None
    relative = app_dir.resolve().relative_to(paths.GAMES_CACHE.resolve())
    return paths.GAMES_CACHE / relative.parts[0]


def _shares_cache(root: Path, record: Record) -> bool:
    for other in load_records():
        if other.state_path != record.state_path and _extracted_root(other.path("app_dir")) == root:
            return True
    return False


def remove(record: Record, *, purge: bool = False) -> RemoveResult:
    """Remove a launcher. With ``purge``, also delete its prefix and extracted copy."""
    result = RemoveResult(record)
    _unlink(record.path("launch"), paths.LAUNCH_DIR, result)
    _unlink(record.path("desktop"), paths.DESKTOP_DIR, result)
    _unlink(record.path("desktop_pin"), paths.xdg_desktop_dir(), result)
    _unlink(record.path("icon"), paths.ICONS, result)

    prefix = record.path("prefix_path")
    extracted = _extracted_root(record.path("app_dir"))
    if not purge:
        if prefix and prefix.exists():
            result.kept.append(f"kept prefix (saves/settings): {prefix}")
        if extracted and extracted.exists():
            result.kept.append(f"kept extracted copy: {extracted}")
    else:
        if prefix and prefix.exists():
            _purge_prefix(record, prefix, result)
        if extracted and extracted.exists():
            if _shares_cache(extracted, record):
                result.kept.append(f"kept extracted copy (used by another launcher): {extracted}")
            else:
                shutil.rmtree(extracted)
                result.removed.append(extracted)

    for state in (record.state_path, record.state_path.with_suffix(".txt")):
        _unlink(state, paths.STATE, result)
    _refresh_menu()
    return result


def _purge_prefix(record: Record, prefix: Path, result: RemoveResult) -> None:
    from .installer import prefix_has_running_processes

    if _shared("prefix_path", prefix, record):
        result.kept.append(f"kept prefix (used by another launcher): {prefix}")
        return
    if prefix_has_running_processes(prefix):
        result.kept.append(f"kept prefix (game still running): {prefix}")
        return
    # Funnel's own Wine/UMU prefixes, or a Steam compatdata folder Funnel created.
    target: Path | None = None
    if _inside(prefix, paths.PREFIXES):
        target = prefix
    elif record.data.get("runtime_kind") == "steam" or record.data.get("proton"):
        compat = record.path("compat_path")
        if compat and compat.parent.name == "compatdata" and compat.name.startswith("42"):
            target = compat
    if target is None:
        result.kept.append(f"kept prefix (not created by Funnel): {prefix}")
        return
    shutil.rmtree(target)
    result.removed.append(target)
    for other in load_records():
        other_prefix = other.path("prefix_path")
        if not other.is_launcher and other_prefix and other_prefix.resolve() == prefix.resolve():
            for state in (other.state_path, other.state_path.with_suffix(".txt")):
                _unlink(state, paths.STATE, result)


def _refresh_menu() -> None:
    import subprocess

    try:
        subprocess.run(
            ["update-desktop-database", str(paths.DESKTOP_DIR)],
            capture_output=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
