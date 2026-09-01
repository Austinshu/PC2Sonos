"""
Self-diagnosis + crash-visibility helpers.

PC2Sonos runs on other people's machines that the developer will never
have hands-on access to. If something goes wrong there, the only way to
troubleshoot it is if the PERSON RUNNING IT can hand over one file that
tells the whole story -- what OS/network/audio setup they have, what
PC2Sonos saw when it looked for Sonos speakers and the virtual cable,
and the exact error if one happened.

Two pieces:
  * install_global_exception_logging() -- makes sure nothing can crash
    silently. Without this, an uncaught exception in a background
    thread (there are several: capture, render, discovery, watchdog,
    tray) just kills that one thread with no trace anywhere -- the app
    looks "stuck" or "half-working" with zero clue why. This hooks both
    the main-thread exception path (sys.excepthook) and the per-thread
    path (threading.excepthook, Python 3.8+) so every uncaught
    exception, anywhere, gets a full traceback written to the log
    before that thread dies.
  * export_diagnostics_zip() -- a one-click "send me this file" bundle:
    a fresh system/network/audio/Sonos snapshot + the log + config,
    zipped to the Desktop. Wired to both the tray icon menu and a
    dashboard button so a non-technical user can find it either way.

Everything here is local-only: nothing is sent anywhere automatically.
The zip only leaves the machine if the person running it chooses to
send it to someone.
"""

import os
import platform
import subprocess
import sys
import threading
import traceback
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path

from config import APP_DIR, CONFIG_PATH, config

# Where "Export Diagnostics" points people by default. This is only ever
# used to pre-address an email in the PERSON'S OWN mail client (see
# open_diagnostics_email below) -- nothing here sends anything on its own.
SUPPORT_EMAIL = "austin1235@gmail.com"


def _log(msg):
    print(msg, flush=True)


def install_global_exception_logging():
    """Call once, as early as possible. Makes every uncaught exception
    -- main thread or background thread -- land in the log with a loud,
    greppable marker and a full traceback, instead of silently killing
    whatever thread hit it."""

    def _write(kind, exc_type, exc_value, exc_tb, thread_name=None):
        where = f" in thread '{thread_name}'" if thread_name else ""
        _log(f"\n[FATAL] uncaught {kind} exception{where}:")
        try:
            traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stdout)
            sys.stdout.flush()
        except Exception:
            pass

    def excepthook(exc_type, exc_value, exc_tb):
        _write("main-thread", exc_type, exc_value, exc_tb)

    def thread_excepthook(args):
        _write("thread", args.exc_type, args.exc_value, args.exc_traceback,
               thread_name=getattr(args.thread, "name", None))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def _run(args, timeout=10):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        return out or (r.stderr or "").strip() or "(no output)"
    except Exception as e:
        return f"(failed: {type(e).__name__}: {e})"


def _network_profile():
    """Public vs Private vs Domain -- the single setting that has already,
    in testing, silently broken Sonos connectivity with zero visible
    error anywhere. Worth surfacing front and center in every report."""
    if sys.platform != "win32":
        return "n/a (not Windows)"
    ps = "Get-NetConnectionProfile | Select-Object -ExpandProperty NetworkCategory"
    return _run(["powershell", "-NoProfile", "-Command", ps])


def _firewall_rules_present():
    if sys.platform != "win32":
        return "n/a (not Windows)"
    try:
        from windows_firewall import RULE_NAME_PROGRAM, RULE_NAME_PORT, _rule_exists
        prog = "present" if _rule_exists(RULE_NAME_PROGRAM) else "MISSING"
        port = "present" if _rule_exists(RULE_NAME_PORT) else "MISSING"
        return f"program rule: {prog}, port rule: {port}"
    except Exception as e:
        return f"couldn't check ({type(e).__name__}: {e})"


def _cable_and_default_output():
    if sys.platform != "win32":
        return "n/a (not Windows)", "n/a (not Windows)"
    try:
        from windows_audio import list_playback_devices, current_default_playback_name
        names = [n for _, n in list_playback_devices()]
        cable = "present" if any("cable" in (n or "").lower() for n in names) else "MISSING"
        default = current_default_playback_name() or "unknown"
        return cable, default
    except Exception as e:
        msg = f"couldn't check ({type(e).__name__}: {e})"
        return msg, msg


