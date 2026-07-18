"""GTK-independent helpers for Funnel's drag/drop and decision UX.

Keeping parsing, user-directory resolution, and state guards outside the GTK
widget class makes the safety boundary testable without a display server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
from typing import Iterable


_BROAD_DIRECTORY_NAMES = {
    "desktop",
    "downloads",
    "home",
    "documents",
    "public",
}


def _lines(value: str | bytes | Iterable[str]) -> list[str]:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Dropped URI data is not valid UTF-8") from exc
    if isinstance(value, str):
        return value.splitlines()
    return [str(line) for line in value]


def parse_uri_list(value: str | bytes | Iterable[str]) -> list[Path]:
    """Parse a local ``text/uri-list`` payload without following remote URIs."""
    paths: list[Path] = []
    for raw_line in _lines(value):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlsplit(line)
        if parsed.scheme.casefold() != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError(f"Only local file URIs are supported for drop: {line}")
        decoded = unquote(parsed.path)
        if not decoded:
            raise ValueError(f"Dropped file URI has no path: {line}")
        paths.append(Path(url2pathname(decoded)))
    return paths


def single_input_path(paths: Iterable[Path]) -> Path:
    """Require exactly one local input for a conversion job."""
    values = [Path(path) for path in paths]
    if not values:
        raise ValueError("Choose or drop one archive, folder, or executable")
    if len(values) != 1:
        raise ValueError("Funnel accepts one input at a time")
    return values[0]


def default_desktop_directory(*, home: Path | None = None) -> Path:
    """Resolve the user's XDG Desktop directory without a hard-coded account."""
    home_path = (home or Path(os.environ.get("HOME", "~")).expanduser()).resolve()
    configured = os.environ.get("XDG_DESKTOP_DIR")
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", str(home_path / ".config"))
    ).expanduser()
    user_dirs = config_home / "user-dirs.dirs"
    if configured is None and user_dirs.is_file():
        try:
            for line in user_dirs.read_text(encoding="utf-8").splitlines():
                if line.startswith("XDG_DESKTOP_DIR="):
                    configured = line.partition("=")[2].strip().strip('"')
                    break
        except OSError:
            configured = None
    if configured:
        configured = configured.replace("$HOME", str(home_path)).replace("${HOME}", str(home_path))
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = home_path / candidate
        return candidate
    return home_path / "Desktop"


def staging_parent_directory(*, home: Path | None = None) -> Path:
    """Return a caller-owned cache staging parent for GUI jobs."""
    home_path = (home or Path(os.environ.get("HOME", "~")).expanduser()).resolve()
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", str(home_path / ".cache"))).expanduser()
    return cache_home / "funnel" / "staging"


def configured_proton_path(*, home: Path | None = None) -> Path:
    """Return the explicit environment setting or the conventional Proton path."""
    configured = os.environ.get("FUNNEL_PROTON")
    if configured:
        return Path(configured).expanduser()
    home_path = (home or Path(os.environ.get("HOME", "~")).expanduser()).resolve()
    return home_path / ".local/share/Steam/steamapps/common/Proton - Experimental/proton"


def display_name_for_input(path: str | os.PathLike[str] | Path) -> str:
    """Choose a readable default app name without inspecting proprietary data."""
    value = Path(path)
    if value.is_dir():
        return value.name or "Windows App"
    name = value.name
    lower = name.casefold()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if lower.endswith(suffix):
            return name[: -len(suffix)] or "Windows App"
    if value.suffix:
        return value.stem or "Windows App"
    return name or "Windows App"


def needs_bounded_folder_confirmation(
    path: str | os.PathLike[str] | Path,
    *,
    home: Path | None = None,
) -> bool:
    """Identify direct executables whose parent is too broad to copy implicitly."""
    candidate = Path(path).expanduser()
    if candidate.suffix.casefold() != ".exe":
        return False
    parent = candidate.parent.resolve()
    home_path = (home or Path(os.environ.get("HOME", "~")).expanduser()).resolve()
    if parent == home_path:
        return True
    if parent.name.casefold() in _BROAD_DIRECTORY_NAMES:
        return True
    return False


def executable_is_bounded(
    executable: str | os.PathLike[str] | Path,
    app_dir: str | os.PathLike[str] | Path,
) -> bool:
    """Return whether a chosen application folder contains the dropped EXE."""
    exe = Path(executable).expanduser()
    folder = Path(app_dir).expanduser()
    if not folder.is_dir() or folder.is_symlink():
        return False
    try:
        exe.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def candidate_description(candidate: object) -> str:
    """Format one detection candidate for a plain-language choice widget."""
    relative = str(getattr(candidate, "relative_path", "(unknown executable)"))
    category = str(getattr(candidate, "category", "unknown"))
    score = getattr(candidate, "score", "?")
    reasons = list(getattr(candidate, "reasons", []) or [])
    explanation = "; ".join(str(reason) for reason in reasons) or "no explanation available"
    return f"{relative} — {category}, score {score}: {explanation}"


def inspection_failure_message(result: object) -> str:
    """Turn typed inspection outcomes into recoverable UI copy."""
    status = str(getattr(result, "status", "unsupported"))
    reasons = list(getattr(result, "reasons", []) or [])
    detail = "; ".join(str(reason) for reason in reasons) or "No additional details were reported."
    labels = {
        "installer_only": "Installer-only payload: Funnel found setup/helper files but did not choose an unsafe installer as the application.",
        "incomplete": "Incomplete payload: Funnel could not find a complete runnable application.",
        "unsupported": "Unsupported input: Funnel could not recognize a safe staged payload.",
        "ambiguous": "A choice is required: multiple executable candidates are plausible.",
    }
    prefix = labels.get(status, f"Conversion cannot continue ({status}).")
    return f"{prefix}\n{detail}"


@dataclass
class OneJobGuard:
    """Small state guard preventing overlapping conversion jobs."""

    busy: bool = False

    def try_start(self) -> bool:
        if self.busy:
            return False
        self.busy = True
        return True

    def finish(self) -> None:
        self.busy = False


# Descriptive aliases keep the helpers convenient for small adapters and
# preserve the vocabulary used by earlier chooser-only integrations.
parse_drop_uris = parse_uri_list
xdg_desktop_directory = default_desktop_directory
needs_bounded_confirmation = needs_bounded_folder_confirmation


__all__ = [
    "OneJobGuard",
    "candidate_description",
    "configured_proton_path",
    "default_desktop_directory",
    "display_name_for_input",
    "executable_is_bounded",
    "inspection_failure_message",
    "needs_bounded_folder_confirmation",
    "needs_bounded_confirmation",
    "parse_drop_uris",
    "parse_uri_list",
    "single_input_path",
    "staging_parent_directory",
    "xdg_desktop_directory",
]
