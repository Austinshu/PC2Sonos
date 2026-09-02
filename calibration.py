"""
Real automatic delay calibration -- no dragging, no guessing.

The manual slider asks a human to nudge a number until two speakers
happen to sound in sync, by ear. This module measures the actual
answer instead: it plays a short, distinctive test chirp through the
exact same audio pipeline everything else uses (audio_engine's
broadcaster), so the chirp reaches both the delayed PC-speaker path and
every enabled Sonos speaker exactly like real audio does. It then
listens to the room with a real microphone, finds the chirp on both
paths with cross-correlation, and calculates -- rather than estimates
-- the delay that lines them up: the actual measured latency
difference between "PC speaker to your ear" and "Sonos speaker to your
ear", in the room the speakers are actually sitting in.

Deliberately conservative about failure: if there's no microphone, no
enabled Sonos speaker, or the recording is too noisy/quiet to get a
confident reading, this reports a clear error and leaves the existing
delay untouched rather than guessing and silently making things worse.
The manual slider is always still there as a fallback.
"""

import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

from audio_engine import broadcaster, get_pyaudio
from config import config, save_config

CHIRP_MS = 180                  # length of the test tone
CHIRP_F0, CHIRP_F1 = 1200, 3600  # Hz sweep -- easy to tell apart from music/voice/hum
RECORD_SECONDS = 6.0             # long enough to catch Sonos's ~1-2s buffering
MIN_PEAK_SEPARATION_MS = 120     # the PC and Sonos echoes must be at least this far apart
# A peak must beat the noise floor (median correlation magnitude) by this
# factor to count as a real echo rather than a coincidence. This has to
# be well above what PURE NOISE alone can produce by chance: with a
# ~6-second recording there are on the order of a quarter million
# correlation samples, and the max of that many noise samples routinely
# reaches several times the median just from extreme-value statistics
# (empirically ~7x in testing with pure noise). 15x leaves real margin
# above that. A genuine, cleanly captured chirp echo scores in the
# hundreds to thousands, so this is nowhere near a hair-trigger for the
# real case -- but this number is a best-effort starting point, not
# something verified against a real microphone/speaker setup (this was
# built without access to real audio hardware); it may need tuning
# after a first real-world run or two.
MIN_CONFIDENCE = 15.0

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


def make_chirp_signal(rate):
    """Mono float64 samples in [-1, 1] at the given sample rate -- the
    same synthetic tone whether it's about to be played out loud or used
    as the reference template for finding itself inside a recording."""
    n = int(rate * CHIRP_MS / 1000)
    t = np.linspace(0, CHIRP_MS / 1000, n, endpoint=False)
    sweep_rate = (CHIRP_F1 - CHIRP_F0) / (CHIRP_MS / 1000)
    phase = 2 * np.pi * (CHIRP_F0 * t + sweep_rate * t ** 2 / 2)
    sig = np.sin(phase) * np.hanning(n)  # windowed so it doesn't click at the edges
    return sig


def signal_to_pcm16(sig_float, channels):
    """Mono float signal -> interleaved little-endian PCM16 bytes, ready
    to hand straight to the broadcaster like any other captured chunk."""
    sig_i16 = (sig_float * 0.9 * 32767).astype(np.int16)
    if channels == 2:
        sig_i16 = np.repeat(sig_i16.reshape(-1, 1), 2, axis=1).reshape(-1)
    return sig_i16.tobytes()


def find_echo_times(recorded_mono, template_mono, sample_rate):
    """Cross-correlation of a recording against the known chirp, in
    numpy's 'valid' mode -- deliberately the simple, unambiguous form
    (output index d IS the start sample of the best-fit alignment, no
    extra offset bookkeeping to get subtly wrong) rather than a faster
    FFT formulation that's easy to get off-by-len(template) on. At the
    sizes involved here (a several-second recording, a ~180ms template)
    this still only takes a fraction of a second.

    Returns up to 2 (time_seconds, confidence) hits, earliest first: one
    for the PC-speaker echo and one for Sonos. Deliberately capped at the
    top 2 strongest candidates rather than "every peak that clears the
    noise floor" -- a real recording has plenty of weak side-lobes and
    room-echo ghosts that technically clear a low confidence bar without
    being either real speaker. Since this feature only ever cares about
    exactly two echoes, taking the two strongest (far enough apart in
    time) is both simpler and more robust than trying to threshold out
    every possible false positive."""
    corr = np.correlate(recorded_mono, template_mono, mode="valid")
    score_raw = np.abs(corr)
    noise_floor = np.median(score_raw) + 1e-9
    score = score_raw / noise_floor

    min_sep = max(1, int(MIN_PEAK_SEPARATION_MS / 1000 * sample_rate))
    order = np.argsort(score)[::-1]
    taken = []
    for i in order:
        if score[i] < MIN_CONFIDENCE:
            break
        if any(abs(int(i) - t) < min_sep for t in taken):
            continue
        taken.append(int(i))
        if len(taken) >= 2:
            break

    hits = [(i / sample_rate, float(score[i])) for i in taken]
    return sorted(hits, key=lambda h: h[0])


