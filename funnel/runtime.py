"""Runtime discovery and command construction for Windows applications."""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping


class RuntimeKind(str, Enum):
    UMU = "umu"
    WINE = "wine"
    STEAM = "steam"


class RuntimeUnavailable(RuntimeError):
    """Raised when no requested or usable runtime can be found."""


@dataclass(frozen=True)
class RuntimeSpec:
    kind: RuntimeKind
    executable: Path
    prefix: Path
    steam_root: Path | None = None
    compat_path: Path | None = None
    proton_path: str = "UMU-Proton"


@dataclass(frozen=True)
class RuntimeInvocation:
    command: tuple[str, ...]
    environment: dict[str, str]


def _usable(path: str | os.PathLike[str] | None) -> Path | None:
    if not path:
        return None
    candidate = Path(path).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate.resolve()
    return None


def _prefix(home: Path, stable_id: str) -> Path:
    return home / ".local/share/funnel/prefixes" / stable_id


def discover_runtime(
    stable_id: str,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    kind: str | RuntimeKind | None = None,
    executable: str | os.PathLike[str] | None = None,
    proton: str | os.PathLike[str] | None = None,
    steam: str | os.PathLike[str] | None = None,
) -> RuntimeSpec:
    """Select a runtime without creating prefixes or other filesystem state."""
    env = os.environ if environ is None else environ
    home_path = Path.home() if home is None else Path(home)
    requested_value = kind.value if isinstance(kind, RuntimeKind) else (kind or env.get("FUNNEL_RUNTIME"))
    override = executable or env.get("FUNNEL_RUNTIME_EXECUTABLE") or env.get("FUNNEL_RUNTIME_PATH")
    proton_override = proton or env.get("FUNNEL_PROTON")
    steam_root = Path(steam or env.get("FUNNEL_STEAM") or home_path / ".local/share/Steam").expanduser()
    funnel_prefix = _prefix(home_path, stable_id)

    if requested_value:
        try:
            requested = RuntimeKind(str(requested_value).casefold())
        except ValueError as exc:
            raise RuntimeUnavailable("FUNNEL_RUNTIME must be umu, wine, or steam") from exc
        if requested is RuntimeKind.UMU:
            candidate = _usable(override or which("umu-run"))
            if not candidate:
                raise RuntimeUnavailable("FUNNEL_RUNTIME=umu was requested, but umu-run is not an executable; set FUNNEL_RUNTIME_EXECUTABLE")
            return RuntimeSpec(requested, candidate, funnel_prefix, proton_path=env.get("PROTONPATH", "UMU-Proton"))
        if requested is RuntimeKind.WINE:
            candidate = _usable(override or which("wine") or which("wine64"))
            if not candidate:
                raise RuntimeUnavailable("FUNNEL_RUNTIME=wine was requested, but wine/wine64 is not executable; set FUNNEL_RUNTIME_EXECUTABLE")
            return RuntimeSpec(requested, candidate, funnel_prefix)
        candidate = _usable(override or proton_override or steam_root / "steamapps/common/Proton - Experimental/proton")
        if not candidate:
            raise RuntimeUnavailable("FUNNEL_RUNTIME=steam was requested, but Proton is not executable; set FUNNEL_RUNTIME_EXECUTABLE or FUNNEL_PROTON")
        compat = steam_root / "steamapps/compatdata" / stable_id
        return RuntimeSpec(requested, candidate, compat / "pfx", steam_root=steam_root, compat_path=compat)

    if override:
        name = Path(override).name.casefold()
        inferred = "umu" if "umu" in name else "wine" if "wine" in name else "steam" if "proton" in name else None
        if inferred is None:
            raise RuntimeUnavailable("FUNNEL_RUNTIME_EXECUTABLE is ambiguous; also set FUNNEL_RUNTIME=umu, wine, or steam")
        return discover_runtime(stable_id, home=home_path, environ=env, which=which, kind=inferred,
                                executable=override, proton=proton_override, steam=steam_root)

    # The established FUNNEL_PROTON argument/environment remains an explicit
    # backwards-compatible request for the Steam backend.
    if proton_override:
        return discover_runtime(stable_id, home=home_path, environ=env, which=which, kind="steam",
                                proton=proton_override, steam=steam_root)
    candidate = _usable(which("umu-run"))
    if candidate:
        return RuntimeSpec(RuntimeKind.UMU, candidate, funnel_prefix, proton_path=env.get("PROTONPATH", "UMU-Proton"))
    candidate = _usable(which("wine") or which("wine64"))
    if candidate:
        return RuntimeSpec(RuntimeKind.WINE, candidate, funnel_prefix)
    candidate = _usable(steam_root / "steamapps/common/Proton - Experimental/proton")
    if candidate:
        compat = steam_root / "steamapps/compatdata" / stable_id
        return RuntimeSpec(RuntimeKind.STEAM, candidate, compat / "pfx", steam_root=steam_root, compat_path=compat)
    raise RuntimeUnavailable(
        "No usable Windows runtime found. Install/configure umu-run (preferred) or Wine, "
        "or explicitly select legacy Steam Proton with FUNNEL_RUNTIME=steam. Steam is not required."
    )


