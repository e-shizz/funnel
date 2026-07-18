"""Extract a PNG icon from a game folder or PE for the shelf."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .paths import ICONS, ensure_layout


def extract_icon(app_dir: Path, exe: Path, slug: str) -> Path | None:
    """Return path to PNG under ~/.funnel/icons/<slug>.png or None."""
    ensure_layout()
    out = ICONS / f"{slug}.png"
    if out.exists() and out.stat().st_size > 100:
        return out

    # A unique work directory avoids replacing another conversion's artifacts.
    # It is intentionally retained: Funnel's recovery path performs no cleanup.
    tmp = Path(tempfile.mkdtemp(prefix=f".{slug}.work-", dir=str(ICONS)))

    candidates: list[Path] = []

    # Prefer .ico next to game
    for pat in ("*.ico", "*.ICO", "*.png", "*.PNG"):
        candidates.extend(sorted(app_dir.glob(pat)))
    # common names
    for name in (f"{exe.stem}.ico", "icon.ico", "game.ico", "app.ico"):
        p = app_dir / name
        if p.is_file():
            candidates.insert(0, p)

    # Try icotool / magick on ico
    for ico in candidates:
        if ico.suffix.lower() == ".png":
            try:
                shutil.copy2(ico, out)
                if out.stat().st_size > 50:
                    return out
            except OSError:
                pass
        if ico.suffix.lower() == ".ico":
            png = _ico_to_png(ico, tmp)
            if png and _finalize(png, out):
                return out

    # wrestool from exe
    if shutil.which("wrestool"):
        try:
            subprocess.run(
                ["wrestool", "-x", "-t14", str(exe), "-o", str(tmp) + "/"],
                capture_output=True,
                timeout=30,
                check=False,
            )
            for ico in tmp.glob("*.ico"):
                png = _ico_to_png(ico, tmp)
                if png and _finalize(png, out):
                    return out
        except (subprocess.TimeoutExpired, OSError):
            pass

    return None


def _ico_to_png(ico: Path, tmp: Path) -> Path | None:
    if shutil.which("icotool"):
        try:
            subprocess.run(
                ["icotool", "-x", "-o", str(tmp), str(ico)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            pngs = sorted(tmp.glob("*.png"), key=lambda p: p.stat().st_size, reverse=True)
            if pngs:
                return pngs[0]
        except (subprocess.TimeoutExpired, OSError):
            pass
    if shutil.which("magick"):
        dest = tmp / "fromico.png"
        try:
            r = subprocess.run(
                ["magick", str(ico), str(dest)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if r.returncode == 0 and dest.exists():
                return dest
        except (subprocess.TimeoutExpired, OSError):
            pass
    return None


def _finalize(src: Path, dest: Path) -> bool:
    try:
        shutil.copy2(src, dest)
        return dest.exists() and dest.stat().st_size > 50
    except OSError:
        return False
