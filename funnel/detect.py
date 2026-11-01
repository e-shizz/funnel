"""Detect Windows payloads: single .exe or folder of files."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .recipes import apply_recipes

# Installer / junk names we never treat as the main game unless sole PE
_SKIP_NAMES = re.compile(
    r"(?i)^(unins\d*|uninstall|setup|install|vcredist.*|dxsetup|"
    r"directx|dotnet|redist|crashreport|unitycrashhandler.*|"
    r"helper|update|launcher_uninstall).*\.exe$"
)

_DEMOTE_MAIN = re.compile(
    r"(?i)(^|[._ -])(config(?:uration)?|settings?|options?|unins\d*|uninstall(?:er)?|"
    r"helper|setup|install(?:er)?)([._ -]|$)"
)


@dataclass
class Detected:
    kind: str  # "exe" | "folder"
    path: Path  # original input
    exe: Path  # main executable
    app_dir: Path  # working directory for the app
    display_name: str
    slug: str
    hints: list[str]
    fixed_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)  # from recipes
    recipe: str = "default"  # names of matched recipes, joined with '+'
    category: str | None = None  # menu category chosen by a recipe
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _AppxManifest:
    identity: str
    executable: Path
    executable_hint: str
    display_name: str


def _slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s[:64] or "windows-app"


def _local_name(tag: str) -> str:
    """Return an XML local name without depending on a manifest prefix."""
    return tag.rsplit("}", 1)[-1]


def _literal_display_name(value: str | None) -> str | None:
    name = (value or "").strip()
    if not name or name.casefold().startswith("ms-resource:"):
        return None
    return name


def _read_appx_manifest(root: Path) -> tuple[_AppxManifest | None, str | None]:
    """Read a root AppxManifest.xml and safely resolve its declared executable."""
    manifest_path = root / "AppxManifest.xml"
    if not manifest_path.is_file():
        return None, None
    try:
        package = ET.parse(manifest_path).getroot()
        identity_element = next(
            (child for child in package if _local_name(child.tag) == "Identity"), None
        )
        identity = (identity_element.get("Name") if identity_element is not None else "") or ""
        identity = identity.strip()

        properties_display: str | None = None
        applications = None
        for child in package:
            local = _local_name(child.tag)
            if local == "Properties":
                display_element = next(
                    (item for item in child if _local_name(item.tag) == "DisplayName"), None
                )
                if display_element is not None:
                    properties_display = _literal_display_name(display_element.text)
            elif local == "Applications":
                applications = child

        application = None
        if applications is not None:
            application = next(
                (
                    child for child in applications
                    if _local_name(child.tag) == "Application" and child.get("Executable")
                ),
                None,
            )
        declared = (application.get("Executable") if application is not None else "") or ""
        declared = declared.strip().replace("\\", "/")
        if not identity:
            raise ValueError("Identity Name is missing")
        if not declared:
            raise ValueError("Application Executable is missing")

        relative = Path(declared)
        if relative.is_absolute() or re.match(r"^[A-Za-z]:", declared) or ".." in relative.parts:
            raise ValueError(f"manifest executable escapes the package: {declared}")
        package_root = root.resolve()
        executable = (package_root / relative).resolve()
        try:
            executable.relative_to(package_root)
        except ValueError as exc:
            raise ValueError(f"manifest executable escapes the package: {declared}") from exc
        if not executable.is_file():
            raise ValueError(f"manifest executable is missing or not a file: {declared}")

        visual_display: str | None = None
        if application is not None:
            visual = next(
                (item for item in application.iter() if _local_name(item.tag) == "VisualElements"),
                None,
            )
            if visual is not None:
                visual_display = _literal_display_name(visual.get("DisplayName"))
        display = visual_display or properties_display or identity.rsplit(".", 1)[-1] or executable.stem
        if identity == "OpenAI.Codex":
            display = "Codex"
        return _AppxManifest(identity, executable, declared, display), None
    except (ET.ParseError, OSError, ValueError) as exc:
        return None, f"msix-manifest-error:{exc}"


def _name_parts(value: str) -> list[str]:
    return re.findall(r"[a-z]+|\d+", value.casefold())


def _score_exe(p: Path, product_name: str = "") -> int:
    name = p.name
    if _SKIP_NAMES.match(name) or _DEMOTE_MAIN.search(p.stem):
        return -1000
    score = 0
    # Retain useful size differences below one MiB without letting size dominate.
    try:
        score += min(p.stat().st_size // (256 * 1024), 800)
    except OSError:
        pass
    low = name.casefold()
    if low in ("game.exe", "start.exe", "main.exe", "app.exe"):
        score += 50
    if "launcher" in low:
        score -= 10
    if low.startswith("unitycrash"):
        score -= 100
    stem = "".join(_name_parts(p.stem))
    product_parts = [part for part in _name_parts(product_name) if not part.isdigit() and len(part) > 1]
    product = "".join(product_parts)
    if stem and product_parts:
        if stem in product_parts or (len(stem) >= 4 and stem in product):
            score += 100
        acronym = "".join(part[0] for part in product_parts)
        if len(stem) >= 2 and acronym.startswith(stem):
            score += 80
    return score


def find_main_exe(folder: Path) -> Path | None:
    exes = [
        p
        for p in folder.rglob("*")
        if p.is_file() and not p.is_symlink() and p.suffix.casefold() == ".exe"
    ]
    # Prefer top-level first
    top = [p for p in exes if p.parent == folder]
    pool = top if top else exes
    if not pool:
        return None
    pool = [p for p in pool if _score_exe(p, folder.name) > -500] or pool
    pool.sort(key=lambda path: (-_score_exe(path, folder.name), path.name.casefold()))
    return pool[0]


def detect_input(raw: str | Path) -> Detected:
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Not found: {path}")

    hints: list[str] = []

    if path.is_file():
        if path.suffix.lower() != ".exe":
            # allow non-exe only if it's clearly wrong — still try
            hints.append(f"not-an-exe:{path.suffix}")
        exe = path
        app_dir = path.parent
        kind = "exe"
        display = path.stem
    else:
        kind = "folder"
        manifest, manifest_error = _read_appx_manifest(path)
        if manifest is not None:
            exe = manifest.executable
            display = manifest.display_name
            hints.extend((
                f"msix-identity:{manifest.identity}",
                f"msix-manifest-exe:{manifest.executable_hint}",
            ))
        else:
            exe = find_main_exe(path)
            display = path.name
            if manifest_error:
                hints.append(manifest_error)
        if exe is None:
            raise ValueError(f"No .exe found in {path}")
        app_dir = exe.parent
        if manifest is None:
            hints.append(f"picked-exe:{exe.name}")

    display_name = display.strip() or "Windows App"
    applied = apply_recipes(
        app_dir=app_dir, exe=exe, text=f"{path} {display_name} {exe.name}", hints=hints,
    )

    return Detected(
        kind=kind,
        path=path,
        exe=exe,
        app_dir=app_dir,
        display_name=display_name,
        slug=_slugify(display_name),
        hints=applied.hints,
        fixed_args=tuple(applied.args),
        env=applied.env,
        recipe=applied.label,
        category=applied.category,
        notes=applied.notes,
    )
