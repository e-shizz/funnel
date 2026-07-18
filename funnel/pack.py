"""Pack one Windows payload into real Linux desktop application entries."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .archive import is_archive, resolve_payload
from .detect import Detected, _slugify, detect_input
from .icons import extract_icon
from .paths import DESKTOP_DIR, ICONS, LAUNCH_DIR, LOGS, STATE, ensure_layout, xdg_desktop_dir
from .runtime import (
    RuntimeKind, RuntimeSpec, RuntimeUnavailable, build_invocation, discover_runtime,
    render_launcher, state_runtime_fields,
)

DEFAULT_STEAM = Path.home() / ".local/share/Steam"

DESKTOP_TEMPLATE = """[Desktop Entry]
Version=1.0
Type=Application
Name={name}
Comment=Windows application via Funnel ({runtime})
Exec={exec_path}
Path={app_dir}
Terminal=false
Icon={icon}
Categories={categories}
StartupNotify=true
X-Funnel-Slug={slug}
X-Funnel-Exe={exe}
X-Funnel-Recipe={recipe}
"""


@dataclass
class PackResult:
    ok: bool
    display_name: str
    slug: str
    exe: Path
    launch_script: Path
    desktop_file: Path
    desktop_copy: Path
    compat_id: str
    compat_path: Path
    recipe: str
    messages: list[str] = field(default_factory=list)
    error: str | None = None
    hints: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    shelf_dir: Path | None = None
    icon: Path | None = None
    published_apps: list[PackResult] = field(default_factory=list)
    fixed_args: tuple[str, ...] = ()


def _desktop_exec(path: Path | str) -> str:
    value = str(path)
    if any(character in value for character in " \t\n\""):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _desktop_value(value: Path | str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "")


def _trust_desktop(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    try:
        subprocess.run(
            ["gio", "set", str(path), "metadata::trusted", "true"],
            capture_output=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _compat_id_for(path: Path, slug: str) -> str:
    digest = hashlib.sha256(f"{path.resolve()}\0{slug}".encode()).hexdigest()
    return f"42{int(digest[:8], 16) % 10_000_000:08d}"


def _pick_recipe(det: Detected) -> tuple[str, dict[str, str], list[str]]:
    env: dict[str, str] = {}
    notes: list[str] = []
    hints = set(det.hints)
    recipe = "proton-default"
    if "msix-identity:OpenAI.Codex" in hints:
        recipe = "codex-msix-proton"
        notes.append("Official Codex MSIX full-trust application")
    if "locale:ja_JP" in hints:
        env.update(LANG="ja_JP.UTF-8", LC_ALL="ja_JP.UTF-8")
        recipe = "proton-jp" if recipe == "proton-default" else recipe + "+jp"
        notes.append("Japanese payload -> ja_JP locale")
    if "engine:unity" in hints:
        env.setdefault("WINEDLLOVERRIDES", "winemenubuilder.exe=d")
        recipe = "proton-unity" if recipe == "proton-default" else recipe + "+unity"
        notes.append("Unity fingerprints")
    if "engine:alicesoft" in hints or "media:alm-mpeg" in hints:
        if recipe == "proton-default":
            recipe = "proton-alicesoft"
        notes.append("AliceSoft/System40 fingerprints")
    low = f"{det.exe.name} {det.display_name}".casefold()
    if "koikatu" in low or "koikatsu" in low or "charastudio" in low:
        env["WINEDLLOVERRIDES"] = "winhttp=n,b;winemenubuilder.exe=d"
        recipe = "proton-koikatsu"
        notes.append("Koikatsu-class overrides")
    try:
        siblings = {path.name.casefold() for path in det.app_dir.iterdir()}
    except OSError:
        siblings = set()
    if "nw.dll" in siblings or "node.dll" in siblings or "package.json" in siblings:
        if recipe == "proton-default":
            recipe = "proton-nwjs"
        notes.append("NW.js/Electron-like payload")
    return recipe, env, notes


def _publication_slot(base_slug: str, exe: Path, steam: Path, reserve_compat: bool = True) -> tuple[str, str, Path, Path, Path]:
    desktop_dir = xdg_desktop_dir()
    compat_root = steam / "steamapps" / "compatdata"
    number = 1
    while True:
        slug = base_slug if number == 1 else f"{base_slug}-{number}"
        compat_id = _compat_id_for(exe, slug)
        launch = LAUNCH_DIR / f"funnel-{slug}"
        menu = DESKTOP_DIR / f"funnel-{slug}.desktop"
        desktop = desktop_dir / f"funnel-{slug}.desktop"
        reserved = (
            launch, menu, desktop, STATE / f"{slug}.json", STATE / f"{slug}.txt",
            ICONS / f"{slug}.png",
        )
        if reserve_compat:
            reserved += (compat_root / compat_id,)
        if not any(path.exists() for path in reserved):
            return slug, compat_id, launch, menu, desktop
        number += 1


def _write_new(path: Path, content: str, mode: int | None = None) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)
    if mode is not None:
        path.chmod(mode)


def pack_path(
    raw: str | Path,
    *,
    proton: Path | None = None,
    steam: Path | None = None,
    install_desktop: bool = True,
    force_name: str | None = None,
    verbose: bool = False,
    install_path_launcher: bool = True,
    confirmed_app_dir: Path | None = None,
    selected_exe: str | Path | None = None,
    status_callback=None,
    runtime: RuntimeSpec | None = None,
) -> PackResult:
    del install_path_launcher, confirmed_app_dir, selected_exe
    if not install_desktop:
        raise ValueError("Funnel recovery requires creation of both desktop application entries")
    ensure_layout()
    source = Path(raw).expanduser().resolve()
    resolved, resolve_notes = resolve_payload(source)
    det = detect_input(resolved)
    if force_name:
        det.display_name = force_name
        det.slug = _slugify(force_name)
    elif source.is_file() and is_archive(source) and not any(
        hint.startswith("msix-identity:") for hint in det.hints
    ):
        det.display_name = source.stem
        det.slug = _slugify(source.stem)

    steam_path = Path(steam or os.environ.get("FUNNEL_STEAM") or DEFAULT_STEAM).expanduser()
    recipe, env, notes = _pick_recipe(det)
    notes = list(resolve_notes) + notes
    if any(hint in det.hints for hint in ("media:alm-mpeg", "engine:alicesoft", "media:playmovie-dshow")):
        notes.append("movie_fix=not-applied (original and extracted payload files are never rewritten)")

    from .installer import installer_product_hint, looks_like_installer, publish_installed_apps
    installer_mode = source.is_file() and looks_like_installer(source)
    slug, compat_id, launch_path, menu_path, desktop_path = _publication_slot(
        det.slug, det.exe, steam_path,
    )
    det.slug = slug
    try:
        runtime_spec = runtime or discover_runtime(slug, proton=proton, steam=steam_path)
        if runtime is None and runtime_spec.kind is RuntimeKind.STEAM:
            runtime_spec = discover_runtime(
                compat_id, kind=RuntimeKind.STEAM, executable=runtime_spec.executable,
                steam=runtime_spec.steam_root or steam_path,
            )
    except RuntimeUnavailable as exc:
        compat_path = steam_path / "steamapps" / "compatdata" / compat_id
        return PackResult(False, det.display_name, slug, det.exe, launch_path, menu_path, desktop_path,
                          compat_id, compat_path, recipe, notes, error=str(exc), hints=list(det.hints), env=env)
    if runtime_spec.kind is RuntimeKind.STEAM:
        compat_path = runtime_spec.compat_path or runtime_spec.prefix.parent
    else:
        compat_id = slug
        compat_path = runtime_spec.prefix

    if installer_mode:
        from .installer import run_installer
        installer_invocation = build_invocation(
            runtime_spec, source, installer=True, msi=source.suffix.casefold() == ".msi",
        )
        recovery_meta = {
            "packed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "display_name": det.display_name,
            "slug": slug, "source": str(source), "kind": "installer-recovery", "exe": str(source),
            "app_dir": str(source.parent), "compat_id": compat_id, "compat_path": str(compat_path),
            "recipe": recipe, "hints": list(det.hints), "env": env, "fixed_args": [],
            "runtime_kind": runtime_spec.kind.value, "runtime_executable": str(runtime_spec.executable),
            "prefix_path": str(runtime_spec.prefix), "command": list(installer_invocation.command),
            "environment": installer_invocation.environment, "fixed_arguments": [],
        }
        if runtime_spec.kind is RuntimeKind.STEAM:
            recovery_meta.update(proton=str(runtime_spec.executable), steam_root=str(runtime_spec.steam_root or steam_path))
        if runtime_spec.kind is RuntimeKind.UMU:
            recovery_meta["proton_path"] = runtime_spec.proton_path
        _write_new(STATE / f"{slug}.json", json.dumps(recovery_meta, indent=2) + "\n")
        if status_callback:
            status_callback("Installer is open. Finish or cancel it; Funnel will create the installed apps when it closes.")
        try:
            before, completed = run_installer(source, runtime_spec)
        except RuntimeError as exc:
            return PackResult(
                False, det.display_name, slug, det.exe, launch_path, menu_path, desktop_path,
                compat_id, compat_path, recipe, notes + ["installer_runtime_failure"],
                error=str(exc), hints=list(det.hints), env=env,
            )
        # A cancelled or partially failed wizard may still have installed the main
        # payload. Always inventory the exact prefix before deciding the outcome.
        try:
            published = publish_installed_apps(runtime_spec.prefix, force_name or installer_product_hint(source), source,
                                               before, runtime=runtime_spec)
        except RuntimeError as exc:
            exit_detail = f"installer_exit={completed.returncode}"
            return PackResult(False, det.display_name, slug, det.exe, launch_path, menu_path, desktop_path,
                              compat_id, compat_path, recipe, notes + [exit_detail],
                              error=f"{exc} Installer exit status was {completed.returncode}; prefix preserved at {runtime_spec.prefix}")
        if completed.returncode != 0:
            for result in published:
                result.messages.append(
                    f"installer_exit={completed.returncode} (installed payload discovered and preserved)"
                )
        first = published[0]
        first.messages.insert(0, f"installer_exit={completed.returncode}")
        first.published_apps = published[1:]
        return first

    icon_path: Path | None = None
    try:
        icon_path = extract_icon(det.app_dir, det.exe, slug)
    except Exception as exc:
        notes.append(f"icon_error={exc}")
    notes.append(f"icon={icon_path}" if icon_path else "icon=default")

    messages = notes + [f"recipe={recipe}", f"compat_id={compat_id}"]
    if det.hints:
        messages.append(f"hints={','.join(det.hints)}")
    if verbose:
        messages.extend((f"app_dir={det.app_dir}", f"kind={det.kind}"))

    script = render_launcher(runtime_spec, det.exe, det.fixed_args, extra_env=env, slug=slug,
                             recipe=recipe, log_dir=LOGS)
    icon_value = str(icon_path) if icon_path else "application-x-executable"
    if "msix-identity:OpenAI.Codex" in det.hints:
        categories = "Development;"
    elif any(hint.startswith("msix-identity:") for hint in det.hints):
        categories = "Utility;"
    else:
        categories = "Utility;" if source.is_file() and source.suffix.casefold() == ".exe" else "Game;"
    desktop_body = DESKTOP_TEMPLATE.format(
        name=_desktop_value(det.display_name), exec_path=_desktop_exec(launch_path),
        app_dir=_desktop_value(det.app_dir), icon=_desktop_value(icon_value),
        categories=categories, slug=_desktop_value(slug), exe=_desktop_value(det.exe),
        recipe=_desktop_value(recipe), runtime=runtime_spec.kind.value.upper(),
    )

    _write_new(launch_path, script, 0o755)
    if install_desktop:
        _write_new(menu_path, desktop_body, 0o755)
        _write_new(desktop_path, desktop_body, 0o755)
        _trust_desktop(menu_path)
        _trust_desktop(desktop_path)
        try:
            subprocess.run(
                ["update-desktop-database", str(DESKTOP_DIR)], capture_output=True,
                timeout=10, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    required = (launch_path, menu_path, desktop_path)
    if not all(path.is_file() for path in required):
        raise RuntimeError("Funnel did not create all required launcher and desktop artifacts")

    messages.extend((
        f"launch={launch_path}", f"desktop={menu_path}", f"desktop_pin={desktop_path}",
        f"exe={det.exe}", f"compat_path={compat_path}",
        "library=KDE Application Launcher", "originals=untouched",
    ))
    meta = {
        "packed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "display_name": det.display_name,
        "slug": slug, "source": str(source), "kind": det.kind, "exe": str(det.exe),
        "app_dir": str(det.app_dir), "compat_id": compat_id, "compat_path": str(compat_path),
        "recipe": recipe, "hints": list(det.hints), "env": env, "launch": str(launch_path),
        "desktop": str(menu_path), "desktop_pin": str(desktop_path), "icon": str(icon_path) if icon_path else None,
        "fixed_args": list(det.fixed_args),
    }
    meta.update(state_runtime_fields(runtime_spec, det.exe, det.fixed_args, extra_env=env))
    if runtime_spec.kind is RuntimeKind.STEAM:
        meta.update(proton=str(runtime_spec.executable), steam_root=str(runtime_spec.steam_root or steam_path))
    if runtime_spec.kind is RuntimeKind.UMU:
        meta["proton_path"] = runtime_spec.proton_path
    _write_new(STATE / f"{slug}.json", json.dumps(meta, indent=2) + "\n")
    _write_new(STATE / f"{slug}.txt", "\n".join(messages) + "\n")
    return PackResult(
        True, det.display_name, slug, det.exe, launch_path, menu_path, desktop_path,
        compat_id, compat_path, recipe, messages, hints=list(det.hints), env=env, icon=icon_path,
        fixed_args=det.fixed_args,
    )


def publish_existing_executable(
    exe: str | Path, display_name: str, compat_id: str, compat_path: str | Path, *,
    source: str | Path, proton: Path | None = None, steam: Path | None = None,
    discovery_messages: list[str] | None = None,
    runtime: RuntimeSpec | None = None,
) -> PackResult:
    """Publish an installed target without allocating another Proton prefix."""
    ensure_layout()
    target = Path(exe).expanduser().resolve()
    steam_path = Path(steam or os.environ.get("FUNNEL_STEAM") or DEFAULT_STEAM).expanduser()
    fixed_compat = Path(compat_path).expanduser().resolve()
    if runtime is None:
        runtime_spec = discover_runtime(str(compat_id), kind="steam", proton=proton, steam=steam_path)
    else:
        runtime_spec = runtime
    det = detect_input(target)
    det.display_name = display_name
    base_slug = _slugify(display_name)
    slug, _unused, launch_path, menu_path, desktop_path = _publication_slot(base_slug, target, steam_path, False)
    recipe, env, notes = _pick_recipe(det)
    notes = list(discovery_messages or []) + notes
    icon_path = None
    try:
        icon_path = extract_icon(det.app_dir, target, slug)
    except Exception as exc:
        notes.append(f"icon_error={exc}")
    script = render_launcher(runtime_spec, target, det.fixed_args, extra_env=env, slug=slug,
                             recipe=recipe, log_dir=LOGS)
    desktop_body = DESKTOP_TEMPLATE.format(
        name=_desktop_value(display_name), exec_path=_desktop_exec(launch_path), app_dir=_desktop_value(target.parent),
        icon=_desktop_value(icon_path or "application-x-executable"), categories="Utility;", slug=_desktop_value(slug),
        exe=_desktop_value(target), recipe=_desktop_value(recipe), runtime=runtime_spec.kind.value.upper(),
    )
    _write_new(launch_path, script, 0o755)
    _write_new(menu_path, desktop_body, 0o755)
    _write_new(desktop_path, desktop_body, 0o755)
    _trust_desktop(menu_path)
    _trust_desktop(desktop_path)
    try:
        subprocess.run(["update-desktop-database", str(DESKTOP_DIR)], capture_output=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    messages = notes + [f"launch={launch_path}", f"desktop={menu_path}", f"desktop_pin={desktop_path}",
                        f"exe={target}", f"compat_path={fixed_compat}", "originals=untouched"]
    meta = {"packed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "display_name": display_name, "slug": slug,
            "source": str(Path(source)), "kind": "installed-exe", "exe": str(target), "app_dir": str(target.parent),
            "compat_id": str(compat_id), "compat_path": str(fixed_compat), "recipe": recipe, "env": env,
            "launch": str(launch_path), "desktop": str(menu_path), "desktop_pin": str(desktop_path),
            "icon": str(icon_path) if icon_path else None,
            "fixed_args": list(det.fixed_args)}
    meta.update(state_runtime_fields(runtime_spec, target, det.fixed_args, extra_env=env))
    if runtime_spec.kind is RuntimeKind.STEAM:
        meta.update(proton=str(runtime_spec.executable), steam_root=str(runtime_spec.steam_root or steam_path))
    if runtime_spec.kind is RuntimeKind.UMU:
        meta["proton_path"] = runtime_spec.proton_path
    _write_new(STATE / f"{slug}.json", json.dumps(meta, indent=2) + "\n")
    _write_new(STATE / f"{slug}.txt", "\n".join(messages) + "\n")
    return PackResult(True, display_name, slug, target, launch_path, menu_path, desktop_path,
                      str(compat_id), fixed_compat, recipe, messages, env=env, icon=icon_path,
                      fixed_args=det.fixed_args)
