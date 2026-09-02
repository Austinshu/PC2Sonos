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
import socket
import sys
import threading
import time
from pathlib import Path


def _wait_for_http(port, stop_event, timeout=20):
    """Block until something is accepting TCP connections on
    127.0.0.1:<port> (our Flask server), or `timeout` seconds pass, or we
    were asked to stop. Returns True if the port came up."""
    deadline = time.monotonic() + timeout
    while not stop_event.is_set() and time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            stop_event.wait(0.2)
    return False


def _setup_logging():
    """The .exe is built with --noconsole (no terminal window), which means
    Windows gives it NO stdout/stderr at all -- sys.stdout is None. Any bare
    print() then raises AttributeError and silently kills whatever thread
    called it, including the main thread. Redirect to a log file instead,
    before anything else runs, so the app doesn't self-destruct on its own
    status messages -- and so there's somewhere to look if something else
    goes wrong."""
    if sys.stdout is None or getattr(sys, "frozen", False):
        log_dir = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "PC2Sonos"
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


def _rediscover_guarded():
    try:
        speaker_mgr.rediscover()
    except Exception as e:
        # never let one bad discovery pass silently kill the loop --
        # without this, a single hiccup means Sonos speakers are never
        # found again for the rest of the run
        print(f"[sonos] discovery loop error: {e}")


def sonos_discovery_loop(stop_event, on_demand=False, ready_event=None):
    """Keep the speaker list current.

    Default ("auto"): a full re-scan every 15s.

    "on_demand" (set once the configured default speaker came up at
    launch): hold the one safety-net scan until audio to the default
    speaker is underway (ready_event, set after stream_keeper's first
    pass) so the scan stays out of the startup crunch -- it only
    populates the dashboard with the other speakers, nothing time-
    critical. Then idle, re-scanning only when the Rescan button asks.
    The 20s is a fallback in case that first pass never completes."""
    if on_demand:
        (ready_event or stop_event).wait(20)
        while not stop_event.is_set():
            _rediscover_guarded()
            while not stop_event.is_set() and not speaker_mgr.rescan_requested.wait(30):
                pass
            speaker_mgr.rescan_requested.clear()
        return

    while not stop_event.is_set():
        _rediscover_guarded()
        stop_event.wait(15)


def main():
    # first thing, no matter what -- so a crash in anything below this
    # line still gets a full traceback in the log instead of just
    # silently stopping. This is the difference between "someone else's
    # PC has a bug we'll never see" and "someone else's PC has a bug we
    # can actually read about."
    install_global_exception_logging()

    stop_event = threading.Event()
    # set after stream_keeper's first watchdog pass -- the on_demand
    # discovery loop waits on this so its (non-urgent) full scan doesn't
    # compete with getting audio to the default speaker at launch
    first_tick_done = threading.Event()

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

    # Fast path: if a default speaker is configured, reach it directly now
    # (one unicast call) so the watchdog can start streaming to it in a
    # second or two, instead of waiting out a full discovery pass.
    primed_uid = None
    try:
        primed_uid = speaker_mgr.prime_default_speaker()
    except Exception as e:
        print(f"[sonos] default-speaker prime failed: {e}")

    on_demand = (config.get("discovery_mode") == "on_demand" and primed_uid is not None)
    if on_demand:
        print("[sonos] discovery_mode=on_demand: background re-scanning off "
              "(one safety-net pass, then only on request)")
    disc_thread = threading.Thread(
        target=sonos_discovery_loop,
        args=(stop_event, on_demand, first_tick_done), daemon=True)
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
        # starts each enabled speaker the first time discovery -- or the
        # default-speaker prime -- hands it to us) and staying alive (if
        # Sonos ever drops the stream -- sleep, wifi blip, source switch
        # and back -- it restarts it automatically, no re-toggle needed).
        #
        # First tick waits only until the HTTP server is actually
        # accepting connections (a Sonos speaker told to play before then
        # would fail to fetch the stream), not a fixed guess.
        _wait_for_http(config["http_port"], stop_event, timeout=20)
        while not stop_event.is_set():
            try:
                speaker_mgr.watchdog_tick(f"http://{get_lan_ip()}:{config['http_port']}")
            except Exception as e:
                print(f"[sonos] watchdog error: {e}")
            first_tick_done.set()  # release the on_demand discovery loop
            stop_event.wait(8)

    threading.Thread(target=stream_keeper, daemon=True).start()

    try:
        from tray_icon import run_tray
        threading.Thread(target=run_tray, args=(config,), daemon=True).start()
    except Exception as e:
        print(f"[tray] system tray icon not available: {e}")

    def announce_startup():
        # webapp.run_web() below blocks until the process exits, so this
        # runs on its own thread and waits for the HTTP server first.
        #
        # First launch on this machine: open the dashboard so setup (sync
        # delay, which speakers) actually gets done. Every launch after
        # that -- i.e. every normal Windows login via the Startup shortcut
        # -- don't steal focus with a browser tab; just pop a tray balloon
        # so there's still a visible sign it started. The dashboard is one
        # click away on the tray icon whenever it's wanted.
        first_run = not config.get("has_launched_before")
        if not _wait_for_http(config["http_port"], stop_event, timeout=30):
            return
        if first_run:
            try:
                import webbrowser
                webbrowser.open(f"http://127.0.0.1:{config['http_port']}")
            except Exception as e:
                print(f"[web] couldn't auto-open the dashboard: {e}")
        else:
            try:
                import tray_icon
                tray_icon.notify(
                    f"Running. Dashboard: http://127.0.0.1:{config['http_port']}")
            except Exception:
                pass
        if first_run:
            config["has_launched_before"] = True
            try:
                from config import save_config
                save_config(config)
            except Exception as e:
                print(f"[config] couldn't record first launch: {e}")

    threading.Thread(target=announce_startup, daemon=True).start()

    print(f"[web] dashboard: http://127.0.0.1:{config['http_port']}")
    webapp.run_web()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()  # goes to the log file, not lost
        raise
