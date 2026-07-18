"""Safe, source-preserving staging for Funnel v2 inputs."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .archive import _validate_tree, extract_archive, is_archive


_BROAD_DIRECTORY_NAMES = {"desktop", "downloads", "home"}
_INSTALLER_NAME = re.compile(
    r"(?i)^(setup|install|unins\d*|uninstall|vcredist.*|dxsetup|directx|dotnet|redist).*\.exe$"
)


class StagingError(RuntimeError):
    """A readable, recoverable error while preserving the source."""

    recoverable = True


@dataclass
class StageResult:
    source: Path
    staged_root: Path
    input_kind: str
    inventory: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    selected_exe_hint: Path | None = None

    @property
    def manifest(self) -> list[str]:
        """Compatibility alias for callers that call the inventory a manifest."""
        return self.inventory


StagingResult = StageResult


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().absolute()


def _reject_source_link(path: Path) -> None:
    try:
        if path.is_symlink():
            raise StagingError(f"Unsafe source symlink is not accepted: {path}")
    except OSError as exc:
        raise StagingError(f"Cannot inspect source: {path}: {exc}") from exc


def _source_snapshot(root: Path) -> dict[str, tuple[str, int, str]]:
    """Capture deterministic source invariants without following links."""
    root = _absolute(root)
    if root.is_file():
        info = os.lstat(root)
        if not stat.S_ISREG(info.st_mode):
            raise StagingError(f"Source is not a regular file: {root}")
        return {".": ("file", info.st_size, _hash_file(root))}
    if not root.is_dir() or root.is_symlink():
        raise StagingError(f"Source is not a safe file or directory: {root}")

    snapshot: dict[str, tuple[str, int, str]] = {".": ("directory", 0, "")}

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                rel = path.relative_to(root).as_posix()
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    raise StagingError(f"Unsafe symlink in source: {rel}")
                if stat.S_ISDIR(info.st_mode):
                    snapshot[rel] = ("directory", 0, "")
                    visit(path)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink > 1:
                        raise StagingError(f"Unsafe hardlink in source: {rel}")
                    snapshot[rel] = ("file", info.st_size, _hash_file(path))
                else:
                    raise StagingError(f"Unsafe special file in source: {rel}")

    visit(root)
    return snapshot


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fresh_stage_parent(parent: Path) -> Path:
    parent = _absolute(parent)
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StagingError(f"Cannot create staging parent {parent}: {exc}") from exc
    if not parent.is_dir() or parent.is_symlink():
        raise StagingError(f"Staging parent is not a safe directory: {parent}")
    return parent


def _assert_disjoint(source_root: Path, staging_parent: Path) -> None:
    try:
        staging_parent.resolve().relative_to(source_root.resolve())
    except ValueError:
        return
    raise StagingError(
        f"Staging parent must not be inside the source payload: {staging_parent}"
    )


def _new_staging_tree(parent: Path) -> Path:
    try:
        return Path(tempfile.mkdtemp(prefix=".funnel-stage-", dir=str(parent)))
    except OSError as exc:
        raise StagingError(f"Cannot create fresh staging tree under {parent}: {exc}") from exc


def _bounded_application_folder(exe: Path, confirmed_app_dir: str | Path | None) -> Path:
    if confirmed_app_dir is None:
        raise StagingError(
            "Direct .exe input requires confirmed_app_dir; Funnel will not copy its parent implicitly"
        )
    app_dir = _absolute(confirmed_app_dir)
    _reject_source_link(app_dir)
    app_dir = app_dir.resolve()
    if not app_dir.is_dir():
        raise StagingError(f"Confirmed application payload is not a directory: {app_dir}")
    try:
        exe.resolve().relative_to(app_dir.resolve())
    except ValueError as exc:
        raise StagingError(
            f"Confirmed application payload does not contain the executable: {exe}"
        ) from exc
    home = Path.home().resolve()
    if app_dir.resolve() == app_dir.parent.resolve() or app_dir.resolve() == home:
        raise StagingError(f"Confirmed application payload is too broad: {app_dir}")
    if app_dir.name.casefold() in _BROAD_DIRECTORY_NAMES:
        raise StagingError(f"Confirmed application payload is too broad: {app_dir}")
    return app_dir


def _copy_folder(source_root: Path, staging_parent: Path) -> Path:
    _assert_disjoint(source_root, staging_parent)
    stage = _new_staging_tree(staging_parent)
    shutil.copytree(
        source_root,
        stage,
        symlinks=True,
        copy_function=shutil.copy2,
        dirs_exist_ok=True,
    )
    return stage


def _manifest(root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(root).as_posix()
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise StagingError(f"Unsafe staged manifest path: {relative}")
            files.append(relative)
    return files


def _selected_exe_hint(root: Path, preferred: Path | None = None) -> Path | None:
    if preferred is not None:
        try:
            return preferred.relative_to(root)
        except ValueError:
            raise StagingError(f"Selected executable escaped staged root: {preferred}")
    candidates = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.casefold() == ".exe"
    )
    usable = [path for path in candidates if not _INSTALLER_NAME.match(path.name)]
    if len(usable) == 1:
        return usable[0].relative_to(root)
    return None


def _result(
    source: Path,
    root: Path,
    input_kind: str,
    *,
    preferred_exe: Path | None = None,
    warnings: list[str] | None = None,
) -> StageResult:
    try:
        _validate_tree(root)
        inventory = _manifest(root)
        hint = _selected_exe_hint(root, preferred_exe)
    except (OSError, ValueError) as exc:
        raise StagingError(f"Unsafe staged payload: {exc}") from exc
    return StageResult(
        source=source,
        staged_root=root,
        input_kind=input_kind,
        inventory=inventory,
        warnings=list(warnings or []),
        selected_exe_hint=hint,
    )


def _check_source_unchanged(source_root: Path, before: dict[str, tuple[str, int, str]]) -> None:
    try:
        after = _source_snapshot(source_root)
    except StagingError:
        raise
    if before != after:
        raise StagingError(
            f"Source changed during staging; refusing to continue: {source_root}"
        )


def stage_input(
    path: str | Path,
    *,
    staging_parent: Path,
    confirmed_app_dir: Path | None = None,
) -> StageResult:
    """Copy or extract an input into a fresh, validated staging tree."""
    source = _absolute(path)
    _reject_source_link(source)
    source = source.resolve()
    if not source.exists():
        raise StagingError(f"Input does not exist: {source}")
    parent = _fresh_stage_parent(staging_parent)

    if is_archive(source):
        before = _source_snapshot(source)
        extraction_parent = _new_staging_tree(parent)
        result = extract_archive(source, dest_parent=extraction_parent)
        if not result.ok or result.output_dir is None:
            raise StagingError(result.error or f"Could not extract archive: {source}")
        _check_source_unchanged(source, before)
        return _result(
            source,
            result.output_dir,
            "archive",
            warnings=list(result.messages),
        )

    if source.is_dir():
        source_root = source
        input_kind = "folder"
        preferred = None
    elif source.is_file() and source.suffix.casefold() == ".exe":
        source_root = _bounded_application_folder(source, confirmed_app_dir)
        input_kind = "exe"
        preferred = source
    else:
        raise StagingError(f"Unsupported input; expected archive, folder, or .exe: {source}")

    _reject_source_link(source_root)
    before = _source_snapshot(source_root)
    try:
        _validate_tree(source_root)
    except (OSError, ValueError) as exc:
        raise StagingError(f"Unsafe source payload: {exc}") from exc
    staged = _copy_folder(source_root, parent)
    _check_source_unchanged(source_root, before)
    preferred_staged = None
    if preferred is not None:
        try:
            preferred_staged = staged / preferred.relative_to(source_root)
        except ValueError as exc:
            raise StagingError(f"Executable is outside confirmed payload: {preferred}") from exc
    return _result(source, staged, input_kind, preferred_exe=preferred_staged)