def runtime_environment(spec: RuntimeSpec, extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    if spec.kind is RuntimeKind.UMU:
        values = {
            "WINEPREFIX": str(spec.prefix), "GAMEID": "umu-default", "STORE": "none",
            "PROTONPATH": spec.proton_path,
        }
    elif spec.kind is RuntimeKind.WINE:
        values = {"WINEPREFIX": str(spec.prefix)}
    else:
        compat = spec.compat_path or spec.prefix.parent
        values = {
            "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(spec.steam_root or ""),
            "STEAM_COMPAT_DATA_PATH": str(compat),
        }
    values.update({str(key): str(value) for key, value in (extra_env or {}).items()})
    return values


def _base_command(spec: RuntimeSpec) -> tuple[str, ...]:
    if spec.kind is RuntimeKind.STEAM:
        return (str(spec.executable), "run")
    return (str(spec.executable),)


def build_invocation(
    spec: RuntimeSpec,
    target: str | os.PathLike[str],
    fixed_arguments: tuple[str, ...] = (),
    *,
    extra_env: Mapping[str, str] | None = None,
    installer: bool = False,
    msi: bool = False,
) -> RuntimeInvocation:
    target_value = str(target)
    command = list(_base_command(spec))
    if installer and msi:
        command.extend(("msiexec", "/i", target_value))
    else:
        command.append(target_value)
        command.extend(fixed_arguments)
    return RuntimeInvocation(tuple(command), runtime_environment(spec, extra_env))


def render_launcher(
    spec: RuntimeSpec,
    target: str | os.PathLike[str],
    fixed_arguments: tuple[str, ...] = (),
    *,
    extra_env: Mapping[str, str] | None = None,
    slug: str = "application",
    recipe: str = "default",
    log_dir: str | os.PathLike[str] | None = None,
) -> str:
    """Render a shell launcher with fixed argv and forwarded user argv kept separate."""
    target_path = Path(target)
    exports = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in runtime_environment(spec, extra_env).items())
    runtime_value = shlex.quote(str(spec.executable))
    runtime_call = '"$RUNTIME" run' if spec.kind is RuntimeKind.STEAM else '"$RUNTIME"'
    debug_exports = ""
    if spec.kind is RuntimeKind.STEAM:
        debug_exports = (
            '  export PROTON_LOG=1 PROTON_LOG_DIR="$LOG_DIR"\n'
            '  export WINEDEBUG="${WINEDEBUG:-+timestamp,+loaddll,+err,+fixme,+quartz,+gstreamer}"\n'
        )
    fixed = "".join(f" {shlex.quote(value)}" for value in fixed_arguments)
    logs = Path(log_dir) if log_dir is not None else Path.home() / ".funnel/logs"
    return f'''#!/usr/bin/env bash
# Generated by Funnel — Windows runtime desktop application
# Debug: FUNNEL_DEBUG=1 this-script
set -euo pipefail

GAME={shlex.quote(str(target_path))}
SLUG={shlex.quote(slug)}
RECIPE={shlex.quote(recipe)}
LOG_DIR={shlex.quote(str(logs))}
RUNTIME={runtime_value}

if [[ ! -x "$RUNTIME" ]]; then
  echo "Funnel: runtime not found or not executable: $RUNTIME" >&2
  exit 1
fi
if [[ ! -f "$GAME" ]]; then
  echo "Funnel: EXE missing: $GAME" >&2
  exit 1
fi

{exports}mkdir -p {shlex.quote(str(spec.compat_path or spec.prefix))} "$LOG_DIR"
cd "$(dirname "$GAME")"

if [[ "${{FUNNEL_DEBUG:-0}}" == "1" || "${{1:-}}" == "--debug" ]]; then
  [[ "${{1:-}}" == "--debug" ]] && shift
  STAMP=$(date +%Y%m%d-%H%M%S)
  LOGFILE="$LOG_DIR/funnel-$SLUG-$STAMP.log"
{debug_exports}  exec > >(tee -a "$LOGFILE") 2>&1
  echo "Funnel DEBUG log=$LOGFILE recipe=$RECIPE game=$GAME"
  exec {runtime_call} "$GAME"{fixed} "$@"
fi

exec {runtime_call} "$GAME"{fixed} "$@" 2>>"$LOG_DIR/funnel-$SLUG-errors.log"
'''


def state_runtime_fields(
    spec: RuntimeSpec,
    target: str | os.PathLike[str],
    fixed_arguments: tuple[str, ...],
    *,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    invocation = build_invocation(spec, target, fixed_arguments, extra_env=extra_env)
    return {
        "runtime_kind": spec.kind.value,
        "runtime_executable": str(spec.executable),
        "prefix_path": str(spec.prefix),
        "command": list(invocation.command),
        "environment": invocation.environment,
        "fixed_arguments": list(fixed_arguments),
    }


def runtime_from_state(state: Mapping[str, object]) -> RuntimeSpec:
    """Read Phase 1 state and the accepted legacy proton/compatdata shape."""
    if state.get("runtime_kind"):
        kind = RuntimeKind(str(state["runtime_kind"]))
        prefix = Path(str(state["prefix_path"]))
        compat = Path(str(state["compat_path"])) if kind is RuntimeKind.STEAM and state.get("compat_path") else None
        steam = Path(str(state["steam_root"])) if state.get("steam_root") else None
        return RuntimeSpec(kind, Path(str(state["runtime_executable"])), prefix, steam_root=steam,
                           compat_path=compat, proton_path=str(state.get("proton_path", "UMU-Proton")))
    if state.get("proton") and state.get("compat_path"):
        compat = Path(str(state["compat_path"]))
        steam = None
        parts = compat.parts
        if "steamapps" in parts:
            steam = Path(*parts[:parts.index("steamapps")])
        return RuntimeSpec(RuntimeKind.STEAM, Path(str(state["proton"])), compat / "pfx",
                           steam_root=steam, compat_path=compat)
    raise RuntimeUnavailable("State record does not contain runtime information or legacy proton/compat_path fields")
