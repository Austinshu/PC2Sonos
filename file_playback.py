"""Play an uploaded audio file through the exact same pipeline live capture
uses. The broadcaster, Sonos streams, and delayed local speaker playback
don't care whether the PCM they're fed came from live system audio or a
file -- so playing a file is really just a different capture source that
temporarily replaces the live one, reusing all of the same downstream sync,
per-speaker volumes, and per-device local outputs.

Decodes with soundfile, which bundles libsndfile 1.1+ -- that covers WAV,
FLAC, OGG, and MP3 without needing ffmpeg or any other external tool
installed, matching the rest of this app's single-file-exe, nothing-else-
to-install approach.
"""

import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

CHUNK = 1024  # matches audio_engine.CHUNK

_status = {"playing": False, "filename": "", "position_s": 0.0, "duration_s": 0.0}
_status_lock = threading.Lock()

_stop_event = None
_thread = None
_lock = threading.Lock()


def get_status():
    with _status_lock:
        return dict(_status)


def _set_status(**kwargs):
    with _status_lock:
        _status.update(kwargs)


def _play_loop(path, stop_event):
    """Every exit from this function -- a file that fails to even open, a
    read/decode error mid-playback, reaching the end of the file, or an
    explicit stop_event.set() -- funnels through the same tail below, so
    "was this an unrequested exit?" (and therefore whether to hand capture
    back automatically) is decided in exactly one place instead of once per
    early-return."""
    import audio_engine

    f = None
    try:
        f = sf.SoundFile(str(path))
        rate = f.samplerate
        # Always publish stereo, same convention _capture_loop_apps already
        # uses for its own mixed sources (_APP_CAPTURE_CHANNELS) -- the rest
        # of the pipeline (EQ, downmix-for-mono-output, etc.) is written and
        # tested around stereo capture, so a mono or surround file is
        # converted here rather than letting an unusual channel count flow
        # downstream.
        channels = 2
        duration_s = len(f) / float(rate) if rate else 0.0
        audio_engine._publish_capture_format(rate, channels)
        audio_engine._set_capture_status("file", path.name, rate, channels)
        _set_status(playing=True, filename=path.name, position_s=0.0, duration_s=duration_s)

        if not rate:
            raise ValueError("file reports a 0 sample rate")
        chunk_seconds = CHUNK / float(rate)
        next_tick = time.monotonic()
        frames_played = 0
        while not stop_event.is_set():
            block = f.read(CHUNK, dtype="int16", always_2d=True)
            if len(block) == 0:
                break  # end of file
            if block.shape[1] == 1:
                block = np.repeat(block, 2, axis=1)  # mono -> duplicated stereo
            elif block.shape[1] > 2:
                block = block[:, :2]  # 5.1/7.1 etc. -> just the first two channels
            audio_engine.broadcaster.publish(np.ascontiguousarray(block).tobytes())
            frames_played += len(block)
            _set_status(position_s=frames_played / float(rate))

            next_tick += chunk_seconds
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()  # fell behind -- resync instead of free-running
    except Exception as e:
        print(f"[file_playback] couldn't play '{path.name}': {e}")
    finally:
        if f is not None:
            f.close()
        audio_engine._set_capture_status()
        _set_status(playing=False, filename="", position_s=0.0, duration_s=0.0)
    if not stop_event.is_set():
        # Ended on its own -- file wouldn't open, a decode error, or a clean
        # end of file -- rather than being told to stop. Hand capture back
        # to the live source automatically, the same as stop() does for an
        # explicit Stop, so a bad upload doesn't leave Sonos/local speakers
        # silently silent.
        audio_engine.restart_capture()


def start(path: Path):
    """Stops live capture (and any external-input capture) and plays `path`
    instead, through the same broadcaster -> Sonos streams -> delayed local
    speakers pipeline. Call stop() (or let the file finish) to hand capture
    back to the live system/app source -- resumed exactly as it was
    configured, since this never touches config['capture_mode'] or its
    target list."""
    import audio_engine

    global _stop_event, _thread
    with audio_engine._source_lock:
        audio_engine.stop_external_input(resume_capture=False)
        with _lock:
            _stop_locked()
            with audio_engine._capture_lock:
                if audio_engine._capture_stop_event is not None:
                    audio_engine._capture_stop_event.set()
                if audio_engine._capture_thread is not None:
                    audio_engine._capture_thread.join(timeout=3)
            _stop_event = threading.Event()
            _thread = threading.Thread(target=_play_loop, args=(path, _stop_event), daemon=True)
            _thread.start()


def _stop_locked():
    """Body of stop(), for callers that already hold _lock (start(), to
    cleanly replace anything already playing before taking over capture).
    Safe to call whether or not a thread is actually still running --
    joining an already-finished thread returns immediately."""
    global _stop_event, _thread
    if _stop_event is not None:
        _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=3)
    _stop_event = None
    _thread = None


def stop(resume_capture=True):
    """Stops file playback (no-op if nothing's actually still playing --
    checked with is_alive(), not just "is there a thread object", since a
    file that already ended on its own leaves a finished-but-not-yet-
    cleared thread behind) and hands capture back to the normal live
    system/app source. resume_capture=False is for callers (restart_capture
    itself) that are about to start live capture anyway, so a redundant
    resume here would just be immediately undone.

    Also safe to call from _play_loop's OWN thread (restart_capture(), at
    the natural-end tail below, calls this with resume_capture=False) --
    a thread can't join itself, so that case just clears the bookkeeping
    instead of trying to."""
    import audio_engine

    global _stop_event, _thread
    current = threading.current_thread()
    with _lock:
        thread = _thread
        if thread is current:
            was_playing = False
            _stop_event = None
            _thread = None
        else:
            was_playing = thread is not None and thread.is_alive()
            _stop_locked()
    if was_playing and resume_capture:
        audio_engine.restart_capture()
