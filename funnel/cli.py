#!/usr/bin/env python3
"""Funnel command line.

    funnel                      open the drop window
    funnel PATH                 convert one game folder, archive, EXE, or installer
    funnel list                 show converted games
    funnel remove NAME          remove a game's launcher (keeps saves/prefix)
    funnel remove NAME --purge  also delete its Wine prefix and extracted copy
    funnel doctor               check dependencies (read-only)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from funnel.pack import pack_path
from funnel.paths import ensure_layout

COMMANDS = ("doctor", "list", "remove")


def _command(argv: list[str]) -> str | None:
    """A leading command word wins unless a file/folder with that name exists here."""
    if argv and argv[0] in COMMANDS and not Path(argv[0]).exists():
        return argv[0]
    return None


def cmd_doctor() -> int:
    from funnel.doctor import doctor_checks, format_checks

    checks = doctor_checks()
    print(format_checks(checks))
    return 0 if all(check.ok for check in checks) else 1


def cmd_list() -> int:
    from funnel.library import launchers

    records = launchers()
    if not records:
        print("No games converted yet. Drop one into Funnel or run: funnel /path/to/game")
        return 0
    width = max(len(record.slug) for record in records)
    for record in records:
        status = "" if record.installed() else "  (launcher missing)"
        print(f"{record.slug:<{width}}  {record.display_name}{status}")
    return 0


def cmd_remove(argv: list[str]) -> int:
    from funnel.library import find, remove

    p = argparse.ArgumentParser(prog="funnel remove", description="Remove a converted game's launcher.")
    p.add_argument("name", help="Slug or display name, as shown by 'funnel list'")
    p.add_argument("--purge", action="store_true",
                   help="Also delete the game's Wine prefix (saves and settings!) and any extracted archive copy")
    args = p.parse_args(argv)
    matches = find(args.name)
    if not matches:
        print(f"No converted game named {args.name!r}. Try 'funnel list'.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"{args.name!r} matches several games; use one of these slugs:", file=sys.stderr)
        for record in matches:
            print(f"  {record.slug}", file=sys.stderr)
        return 1
    result = remove(matches[0], purge=args.purge)
    print(f"Removed: {result.record.display_name}")
    for path in result.removed:
        print(f"  deleted {path}")
    for note in result.kept:
        print(f"  {note}")
    return 0


def cmd_finish_install(compat_id: str, product: str, source: str | None) -> int:
    from funnel.installer import prefix_has_running_processes, publish_installed_apps
    from funnel.paths import STATE
    from funnel.runtime import runtime_from_state

    runtime = None
    state_file = STATE / f"{compat_id}.json"
    if state_file.is_file():
        try:
            runtime = runtime_from_state(json.loads(state_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"FAILED: cannot read runtime from {state_file}: {exc}", file=sys.stderr)
            return 1
        prefix = runtime.prefix
    else:
        prefix = Path.home() / ".local/share/Steam/steamapps/compatdata" / compat_id / "pfx"
    if prefix_has_running_processes(prefix):
        print(f"FAILED: prefix {prefix} still has running processes; cancel the installer first.", file=sys.stderr)
        return 1
    try:
        results = publish_installed_apps(prefix, product, source or "recovery", None, runtime=runtime)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    for result in results:
        print(f"OK: {result.display_name}")
        print(f"     target={result.exe}")
        print(f"     menu_desktop={result.desktop_file}")
        print(f"     desktop_application={result.desktop_copy}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = _command(argv)
    if command == "doctor":
        return cmd_doctor()
    if command is not None:
        ensure_layout()
        return cmd_list() if command == "list" else cmd_remove(argv[1:])

    p = argparse.ArgumentParser(
        prog="funnel",
        description="Funnel: Windows game folder/archive/EXE/installer → Linux desktop launcher. "
                    "Other commands: funnel list | funnel remove NAME [--purge] | funnel doctor",
    )
    p.add_argument("path", nargs="?", help="Game folder, archive, .exe, or installer")
    p.add_argument("--name", help="Override display name")
    p.add_argument("--new", action="store_true",
                   help="Create a fresh launcher even if this input was converted before")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--ui", "--add", action="store_true", dest="ui", help="Open the drop window")
    p.add_argument("--list", action="store_true", help="Same as 'funnel list'")
    p.add_argument("--finish-install", metavar="COMPAT_ID", help="Publish apps already installed in one Funnel prefix")
    p.add_argument("--product", help="Product name hint for --finish-install")
    args = p.parse_args(argv)

    ensure_layout()

    if args.finish_install:
        if not args.product:
            p.error("--finish-install requires --product")
        return cmd_finish_install(args.finish_install, args.product, args.path)

    if args.list:
        return cmd_list()

    if args.ui or args.path is None:
        gui = ROOT / "funnel_gui.py"
        return subprocess.call(["/usr/bin/python3", str(gui)])

    try:
        r = pack_path(args.path, force_name=args.name, verbose=args.verbose, force_new=args.new)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for m in r.messages:
        print(m)
    if not r.ok:
        print(f"FAILED: {r.error}", file=sys.stderr)
        return 1
    if r.already_existed:
        print(f"ALREADY CONVERTED: {r.display_name}  (use --new for another copy, or 'funnel remove {r.slug}')")
    else:
        print(f"OK: {r.display_name}")
        print(f"     Open it from your application menu — search: {r.display_name}")
    print(f"     menu_desktop={r.desktop_file}")
    print(f"     desktop_application={r.desktop_copy}")
    for application in r.published_apps:
        print(f"OK: {application.display_name}")
        print(f"     menu_desktop={application.desktop_file}")
        print(f"     desktop_application={application.desktop_copy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
