# Funnel

**One Windows payload in → one trusted Linux desktop application out.**

Drop a Windows folder, `.zip`, `.rar`, `.7z`, portable `.exe`, installer, or
full-trust `.msix` onto Funnel. The result appears in the Linux application menu
and on the XDG Desktop. Funnel does not move or rewrite the original payload.

Steam is **not required**. Funnel prefers UMU, falls back to system Wine, and
retains Steam Proton only as an optional legacy fallback.

## Open-source foundation

Funnel's compatibility path is built around three open-source projects:

1. [UMU Launcher](https://github.com/Open-Wine-Components/umu-launcher) — the
   preferred Steam-independent launcher and runtime manager;
2. [Wine](https://www.winehq.org/) — the direct fallback and compatibility
   foundation;
3. [Proton](https://github.com/ValveSoftware/Proton) — the Wine-based runtime
   selected through UMU, with an existing Steam installation supported only as
   an optional legacy path.

Funnel does not vendor these projects. It discovers and invokes user-installed
executables, keeping their installation, updates, and licenses separate. See
[`THIRD_PARTY.md`](THIRD_PARTY.md) for upstream links and license details.

## Quick start

```bash
./install.sh --check       # read-only dependency and destination check
./install.sh               # user-owned XDG install; never invokes sudo
funnel doctor              # read-only readiness report
funnel                     # drag-and-drop Add window
funnel -v "/path/to/App.zip"
funnel -v "/path/to/folder" --name "Pretty Name"
funnel --list
```

The GUI remains Add-only: the desktop environment's application menu is the
library.

## Runtime selection

Automatic selection is deliberately small:

1. an executable `umu-run`;
2. an executable `wine` or `wine64`;
3. Steam Proton Experimental as a legacy fallback, if already present.

UMU runs Proton without a Steam client or account. With the default
`PROTONPATH=UMU-Proton`, UMU may download its managed Proton and container runtime on
the first launch; that requires network access and can take time. Funnel itself
does not hide this download or report the application ready before UMU finishes.
See the UMU project documentation for its runtime/cache behavior.

System Wine uses the same Funnel-owned per-application prefix layout. Legacy
Steam state containing `proton`, `compat_id`, and `compat_path` remains readable.

Explicit selection and validation:

```bash
FUNNEL_RUNTIME=umu funnel "/path/to/App.exe"
FUNNEL_RUNTIME=wine funnel "/path/to/App.exe"
FUNNEL_RUNTIME=steam funnel "/path/to/App.exe"
FUNNEL_RUNTIME=umu FUNNEL_RUNTIME_EXECUTABLE=/path/to/umu-run funnel "/path/to/App.exe"
```

`FUNNEL_RUNTIME_EXECUTABLE` (or `FUNNEL_RUNTIME_PATH`) overrides the executable.
`FUNNEL_PROTON` and `FUNNEL_STEAM` remain legacy Steam overrides. UMU defaults to
`GAMEID=umu-default`, `STORE=none`, and `PROTONPATH=UMU-Proton`; an explicit
`PROTONPATH` is honored.

UMU and Wine prefixes live at:

```text
~/.local/share/funnel/prefixes/<stable-id>/
```

Installer execution and installed-application discovery use that exact same
runtime and prefix. If discovery cannot identify a real application, Funnel
reports failure and preserves the prefix for inspection/recovery.

## Dependencies

Required for the GUI and common payloads:

- Python 3 with GTK3/PyGObject bindings
- `7z` (`7zz` is also detected) and `unrar` for archive coverage
- FreeDesktop tools such as `desktop-file-validate`, `update-desktop-database`,
  and `gio`
- either UMU (preferred) or system Wine; Steam Proton is optional
- optional `icotool`, `wrestool`, or ImageMagick for richer icon extraction

`install.sh` prints these commands but never runs them:

```bash
# Fedora
sudo dnf install 7zip unrar wine desktop-file-utils python3-gobject gtk3

# Ubuntu / Debian
sudo apt install p7zip-full unrar wine desktop-file-utils python3-gi gir1.2-gtk-3.0

# Arch Linux
sudo pacman -S 7zip unrar wine desktop-file-utils python-gobject gtk3
```

UMU packaging differs by distribution; follow the upstream UMU installation
instructions if it is not provided by the distribution. These are dependency
paths, not a claim that Funnel has passed clean-install acceptance on each
distribution. Run `funnel doctor` for an actionable, read-only local report.

## What conversion does

1. List archive members before extraction, reject absolute/drive/traversal paths,
   extract into Funnel's collision-safe cache, then reject links, hard links,
   special files, and paths escaping the extraction root. Folder/EXE inputs remain
   in place.
2. Detect the main PE and bounded engine/application hints.
3. Select and validate one runtime.
4. Apply locale or DLL environment overrides for known payload classes.
5. Extract an icon when tooling permits.
6. Write an argv-safe launcher, application-menu entry, XDG Desktop entry, and
   compatible JSON state.

AliceSoft/System40 and `.alm` fingerprints are detected, but conversion never
rewrites the original or extracted payload.

## User-owned layout

```text
~/.funnel/
  state/                    per-app JSON and readable logs
  icons/
  cache/games/              collision-safe archive extractions
  logs/

~/.local/share/funnel/prefixes/<stable-id>/
~/.local/share/applications/funnel-*.desktop
$(xdg-user-dir DESKTOP)/funnel-*.desktop
~/.local/bin/funnel-<slug>
```

The installer places Funnel's own application code under
`${XDG_DATA_HOME:-~/.local/share}/funnel/app`, the `funnel` command under
`${XDG_BIN_HOME:-~/.local/bin}`, and its Add-window desktop entry under
`${XDG_DATA_HOME:-~/.local/share}/applications`.

## Debug and recovery

```bash
FUNNEL_DEBUG=1 ~/.local/bin/funnel-<slug>
funnel --finish-install <state-slug> --product "Product name"
```

Debug logs go to `~/.funnel/logs/`. Finish-install reads the runtime and prefix
from new state records; a legacy numeric Steam compat ID remains supported.

## Release verification

Funnel's release gate is manual use of real payloads: open the GTK window, drop
the actual archive/folder/installer/MSIX, click the generated Desktop entry, and
use the resulting application. Synthetic payloads, fake runtimes, mocks, and test
counts are not accepted as product proof.

Read-only/static diagnostics remain useful for catching local mistakes:

```bash
./install.sh --check
PYTHONPATH=. python3 -m funnel.cli doctor
python3 -m compileall -q funnel funnel_gui.py
bash -n install.sh bin/funnel
desktop-file-validate funnel-app.desktop
```

Historical V2 contract tests remain in the private development repository as an
honest record of a discarded architecture; they are not shipped as public
release evidence. Funnel intentionally has no database, daemon, store, recipe
service, or cloud component.

## Built with Codex and GPT-5.6

Funnel was created during OpenAI Build Week. The primary Codex thread used
GPT-5.6 Sol and GPT-5.6 Luna in an isolated git worktree; its session ID is
`019f72a8-9110-7683-87bb-7a6b1c8d594b`.

Codex accelerated the implementation of the safe staging boundary, GTK
one-drop interface, executable selection, installer recovery, full-trust MSIX
manifest support, and generated Linux launchers. A later GPT-5.6 polish lane
migrated the runtime from mandatory Steam Proton to UMU-Proton with system Wine
fallback, added archive hardening, the read-only doctor, user-owned installer,
and branded icon assets.

The human product decisions remained explicit: Funnel had to produce a normal
Desktop/application-menu app rather than another manager or output folder; keep
original inputs untouched; use silent extraction; avoid an LLM or network API
in the conversion path; and treat real manual use as the release gate. Codex
proposed and implemented bounded changes inside those constraints. Ethan tested
the actual ZIP, RAR5, installer, and MSIX paths and rejected simulated results
as product proof. The dated commit history documents the Build Week work from
initial product lock through the accepted Steam-independent release.