def debug_top_candidates(recorded_mono, template_mono, sample_rate, n=6):
    """Same correlation as find_echo_times, but returns the top N peaks
    regardless of MIN_CONFIDENCE/separation -- for logging when a
    calibration attempt fails to find 2 clean hits, so there's something
    to look at besides a guess (was there a signal at all? how far below
    threshold? how close together?) instead of re-running blind."""
    corr = np.correlate(recorded_mono, template_mono, mode="valid")
    score_raw = np.abs(corr)
    noise_floor = np.median(score_raw) + 1e-9
    score = score_raw / noise_floor
    min_sep = max(1, int(MIN_PEAK_SEPARATION_MS / 1000 * sample_rate))
    order = np.argsort(score)[::-1]
    taken = []
    for i in order:
        if any(abs(int(i) - t) < min_sep for t in taken):
            continue
        taken.append(int(i))
        if len(taken) >= n:
            break
    return [(i / sample_rate, float(score[i])) for i in taken]


def _record_microphone(seconds):
    """Records from Windows' current default input device (whatever a
    normal app like Zoom or Discord would also treat as "the
    microphone"). Returns (pcm_bytes, channels, sample_rate, device_name).
    Raises RuntimeError with a human-readable reason on failure -- no
    default input device, or the open/read itself fails."""
    pa = get_pyaudio()
    try:
        info = pa.get_default_input_device_info()
    except Exception as e:
        raise RuntimeError(f"no microphone found ({type(e).__name__}: {e})")

    idx = info["index"]
    name = info.get("name", "microphone")
    rate = int(info.get("defaultSampleRate", 44100))
    channels = max(1, min(2, int(info.get("maxInputChannels", 1) or 1)))

    stream = pa.open(format=pyaudio.paInt16, channels=channels, rate=rate,
                      input=True, input_device_index=idx, frames_per_buffer=1024)
    frames = []
    n_chunks = int(seconds * rate / 1024) + 1
    try:
        for _ in range(n_chunks):
            frames.append(stream.read(1024, exception_on_overflow=False))
    finally:
        stream.stop_stream()
        stream.close()
    return b"".join(frames), channels, rate, name


