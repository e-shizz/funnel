"""Read-only system readiness checks for Funnel."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .runtime import RuntimeUnavailable, discover_runtime


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""


_PACKAGE_HINTS = {
    "fedora": "sudo dnf install 7zip unrar wine desktop-file-utils python3-gobject gtk3",
    "ubuntu": "sudo apt install p7zip-full unrar wine desktop-file-utils python3-gi gir1.2-gtk-3.0",
    "debian": "sudo apt install p7zip-full unrar wine desktop-file-utils python3-gi gir1.2-gtk-3.0",
    "arch": "sudo pacman -S 7zip unrar wine desktop-file-utils python-gobject gtk3",
}


def _parse_os_release(text: str) -> str:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    distro = values.get("ID", "unknown").casefold()
    if distro in {"ubuntu", "debian", "fedora", "arch"}:
        return distro
    family = values.get("ID_LIKE", "").casefold().split()
    return next((item for item in ("fedora", "ubuntu", "debian", "arch") if item in family), distro)


def _gtk_probe() -> tuple[bool, str]:
    environment = os.environ.copy()
    environment["XDG_CACHE_HOME"] = "/dev/null"
    completed = subprocess.run(
        [
            sys.executable, "-c",
            "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk",
        ],
        env=environment, capture_output=True, text=True, check=False,
    )
    if completed.returncode == 0:
        return True, "GTK 3 available"
    detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "probe failed"
    return False, f"GTK 3 unavailable: {detail}"


def _read_os_release() -> str:
    try:
        return Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "ID=unknown\n"


def _writable_without_creating(path: Path) -> bool:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)


def doctor_checks(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    os_release: str | None = None,
    python_version: tuple[int, ...] | None = None,
    gtk_probe: Callable[[], tuple[bool, str]] = _gtk_probe,
) -> list[Check]:
    """Return diagnostics without creating directories or running installers."""
    home_path = Path.home() if home is None else Path(home)
    env = os.environ if environ is None else environ
    distro = _parse_os_release(_read_os_release() if os_release is None else os_release)
    package_hint = _PACKAGE_HINTS.get(distro, "Install 7z, unrar, Wine, desktop-file-utils, Python 3 GTK bindings, and GTK 3 with your distribution package manager.")
    version = tuple(sys.version_info[:3]) if python_version is None else python_version
    gtk_ok, gtk_detail = gtk_probe()
    python_ok = version >= (3, 10)
    checks = [Check("Python/GTK", python_ok and gtk_ok, f"Python {'.'.join(map(str, version))}; {gtk_detail}", package_hint)]

    seven = which("7z") or which("7zz")
    checks.append(Check("7z", bool(seven), seven or "not found", package_hint))
    unrar = which("unrar")
    checks.append(Check("unrar", bool(unrar), unrar or "not found (7z may handle some RAR files)", package_hint))
    desktop_names = ("desktop-file-validate", "update-desktop-database", "gio")
    desktop_found = {name: which(name) for name in desktop_names}
    desktop_missing = [name for name, value in desktop_found.items() if not value]
    desktop_detail = (
        ", ".join(str(value) for value in desktop_found.values() if value)
        if not desktop_missing else "missing: " + ", ".join(desktop_missing)
    )
    checks.append(Check("desktop-file tools", not desktop_missing, desktop_detail, package_hint))
    try:
        runtime = discover_runtime("doctor-check", home=home_path, environ=env, which=which)
        checks.append(Check("runtime", True, f"{runtime.kind.value}: {runtime.executable}; Steam is optional and not required"))
    except RuntimeUnavailable as exc:
        checks.append(Check("runtime", False, f"{exc} Steam is optional and not required", "Install/configure umu-run or Wine; legacy Steam Proton is optional."))
    prefix_root = home_path / ".local/share/funnel/prefixes"
    checks.append(Check("prefix directory", _writable_without_creating(prefix_root), f"{prefix_root} (checked without creating it)", "Make the user-owned XDG data directory writable."))
    checks.append(Check("distribution", distro != "unknown", distro, package_hint))
    return checks


def format_checks(checks: list[Check]) -> str:
    lines: list[str] = []
    for check in checks:
        lines.append(f"{'OK' if check.ok else 'MISSING'}  {check.name}: {check.detail}")
        if not check.ok and check.hint:
            lines.append(f"         hint: {check.hint}")
    lines.append("Steam client/account/binaries are optional and are not required by the preferred UMU or Wine backends.")
    return "\n".join(lines)
