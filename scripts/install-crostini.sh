#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$HOME/.local/opt/chromeos-vm-studio"
DESKTOP_DIR="$HOME/.local/share/applications"
BIN_DIR="$HOME/.local/bin"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer targets Debian-based Crostini containers." >&2
  exit 1
fi

echo "Installing Bochs and native desktop dependencies..."
sudo apt-get update
sudo apt-get install -y bochs bochs-sdl bochsbios vgabios python3-tk python3-venv python3-setuptools

mkdir -p "$APP_DIR" "$DESKTOP_DIR" "$BIN_DIR"
python3 -m venv --system-site-packages "$APP_DIR/venv"
# Force reinstall even when the project version is unchanged; source fixes must
# replace stale site-packages code when the user reruns this installer.
"$APP_DIR/venv/bin/python" -m pip install --no-deps --no-build-isolation --upgrade --force-reinstall "$PROJECT_DIR"
ln -sf "$APP_DIR/venv/bin/chromeos-vm" "$BIN_DIR/chromeos-vm"
ln -sf "$APP_DIR/venv/bin/chromeos-vm-update" "$BIN_DIR/chromeos-vm-update"

# Record the source revision so the built-in updater knows which commit is installed.
COMMIT="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || true)"
printf '{"repository":"ashrworkman97-debug/ChromeOS_Emulator","branch":"main","commit":"%s"}\n' "$COMMIT" > "$APP_DIR/install.json"

cat > "$DESKTOP_DIR/chromeos-vm-studio.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=ChromeOS VM Studio
Comment=Create and run x86 PC emulators on ChromeOS
Exec=$APP_DIR/venv/bin/chromeos-vm-studio
Icon=computer
Terminal=false
Categories=System;Emulator;
StartupNotify=true
EOF

chmod 644 "$DESKTOP_DIR/chromeos-vm-studio.desktop"
echo
echo "Installed. Open ChromeOS Launcher > Linux apps > ChromeOS VM Studio."
echo "The app uses Bochs for full-PC software emulation and does not use QEMU."
echo "The app checks GitHub for updates automatically when launched (maximum once every 12 hours)."
