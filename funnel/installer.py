"""Small, prefix-local installer inventory and application discovery helper."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .runtime import RuntimeSpec, build_invocation

PROC_ROOT = Path("/proc")

_BAD = re.compile(
    r"(?i)(unins|uninstall|setup|install|update|updater|crash|report|service|driver|"
    r"redist|vc_redist|helper|elevate|toast|adb|daemon|watch)"
)
_BAD_DIRS = {
    "drivers", "resources", "node_modules", "temp", "windows",
    "internet explorer", "windows media player", "windows nt",
}
_REG_SECTION = re.compile(r"^\[([^]]+)]")
_REG_VALUE = re.compile(r'^"([^"\\]+)"=(?:str\(2\):)?"(.*)"$')


@dataclass(frozen=True)
class PrefixInventory:
    executables: dict[str, tuple[int, int]]
    shortcuts: dict[str, tuple[int, int]]
    registry: dict[str, dict[str, str]]


@dataclass(frozen=True)
class InstalledCandidate:
    path: Path
    display_name: str
    score: int
    evidence: tuple[str, ...]


def looks_like_installer(path: str | Path) -> bool:
    candidate = Path(path)
    if candidate.suffix.casefold() == ".msi":
        return True
    if candidate.suffix.casefold() != ".exe":
        return False
    if re.search(r"(?i)(^|[._ -])(setup|install|installer)([._ -]|$)", candidate.stem):
        return True
    try:
        size = candidate.stat().st_size
        with candidate.open("rb") as handle:
            data = handle.read(4 * 1024 * 1024)
            if size > len(data):
                handle.seek(max(0, size - 1024 * 1024))
                data += handle.read(1024 * 1024)
        low = data.lower()
        robust_markers = (
            b"nullsoftinst",
            b"nullsoft install system",
            b"nullsoft.nsis",
            b"inno setup setup data",
            b"inno setup",
        )
        return any(
            marker in low or marker.decode("ascii").encode("utf-16le") in low
            for marker in robust_markers
        )
    except OSError:
        return False


def installer_product_hint(path: str | Path) -> str:
    words = re.split(r"[._ -]+", Path(path).stem)
    kept: list[str] = []
    for word in words:
        if word.casefold() in {"setup", "install", "installer"} or re.fullmatch(r"(?i)v?\d+(?:\.\d+)*", word):
            break
        kept.append(word)
    return " ".join(kept).strip() or Path(path).stem


def _files(root: Path, suffix: str) -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return found
    for path in root.rglob("*"):
        try:
            if path.suffix.casefold() == suffix and path.is_file() and not path.is_symlink():
                stat = path.stat()
                found[str(path)] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            continue
    return found


def _registry(prefix: Path) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    for registry in (prefix / "system.reg", prefix / "user.reg"):
        try:
            lines = registry.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        section = ""
        for line in lines:
            match = _REG_SECTION.match(line)
            if match:
                section = match.group(1)
                continue
            value = _REG_VALUE.match(line)
            if section and value and (
                "\\uninstall\\" in section.casefold()
                or value.group(1).casefold() in {"displayicon", "installlocation", "instpath"}
            ):
                entries.setdefault(section, {})[value.group(1).casefold()] = value.group(2)
    return entries


def inventory_prefix(prefix: str | Path) -> PrefixInventory:
    root = Path(prefix).expanduser().resolve()
    exes: dict[str, tuple[int, int]] = {}
    for directory in (root / "drive_c/Program Files", root / "drive_c/Program Files (x86)"):
        exes.update(_files(directory, ".exe"))
    links: dict[str, tuple[int, int]] = {}
    users = root / "drive_c/users"
    start_menus = [root / "drive_c/ProgramData/Microsoft/Windows/Start Menu"]
    start_menus.extend(users.glob("*/AppData/Roaming/Microsoft/Windows/Start Menu"))
    start_menus.extend(users.glob("*/Start Menu"))
    for directory in start_menus:
        links.update(_files(directory, ".lnk"))
    return PrefixInventory(exes, links, _registry(root))


def _windows_path(prefix: Path, value: str) -> Path | None:
    cleaned = value.replace("\\\\", "\\").replace('\\"', '"').strip().strip('"')
    cleaned = cleaned.split(",", 1)[0].strip().strip('"')
    match = re.match(r"(?i)^c:\\(.*)$", cleaned)
    if not match:
        return None
    return prefix / "drive_c" / Path(match.group(1).replace("\\", "/"))


def _shortcut_target(prefix: Path, shortcut: Path) -> Path | None:
    try:
        data = shortcut.read_bytes()[:2 * 1024 * 1024]
    except OSError:
        return None
    text = data.decode("utf-16le", errors="ignore") + "\n" + data.decode("latin1", errors="ignore")
    paths = re.findall(r"(?i)[a-z]:\\[^\x00\r\n]{2,500}?\.exe", text)
    for raw in sorted(paths, key=len, reverse=True):
        target = _windows_path(prefix, raw)
        if target and target.is_file():
            return target
    return None


def _words(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", value.casefold()) if word not in {"setup", "install", "installer", "exe"} and not re.fullmatch(r"v?\d+", word)}


def discover_installed_apps(
    prefix: str | Path, product_hint: str, before_inventory: PrefixInventory | None = None,
) -> list[InstalledCandidate]:
    root = Path(prefix).expanduser().resolve()
    after = inventory_prefix(root)
    changed = set(after.executables) if before_inventory is None else {
        path for path, stamp in after.executables.items()
        if before_inventory.executables.get(path) != stamp
    }
    evidence: dict[str, list[str]] = {}
    before_links = before_inventory.shortcuts if before_inventory else {}
    for name, stamp in after.shortcuts.items():
        if before_links.get(name) == stamp:
            continue
        target = _shortcut_target(root, Path(name))
        if target:
            evidence.setdefault(str(target), []).append(f"new Start Menu shortcut: {Path(name).name}")
            changed.add(str(target))
    before_reg = before_inventory.registry if before_inventory else {}
    for section, values in after.registry.items():
        if before_reg.get(section) == values:
            continue
        icon = _windows_path(root, values.get("displayicon", ""))
        if icon and icon.is_file():
            evidence.setdefault(str(icon), []).append("new uninstall DisplayIcon")
            changed.add(str(icon))
        location = _windows_path(root, values.get("installlocation", "") or values.get("instpath", ""))
        if location and location.is_dir():
            for path in location.glob("*.exe"):
                if path.is_file():
                    evidence.setdefault(str(path), []).append("registered install location")
                    changed.add(str(path))

    hints = _words(product_hint)
    compact_hint = re.sub(r"[^a-z0-9]", "", product_hint.casefold())
    ranked: list[InstalledCandidate] = []
    for name in changed:
        path = Path(name)
        parts = {part.casefold() for part in path.parts}
        if not path.is_file() or _BAD.search(path.stem) or parts & _BAD_DIRS:
            continue
        score = min(path.stat().st_size // (1024 * 1024), 120) - max(0, len(path.parts) - len((root / "drive_c").parts) - 3) * 15
        candidate_words = _words(path.stem) | _words(path.parent.name)
        compact_candidate = re.sub(r"[^a-z0-9]", "", f"{path.stem}{path.parent.name}".casefold())
        if compact_hint and compact_hint in compact_candidate:
            score += 180
        elif hints and hints <= candidate_words:
            score += 180
        elif hints & candidate_words:
            score += 90
        reasons = evidence.get(name, [])
        if reasons:
            score += 300
        if path.stem.casefold() == path.parent.name.casefold():
            score += 80
        if "launcher" in path.stem.casefold():
            score -= 250
        ranked.append(InstalledCandidate(path, path.stem.replace("_", " ").strip(), score, tuple(reasons or ("new Program Files executable",))))
    ranked.sort(key=lambda item: (-item.score, str(item.path).casefold()))
    if not ranked:
        return []
    explicit = [
        item for item in ranked
        if "launcher" not in item.path.stem.casefold()
        and any("shortcut" in reason or "DisplayIcon" in reason for reason in item.evidence)
    ]
    return explicit or [ranked[0]]


def prefix_has_running_processes(prefix: str | Path) -> bool:
    prefix_path = Path(prefix).expanduser().resolve()
    expected_prefix = f"WINEPREFIX={prefix_path}".encode()
    expected_compat = f"STEAM_COMPAT_DATA_PATH={prefix_path.parent}".encode()
    for proc in PROC_ROOT.glob("[0-9]*"):
        if proc.name == str(os.getpid()):
            continue
        try:
            env = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if expected_prefix in env or expected_compat in env:
            return True
    return False


def run_installer(installer_path: str | Path, runtime: RuntimeSpec):
    """Run one installer and return its pre-install inventory and real exit result."""
    source = Path(installer_path).expanduser().resolve()
    container = runtime.compat_path or runtime.prefix
    container.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(build_invocation(runtime, "wineboot").environment)
    initialization = build_invocation(runtime, "wineboot", ("-u",))
    initialized = subprocess.run(list(initialization.command), env=environment, check=False)
    if initialized.returncode != 0:
        raise RuntimeError(
            f"Runtime prefix initialization failed with status {initialized.returncode}; "
            f"prefix preserved at {runtime.prefix}"
        )
    before = inventory_prefix(runtime.prefix)
    invocation = build_invocation(runtime, source, installer=True, msi=source.suffix.casefold() == ".msi")
    environment.update(invocation.environment)
    completed = subprocess.run(list(invocation.command), env=environment, check=False)
    return before, completed


def publish_installed_apps(
    prefix_or_compat_id, product_hint, installer_path, before_inventory=None, *,
    runtime: RuntimeSpec | None = None,
):
    """Discover and publish apps while retaining the installer's exact compatdata."""
    from .pack import DEFAULT_STEAM, publish_existing_executable

    value = Path(str(prefix_or_compat_id)).expanduser()
    if runtime is not None:
        prefix = runtime.prefix
        compat = runtime.compat_path or prefix
        compat_id = compat.name if runtime.compat_path else prefix.name
    else:
        compat = value if value.is_dir() else DEFAULT_STEAM / "steamapps/compatdata" / str(prefix_or_compat_id)
        prefix = compat if compat.name == "pfx" else compat / "pfx"
        compat = prefix.parent
        compat_id = compat.name
    candidates = discover_installed_apps(prefix, product_hint, before_inventory)
    if not candidates:
        raise RuntimeError(f"No newly installed user-facing application was found in {prefix}; the prefix was preserved for inspection.")
    results = []
    for candidate in candidates:
        display = product_hint if len(candidates) == 1 else candidate.display_name
        results.append(publish_existing_executable(
            candidate.path, display, compat_id, compat, source=Path(installer_path), runtime=runtime,
            discovery_messages=[f"discovery={reason}" for reason in candidate.evidence] + [f"discovery_score={candidate.score}"],
        ))
    return results
