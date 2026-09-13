#!/bin/bash
# Builds a self-contained PC2Sonos.app and a drag-to-Applications
# PC2Sonos-macOS-<arch>.dmg with PyInstaller -- the macOS counterpart of
# build_installer.ps1. Runs on a Mac (locally, or in the GitHub Actions
# workflow .github/workflows/macos.yml).
#
#   ./build_macos_app.sh                 # uses ./venv/bin/python (from install.sh)
#   PYTHON=python3 ./build_macos_app.sh  # or any interpreter with the deps installed
#
# The .app gets an ad-hoc signature only (no Apple Developer ID), so the
# first launch needs System Settings > Privacy & Security > Open Anyway.
# BlackHole is not bundled; the app offers to install it on first run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
PYTHON="${PYTHON:-$HERE/venv/bin/python}"
VERSION="$("$PYTHON" -c 'from version import VERSION; print(VERSION)')"

"$PYTHON" -m pip install --quiet pyinstaller

rm -rf build dist
"$PYTHON" -m PyInstaller --noconfirm --clean --windowed --name PC2Sonos \
  --osx-bundle-identifier com.pc2sonos.app \
  --collect-all sounddevice \
  --collect-submodules soco \
  --hidden-import pystray._darwin --hidden-import AVFoundation \
  main.py

PLIST=dist/PC2Sonos.app/Contents/Info.plist
plist_set() { /usr/bin/plutil -replace "$1" "-$2" "$3" "$PLIST"; }   # plutil handles quoting
plist_set LSUIElement bool true   # menu-bar-only app: no Dock icon
plist_set CFBundleShortVersionString string "$VERSION"
plist_set CFBundleVersion string "$VERSION"
plist_set LSMinimumSystemVersion string 12.0
# macOS treats reading from BlackHole as microphone access. Without this
# key macOS silently denies audio input, and terminates the app when it
# asks for permission. Keep the wording free of quotes.
plist_set NSMicrophoneUsageDescription string "PC2Sonos captures the audio your Mac plays through the BlackHole virtual device, which macOS treats as a microphone. The optional test-tone calibration also listens through your real microphone."
plist_set NSAppleEventsUsageDescription string "Used to add PC2Sonos to your Login Items and to open Terminal for the BlackHole installer."
for key in NSMicrophoneUsageDescription NSAppleEventsUsageDescription LSUIElement CFBundleVersion; do
  /usr/bin/plutil -extract "$key" raw -o - "$PLIST" >/dev/null || { echo "Info.plist is missing $key"; exit 1; }
done

codesign --force --deep --sign - dist/PC2Sonos.app

# Smoke test: the frozen binary must import everything and report its version.
test "$(dist/PC2Sonos.app/Contents/MacOS/PC2Sonos --version)" = "PC2Sonos $VERSION"

ARCH="$(uname -m)"   # arm64 (Apple Silicon) or x86_64 (Intel); one dmg per architecture
DMG="dist/PC2Sonos-macOS-$ARCH.dmg"
STAGE="$(mktemp -d)"
cp -R dist/PC2Sonos.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "PC2Sonos $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
echo "Built dist/PC2Sonos.app and $DMG (version $VERSION)"
