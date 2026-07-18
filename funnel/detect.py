"""Detect Windows payloads: single .exe or folder of files."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

# Installer / junk names we never treat as the main game unless sole PE
_SKIP_NAMES = re.compile(
    r"(?i)^(unins\d*|uninstall|setup|install|vcredist.*|dxsetup|"
    r"directx|dotnet|redist|crashreport|unitycrashhandler.*|"
    r"helper|update|launcher_uninstall).*\.exe$"
)

# Prefer these names when multiple exes exist
_PREFER = re.compile(
    r"(?i)(game|start|main|app|client|play|\.exe$)"
)

_DEMOTE_MAIN = re.compile(
    r"(?i)(^|[._ -])(config(?:uration)?|settings?|options?|unins\d*|uninstall(?:er)?|"
    r"helper|setup|install(?:er)?)([._ -]|$)"
)

_UNINSTALLER_NAME = re.compile(r"(?i)^(unins\d*|uninstall|uninstaller|remove|uninstall).*\.exe$")
_INSTALLER_NAME = re.compile(r"(?i)^(setup|install|installer|bootstrap|autorun).*\.exe$")
_REDISTRIBUTABLE_NAME = re.compile(
    r"(?i)^(vcredist.*|dxsetup|directx.*|dotnet.*|redist.*|physx.*).*\.exe$"
)
_HELPER_NAME = re.compile(
    r"(?i)^(helper|update|updater|patcher|crashreport|crashhandler|unitycrashhandler.*).*\.exe$"
)

PAYLOAD_STATUSES = frozenset(
    {"ready", "ambiguous", "installer_only", "incomplete", "unsupported"}
)


@dataclass
class ExecutableCandidate:
    """Explainable record for one executable in a staged payload."""

    relative_path: str
    score: int
    category: str
    reasons: list[str]


Candidate = ExecutableCandidate


@dataclass
class InspectionResult:
    """Recoverable completeness and selection result."""

    source: Path
    root: Path | None
    input_kind: str
    status: str
    candidates: list[ExecutableCandidate]
    selected_exe: Path | None
    reasons: list[str]
    hints: list[str]
    recoverable: bool = True

    @property
    def selected_path(self) -> Path | None:
        """Alias useful to adapters that call the selection a path."""
        return self.selected_exe


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


def _looks_japanese(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", text))


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

    # Human name cleanup
    display_name = display.strip() or "Windows App"
    slug = "codex" if "msix-identity:OpenAI.Codex" in hints else _slugify(display_name)

    if _looks_japanese(str(path)) or _looks_japanese(display_name):
        hints.append("locale:ja_JP")

    # Engine fingerprints (cheap)
    try:
        siblings = {p.name.lower() for p in app_dir.iterdir()}
    except OSError:
        siblings = set()
    if "unityplayer.dll" in siblings or any("unity" in s for s in siblings):
        hints.append("engine:unity")
    if any(s.endswith(".xp3") or s == "data.xp3" for s in siblings):
        hints.append("engine:kirikiri")
        hints.append("locale:ja_JP")  # KiriKiri almost always JP VN
    if "game.exe" in siblings and "data.win" in siblings:
        hints.append("engine:gamemaker")

    # AliceSoft System40 / System4 (Rance, etc.)
    exe_l = exe.name.lower()
    if exe_l in {"system40.exe", "system4.exe"} or any(
        s.endswith(".ain") for s in siblings
    ):
        hints.append("engine:alicesoft")
    movie_dir = app_dir / "movie"
    if movie_dir.is_dir():
        try:
            for m in movie_dir.iterdir():
                if m.suffix.lower() == ".alm":
                    hints.append("media:alm-mpeg")
                    break
        except OSError:
            pass
    if (app_dir / "DLL" / "PlayMovie.dll").is_file():
        hints.append("media:playmovie-dshow")

    # Lilith / common JP adult VN product codes & titles (Atlas golden set)
    blob = f"{path} {display_name} {exe.name}".lower()
    if re.search(
        r"\b(lbk-|lpk-|asagi|hitozuma|netori|taima|taimanin|koikatu|koikatsu|"
        r"lilith|black lilith|ntr)\b",
        blob,
    ):
        hints.append("locale:ja_JP")
        hints.append("class:jp-vn")

    return Detected(
        kind=kind,
        path=path,
        exe=exe,
        app_dir=app_dir,
        display_name=display_name,
        slug=slug,
        hints=hints,
        fixed_args=("--disable-gpu", "--disable-gpu-compositing")
        if "msix-identity:OpenAI.Codex" in hints else (),
    )


def _iter_executables(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not (current_path / name).is_symlink()
        ]
        for name in filenames:
            path = current_path / name
            if path.is_symlink() or path.suffix.casefold() != ".exe":
                continue
            try:
                if path.is_file():
                    found.append(path)
            except OSError:
                continue
    return sorted(found, key=lambda path: path.relative_to(root).as_posix().casefold())


def _looks_like_pe(path: Path) -> bool:
    try:
        return path.read_bytes()[:2] == b"MZ"
    except OSError:
        return False


def _candidate_category(path: Path) -> str:
    name = path.name
    stem = path.stem.casefold()
    if _UNINSTALLER_NAME.match(name) or any(
        token in stem for token in ("uninstall", "unins", "remove")
    ):
        return "uninstaller"
    if _INSTALLER_NAME.match(name) or any(
        token in stem for token in ("setup", "installer", "install", "bootstrap")
    ):
        return "installer"
    if _REDISTRIBUTABLE_NAME.match(name) or any(
        token in stem for token in ("redist", "vcredist", "directx", "dotnet", "physx")
    ):
        return "redistributable"
    if _HELPER_NAME.match(name) or any(
        token in stem for token in ("helper", "crash", "update", "updater", "patcher")
    ):
        return "helper"
    if "launcher" in stem:
        return "launcher"
    if not _looks_like_pe(path):
        return "unknown"
    return "application"


def _support_signals(root: Path, candidate: Path) -> list[str]:
    signals: list[str] = []
    try:
        siblings = sorted(candidate.parent.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return signals

    for sibling in siblings:
        if sibling == candidate:
            continue
        if sibling.is_dir() and sibling.name.casefold() in {
            "assets",
            "data",
            "content",
            "resources",
            "movie",
        }:
            signals.append(f"supporting directory: {sibling.name}")
        elif sibling.is_file() and sibling.suffix.casefold() in {
            ".dll",
            ".dat",
            ".bin",
            ".xp3",
            ".pak",
            ".assets",
            ".json",
            ".ogg",
            ".mp3",
            ".wav",
            ".png",
            ".jpg",
        }:
            signals.append(f"supporting asset: {sibling.name}")
        if len(signals) >= 3:
            break

    if not signals and candidate.parent == root:
        try:
            if any(path.is_dir() for path in root.iterdir()):
                signals.append("supporting subdirectory present")
        except OSError:
            pass
    return signals


def _candidate_record(root: Path, path: Path, hints: list[str]) -> ExecutableCandidate:
    relative = path.relative_to(root).as_posix()
    category = _candidate_category(path)
    reasons: list[str] = []
    score = 0

    if category == "application":
        score = 100
        reasons.append("PE executable treated as an application candidate")
        low = path.name.casefold()
        if low in {"game.exe", "start.exe", "main.exe", "app.exe", "play.exe"}:
            score += 50
            reasons.append("name strongly suggests the main application")
        elif _PREFER.search(path.name):
            score += 10
            reasons.append("name contains an application signal")
        try:
            size_mb = path.stat().st_size // (1024 * 1024)
        except OSError:
            size_mb = 0
        if size_mb:
            score += min(size_mb, 200)
            reasons.append(f"binary size contributes {size_mb} dominance point(s)")
        if path.parent == root:
            score += 5
            reasons.append("executable is at the payload root")
        support = _support_signals(root, path)
        score += min(len(support), 3) * 20
        reasons.extend(support)
        if not support:
            reasons.append("no supporting payload assets or signals found")
        if hints:
            score += 5
            reasons.append("payload engine/locale signals are available")
    elif category == "unknown":
        score = -200
        reasons.append("file is not recognized as a PE executable")
        reasons.append("cannot safely classify it as a runnable Windows application")
    else:
        score = -100
        reasons.append(f"classified as {category}; it is not auto-selectable")
        reasons.append(f"human choice cannot make a {category} the main application")

    return ExecutableCandidate(
        relative_path=relative,
        score=score,
        category=category,
        reasons=reasons,
    )


def _inspection_hints(root: Path) -> list[str]:
    try:
        return list(detect_input(root).hints)
    except (OSError, ValueError):
        return []


def _relative_override(root: Path, selected_exe: str | Path) -> str | None:
    value = str(selected_exe).replace("\\", "/")
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        candidate = candidate.resolve()
    else:
        candidate = (root / candidate).resolve()
    try:
        return candidate.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _result(
    source: Path,
    root: Path | None,
    input_kind: str,
    status: str,
    candidates: list[ExecutableCandidate],
    selected: Path | None,
    reasons: list[str],
    hints: list[str],
) -> InspectionResult:
    if status not in PAYLOAD_STATUSES:
        raise ValueError(f"invalid inspection status: {status}")
    return InspectionResult(
        source=source,
        root=root,
        input_kind=input_kind,
        status=status,
        candidates=candidates,
        selected_exe=selected,
        reasons=reasons,
        hints=hints,
    )


def inspect_payload(
    raw: str | Path,
    *,
    selected_exe: str | Path | None = None,
) -> InspectionResult:
    """Inspect a staged payload without silently choosing an unsafe executable."""
    source = Path(raw).expanduser().absolute()
    if source.is_symlink():
        return _result(
            source,
            None,
            "unsupported",
            "unsupported",
            [],
            None,
            ["Symlink inputs are unsupported; stage a regular bounded payload first"],
            [],
        )
    source = source.resolve()
    if not source.exists():
        return _result(
            source,
            None,
            "unsupported",
            "unsupported",
            [],
            None,
            [f"Input does not exist: {source}"],
            [],
        )
    if source.is_file() and source.suffix.casefold() != ".exe":
        return _result(
            source,
            None,
            "unsupported",
            "unsupported",
            [],
            None,
            ["Inspection accepts a staged folder or .exe; archives must be staged first"],
            [],
        )
    if source.is_file() and source.suffix.casefold() == ".exe":
        root = source.parent
        input_kind = "exe"
    elif source.is_dir():
        root = source
        input_kind = "folder"
    else:
        return _result(
            source,
            None,
            "unsupported",
            "unsupported",
            [],
            None,
            ["Input is not a regular folder or executable"],
            [],
        )

    try:
        paths = _iter_executables(root)
    except OSError as exc:
        return _result(
            source,
            root,
            input_kind,
            "unsupported",
            [],
            None,
            [f"Could not inspect payload safely: {exc}"],
            [],
        )
    manifest, manifest_error = (
        _read_appx_manifest(root) if input_kind == "folder" else (None, None)
    )
    hints = _inspection_hints(root) if paths else []
    if manifest_error and manifest_error not in hints:
        hints.append(manifest_error)
    candidates = [_candidate_record(root, path, hints) for path in paths]
    by_relative = {candidate.relative_path.casefold(): candidate for candidate in candidates}
    app_candidates = [candidate for candidate in candidates if candidate.category == "application"]
    non_app = [candidate for candidate in candidates if candidate.category not in {"application", "unknown"}]

    if manifest is not None:
        relative = manifest.executable.relative_to(root.resolve()).as_posix()
        manifest_hints = (
            f"msix-identity:{manifest.identity}",
            f"msix-manifest-exe:{manifest.executable_hint}",
        )
        for hint in manifest_hints:
            if hint not in hints:
                hints.append(hint)
        return _result(
            source,
            root,
            input_kind,
            "ready",
            candidates,
            Path(relative),
            [f"Selected the AppxManifest.xml full-trust executable: {relative}"],
            hints,
        )

    if selected_exe is not None:
        relative = _relative_override(root, selected_exe)
        if relative is None:
            return _result(
                source,
                root,
                input_kind,
                "ambiguous" if app_candidates else "incomplete",
                candidates,
                None,
                [f"Explicit executable override is outside the payload: {selected_exe}"],
                hints,
            )
        candidate = by_relative.get(relative.casefold())
        if candidate is None:
            return _result(
                source,
                root,
                input_kind,
                "ambiguous" if app_candidates else "incomplete",
                candidates,
                None,
                [f"Explicit executable override is not a candidate: {relative}"],
                hints,
            )
        if candidate.category != "application":
            return _result(
                source,
                root,
                input_kind,
                "ambiguous",
                candidates,
                None,
                [
                    f"Explicit override {relative} is classified as {candidate.category} and cannot be the main application",
                ],
                hints,
            )
        path = root / relative
        support = _support_signals(root, path)
        reasons = [f"Explicit executable override selected: {relative}"]
        if not support:
            reasons.append(
                "No supporting payload assets or signals detected; verify completeness before launch"
            )
        return _result(
            source,
            root,
            input_kind,
            "ready",
            candidates,
            Path(relative),
            reasons,
            hints,
        )

    if not app_candidates:
        if non_app:
            names = ", ".join(candidate.relative_path for candidate in non_app)
            return _result(
                source,
                root,
                input_kind,
                "installer_only",
                candidates,
                None,
                [f"No runnable application candidate; only installer/helper executables found: {names}"],
                hints,
            )
        if any(candidate.category == "unknown" for candidate in candidates):
            return _result(
                source,
                root,
                input_kind,
                "unsupported",
                candidates,
                None,
                ["Executable candidates are not recognized PE applications"],
                hints,
            )
        return _result(
            source,
            root,
            input_kind,
            "incomplete",
            candidates,
            None,
            ["Payload has no executable candidate to launch"],
            hints,
        )

    ranked = sorted(app_candidates, key=lambda candidate: (-candidate.score, candidate.relative_path.casefold()))
    top = ranked[0]
    top_path = root / top.relative_path
    top_support = _support_signals(root, top_path)
    if len(ranked) == 1:
        if not top_support:
            return _result(
                source,
                root,
                input_kind,
                "incomplete",
                candidates,
                None,
                [f"Only application candidate {top.relative_path} has no supporting payload assets or signals"],
                hints,
            )
        return _result(
            source,
            root,
            input_kind,
            "ready",
            candidates,
            Path(top.relative_path),
            [f"Selected the only application candidate with supporting payload signals: {top.relative_path}"],
            hints,
        )

    runner_up = ranked[1]
    gap = top.score - runner_up.score
    if gap >= 25 and top_support:
        return _result(
            source,
            root,
            input_kind,
            "ready",
            candidates,
            Path(top.relative_path),
            [
                f"Selected {top.relative_path}: score {top.score} is clearly dominant over {runner_up.relative_path} ({runner_up.score})",
                "Supporting payload assets or signals were found",
            ],
            hints,
        )

    names = ", ".join(candidate.relative_path for candidate in ranked)
    return _result(
        source,
        root,
        input_kind,
        "ambiguous",
        candidates,
        None,
        [
            f"Multiple plausible application executables require a human choice: {names}",
            f"Top score gap is only {gap}; Funnel will not silently guess",
        ],
        hints,
    )
