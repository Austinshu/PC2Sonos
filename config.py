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

# Optional dashboard password. Kept in its own file, NOT in config.json,
# on purpose: config.json is bundled verbatim into the diagnostics zip
# (see diagnostics.export_diagnostics_zip) that users email to support --
# a password in there would ride along in every submission. This file is
# never read by diagnostics and is git-ignored. One line of plain text;
# if the file is missing or empty the dashboard has no password.
PASSWORD_PATH = APP_DIR / "dashboard_password.txt"

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
    # "system" (default): capture whatever's playing through the virtual
    # cable, i.e. everything. "process": capture only capture_target_name's
    # audio (see per_app_audio.py), by exe name rather than PID since a
    # relaunched app gets a new PID every time. Falls back to "system" at
    # runtime if that process isn't currently running, isn't capturable
    # this way, or this Windows version doesn't support it at all.
    "capture_mode": "system",
    "capture_target_name": "",
    # How long (ms) to hold back the LOCAL speaker output so it lines up
    # with the Sonos speakers. This is the number the dashboard slider
    # controls. Starts at 0 on a fresh install -- use the Auto button (or
    # drag the slider up by ear) to find the right value for your setup.
    "local_delay_ms": 0,
    # Volume for the LOCAL (aux/line-out) speaker path only -- Windows'
    # own volume mixer controls what's captured going IN, not what this
    # app does with it afterward, so a quiet aux speaker has no other way
    # to get louder than the source signal already is. 1.0 = unchanged
    # passthrough (the old, only-ever behavior); the dashboard slider
    # shows this as a 0-300% range. Sonos speakers are unaffected -- they
    # have their own independent volume (config['speakers'][uid]['volume']).
    "local_render_gain": 1.0,
    # Bass/mid/treble EQ, LOCAL speaker path only (0.0 dB = flat/off,
    # each -12..+12 in the dashboard). Sonos speakers already have their
    # own Bass/Treble controls in the Sonos app/hardware -- this never
    # touches what gets sent to Sonos, only what audio_engine.py plays
    # out to your real PC speakers/headphones.
    "local_eq_bass_db": 0.0,
    "local_eq_mid_db": 0.0,
    "local_eq_treble_db": 0.0,
    "sample_rate": 44100,
    "channels": 2,
    "sample_width": 2,  # bytes (16-bit PCM)
    "http_port": 5757,
    "speakers": {},  # uid -> {"enabled": bool, "volume": int}
    # Normally the app finds speakers by SSDP multicast and these stay
    # empty. They only matter when the Sonos speakers are on a different
    # subnet / VLAN (e.g. an IoT VLAN) than this PC: routers don't forward
    # SSDP multicast across VLANs, so automatic discovery finds nothing,
    # but unicast control still works. Put ONE speaker's IP in
    # sonos_seed_ips (give it a DHCP reservation) -- the app reaches it
    # directly and learns the whole household from it. sonos_scan_cidrs
    # is an alternative: a subnet in CIDR form (e.g. "10.0.20.0/24") that
    # gets a unicast port-1400 sweep when no seed is reachable.
    "sonos_seed_ips": [],
    "sonos_scan_cidrs": [],
    # Startup behavior -----------------------------------------------------
    # default_speaker_ip: if set, PC2Sonos talks to this one speaker
    # directly at launch instead of waiting for a discovery pass -- audio
    # starts in ~1-2s instead of ~15-20s. Give that speaker a DHCP
    # reservation. default_speaker_uid is filled in automatically the
    # first time we reach it, and is then used to notice if DHCP later
    # hands that IP to a different device (in which case the IP is
    # ignored and normal discovery takes over). Blank = today's behavior.
    "default_speaker_ip": "",
    "default_speaker_uid": "",
    # "auto" = keep re-scanning the network every 15s in the background.
    # "on_demand" = once the default speaker is up, stop the background
    # loop; only re-scan at startup and when the dashboard asks. Useful
    # if you only ever stream to the one speaker.
    "discovery_mode": "auto",
    # When a brand-new speaker is discovered, start streaming to it right
    # away (true, the default -- a fresh install lights up every speaker
    # and you turn off the ones you don't want) or leave it off until you
    # enable it in the dashboard (false). The default speaker is always
    # enabled regardless.
    "new_speakers_default_enabled": True,
    # Flipped to true after the app's first successful launch. While
    # false, the dashboard opens in the browser automatically; after
    # that, use the tray icon (a startup toast still says it's running).
    "has_launched_before": False,
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
