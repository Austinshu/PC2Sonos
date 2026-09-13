#!/bin/bash
# PC2Sonos installer for macOS (run from this folder):  ./install.sh
#
# The macOS counterpart of install.ps1. It:
# 1. Installs BlackHole 2ch (the virtual audio device) via Homebrew if missing.
# 2. Creates a Python virtualenv here and installs the dependencies.
# 3. Adds a macOS application-firewall allow rule for that interpreter
#    (only if the firewall is on; asks for your password via sudo).
# 4. Runs the app once in the foreground so macOS can show its Microphone
#    (audio capture) permission prompt -- launchd-started processes never get one.
# 5. Installs a LaunchAgent so PC2Sonos starts at login, and starts it now.
#
# Re-running is safe: every step is idempotent. To build a standalone
# PC2Sonos.app instead, see build_macos_app.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.pc2sonos.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DATA_DIR="$HOME/Library/Application Support/PC2Sonos"
VENV="$HERE/venv"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer is for macOS; on Windows use install.ps1." >&2; exit 1
fi

# --- 1. BlackHole --------------------------------------------------------------
if system_profiler SPAudioDataType 2>/dev/null | grep -qi "BlackHole"; then
  say "BlackHole is already installed"
else
  if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew not found. Install it from https://brew.sh, or install BlackHole"
    warn "manually from https://existential.audio/blackhole/ and re-run this script."
    exit 1
  fi
  say "Installing BlackHole 2ch (Homebrew will ask for your password)"
  brew install --cask blackhole-2ch
fi

# --- 2. Python environment -----------------------------------------------------
PY="$(command -v python3.12 || command -v python3.11 || command -v python3 || true)"
if [[ -z "$PY" ]]; then
  warn "python3 not found. Install it with: brew install python@3.12"; exit 1
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in 3.1[0-9]|3.[2-9]*) ;; *) warn "Python 3.10+ required (found $PYVER)"; exit 1;; esac

if [[ ! -x "$VENV/bin/python" ]]; then
  say "Creating virtualenv with $PY ($PYVER)"
  "$PY" -m venv "$VENV"
fi
say "Installing Python dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
if ! "$VENV/bin/python" -c 'import sounddevice' 2>/dev/null; then
  say "Installing PortAudio via Homebrew (sounddevice needs it)"
  brew install portaudio
fi

mkdir -p "$DATA_DIR"

# --- 3. Firewall ----------------------------------------------------------------
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
REAL_PY="$("$VENV/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
if "$FW" --getglobalstate 2>/dev/null | grep -qi "enabled"; then
  say "Application firewall is on: allowing incoming connections for $REAL_PY (sudo)"
  sudo "$FW" --add "$REAL_PY" >/dev/null || warn "couldn't add firewall rule"
  sudo "$FW" --unblockapp "$REAL_PY" >/dev/null || warn "couldn't unblock in firewall"
else
  say "Application firewall is off; no rule needed"
fi

# --- 4. First foreground run (privacy prompt) ----------------------------------
say "Starting PC2Sonos once in the foreground (grant the Microphone prompt if macOS shows one)"
( cd "$HERE" && "$VENV/bin/python" main.py >/dev/null 2>&1 & FG_PID=$!; sleep 10; kill "$FG_PID" 2>/dev/null; wait "$FG_PID" 2>/dev/null ) || true

# --- 5. LaunchAgent -------------------------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$HERE/main.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$DATA_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$DATA_DIR/launchd.err.log</string>
</dict>
</plist>
PLISTEOF

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
if launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null; then
  say "LaunchAgent installed and started ($LABEL)"
else
  launchctl load -w "$PLIST"
  say "LaunchAgent installed and started via launchctl load ($LABEL)"
fi

cat <<MSG

PC2Sonos is running and will start automatically at every login.

  Dashboard:  http://127.0.0.1:5757   (opens automatically on first run)
  Menu bar:   look for the PC2Sonos icon
  Log:        $DATA_DIR/pc2sonos.log
  Settings:   $DATA_DIR/config.json

PC2Sonos switches your default output to "BlackHole 2ch" while it runs
and restores your previous output when you quit it from the menu bar.
MSG
