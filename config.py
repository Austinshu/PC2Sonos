import json
import os
import shutil
import threading
from pathlib import Path

# Documents rather than AppData: AppData is hidden by default in Explorer
# and plenty of people who want to look at the log/config by hand (or find
# the app to move/back it up) never find it there. Documents is always
# visible, per-user, and needs no elevation to write to.
APP_DIR = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / "PC2Sonos"
CONFIG_PATH = APP_DIR / "config.json"

_OLD_APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PC2Sonos"


def _migrate_from_appdata():
    """One-time migration for anyone who installed a version of PC2Sonos
    that lived in %LOCALAPPDATA%\\PC2Sonos (every build before this one).
    Without this, upgrading would silently orphan their old config --
    their tuned delay, which speakers they'd enabled/disabled, and per-
    speaker volumes -- and start them over from scratch in the new
    Documents location, with the old folder just left behind forever."""
    old_config = _OLD_APP_DIR / "config.json"
    if CONFIG_PATH.exists() or not old_config.exists():
        return
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_config, CONFIG_PATH)
    except Exception:
        pass


APP_DIR.mkdir(parents=True, exist_ok=True)
_migrate_from_appdata()

DEFAULT_CONFIG = {
    # Substring match against Windows device names. VB-Audio Virtual
    # Cable's recording endpoint is normally named "CABLE Output".
    "capture_device_substr": "CABLE Output",
    # Blank = auto-pick the first real (non-virtual) WASAPI output device.
    "render_device_substr": "",
    # How long (ms) to hold back the LOCAL speaker output so it lines up
    # with the Sonos speakers. This is the number the dashboard slider
    # controls. Start around 1500ms and tune by ear.
    "local_delay_ms": 1500,
    "sample_rate": 44100,
    "channels": 2,
    "sample_width": 2,  # bytes (16-bit PCM)
    "http_port": 5757,
    "speakers": {},  # uid -> {"enabled": bool, "volume": int}
    # Donation nag state -- see webapp.py's /api/donate/* routes. Once
    # donated is True, the weekly popup stops forever; last_donate_prompt_at
    # (unix seconds, 0 = never) just throttles it to once a week otherwise.
    "donated": False,
    "last_donate_prompt_at": 0,
}

_lock = threading.RLock()


def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with _lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        tmp.replace(CONFIG_PATH)


config = load_config()
