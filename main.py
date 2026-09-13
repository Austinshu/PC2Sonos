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

macOS: the same design with BlackHole (https://existential.audio/blackhole/)
as the virtual device, sounddevice/CoreAudio instead of WASAPI
(audio_backend.py), and macos_audio.py / macos_app.py in place of the
windows_* helpers. See README.md, "macOS".
"""

import os
import socket
import sys
import threading
import time
from pathlib import Path


_NOOP_LOCK = object()  # stand-in "we hold the lock" on non-Windows


def acquire_single_instance_lock(name="PC2Sonos"):
    """Take a machine-session-wide named lock so a second copy of the app
    can bow out instead of fighting the first one for the HTTP port, the
    capture device, and control of the speakers.

    This actually bites at login: Windows sometimes runs a Startup-folder
    item twice (Explorer re-processing the folder when it restarts early
    in the session), and both copies then boot-start the same speaker and
    run their own watchdogs against it.

    Returns an opaque handle to keep alive for the process lifetime, or
    None if another instance already holds the lock. Never raises -- if
    the OS call fails we return the handle and let the app start."""
    if sys.platform == "darwin":
        return _acquire_flock(name)
    if sys.platform != "win32":
        return _NOOP_LOCK
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # no "Global\\" prefix -> scoped to this login session, which is
        # exactly where the double-launch happens; also avoids needing
        # any special privilege
        handle = kernel32.CreateMutexW(None, False, f"{name}-single-instance")
        if not handle:
            return _NOOP_LOCK
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return _NOOP_LOCK


_lock_file = None  # kept referenced for the process lifetime (flock is released on close)


def _acquire_flock(name):
    """The non-Windows single-instance lock: flock on a file in the data
    dir. Returns a handle, or None if another instance holds it."""
    global _lock_file
    try:
        import fcntl
        from config import APP_DIR
        f = open(APP_DIR / f"{name}.lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return None
        f.write(str(os.getpid()))
        f.flush()
        _lock_file = f  # only a held lock is kept referenced
        return f
    except OSError:
        return None
    except Exception:
        return _NOOP_LOCK


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
        if sys.platform == "darwin":
            log_dir = Path.home() / "Library" / "Application Support" / "PC2Sonos"
        else:
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


if "--version" in sys.argv[1:]:
    # build_macos_app.sh smoke-tests the frozen bundle with this; it must
    # run before stdout is redirected to the log file
    from version import VERSION as _V
    print(f"PC2Sonos {_V}")
    sys.exit(0)

_setup_logging()

from audio_engine import start_audio_engine, get_lan_ip  # noqa: E402
from config import config  # noqa: E402
from sonos_ctl import speaker_mgr  # noqa: E402
from diagnostics import install_global_exception_logging, system_snapshot  # noqa: E402
from updater import check_for_update_async  # noqa: E402
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


def _macos_startup(stop_event):
    """The macOS equivalents of the Windows self-configuration above:
    make BlackHole the default output (remembering the previous one so
    quitting can restore it), and ask for the audio-input permission
    that reading from BlackHole needs. Without that permission CoreAudio
    delivers silence and never says why -- and the CoreAudio input
    stream alone does not reliably trigger the prompt, so ask via
    AVFoundation explicitly. Also restore the output on SIGTERM."""
    from config import save_config
    try:
        from macos_audio import ensure_blackhole_is_default
        changed, status, previous = ensure_blackhole_is_default(config["capture_device_substr"])
        print(f"[audio] {status}")
        if changed and previous:
            config["previous_default_output"] = previous
            save_config(config)
    except Exception as e:
        print(f"[audio] default-output helper unavailable: {e}")

    try:
        from macos_app import ask, microphone_status, open_microphone_settings, request_microphone_access

        def denied_dialog():
            if ask("macOS is not allowing PC2Sonos to capture audio, so Sonos and your Mac's "
                   "speakers will only get silence.\n\nIn System Settings > Privacy & Security > "
                   "Microphone, turn on PC2Sonos, then quit and reopen PC2Sonos.",
                   ["Later", "Open Microphone Settings"], default="Open Microphone Settings") \
                    == "Open Microphone Settings":
                open_microphone_settings()

        def on_result(granted):
            if granted:
                from audio_engine import restart_capture
                restart_capture()  # a stream opened before the grant keeps delivering silence
            else:
                denied_dialog()

        status = microphone_status()
        print(f"[audio] audio-input (microphone) permission: {status}")
        if status == "not determined":
            request_microphone_access(on_result)
        elif status in ("denied", "restricted"):
            threading.Thread(target=denied_dialog, daemon=True).start()
    except Exception as e:
        print(f"[audio] microphone permission helper unavailable: {e}")

    def on_term(signum, frame):
        print(f"[startup] signal {signum}; shutting down")
        _macos_shutdown(stop_event)
        os._exit(0)

    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, on_term)
        except Exception:
            pass


def _macos_shutdown(stop_event):
    """Stop the streams and hand the Mac's default output back, so
    quitting never leaves the Mac silently playing into BlackHole."""
    stop_event.set()
    try:
        speaker_mgr.stop_all()
    except Exception as e:
        print(f"[sonos] stop on quit failed: {e}")
    if config.get("manage_default_output", True):
        try:
            from macos_audio import restore_default_output
            print(f"[audio] {restore_default_output(config.get('previous_default_output', ''))}")
        except Exception as e:
            print(f"[audio] couldn't restore default output: {e}")


def _macos_first_run():
    """From the .app there is no install.sh: offer to install BlackHole
    if it's missing and to start at login, once, on the first launch."""
    try:
        from macos_app import is_bundled, offer_blackhole_install, set_login_item, ask
        from audio_engine import find_device_index
        if not is_bundled():
            return
        idx, _ = find_device_index(config["capture_device_substr"], want_input=True)
        if idx is None:
            print(f"[blackhole] not installed; user chose: {offer_blackhole_install()}")
        if ask("Start PC2Sonos automatically when you log in? You can change this later "
               "from the menu bar icon.", ["Not now", "Start at Login"],
               default="Start at Login") == "Start at Login":
            print(f"[login] {set_login_item(True)}")
    except Exception as e:
        print(f"[startup] first-run setup failed: {e}")


def main():
    # Bail out early if another copy is already running -- before we touch
    # the HTTP port, the capture device, or any speaker. Held for the
    # process lifetime via this local (main() never returns until exit).
    _instance_lock = acquire_single_instance_lock()
    if _instance_lock is None:
        print("[startup] another PC2Sonos instance is already running -- exiting")
        if sys.platform == "darwin":
            try:
                from macos_app import ask
                ask("PC2Sonos is already running: look for its icon in the menu bar and use "
                    "Quit PC2Sonos there before opening a new copy.", ["OK"])
            except Exception:
                pass
        return

    # first thing after that -- so a crash in anything below this line
    # still gets a full traceback in the log instead of just silently
    # stopping. This is the difference between "someone else's PC has a
    # bug we'll never see" and "someone else's PC has a bug we can
    # actually read about."
    install_global_exception_logging()

    # The one outbound internet request this app ever makes on its own,
    # fired once per launch and never repeated for the rest of the run
    # (see updater.py) -- the dashboard just displays whatever this finds.
    check_for_update_async()

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
    elif sys.platform == "darwin":
        _macos_startup(stop_event)

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

    tray_on_main_thread = sys.platform == "darwin"  # AppKit insists
    if not tray_on_main_thread:
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
            if sys.platform == "darwin":
                _macos_first_run()
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
    if tray_on_main_thread:
        # macOS: Flask on a background thread, the tray icon owns the main
        # thread; quitting from the tray restores the default output first.
        threading.Thread(target=webapp.run_web, daemon=True).start()
        try:
            from tray_icon import run_tray
            run_tray(config, on_quit=lambda: _macos_shutdown(stop_event))
        except Exception as e:
            print(f"[tray] system tray icon not available ({e}); running without it")
            try:
                webapp.run_web() if not _wait_for_http(config["http_port"], stop_event, 5) \
                    else threading.Event().wait()
            finally:
                _macos_shutdown(stop_event)
        return
    webapp.run_web()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()  # goes to the log file, not lost
        raise
