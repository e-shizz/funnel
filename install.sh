#!/usr/bin/env bash
# Install Funnel into user-owned XDG locations. This script never invokes sudo
# and never changes package repositories.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MODE=${1:-install}
if [[ "$MODE" != "install" && "$MODE" != "--check" && "$MODE" != "--dry-run" ]]; then
  echo "usage: ./install.sh [--check|--dry-run]" >&2
  exit 2
fi

DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
BIN_HOME=${XDG_BIN_HOME:-"$HOME/.local/bin"}
APP_ROOT="$DATA_HOME/funnel/app"
DESKTOP_ROOT="$DATA_HOME/applications"
ICON_ROOT="$DATA_HOME/icons/hicolor"

DISTRO=unknown
if [[ -r /etc/os-release ]]; then
  # Reading distribution identifiers is safe; no repository is changed.
  DISTRO=$(sed -n 's/^ID=//p' /etc/os-release | head -n 1 | tr -d '"')
fi

echo "Funnel dependency guidance ($DISTRO):"
case "$DISTRO" in
  fedora) echo "  Fedora: sudo dnf install 7zip unrar wine desktop-file-utils python3-gobject gtk3" ;;
  ubuntu|debian) echo "  Ubuntu/Debian: sudo apt install p7zip-full unrar wine desktop-file-utils python3-gi gir1.2-gtk-3.0" ;;
  arch) echo "  Arch: sudo pacman -S 7zip unrar wine desktop-file-utils python-gobject gtk3" ;;
  *) echo "  Install Python 3 + GTK3 bindings, 7z, unrar, desktop-file-utils, and either umu-run or Wine." ;;
esac
echo "This script does not run those commands. Steam is optional and not required."
echo "Would install application files to: $APP_ROOT"
echo "Would install command to: $BIN_HOME/funnel"
echo "Would install desktop entry to: $DESKTOP_ROOT/funnel.desktop"
echo "Would install Funnel icons under: $ICON_ROOT"

missing=0
if command -v python3 >/dev/null 2>&1; then
  # Keep dependency probing read-only even when GTK/fontconfig wants to create caches.
  if ! XDG_CACHE_HOME=/dev/null PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m funnel.cli doctor; then
    missing=1
  fi
else
  echo "Missing dependency: python3" >&2
  missing=1
fi

check_writable_destination() {
  local requested=$1
  local ancestor=$requested
  local parent
  while [[ ! -e "$ancestor" ]]; do
    parent=$(dirname -- "$ancestor")
    [[ "$parent" == "$ancestor" ]] && break
    ancestor=$parent
  done
  if [[ ! -d "$ancestor" || ! -w "$ancestor" || ! -x "$ancestor" ]]; then
    echo "Install destination is not writable: $requested (nearest existing parent: $ancestor)" >&2
    return 1
  fi
}

for destination in "$APP_ROOT" "$BIN_HOME" "$DESKTOP_ROOT" "$ICON_ROOT"; do
  if ! check_writable_destination "$destination"; then
    missing=1
  fi
done

if [[ "$MODE" == "--check" || "$MODE" == "--dry-run" ]]; then
  exit "$missing"
fi

mkdir -p "$APP_ROOT/funnel" "$BIN_HOME" "$DESKTOP_ROOT" \
  "$ICON_ROOT/scalable/apps" "$ICON_ROOT/48x48/apps" "$ICON_ROOT/128x128/apps" "$ICON_ROOT/256x256/apps"
for module in "$ROOT"/funnel/*.py; do
  install -m 0644 "$module" "$APP_ROOT/funnel/"
done
install -m 0755 "$ROOT/funnel_gui.py" "$APP_ROOT/funnel_gui.py"
install -m 0644 "$ROOT/assets/funnel.svg" "$ICON_ROOT/scalable/apps/funnel.svg"
install -m 0644 "$ROOT/assets/funnel-48.png" "$ICON_ROOT/48x48/apps/funnel.png"
install -m 0644 "$ROOT/assets/funnel-128.png" "$ICON_ROOT/128x128/apps/funnel.png"
install -m 0644 "$ROOT/assets/funnel-256.png" "$ICON_ROOT/256x256/apps/funnel.png"

{
  echo '#!/usr/bin/env bash'
  echo 'set -euo pipefail'
  # Generated wrapper expands its own PYTHONPATH at runtime.
  # shellcheck disable=SC2016
  printf 'export PYTHONPATH=%q${PYTHONPATH:+:$PYTHONPATH}\n' "$APP_ROOT"
  echo 'exec /usr/bin/env python3 -m funnel.cli "$@"'
} >"$BIN_HOME/funnel"
chmod 0755 "$BIN_HOME/funnel"

{
  echo '[Desktop Entry]'
  echo 'Version=1.0'
  echo 'Type=Application'
  echo 'Name=Funnel'
  echo 'Comment=Turn one Windows payload into a Linux desktop app'
  desktop_exec=${BIN_HOME//\\/\\\\}/funnel
  desktop_exec=${desktop_exec//\"/\\\"}
  printf 'Exec="%s" --ui\n' "$desktop_exec"
  echo 'Terminal=false'
  echo 'Icon=funnel'
  echo 'Categories=Utility;'
  echo 'StartupNotify=true'
  echo 'Keywords=wine;umu;proton;windows;exe;'
} >"$DESKTOP_ROOT/funnel.desktop"
chmod 0755 "$DESKTOP_ROOT/funnel.desktop"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$ICON_ROOT" >/dev/null 2>&1 || true
fi

echo "Installed. Run '$BIN_HOME/funnel doctor' to inspect readiness."
