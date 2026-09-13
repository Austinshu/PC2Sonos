#!/bin/bash
# PC2Sonos uninstaller for macOS:  ./uninstall.sh [--purge]
# Stops the app, removes the LaunchAgent and firewall rule, restores the
# default output device, and deletes the virtualenv. Settings/log in
# ~/Library/Application Support/PC2Sonos are kept unless --purge is given.
# BlackHole itself is left installed: brew uninstall --cask blackhole-2ch
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.pc2sonos.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DATA_DIR="$HOME/Library/Application Support/PC2Sonos"
VENV="$HERE/venv"
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

say "Stopping PC2Sonos"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
pkill -f "$HERE/main.py" 2>/dev/null || true
rm -f "$PLIST"

if [[ -x "$VENV/bin/python" ]]; then
  say "Restoring the default output device"
  (cd "$HERE" && "$VENV/bin/python" - <<'PYEOF'
import json
from pathlib import Path
try:
    from macos_audio import restore_default_output
    cfg_path = Path.home() / "Library/Application Support/PC2Sonos/config.json"
    prev = json.loads(cfg_path.read_text()).get("previous_default_output", "") if cfg_path.exists() else ""
    print("   ", restore_default_output(prev))
except Exception as e:
    print("    could not restore default output:", e)
PYEOF
  )
  FW=/usr/libexec/ApplicationFirewall/socketfilterfw
  REAL_PY="$("$VENV/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
  if "$FW" --listapps 2>/dev/null | grep -q "$REAL_PY"; then
    say "Removing firewall rule (sudo)"
    sudo "$FW" --remove "$REAL_PY" >/dev/null || true
  fi
  say "Removing virtualenv"
  rm -rf "$VENV"
fi
if [[ "${1:-}" == "--purge" ]]; then
  say "Removing settings and logs"; rm -rf "$DATA_DIR"
else
  say "Settings and log kept in: $DATA_DIR  (pass --purge to delete)"
fi
say "Done. If your Mac is silent, pick your speakers in System Settings > Sound > Output."
