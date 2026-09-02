import json
import os
import shutil
import threading
from pathlib import Path

# %ProgramData%\PC2Sonos (normally C:\ProgramData\PC2Sonos): a per-machine
# data folder that is NEVER touched by OneDrive's Known Folder Move, unlike
# Desktop/Documents/Pictures -- those get silently redirected into OneDrive
# on any PC with "Backup" turned on, which previously meant this app's
# config/log lived inside a cloud-synced folder without anyone choosing
# that. ProgramData also needs no elevation to write into once the folder
# itself exists (see setup_installer.py, which creates it during the
# elevated install and grants standard users write access), so the app
# can keep saving config.json during normal, unelevated use. The actual
# PC2Sonos.exe/PC2Sonos-Uninstall.exe binaries live elsewhere (Program
# Files by default) -- this is just the small, frequently-rewritten data
# file, kept separate because Program Files itself can't be written to
# without elevation.
APP_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "PC2Sonos"
CONFIG_PATH = APP_DIR / "config.json"

# Every location this data has lived in before, oldest first -- checked in
# order so an upgrade from ANY prior version carries the existing config
# forward instead of silently starting over.
_OLD_APP_DIRS = [
    Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / "PC2Sonos",
    Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PC2Sonos",
]


# Every build before this one shipped with local_delay_ms defaulting to
# this -- so a config carried forward from an old install that never had
# its delay touched would still say 1500, even though a genuinely fresh
# install now starts at 0. That's not a real, deliberately-chosen value,
# just a stale old default that happened to get written to disk the
# first time the app ran -- migration below resets exactly this one
# value (and only this exact value) back to the new default, while still
# preserving anything the person actually did configure: which speakers
# are enabled, per-speaker volumes, or a delay they set to anything else,
# including one that happens to also be 1500 by deliberate choice being
# indistinguishable from the stale case -- an acceptable tradeoff since
# the whole point of the new default is that unedited installs should
# land on 0, not on a number nobody chose.
_STALE_OLD_DEFAULT_DELAY_MS = 1500


def _migrate_from_old_locations():
    """One-time migration for anyone upgrading from a version of PC2Sonos
    that stored config.json in Documents (OneDrive-synced, if Documents
    is redirected there -- the reason this moved again) or, before that,
    %LOCALAPPDATA%. Without this, upgrading would silently orphan the
    existing config -- tuned delay, which speakers were enabled, per-
    speaker volumes -- and start over from scratch."""
    if CONFIG_PATH.exists():
        return
    for old_dir in _OLD_APP_DIRS:
        old_config = old_dir / "config.json"
        if old_config.exists():
            try:
                APP_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_config, CONFIG_PATH)
                with open(CONFIG_PATH, "r") as f:
                    migrated = json.load(f)
                if migrated.get("local_delay_ms") == _STALE_OLD_DEFAULT_DELAY_MS:
                    migrated["local_delay_ms"] = 0
                    with open(CONFIG_PATH, "w") as f:
                        json.dump(migrated, f, indent=2)
            except Exception:
                pass
            return


APP_DIR.mkdir(parents=True, exist_ok=True)
_migrate_from_old_locations()

DEFAULT_CONFIG = {
    # Substring match against Windows device names. VB-Audio Virtual
    # Cable's recording endpoint is normally named "CABLE Output".
    "capture_device_substr": "CABLE Output",
    # Blank = auto-pick the first real (non-virtual) WASAPI output device.
    "render_device_substr": "",
    # How long (ms) to hold back the LOCAL speaker output so it lines up
    # with the Sonos speakers. This is the number the dashboard slider
    # controls. Starts at 0 on a fresh install -- use the Auto button (or
    # drag the slider up by ear) to find the right value for your setup.
    "local_delay_ms": 0,
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
