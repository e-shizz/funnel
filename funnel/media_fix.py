"""Mechanical media fixes so Proton/winegstreamer can play game movies.

Root cause (AliceSoft .alm): files are MPEG-1 Program Streams. Proton's bundled
GStreamer lacks mpegps demux (+ often lacks mpeg1 decoder without its private
libav path). Host gstreamer plays them fine; Proton's winegstreamer does not.

Fix: remux/transcode to WebM (VP8+Vorbis) which Proton ships plugins for
(matroska + vpx + vorbis), keep the original filename so the game path still
works, and stash a backup under ~/.local/share/funnel/media-backup/.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .detect import Detected
from .paths import MEDIA_BACKUP

BACKUP_ROOT = MEDIA_BACKUP  # ~/.funnel/media-backup


@dataclass
class MediaFixResult:
    changed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _is_mpeg_ps(path: Path) -> bool:
    try:
        r = subprocess.run(
            ["file", "-b", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        blob = (r.stdout or "").lower()
        return "mpeg sequence" in blob or "mpeg" in blob and "system" in blob
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # magic bytes: MPEG-PS often starts with 00 00 01 ba (pack start)
        try:
            head = path.read_bytes()[:4]
            return head == b"\x00\x00\x01\xba"
        except OSError:
            return False


def _already_fixed(path: Path) -> bool:
    """Skip if we already rewrote to webm/ogg (typefind)."""
    try:
        r = subprocess.run(
            ["file", "-b", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        blob = (r.stdout or "").lower()
        if "webm" in blob or "matroska" in blob or "theora" in blob or "ogg" in blob:
            return True
        # funnel marker sidecar
        if path.with_suffix(path.suffix + ".funnel-webm").exists():
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return False


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def fix_alm_movies(det: Detected, *, force: bool = False) -> MediaFixResult:
    """Transcode MPEG-PS .alm movies next to AliceSoft games into Proton-playable WebM."""
    out = MediaFixResult()
    if not _ffmpeg_available():
        out.errors.append("ffmpeg not found — cannot fix .alm movies")
        out.notes.append("Install ffmpeg for AliceSoft/in-game movie support")
        return out

    movie_dir = det.app_dir / "movie"
    # also scan app_dir for loose .alm
    candidates: list[Path] = []
    if movie_dir.is_dir():
        candidates.extend(sorted(movie_dir.glob("*.alm")))
        candidates.extend(sorted(movie_dir.glob("*.ALM")))
    candidates.extend(sorted(det.app_dir.glob("*.alm")))

    # unique
    seen: set[Path] = set()
    uniq: list[Path] = []
    for c in candidates:
        rp = c.resolve()
        if rp not in seen and c.is_file():
            seen.add(rp)
            uniq.append(c)

    if not uniq:
        return out

    backup_dir = BACKUP_ROOT / det.slug
    backup_dir.mkdir(parents=True, exist_ok=True)

    for src in uniq:
        try:
            if not force and _already_fixed(src):
                out.skipped.append(f"{src.name}: already funnel-fixed")
                continue
            if not _is_mpeg_ps(src) and not force:
                # only rewrite MPEG-PS; leave unknown alone
                out.skipped.append(f"{src.name}: not MPEG-PS ({_file_brief(src)})")
                continue

            rel = src.relative_to(det.app_dir) if src.is_relative_to(det.app_dir) else Path(src.name)
            bak = backup_dir / rel
            bak.parent.mkdir(parents=True, exist_ok=True)
            if not bak.exists():
                shutil.copy2(src, bak)
                out.notes.append(f"backup={bak}")

            tmp = src.with_suffix(src.suffix + ".funnel-tmp.webm")
            # VP8+Vorbis WebM — Proton ships matroska/vpx/vorbis plugins
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(src if not bak.exists() else (bak if bak.stat().st_size >= src.stat().st_size else src)),
                "-c:v",
                "libvpx",
                "-deadline",
                "good",
                "-cpu-used",
                "8",
                "-b:v",
                "2500k",
                "-c:a",
                "libvorbis",
                "-q:a",
                "5",
                str(tmp),
            ]
            # Prefer backup as source if we already overwrote src once mid-failure
            source = bak if bak.exists() else src
            cmd[cmd.index("-i") + 1] = str(source)

            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
            if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1000:
                err = (r.stderr or r.stdout or "ffmpeg failed").strip()[:300]
                out.errors.append(f"{src.name}: {err}")
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                continue

            # Atomic replace: keep original filename (.alm) so game paths work
            tmp.replace(src)
            # marker so we can detect fixed files quickly
            marker = src.with_name(src.name + ".funnel-webm")
            marker.write_text(
                f"source_backup={bak}\nformat=webm/vp8+vorbis\n",
                encoding="utf-8",
            )
            out.changed.append(str(src))
        except Exception as e:
            out.errors.append(f"{src.name}: {e}")

    if out.changed:
        out.notes.append(
            f"remuxed {len(out.changed)} .alm movie(s) → WebM (VP8+Vorbis) for Proton winegstreamer"
        )
    return out


def _file_brief(path: Path) -> str:
    try:
        r = subprocess.run(
            ["file", "-b", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (r.stdout or "").strip()[:80]
    except Exception:
        return "unknown"
