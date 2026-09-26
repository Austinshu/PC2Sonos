"""
Real automatic delay calibration -- no dragging, no guessing.

The manual slider asks a human to nudge a number until two speakers
happen to sound in sync, by ear. This module measures the actual
answer instead, from Sonos's own reported transport position (RelTime)
against wall-clock time right after telling it to (re)start the stream
-- see sonos_ctl.measure_transport_delay() for the mechanics. No test
tone, no microphone, no quiet room needed.

Deliberately conservative about failure: if there's no enabled Sonos
speaker, or no timing reading comes back, this reports a clear error
and leaves the existing delay untouched rather than guessing and
silently making things worse. The manual slider is always still there
as a fallback.
"""

import threading

from config import config, save_config

_status = {"state": "idle", "detail": "", "result_ms": None}
_status_lock = threading.Lock()


def get_status():
    with _status_lock:
        return dict(_status)


def _set_status(state, detail="", result_ms=None):
    with _status_lock:
        _status["state"] = state
        _status["detail"] = detail
        _status["result_ms"] = result_ms


def run_calibration_silent():
    """Measures each enabled Sonos speaker's own reported transport
    position (RelTime) against wall-clock time right after telling it to
    (re)start the stream -- see sonos_ctl.measure_transport_delay() for
    the mechanics. Consistent run-to-run: no room acoustics, no mic
    placement, nothing for background noise to confuse.

    The one thing it can't see is any decode/buffer-priming time inside
    Sonos that never shows up in RelTime -- so treat the result as a
    strong, repeatable starting point, and nudge the manual slider by ear
    afterward if there's still a hint of echo."""
    try:
        from sonos_ctl import speaker_mgr
        from audio_engine import get_lan_ip

        enabled = [s for s in speaker_mgr.list() if s["enabled"]]
        if not enabled:
            _set_status("error", "Enable at least one Sonos speaker first, then try Auto again.")
            return

        _set_status("recording", "Measuring Sonos's own playback timing (no sound needed)...")
        base_url = f"http://{get_lan_ip()}:{config['http_port']}"
        # A single pass can land up to ~1 tick (about a second) off just from
        # RelTime's 1-second resolution -- see measure_transport_delay's own
        # docstring. That's the real reason back-to-back Auto runs can read
        # noticeably different numbers; it's measurement quantization noise,
        # not the app being wrong. Averaging two independent passes (each
        # already an internal median over ~8s of ticks) cancels most of
        # that out without a much longer single pass.
        pass1 = speaker_mgr.measure_delay_ms(base_url)
        pass2 = speaker_mgr.measure_delay_ms(base_url)
        results = {uid: int(round((pass1[uid] + pass2[uid]) / 2))
                   for uid in pass1 if uid in pass2}
        if not results:
            results = pass1 or pass2

        if not results:
            _set_status("error",
                        "Couldn't get a timing reading from any enabled Sonos speaker. "
                        "Try again, or use the manual slider.")
            return

        new_delay = max(0, min(4000, max(results.values())))
        config["local_delay_ms"] = new_delay
        save_config(config)
        detail = ", ".join(
            f"{speaker_mgr.speakers[uid].player_name if uid in speaker_mgr.speakers else uid}={ms}ms"
            for uid, ms in results.items()
        )
        print(f"[calibrate] silent: {detail} -> using {new_delay}ms")
        _set_status("done", f"Measured {new_delay}ms from Sonos's own playback clock ({detail}).",
                    result_ms=new_delay)
    except Exception as e:
        _set_status("error", f"Calibration failed: {type(e).__name__}: {e}")


def start_calibration_async():
    """No-op if a calibration is already running -- avoids two overlapping
    attempts if someone double-clicks Auto."""
    if get_status()["state"] in ("preparing", "recording"):
        return
    _set_status("preparing", "Starting...")
    threading.Thread(target=run_calibration_silent, daemon=True).start()