def system_snapshot():
    """A short, plain-text, human-readable report -- the first thing to
    read when troubleshooting a machine we've never seen. Deliberately
    contains no personal data beyond LAN-local info (Sonos speaker
    names the user gave them, and the PC's own LAN IP) -- no usernames,
    no file paths outside this app, nothing from other applications."""
    from audio_engine import get_lan_ip, list_output_devices, get_current_render_device_name

    lines = []
    lines.append(f"generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"PC2Sonos running as frozen .exe: {getattr(sys, 'frozen', False)}")
    lines.append(f"OS: {platform.platform()}")
    lines.append(f"Python: {sys.version.split()[0]}")
    lines.append(f"LAN IP: {get_lan_ip()}")
    lines.append(f"Windows network profile: {_network_profile()}")
    lines.append(f"Firewall rules: {_firewall_rules_present()}")

    cable, default_out = _cable_and_default_output()
    lines.append(f"VB-CABLE driver: {cable}")
    lines.append(f"Windows default playback device: {default_out}")
    lines.append(f"PC-speaker (delayed) render device in use: "
                 f"{get_current_render_device_name() or 'none picked yet'}")

    lines.append("Output devices seen by PC2Sonos:")
    try:
        devices = list_output_devices()
        if not devices:
            lines.append("  (none)")
        for d in devices:
            flag = "  [looks virtual]" if d.get("likely_virtual") else ""
            lines.append(f"  - {d['name']}{flag}")
    except Exception as e:
        lines.append(f"  (couldn't list: {type(e).__name__}: {e})")

    lines.append("Sonos speakers PC2Sonos has found:")
    try:
        from sonos_ctl import speaker_mgr
        speakers = speaker_mgr.list()
        if not speakers:
            lines.append("  (none found yet)")
        for s in speakers:
            lines.append(f"  - {s['name']}: enabled={s['enabled']} "
                         f"streaming={s['streaming']} volume={s['volume']}%")
    except Exception as e:
        lines.append(f"  (couldn't list: {type(e).__name__}: {e})")

    lines.append(f"Config: local_delay_ms={config.get('local_delay_ms')} "
                 f"http_port={config.get('http_port')}")
    return "\n".join(lines)


def export_diagnostics_zip():
    """Bundles a fresh snapshot + the running log + config into one zip
    on the Desktop and returns its path. This is the one file a random
    person on the internet can hand back to us when something's wrong
    -- without it, troubleshooting a machine we've never touched means
    guessing blind."""
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    if not desktop.exists():
        desktop = Path.home()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = desktop / f"PC2Sonos-Diagnostics-{stamp}.zip"

    snapshot = system_snapshot()
    log_path = APP_DIR / "pc2sonos.log"

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("snapshot.txt", snapshot)
        if log_path.exists():
            z.write(log_path, arcname="pc2sonos.log")
        if CONFIG_PATH.exists():
            z.write(CONFIG_PATH, arcname="config.json")
    return out_path


def open_diagnostics_email(zip_path):
    """Best-effort: open the PERSON'S OWN default mail app with a message
    already addressed to SUPPORT_EMAIL, subject/body filled in, so all
    they have to do is attach the file we just saved and hit send.

    Deliberately NOT an automatic send: that would require embedding a
    real email account's credentials (SMTP login, or an API key) inside
    a binary that ends up on strangers' computers. PyInstaller .exe's are
    trivially unpacked, so any secret shipped inside one should be
    treated as public -- anyone could pull it out and send mail (or
    worse, spam) as that account. Opening the user's own mail client
    costs nothing and exposes nothing; it just saves them a copy/paste."""
    subject = "PC2Sonos Diagnostics"
    body = (
        f"Attach the file saved to your Desktop: {zip_path.name}\n\n"
        "(Describe what's not working, if anything, below.)\n\n"
    )
    uri = (f"mailto:{SUPPORT_EMAIL}"
           f"?subject={urllib.parse.quote(subject)}"
           f"&body={urllib.parse.quote(body)}")
    try:
        if sys.platform == "win32":
            os.startfile(uri)  # noqa
        else:
            import webbrowser
            webbrowser.open(uri)
        return True
    except Exception as e:
        _log(f"[diagnostics] couldn't open mail client: {e}")
        return False
