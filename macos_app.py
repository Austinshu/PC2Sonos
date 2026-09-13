"""macOS-only helpers (the counterpart of windows_audio.py /
windows_firewall.py for the things macOS does differently): native
dialogs via osascript, the Login Items entry that replaces the Windows
Startup shortcut when running as a bundled .app (build_macos_app.sh),
BlackHole install guidance, and the audio-input ("Microphone")
permission that reading from BlackHole requires.

Everything goes through osascript so there is no extra dependency, and
everything fails soft -- a dialog that can't be shown just logs."""

import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

BLACKHOLE_URL = "https://existential.audio/blackhole/"
BREW_CASK = "blackhole-2ch"


def is_bundled():
    return bool(getattr(sys, "frozen", False)) and sys.platform == "darwin"


def app_bundle_path():
    """/Applications/PC2Sonos.app (wherever it actually lives), or None
    when not running from a bundle."""
    if not is_bundled():
        return None
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def _osascript(script, timeout=120):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return (r.stdout or "").strip()


def _quote(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def ask(message, buttons, default=None, title="PC2Sonos"):
    """Show a dialog with up to three buttons; returns the button text
    pressed, or None if the dialog couldn't be shown / was cancelled."""
    if sys.platform != "darwin":
        print(f"[dialog] {title}: {message} {buttons}")
        return None
    default = default or buttons[-1]
    btns = "{" + ", ".join(_quote(b) for b in buttons) + "}"
    script = (f"display dialog {_quote(message)} with title {_quote(title)} "
              f"buttons {btns} default button {_quote(default)} with icon note")
    try:
        out = _osascript(script)
    except Exception as e:
        print(f"[dialog] couldn't show dialog: {e}")
        return None
    for b in buttons:
        if f"button returned:{b}" in out:
            return b
    return None


# --- Login Items -------------------------------------------------------------

def login_item_enabled():
    app = app_bundle_path()
    if app is None:
        return False
    try:
        out = _osascript('tell application "System Events" to get the path of every login item')
    except Exception:
        return False
    return str(app) in out


def set_login_item(enabled):
    """Add or remove the .app from the user's Login Items. Returns a
    status line for the log."""
    app = app_bundle_path()
    if app is None:
        return "not running from an .app bundle; install.sh's LaunchAgent handles login start"
    try:
        if enabled:
            if login_item_enabled():
                return "already in Login Items"
            _osascript('tell application "System Events" to make login item at end with properties '
                       f"{{path:{_quote(app)}, hidden:true}}")
            return "added to Login Items"
        _osascript('tell application "System Events" to delete (every login item whose path is '
                   f"{_quote(app)})")
        return "removed from Login Items"
    except Exception as e:
        return f"couldn't change Login Items: {e}"


# --- BlackHole ---------------------------------------------------------------

def offer_blackhole_install():
    """Called when the capture device is missing. Offers a Homebrew
    install in Terminal when brew is available, otherwise the download
    page. Returns what was chosen (for the log)."""
    brew = shutil.which("brew") or next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                                         if Path(p).exists()), None)
    msg = ("PC2Sonos needs the free BlackHole virtual audio device to capture your Mac's audio, "
           "and it isn't installed yet.\n\nInstall it, then PC2Sonos will start streaming "
           "automatically (no restart needed).")
    if brew:
        choice = ask(msg, ["Later", "Open download page", "Install with Homebrew"],
                     default="Install with Homebrew")
    else:
        choice = ask(msg, ["Later", "Open download page"], default="Open download page")
    if choice == "Install with Homebrew":
        cmd = f"{brew} install --cask {BREW_CASK}"
        try:
            _osascript(f'tell application "Terminal" to activate\n'
                       f'tell application "Terminal" to do script {_quote(cmd)}')
        except Exception as e:
            print(f"[blackhole] couldn't open Terminal: {e}")
            webbrowser.open(BLACKHOLE_URL)
    elif choice == "Open download page":
        webbrowser.open(BLACKHOLE_URL)
    return choice


# --- Microphone / audio-input permission --------------------------------------
# Reading from BlackHole is "microphone" access as far as macOS is
# concerned. Opening the CoreAudio input stream is supposed to trigger
# the permission prompt implicitly, but on a real macOS 26 machine it
# never did and the app silently captured zeros -- so ask explicitly
# through AVFoundation, which both shows the prompt and lists the app
# under Privacy & Security > Microphone.

MIC_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
_MIC_STATUS = {0: "not determined", 1: "restricted", 2: "denied", 3: "authorized"}


def microphone_status():
    """'authorized' | 'denied' | 'restricted' | 'not determined', or
    'unknown (<reason>)' when AVFoundation isn't available."""
    if sys.platform != "darwin":
        return "n/a (not macOS)"
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        code = int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
        return _MIC_STATUS.get(code, f"unknown ({code})")
    except Exception as e:
        return f"unknown ({type(e).__name__}: {e})"


def request_microphone_access(on_result=None):
    """Show the system prompt (only shown once per app by macOS). Calls
    on_result(granted: bool) from a background thread when the user
    answers. Returns False if the request couldn't be made."""
    if sys.platform != "darwin":
        return False
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
    except Exception as e:
        print(f"[audio] can't request microphone access ({type(e).__name__}: {e})")
        return False

    def handler(granted):
        print(f"[audio] microphone access {'granted' if granted else 'DENIED'} by the user")
        if on_result is not None:
            try:
                on_result(bool(granted))
            except Exception as e:
                print(f"[audio] microphone callback failed: {e}")

    try:
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, handler)
        return True
    except Exception as e:
        print(f"[audio] microphone access request failed: {type(e).__name__}: {e}")
        return False


def open_microphone_settings():
    try:
        subprocess.run(["open", MIC_SETTINGS_URL], capture_output=True, timeout=10)
    except Exception as e:
        print(f"[audio] couldn't open Microphone settings: {e}")
