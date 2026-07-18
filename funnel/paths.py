"""Invisible Funnel paths — hidden engine only. Library = OS app menu."""

from __future__ import annotations

import subprocess
from pathlib import Path

HOME = Path.home()

# Hidden engine (never the user-facing library)
ENGINE = HOME / ".funnel"
LOGS = ENGINE / "logs"
ICONS = ENGINE / "icons"
STATE = ENGINE / "state"
MEDIA_BACKUP = ENGINE / "media-backup"
CACHE = ENGINE / "cache"
GAMES_CACHE = CACHE / "games"  # extracted archives live here
TEST_RUNS = ENGINE / "test-runs"

# Standard FreeDesktop install locations (the real library)
LAUNCH_DIR = HOME / ".local/bin"
DESKTOP_DIR = HOME / ".local/share/applications"


def xdg_desktop_dir() -> Path:
    """Return the user's actual XDG Desktop, with the conventional fallback."""
    try:
        result = subprocess.run(
            ["xdg-user-dir", "DESKTOP"], capture_output=True, text=True,
            timeout=5, check=False,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return Path(value).expanduser()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return HOME / "Desktop"


def ensure_layout() -> None:
    for p in (ENGINE, LOGS, ICONS, STATE, MEDIA_BACKUP, CACHE, GAMES_CACHE, TEST_RUNS, LAUNCH_DIR, DESKTOP_DIR, xdg_desktop_dir()):
        p.mkdir(parents=True, exist_ok=True)
