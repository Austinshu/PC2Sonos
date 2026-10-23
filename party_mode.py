"""
Party Mode: turn the music up and down by how loud the room is.

Listens to a microphone the user picks, works out how loud the room is,
and nudges the overall volume (the same "Scale everything together"
control on the dashboard) between a quietest and a loudest setting: a
10-person get-together stays gentle, a 50-person party gets louder.

Privacy: the microphone is only ever measured, never recorded or sent
anywhere. Each block of audio is reduced to one loudness number and
thrown away. Nothing is written to disk and nothing leaves the PC. It
only runs while the person has switched Party Mode on, and it is always
off after a restart.

Limits that keep it from misbehaving:
  * Slow on purpose. The room level is smoothed over several seconds and
    the volume moves in small steps every few seconds, so one shout or a
    dropped glass doesn't spike it.
  * Bounded. The volume stays between the person's own minimum and
    maximum and never goes outside them.
  * The mic also hears the music itself, so it works best with the mic
    placed away from the speakers (the dashboard says so).
"""

import threading
import time

import numpy as np

from config import config, save_config

# How often the volume is re-evaluated, and the most it may move per check.
CONTROL_INTERVAL_S = 4.0
MAX_STEP_PCT = 4
# Smooth the measured room level over roughly this many seconds.
SMOOTH_SECONDS = 6.0
# Don't bother Sonos with tiny changes.
MIN_APPLY_DELTA_PCT = 2

_lock = threading.Lock()
_thread = None
_stop = threading.Event()
_apply = None  # fn(percent) -> sets overall volume; registered by webapp
_status = {"active": False, "level": 0.0, "volume_pct": 100, "device": None, "error": None}


def set_apply(fn):
    global _apply
    _apply = fn


def get_status():
    with _lock:
        return dict(_status)


def _set(**kw):
    with _lock:
        _status.update(kw)


def _settings():
    lo = max(10, min(300, int(config.get("party_min_pct", 70))))
    hi = max(lo, min(300, int(config.get("party_max_pct", 140))))
    full = max(10, min(95, int(config.get("party_full_level", 60))))
    return lo, hi, full


def target_pct(level, lo, hi, full):
    """Room level (0-100) -> overall volume percent: `lo` at silence,
    `hi` once the room reaches the 'full party' level, linear between."""
    frac = max(0.0, min(1.0, level / float(full)))
    return lo + (hi - lo) * frac


def _step_toward(current, target):
    delta = target - current
    if abs(delta) <= MAX_STEP_PCT:
        return target
    return current + MAX_STEP_PCT * (1 if delta > 0 else -1)


REFERENCE_FEET = 3.0   # a mic this close to people needs no distance boost
MAX_TOTAL_BOOST_DB = 36.0


def distance_boost_db(feet):
    """Sound falls about 6 dB every time the distance doubles, so a mic
    `feet` away from the crowd hears it 20*log10(feet/3) dB quieter than one
    3 feet away. Boosting by that much makes both read about the same."""
    feet = max(1.0, min(100.0, float(feet)))
    return max(0.0, 20.0 * float(np.log10(feet / REFERENCE_FEET)))


def total_boost_db():
    """Distance compensation plus the manual boost slider, capped."""
    db = distance_boost_db(config.get("party_distance_ft", REFERENCE_FEET))         + float(config.get("party_boost_db", 0))
    return min(MAX_TOTAL_BOOST_DB, db)


def _level_from_block(samples, boost_db=0.0):
    """int16 samples -> 0-100, the same scale as the dashboard's input meter
    (-60 dBFS and below is 0, full scale is 100)."""
    if samples.size == 0:
        return 0.0
    # software mic boost: multiplies only what we measure; the music and the
    # Windows mic setting are untouched
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64))))) * (10.0 ** (boost_db / 20.0))
    if rms <= 0:
        return 0.0
    dbfs = 20.0 * np.log10(rms / 32768.0)
    return max(0.0, min(100.0, (dbfs + 60.0) * (100.0 / 60.0)))


def _find_mic(substr):
    import audio_engine as ae
    pa = ae._pa
    devices = ae.list_input_devices()
    if substr:
        for d in devices:
            if substr.lower() in d["name"].lower():
                return d["index"], pa.get_device_info_by_index(d["index"])
        raise RuntimeError(f"microphone matching '{substr}' not found")
    info = pa.get_default_input_device_info()
    return int(info["index"]), info


def _run():
    import audio_engine as ae
    stream = None
    try:
        idx, info = _find_mic(config.get("party_mic", ""))
        rate = int(info.get("defaultSampleRate", 48000))
        channels = max(1, min(int(info.get("maxInputChannels", 1)), 2))
        stream = ae._pa.open(format=ae.pyaudio.paInt16, channels=channels, rate=rate,
                             input=True, input_device_index=idx, frames_per_buffer=ae.CHUNK)
        _set(device=info["name"], error=None)
        print(f"[party] listening to: {info['name']} @ {rate}Hz x{channels}ch")

        chunk_s = ae.CHUNK / float(rate)
        alpha = min(1.0, chunk_s / SMOOTH_SECONDS)
        smoothed = None
        current = 100.0     # where we last set the overall volume
        applied = 100.0
        next_control = time.monotonic() + CONTROL_INTERVAL_S
        while not _stop.is_set():
            data = stream.read(ae.CHUNK, exception_on_overflow=False)
            block = np.frombuffer(data, dtype=np.int16)
            lvl = _level_from_block(block, total_boost_db())
            smoothed = lvl if smoothed is None else smoothed + alpha * (lvl - smoothed)
            _set(level=round(smoothed, 1))

            now = time.monotonic()
            if now >= next_control:
                next_control = now + CONTROL_INTERVAL_S
                lo, hi, full = _settings()
                current = _step_toward(current, target_pct(smoothed, lo, hi, full))
                current = max(lo, min(hi, current))
                if abs(current - applied) >= MIN_APPLY_DELTA_PCT and _apply is not None:
                    _apply(int(round(current)))
                    applied = current
                    _set(volume_pct=int(round(current)))
    except Exception as e:
        print(f"[party] stopped: {type(e).__name__}: {e}")
        _set(error=f"{type(e).__name__}: {e}")
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        # hand the volume back to where the person had it
        if _apply is not None:
            try:
                _apply(100)
            except Exception as e:
                print(f"[party] couldn't restore volume: {e}")
        _set(active=False, level=0.0, volume_pct=100)


def start():
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _status.update(active=True, error=None, level=0.0, volume_pct=100)
    _stop.clear()
    _thread = threading.Thread(target=_run, name="party-mode", daemon=True)
    _thread.start()


def stop():
    _stop.set()
    t = _thread
    if t is not None:
        t.join(timeout=3)