def run_calibration_acoustic():
    """The test-tone/microphone flow, meant to run on a background thread.
    Never raises -- every failure path lands in get_status() as
    state='error' with a plain-English reason, and any temporary change
    to local_delay_ms is rolled back so a failed attempt can't leave
    playback worse off.

    In practice this needs a mic that can clearly hear BOTH the PC
    speakers and the Sonos speaker at once, in a quiet room -- if that
    doesn't hold (wireless headset mic, speakers in different rooms,
    background noise) the cross-correlation can lock onto the wrong pair
    of peaks and return a confident-looking but wrong number, or a
    drifting one across repeated runs. See run_calibration_silent() for
    an alternative that doesn't depend on the room's acoustics at all."""
    old_delay = config.get("local_delay_ms", 1500)
    changed_delay = False
    try:
        from sonos_ctl import speaker_mgr
        from audio_engine import get_lan_ip

        enabled = [s for s in speaker_mgr.list() if s["enabled"]]
        if not enabled:
            _set_status("error", "Enable at least one Sonos speaker first, then try Auto again.")
            return

        _set_status("preparing", "Making sure your speakers are streaming...")
        base_url = f"http://{get_lan_ip()}:{config['http_port']}"
        for s in enabled:
            zone = speaker_mgr.speakers.get(s["uid"])
            if zone is not None and not speaker_mgr.streams.get(s["uid"]):
                speaker_mgr.start_stream(s["uid"], zone, base_url)
        time.sleep(2)  # give Sonos a moment to actually connect before the test tone

        # isolate the PC-speaker path down to just hardware/driver latency
        # so the two echoes we're about to measure are directly comparable
        config["local_delay_ms"] = 0
        changed_delay = True

        play_sig = make_chirp_signal(config["sample_rate"])
        chirp_pcm = signal_to_pcm16(play_sig, config["channels"])

        _set_status("recording",
                    "Playing a short test tone and listening with your microphone -- "
                    "please stay quiet for a few seconds.")

        result = {}

        def do_record():
            try:
                pcm, mic_ch, mic_rate, mic_name = _record_microphone(RECORD_SECONDS)
                result["pcm"] = pcm
                result["channels"] = mic_ch
                result["rate"] = mic_rate
                result["mic_name"] = mic_name
            except Exception as e:
                result["error"] = str(e)

        rec_thread = threading.Thread(target=do_record, daemon=True)
        rec_thread.start()
        time.sleep(0.4)  # let the delay-change settle and the mic stream open
        broadcaster.publish(chirp_pcm)
        rec_thread.join(timeout=RECORD_SECONDS + 5)

        if "error" in result:
            _set_status("error", f"Couldn't record from a microphone: {result['error']}")
            return
        if "pcm" not in result:
            _set_status("error", "Recording timed out -- try again.")
            return

        _set_status("analyzing", "Analyzing the recording...")
        recorded = np.frombuffer(result["pcm"], dtype=np.int16).astype(np.float64)
        mic_channels = result["channels"]
        if mic_channels > 1:
            recorded = recorded.reshape(-1, mic_channels).mean(axis=1)

        template = make_chirp_signal(result["rate"])
        hits = find_echo_times(recorded, template, result["rate"])

        rms = float(np.sqrt(np.mean(np.square(recorded)))) if recorded.size else 0.0
        top = debug_top_candidates(recorded, template, result["rate"])
        candidates = ", ".join(f"{t:.3f}s@{s:.1f}x" for t, s in top) or "none"
        print(f"[calibrate] debug: mic={result.get('mic_name')} rms={rms:.1f} "
              f"need_confidence>={MIN_CONFIDENCE}x accepted_hits={hits} "
              f"top_candidates=[{candidates}]")

        if len(hits) < 2:
            _set_status("error",
                        "Couldn't clearly hear the test tone on both your PC speakers and "
                        "Sonos. Try again in a quieter room, turn your volume up a bit, or "
                        "check that your microphone can actually hear both sets of speakers.")
            return

        pc_time, sonos_time = hits[0][0], hits[1][0]
        new_delay = int(round((sonos_time - pc_time) * 1000))
        new_delay = max(0, min(4000, new_delay))

        config["local_delay_ms"] = new_delay
        changed_delay = False  # this is now the deliberate, saved value -- not a rollback case
        save_config(config)
        _set_status("done",
                    f"Auto-calibrated to {new_delay}ms using {result.get('mic_name', 'your microphone')}.",
                    result_ms=new_delay)
    except Exception as e:
        _set_status("error", f"Calibration failed: {type(e).__name__}: {e}")
    finally:
        if changed_delay:
            config["local_delay_ms"] = old_delay


def run_calibration_silent():
    """No test tone, no microphone, no quiet room needed: measures each
    enabled Sonos speaker's own reported transport position (RelTime)
    against wall-clock time right after telling it to (re)start the
    stream -- see sonos_ctl.measure_transport_delay() for the mechanics.
    Far more consistent run-to-run than the acoustic method (no room
    acoustics, no mic placement, nothing for background noise to
    confuse), which is why this is the default behind the dashboard's
    Auto button.

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


def start_calibration_async(method="silent"):
    """No-op if a calibration is already running -- avoids two overlapping
    attempts if someone double-clicks Auto. method="silent" (default) uses
    run_calibration_silent(); method="acoustic" uses the test-tone/mic
    flow instead."""
    if get_status()["state"] in ("preparing", "recording", "analyzing"):
        return
    _set_status("preparing", "Starting...")
    target = run_calibration_acoustic if method == "acoustic" else run_calibration_silent
    threading.Thread(target=target, daemon=True).start()
