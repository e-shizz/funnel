#!/usr/bin/env python3
"""Funnel CLI — clean OS-launcher pack. GUI is Add-only."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from funnel.pack import pack_path
from funnel.paths import ensure_layout


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="funnel",
        description="Funnel: Windows archive/folder/EXE → Linux Desktop application",
    )
    p.add_argument("path", nargs="?", help="Archive, game folder, .exe, or the read-only 'doctor' command")
    p.add_argument("--name", help="Override display name")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--ui", "--add", action="store_true", dest="ui", help="Open Add GUI")
    p.add_argument("--list", action="store_true", help="List converted apps in ~/.funnel/state")
    p.add_argument("--finish-install", metavar="COMPAT_ID", help="Publish apps already installed in one Funnel prefix")
    p.add_argument("--product", help="Product name hint for --finish-install")
    args = p.parse_args(argv)

    if args.path == "doctor":
        from funnel.doctor import doctor_checks, format_checks
        checks = doctor_checks()
        print(format_checks(checks))
        return 0 if all(check.ok for check in checks) else 1

    ensure_layout()

    if args.finish_install:
        if not args.product:
            p.error("--finish-install requires --product")
        from funnel.installer import prefix_has_running_processes, publish_installed_apps
        from funnel.paths import STATE
        from funnel.runtime import runtime_from_state
        runtime = None
        state_file = STATE / f"{args.finish_install}.json"
        if state_file.is_file():
            try:
                import json
                runtime = runtime_from_state(json.loads(state_file.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f"FAILED: cannot read runtime from {state_file}: {exc}", file=sys.stderr)
                return 1
            prefix = runtime.prefix
        else:
            compat = Path.home() / ".local/share/Steam/steamapps/compatdata" / args.finish_install
            prefix = compat / "pfx"
        if prefix_has_running_processes(prefix):
            print(f"FAILED: prefix {prefix} still has running processes; cancel the installer first.", file=sys.stderr)
            return 1
        try:
            results = publish_installed_apps(prefix, args.product, args.path or "recovery", None, runtime=runtime)
        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
        for result in results:
            print(f"OK: {result.display_name}")
            print(f"     target={result.exe}")
            print(f"     menu_desktop={result.desktop_file}")
            print(f"     desktop_application={result.desktop_copy}")
        return 0

    if args.list:
        from funnel.paths import STATE
        import json
        for f in sorted(STATE.glob("*.json")):
            try:
                meta = json.loads(f.read_text())
                print(f"{meta.get('display_name')}\t{meta.get('desktop')}\t{meta.get('recipe')}")
            except Exception:
                print(f.name)
        return 0

    if args.ui or args.path is None:
        gui = ROOT / "funnel_gui.py"
        return subprocess.call(["/usr/bin/python3", str(gui)])

    try:
        r = pack_path(
            args.path,
            force_name=args.name,
            verbose=args.verbose,
        )
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for m in r.messages:
        print(m)
    if not r.ok:
        print(f"FAILED: {r.error}", file=sys.stderr)
        return 1
    print(f"OK: {r.display_name}")
    print(f"     Open from KDE app launcher — search: {r.display_name}")
    print(f"     menu_desktop={r.desktop_file}")
    print(f"     desktop_application={r.desktop_copy}")
    for application in r.published_apps:
        print(f"OK: {application.display_name}")
        print(f"     menu_desktop={application.desktop_file}")
        print(f"     desktop_application={application.desktop_copy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
