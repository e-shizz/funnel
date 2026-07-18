"""Portable Funnel bundle creation and self-relative Proton launcher.

The bundle builder accepts an already staged payload.  It validates that tree
before copying it into a hidden temporary sibling, builds the complete bundle,
validates the temporary output, and publishes it with one same-filesystem
rename.  No source or completed bundle path is ever used as a temporary path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BundleError(RuntimeError):
    """A readable, recoverable error while creating a bundle."""

    recoverable = True


class MissingDependencyError(BundleError):
    """A required external executable is unavailable."""


@dataclass(frozen=True)
class BundleResult:
    """Paths produced by a successful bundle build."""

    bundle_root: Path
    payload_root: Path
    launcher: Path
    metadata_path: Path
    manifest_path: Path
    icon_path: Path

    @property
    def root(self) -> Path:
        """Compatibility alias for callers that call the output root ``root``."""
        return self.bundle_root


# A tiny transparent PNG.  Keeping this byte sequence in source makes the
# fallback deterministic and avoids relying on an optional image library.
_FALLBACK_ICON = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _absolute(path: str | os.PathLike[str] | Path) -> Path:
    return Path(path).expanduser().absolute()


def _safe_app_name(value: str) -> str:
    name = str(value).strip()
    if not name or name in {".", ".."}:
        raise BundleError("Application name must not be empty or a path marker")
    if any(char in name for char in ("/", "\\", "\x00")):
        raise BundleError("Application name must not contain a path separator")
    if any(ord(char) < 32 for char in name):
        raise BundleError("Application name contains an unsafe control character")
    return name


def _safe_relative(value: str | os.PathLike[str] | Path, *, label: str) -> Path:
    """Parse a payload-relative path without accepting traversal."""
    text = str(value).replace("\\", "/")
    if not text or "\x00" in text:
        raise BundleError(f"{label} is empty or contains NUL")
    candidate = Path(text)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise BundleError(f"{label} must remain inside the staged payload: {value}")
    # ``Path('.')`` is useful for a working-directory calculation, but not as
    # an executable path.  Callers decide whether to permit it.
    return candidate


def _relative_to_root(
    value: str | os.PathLike[str] | Path,
    root: Path,
    *,
    label: str,
) -> Path:
    """Resolve an absolute or relative selection and keep it inside ``root``."""
    raw = Path(str(value).replace("\\", "/"))
    if raw.is_absolute():
        candidate = raw.expanduser().absolute()
        if candidate.is_symlink():
            raise BundleError(f"Selected executable is an unsafe link: {value}")
        candidate = candidate.resolve()
    else:
        relative = _safe_relative(raw, label=label)
        candidate = (root / relative).resolve()
    try:
        return candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise BundleError(f"{label} escapes the staged payload: {value}") from exc


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_tree(root: Path, *, label: str) -> list[str]:
    """Validate a tree without following links and return file inventory."""
    raw_root = Path(root)
    try:
        root_info = os.lstat(raw_root)
    except OSError as exc:
        raise BundleError(f"{label} cannot be inspected: {raw_root}: {exc}") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise BundleError(f"{label} is not a regular directory: {raw_root}")

    resolved_root = raw_root.resolve()
    inventory: list[str] = []

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as stream:
                entries = sorted(stream, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise BundleError(f"Cannot inspect {label} directory {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                info = os.lstat(path)
            except OSError as exc:
                raise BundleError(f"Cannot inspect {label} entry {path}: {exc}") from exc
            relative = path.relative_to(resolved_root).as_posix()
            # A relative path produced by the traversal must stay relative
            # even if a caller supplied an unusual directory name.
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise BundleError(f"Unsafe path in {label}: {relative}")
            if stat.S_ISLNK(info.st_mode):
                raise BundleError(f"Unsafe link in {label}: {relative}")
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink > 1:
                    raise BundleError(f"Unsafe hardlink in {label}: {relative}")
                inventory.append(relative)
            else:
                raise BundleError(f"Unsafe special file in {label}: {relative}")

    visit(resolved_root)
    inventory.sort()
    return inventory


def _source_fingerprint(root: Path, inventory: list[str]) -> dict[str, tuple[int, int, int, str]]:
    """Capture enough source state to detect a concurrent source mutation."""
    result: dict[str, tuple[int, int, int, str]] = {}
    for relative in inventory:
        path = root / Path(relative)
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise BundleError(f"Unsafe source entry while fingerprinting: {relative}")
            result[relative] = (
                stat.S_IMODE(info.st_mode),
                info.st_size,
                info.st_mtime_ns,
                _hash_file(path),
            )
        except BundleError:
            raise
        except OSError as exc:
            raise BundleError(f"Cannot fingerprint staged payload entry {relative}: {exc}") from exc
    return result


def _resolve_proton(proton: str | os.PathLike[str] | Path | None) -> Path:
    """Resolve only an explicitly configured Proton executable."""
    configured: str | os.PathLike[str] | Path | None = proton
    if configured is None:
        configured = os.environ.get("FUNNEL_PROTON")
    if configured is None or not str(configured).strip():
        raise MissingDependencyError(
            "Proton is not configured; pass proton=... or set FUNNEL_PROTON"
        )
    raw = _absolute(configured)
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise MissingDependencyError(f"Configured Proton is unavailable: {raw}: {exc}") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise MissingDependencyError(
            f"Configured Proton is not an executable file: {resolved}"
        )
    return resolved


def _destination(path: str | os.PathLike[str] | Path, source_root: Path) -> Path:
    raw = _absolute(path)
    if raw.is_symlink():
        raise BundleError(f"Bundle destination must not be a symlink: {raw}")
    resolved = raw.resolve()
    try:
        resolved.relative_to(source_root.resolve())
    except ValueError:
        return resolved
    raise BundleError(
        f"Bundle destination must not be inside the staged payload: {resolved}"
    )


def _unique_bundle_path(destination: Path, app_name: str) -> Path:
    base = destination / f"{app_name}.Funnel"
    if not os.path.lexists(base):
        return base
    counter = 2
    while True:
        candidate = destination / f"{app_name} ({counter}).Funnel"
        if not os.path.lexists(candidate):
            return candidate
        counter += 1


def _copy_icon(icon_path: str | os.PathLike[str] | Path | None, target: Path) -> str:
    """Copy a safe supplied icon, or write the deterministic fallback."""
    if icon_path is None:
        target.write_bytes(_FALLBACK_ICON)
        return "fallback"
    source = _absolute(icon_path)
    try:
        info = os.lstat(source)
    except OSError:
        # Icon extraction is optional in this phase; an absent icon falls back
        # deterministically rather than preventing an otherwise valid bundle.
        target.write_bytes(_FALLBACK_ICON)
        return "fallback"
    if stat.S_ISLNK(info.st_mode):
        raise BundleError(f"Icon source is an unsafe link: {source}")
    if not stat.S_ISREG(info.st_mode):
        raise BundleError(f"Icon source is not a regular file: {source}")
    try:
        shutil.copyfile(source, target)
    except OSError as exc:
        raise BundleError(f"Could not copy icon {source}: {exc}") from exc
    return "provided"


def _launcher_text(
    *,
    proton: Path,
    selected_relative: Path,
) -> str:
    selected_posix = selected_relative.as_posix()
    parent_posix = selected_relative.parent.as_posix() or "."
    proton_literal = shlex.quote(str(proton))
    selected_literal = shlex.quote(selected_posix)
    parent_literal = shlex.quote(parent_posix)
    return f'''#!/usr/bin/env bash
set -euo pipefail

# Resolve the bundle from this launcher so the whole bundle can be moved.
BUNDLE_ROOT="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd -P)"
PROTON={proton_literal}
SELECTED_EXE_RELATIVE={selected_literal}
APP_DIR_RELATIVE={parent_literal}
GAME="$BUNDLE_ROOT/payload/$SELECTED_EXE_RELATIVE"
APP_DIR="$BUNDLE_ROOT/payload/$APP_DIR_RELATIVE"
COMPATDATA="$BUNDLE_ROOT/runtime/compatdata"
LOG_DIR="$BUNDLE_ROOT/logs"

if [[ ! -x "$PROTON" ]]; then
    printf 'Funnel: configured Proton is missing or not executable: %s\\n' "$PROTON" >&2
    exit 1
fi
if [[ ! -f "$GAME" ]]; then
    printf 'Funnel: selected executable is missing from the bundle: %s\\n' "$GAME" >&2
    exit 1
fi

mkdir -p -- "$COMPATDATA" "$LOG_DIR"
export STEAM_COMPAT_DATA_PATH="$COMPATDATA"
cd -- "$APP_DIR"
exec "$PROTON" run "$GAME" "$@"
'''


def _write_json(path: Path, value: dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as exc:
        raise BundleError(f"Could not write metadata {path}: {exc}") from exc


def _validate_bundle(
    root: Path,
    *,
    selected_relative: Path,
    expected_inventory: list[str],
    launcher: Path,
    metadata_path: Path,
    manifest_path: Path,
    icon_path: Path,
) -> None:
    """Validate all publication-critical output before the final rename."""
    payload_inventory = _validate_tree(root / "payload", label="bundle payload")
    if payload_inventory != expected_inventory:
        raise BundleError("Bundle payload inventory differs from the staged payload")
    selected = root / "payload" / selected_relative
    try:
        selected_info = os.lstat(selected)
    except OSError as exc:
        raise BundleError(f"Selected executable is missing from bundle: {selected}") from exc
    if stat.S_ISLNK(selected_info.st_mode) or not stat.S_ISREG(selected_info.st_mode):
        raise BundleError(f"Selected executable is not a regular file: {selected}")
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise BundleError(f"Bundle launcher is not executable: {launcher}")
    for directory in (
        root / "runtime" / "compatdata",
        root / "logs",
        root / "metadata",
    ):
        if not directory.is_dir() or directory.is_symlink():
            raise BundleError(f"Bundle directory is missing or unsafe: {directory}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(f"Bundle metadata is not valid JSON: {exc}") from exc
    if metadata.get("schema_version") != 1:
        raise BundleError("Bundle metadata has an unsupported schema version")
    if manifest.get("root") != "payload" or manifest.get("files") != expected_inventory:
        raise BundleError("Bundle manifest does not match the copied payload")
    try:
        icon_header = icon_path.read_bytes()[:8]
    except OSError as exc:
        raise BundleError(f"Bundle icon is unreadable: {icon_path}: {exc}") from exc
    if icon_header != b"\x89PNG\r\n\x1a\n":
        raise BundleError("Bundle icon is not a PNG")


def _cleanup_temporary(path: Path | None) -> None:
    if path is None:
        return
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except OSError:
        # Cleanup is limited to a path created by this builder.  The original
        # source and any completed bundle are intentionally never targets here.
        pass


def build_bundle(
    staged_root: str | os.PathLike[str] | Path | Any,
    *,
    selected_exe: str | os.PathLike[str] | Path,
    app_name: str,
    destination: str | os.PathLike[str] | Path,
    proton: str | os.PathLike[str] | Path | None = None,
    icon_path: str | os.PathLike[str] | Path | None = None,
    icon: str | os.PathLike[str] | Path | None = None,
    recipe: str | None = None,
    source_description: str | None = None,
) -> BundleResult:
    """Build and atomically publish a portable Funnel bundle.

    ``staged_root`` may be a path or a staging result exposing
    ``staged_root``.  All paths under the resulting bundle are relative to its
    own root; the only external path embedded in the launcher is the explicit
    configured Proton executable.
    """
    if hasattr(staged_root, "staged_root"):
        staged_value = getattr(staged_root, "staged_root")
        if source_description is None:
            source_value = getattr(staged_root, "source", None)
            if source_value is not None:
                source_description = Path(source_value).name
    else:
        staged_value = staged_root
    source_root = _absolute(staged_value)
    app = _safe_app_name(app_name)

    source_inventory = _validate_tree(source_root, label="staged payload")
    if not source_inventory:
        raise BundleError("Staged payload is empty; no bundle can be created")
    source_fingerprint = _source_fingerprint(source_root, source_inventory)
    selected_relative = _relative_to_root(
        selected_exe,
        source_root,
        label="Selected executable",
    )
    if selected_relative == Path("."):
        raise BundleError("Selected executable must name a file inside the payload")
    selected_source = source_root / selected_relative
    try:
        selected_info = os.lstat(selected_source)
    except OSError as exc:
        raise BundleError(
            f"Selected executable is missing from staged payload: {selected_relative.as_posix()}"
        ) from exc
    if stat.S_ISLNK(selected_info.st_mode) or not stat.S_ISREG(selected_info.st_mode):
        raise BundleError(
            f"Selected executable is not a regular file: {selected_relative.as_posix()}"
        )
    if selected_relative.as_posix() not in source_inventory:
        raise BundleError(
            f"Selected executable is not present in staged payload: {selected_relative.as_posix()}"
        )

    configured_proton = _resolve_proton(proton)
    output_parent = _destination(destination, source_root)
    temporary: Path | None = None
    final_bundle: Path | None = None
    try:
        try:
            output_parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BundleError(f"Cannot create bundle destination {output_parent}: {exc}") from exc
        if not output_parent.is_dir() or output_parent.is_symlink():
            raise BundleError(f"Bundle destination is not a safe directory: {output_parent}")

        final_bundle = _unique_bundle_path(output_parent, app)
        try:
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{app}.funnel-build-", dir=str(output_parent))
            )
        except OSError as exc:
            raise BundleError(f"Cannot create temporary bundle on destination filesystem: {exc}") from exc

        payload_target = temporary / "payload"
        try:
            shutil.copytree(
                source_root,
                payload_target,
                symlinks=True,
                copy_function=shutil.copy2,
            )
        except OSError as exc:
            raise BundleError(f"Could not copy staged payload into bundle: {exc}") from exc

        # Detect a source mutation before any publication.  The builder never
        # repairs or deletes source files; a concurrent change is a recoverable
        # failure instead.
        after_inventory = _validate_tree(source_root, label="staged payload")
        after_fingerprint = _source_fingerprint(source_root, after_inventory)
        if after_inventory != source_inventory or after_fingerprint != source_fingerprint:
            raise BundleError("Staged payload changed during bundle creation; refusing to publish")

        runtime_compat = temporary / "runtime" / "compatdata"
        logs = temporary / "logs"
        metadata_dir = temporary / "metadata"
        try:
            runtime_compat.mkdir(parents=True)
            logs.mkdir(parents=True)
            metadata_dir.mkdir(parents=True)
        except OSError as exc:
            raise BundleError(f"Could not create bundle runtime directories: {exc}") from exc

        launcher = temporary / f"Launch {app}"
        try:
            launcher.write_text(
                _launcher_text(proton=configured_proton, selected_relative=selected_relative),
                encoding="utf-8",
            )
            launcher.chmod(0o755)
        except OSError as exc:
            raise BundleError(f"Could not write bundle launcher: {exc}") from exc

        effective_icon = icon_path if icon_path is not None else icon
        icon_target = temporary / "icon.png"
        icon_kind = _copy_icon(effective_icon, icon_target)

        selected_posix = selected_relative.as_posix()
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "app_name": app,
            "bundle_name": final_bundle.name,
            "source_description": Path(source_description).name
            if source_description
            else source_root.name,
            "payload_root": "payload",
            "selected_exe": selected_posix,
            "selected_exe_path": f"payload/{selected_posix}",
            "icon": "icon.png",
            "icon_source": icon_kind,
            "recipe": recipe,
        }
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "root": "payload",
            "files": source_inventory,
            "inventory": source_inventory,
        }
        metadata_path = metadata_dir / "funnel.json"
        manifest_path = metadata_dir / "manifest.json"
        _write_json(metadata_path, metadata)
        _write_json(manifest_path, manifest)

        _validate_bundle(
            temporary,
            selected_relative=selected_relative,
            expected_inventory=source_inventory,
            launcher=launcher,
            metadata_path=metadata_path,
            manifest_path=manifest_path,
            icon_path=icon_target,
        )

        # Recheck the collision immediately before publication.  This keeps
        # normal collisions unique and ensures we never intentionally replace
        # a completed bundle.
        if os.path.lexists(final_bundle):
            final_bundle = _unique_bundle_path(output_parent, app)
        try:
            temporary.rename(final_bundle)
        except OSError as exc:
            raise BundleError(f"Could not publish bundle atomically: {exc}") from exc
        temporary = None
        return BundleResult(
            bundle_root=final_bundle,
            payload_root=final_bundle / "payload",
            launcher=final_bundle / launcher.name,
            metadata_path=final_bundle / "metadata" / "funnel.json",
            manifest_path=final_bundle / "metadata" / "manifest.json",
            icon_path=final_bundle / "icon.png",
        )
    except BundleError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise BundleError(f"Could not create Funnel bundle: {exc}") from exc
    finally:
        _cleanup_temporary(temporary)


# A compact alias is useful to library callers while keeping ``build_bundle``
# the canonical API used by the tests and orchestration layer.
build = build_bundle


__all__ = [
    "BundleError",
    "BundleResult",
    "MissingDependencyError",
    "build",
    "build_bundle",
]
