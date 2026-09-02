"""
PC2Sonos
========
Free, local, no-account software that:
  1. Discovers every Sonos speaker on your network.
  2. Streams your PC's system audio to whichever ones you enable.
  3. Delays your PC's own local speaker output to match, so the PC and
     Sonos play in sync instead of echoing each other.
  4. Runs automatically at Windows login (see install.ps1).

Everything runs on your own machine. Nothing is sent anywhere but your
own Sonos speakers on your own LAN.

Requires (see requirements.txt): soco, pyaudiowpatch, flask, pystray, pillow
Requires VB-Audio Virtual Cable (free, https://vb-audio.com/Cable/) set as
your Windows default playback device -- see README.md.
"""

import os
import sys
import threading
import time
from pathlib import Path


def _setup_logging():
    """The .exe is built with --noconsole (no terminal window), which means
    Windows gives it NO stdout/stderr at all -- sys.stdout is None. Any bare
    print() then raises AttributeError and silently kills whatever thread
    called it, including the main thread. Redirect to a log file instead,
    before anything else runs, so the app doesn't self-destruct on its own
    status messages -- and so there's somewhere to look if something else
    goes wrong."""
    if sys.stdout is None or getattr(sys, "frozen", False):
        log_dir = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / "PC2Sonos"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "pc2sonos.log"
        try:
            # don't let the log grow forever across months of 24/7 use
            if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
                log_path.unlink()
        except Exception:
            pass
        log_file = open(log_path, "a", buffering=1, encoding="utf-8")
        sys.stdout = log_file
        sys.stderr = log_file
        print(f"\n--- PC2Sonos starting: {log_dir} ---")


_setup_logging()

from audio_engine import start_audio_engine, get_lan_ip  # noqa: E402
from config import config  # noqa: E402
from sonos_ctl import speaker_mgr  # noqa: E402
from diagnostics import install_global_exception_logging, system_snapshot  # noqa: E402
import webapp  # noqa: E402


def sonos_discovery_loop(stop_event):
    while not stop_event.is_set():
        try:
            speaker_mgr.rediscover()
        except Exception as e:
            # never let one bad discovery pass silently kill the loop --
            # without this, a single hiccup means Sonos speakers are
            # never found again for the rest of the run
            print(f"[sonos] discovery loop error: {e}")
        time.sleep(15)


def main():
    # first thing, no matter what -- so a crash in anything below this
    # line still gets a full traceback in the log instead of just
    # silently stopping. This is the difference between "someone else's
    # PC has a bug we'll never see" and "someone else's PC has a bug we
    # can actually read about."
    install_global_exception_logging()

    stop_event = threading.Event()

    if sys.platform == "win32":
        # self-configure: if the virtual cable exists but isn't the
        # Windows default output, fix that before we start capturing --
        # this is the one manual Sound-settings step nobody should
        # ever have to do by hand
        try:
            from windows_audio import ensure_cable_is_default
            print(f"[audio] {ensure_cable_is_default()}")
        except Exception as e:
            print(f"[audio] default-device helper unavailable: {e}")

        # best-effort: the installer already does this elevated, but if
        # someone ever runs the app without the installer (or the rule
        # got removed by a Windows update/reset), try again here too.
        # Silently no-ops if we're not elevated -- that's fine, the
        # installer path is the one that actually matters.
        try:
            from windows_firewall import ensure_firewall_rules
            exe = sys.executable if getattr(sys, "frozen", False) else __file__
            print(f"[firewall] {ensure_firewall_rules(exe, port=config['http_port'])}")
        except Exception as e:
            print(f"[firewall] rule check unavailable: {e}")

    start_audio_engine(stop_event)

    disc_thread = threading.Thread(target=sonos_discovery_loop, args=(stop_event,), daemon=True)
    disc_thread.start()

    def log_startup_snapshot():
        # give discovery a few seconds first so this actually has
        # something useful in the Sonos section; not required for
        # troubleshooting (the tray/dashboard can regenerate this live
        # at any point) but means the very first minute of a run is
        # already fully readable in the log without any extra step.
        time.sleep(6)
        try:
            print("\n=== startup diagnostics snapshot ===")
            print(system_snapshot())
            print("=== end snapshot ===\n")
        except Exception as e:
            print(f"[diagnostics] startup snapshot failed: {e}")

    threading.Thread(target=log_startup_snapshot, daemon=True).start()

    def stream_keeper():
        # The watchdog owns both jobs: the boot-time start (it force-
        # starts each enabled speaker the first time discovery hands it
        # to us -- no race against discovery timing) and staying alive
        # (if Sonos ever drops the stream -- sleep, wifi blip, source
        # switch and back -- it restarts it automatically instead of
        # waiting for a human to re-toggle).
        time.sleep(5)
        while not stop_event.is_set():
            try:
                speaker_mgr.watchdog_tick(f"http://{get_lan_ip()}:{config['http_port']}")
            except Exception as e:
                print(f"[sonos] watchdog error: {e}")
            time.sleep(8)

    threading.Thread(target=stream_keeper, daemon=True).start()

    try:
        from tray_icon import run_tray
        threading.Thread(target=run_tray, args=(config,), daemon=True).start()
    except Exception as e:
        print(f"[tray] system tray icon not available: {e}")

    print(f"[web] dashboard: http://127.0.0.1:{config['http_port']}")
    webapp.run_web()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()  # goes to the log file, not lost
        raise
