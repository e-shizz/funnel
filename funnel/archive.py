"""Silent, collision-safe archive extraction for Funnel."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .paths import GAMES_CACHE, ensure_layout

ARCHIVE_SUFFIXES = {
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".txz", ".zst", ".zzip", ".001", ".msix",
}


@dataclass
class ExtractResult:
    ok: bool
    source: Path
    output_dir: Path | None = None
    tool: str | None = None
    file_count: int = 0
    messages: list[str] = field(default_factory=list)
    error: str | None = None


def is_archive(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.casefold() in ARCHIVE_SUFFIXES:
        return True
    if path.name.casefold().endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")):
        return True
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return False
    return head[:2] == b"PK" or head[:6] == b"7z\xbc\xaf\x27\x1c" or head[:4] == b"Rar!"


def _which_7z() -> str | None:
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _cache_name(source: Path, keep_name: str | None) -> str:
    base = keep_name or source.stem
    if base.casefold().endswith(".tar"):
        base = base[:-4]
    base = re.sub(r"[^\w .-]+", "-", base, flags=re.UNICODE).strip(" .-")
    return base or "payload"


def _unused_directory(parent: Path, base: str) -> Path:
    candidate = parent / base
    number = 2
    while candidate.exists():
        candidate = parent / f"{base}-{number}"
        number += 1
    return candidate


def _safe_member_name(name: str) -> bool:
    value = name.replace("\\", "/")
    if not value or "\x00" in value or value.startswith(("/", "//")):
        return False
    if re.match(r"^[A-Za-z]:", value):
        return False
    return ".." not in Path(value).parts


def _archive_members(source: Path, tool: str) -> tuple[list[str], str | None]:
    if tool == "unrar":
        command = ["unrar", "lb", str(source)]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "archive listing failed").strip()[-1000:]
            return [], detail
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()], None

    completed = subprocess.run(
        [tool, "l", "-slt", str(source)], capture_output=True, text=True, timeout=300, check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "archive listing failed").strip()[-1000:]
        return [], detail
    members: list[str] = []
    inside_entries = False
    for line in completed.stdout.splitlines():
        if line.strip() == "----------":
            inside_entries = True
            continue
        if inside_entries and line.startswith("Path = "):
            members.append(line[7:])
    return members, None


def _validate_tree(root: Path) -> None:
    """Reject links, hard links, special files, and paths escaping an extracted root."""
    root = root.resolve()
    root_info = os.lstat(root)
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise ValueError(f"Unsafe extraction root: {root}")
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in names + files:
            path = base / name
            info = os.lstat(path)
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise ValueError(f"Extracted path escapes output: {path}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Archive link is not accepted: {path.relative_to(root)}")
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink > 1:
                    raise ValueError(f"Archive hard link is not accepted: {path.relative_to(root)}")
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"Archive special file is not accepted: {path.relative_to(root)}")


def extract_archive(
    archive: str | Path,
    *,
    dest_parent: Path | None = None,
    keep_name: str | None = None,
) -> ExtractResult:
    """Extract into a new stable cache directory without replacing old output."""
    ensure_layout()
    source = Path(archive).expanduser().resolve()
    if not source.is_file():
        return ExtractResult(False, source, error=f"Not a file: {source}")
    if not is_archive(source):
        return ExtractResult(False, source, error=f"Not a recognized archive: {source}")

    parent = Path(dest_parent or GAMES_CACHE)
    parent.mkdir(parents=True, exist_ok=True)
    output = _unused_directory(parent, _cache_name(source, keep_name))
    output.mkdir()

    tool: str | None = None
    suffix = source.suffix.casefold()
    if suffix == ".rar" and shutil.which("unrar"):
        tool = "unrar"
        command = ["unrar", "x", "-o-", "-y", str(source), str(output) + "/"]
    else:
        tool = _which_7z()
        if not tool:
            return ExtractResult(
                False, source, output_dir=output,
                error="No silent extractor found (unrar is required for RAR5; 7z for other archives)",
            )
        command = [tool, "x", "-aos", "-y", f"-o{output}", str(source)]

    try:
        members, listing_error = _archive_members(source, tool)
        if listing_error is not None:
            return ExtractResult(False, source, output_dir=output, tool=tool, error=listing_error)
        unsafe = next((name for name in members if not _safe_member_name(name)), None)
        if unsafe is not None:
            return ExtractResult(
                False, source, output_dir=output, tool=tool,
                error=f"Unsafe archive member path rejected before extraction: {unsafe}",
            )
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=3600, check=False,
        )
    except subprocess.TimeoutExpired:
        return ExtractResult(False, source, output_dir=output, tool=tool, error="Extract timed out")
    except OSError as exc:
        return ExtractResult(False, source, output_dir=output, tool=tool, error=str(exc))

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "extractor failed").strip()[-1000:]
        return ExtractResult(False, source, output_dir=output, tool=tool, error=detail)

    try:
        _validate_tree(output)
    except (OSError, ValueError) as exc:
        return ExtractResult(False, source, output_dir=output, tool=tool, error=str(exc))

    file_count = sum(1 for path in output.rglob("*") if path.is_file() and not path.is_symlink())
    if file_count == 0:
        return ExtractResult(False, source, output_dir=output, tool=tool, error="Extract produced no files")
    messages = [
        f"archive={source}", f"output={output}", f"tool={tool}", f"files={file_count}",
    ]
    return ExtractResult(True, source, output, tool, file_count, messages)


def resolve_payload(raw: str | Path) -> tuple[Path, list[str]]:
    """Return an in-place folder/EXE or a silently extracted archive tree."""
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Not found: {path}")
    if path.is_dir() or (path.is_file() and path.suffix.casefold() == ".exe"):
        return path, [f"payload=direct:{path}"]
    if is_archive(path):
        result = extract_archive(path)
        if not result.ok or result.output_dir is None:
            raise RuntimeError(result.error or "extract failed")
        return result.output_dir, result.messages + ["payload=extracted"]
    raise ValueError(f"Unsupported input (expected folder, EXE, or archive): {path}")
