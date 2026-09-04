"""
Audio capture / delay / render engine.

Pipeline:
  Windows apps -> "CABLE Input" (virtual device, becomes your Windows
  default output) -> we capture from "CABLE Output" (the matching
  virtual recording device) -> fan out to:
      (a) a delayed render thread that writes to your REAL speakers,
          held back by config['local_delay_ms'] so it lines up with
      (b) one HTTP stream per enabled Sonos speaker (undelayed on our
          end -- Sonos adds its own delay on the receiving side).

We never write anything to your real speakers except through this
delayed path, so there is exactly one "instant" copy (Sonos, delayed by
Sonos itself) and one deliberately-delayed copy (your PC speakers) --
tune local_delay_ms until they land together.
"""

import audioop
import queue
import socket
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

from config import config

CHUNK = 1024  # frames per buffer

_pa = pyaudio.PyAudio()


class Broadcaster:
    """Fans out raw PCM chunks to any number of subscribers without
    letting a slow subscriber stall audio capture."""

    def __init__(self):
        self._subs = {}
        self._next_id = 0
        self._lock = threading.Lock()

    def subscribe(self, maxlen=200):
        q = queue.Queue(maxsize=maxlen)
        with self._lock:
            sid = self._next_id
            self._next_id += 1
            self._subs[sid] = q
        return sid, q

    def unsubscribe(self, sid):
        with self._lock:
            self._subs.pop(sid, None)

    def publish(self, chunk):
        with self._lock:
            subs = list(self._subs.items())
        for sid, q in subs:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                # subscriber falling behind (e.g. flaky wifi speaker) --
                # drop the oldest sample rather than build up latency
                try:
                    q.get_nowait()
                    q.put_nowait(chunk)
                except Exception:
                    pass


broadcaster = Broadcaster()


def find_device_index(substr, want_input):
    substr_l = substr.lower()
    for i in range(_pa.get_device_count()):
        info = _pa.get_device_info_by_index(i)
        name = info.get("name", "")
        if substr_l in name.lower():
            if want_input and info.get("maxInputChannels", 0) > 0:
                return i, info
            if not want_input and info.get("maxOutputChannels", 0) > 0:
                return i, info
    return None, None


# Substrings of known VIRTUAL/software output devices that are never what
# a user means by "my speakers" -- these have caused real, confusing bugs
# before (e.g. Steam's virtual mic silently getting picked as the render
# device). Auto-pick skips anything matching these; the dashboard's device
# dropdown lets the user override explicitly regardless of this list.
_VIRTUAL_DEVICE_BLOCKLIST = [
    "cable", "vb-audio", "steam streaming", "voicemeeter", "virtual",
    "voicemod", "nvidia broadcast", "wave link", "loopback",
]


def _looks_virtual(name):
    name_l = name.lower()
    return any(bad in name_l for bad in _VIRTUAL_DEVICE_BLOCKLIST)


def list_output_devices():
    """All real WASAPI output devices, for the dashboard's device picker."""
    try:
        wasapi_info = _pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except Exception:
        wasapi_info = None
    out = []
    for i in range(_pa.get_device_count()):
        info = _pa.get_device_info_by_index(i)
        if wasapi_info and info.get("hostApi") != wasapi_info["index"]:
            continue
        if info.get("maxOutputChannels", 0) <= 0:
            continue
        name = info.get("name", "")
        out.append({"index": i, "name": name, "likely_virtual": _looks_virtual(name)})
    return out


def auto_pick_render_device():
    """First real (non-virtual) WASAPI output device -- i.e. your
    physical speakers/headphones, not the CABLE virtual device or other
    known virtual/software outputs. Best-effort only -- if this picks
    wrong, use the dashboard's device dropdown to override it."""
    try:
        wasapi_info = _pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except Exception:
        wasapi_info = None
    for i in range(_pa.get_device_count()):
        info = _pa.get_device_info_by_index(i)
        if wasapi_info and info.get("hostApi") != wasapi_info["index"]:
            continue
        if info.get("maxOutputChannels", 0) <= 0:
            continue
        if _looks_virtual(info.get("name", "")):
            continue
        return i, info
    return None, None


def get_pyaudio():
    """The shared PyAudio instance, for modules (like calibration.py) that
    need to open their own extra stream -- a microphone, in that case --
    without each opening a second, separate PyAudio host and risking two
    different views of the device list."""
    return _pa


def _source_ip_for(target):
    """The local IP the OS would use as the source address to reach
    `target`. No packet is actually sent -- connect() on a UDP socket
    just does the route lookup."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def get_lan_ip():
    """This PC's LAN IP -- the address Sonos speakers fetch the stream
    from, so it has to be the one reachable FROM the speakers.

    When the speakers sit on another subnet/VLAN and this PC has more
    than one interface (Wi-Fi + Ethernet, a VPN, Docker, etc.), the
    route to the internet and the route to the speakers can leave from
    different NICs with different IPs. Ask the routing table which
    source IP it would use to reach an actual speaker first, and only
    fall back to the internet-facing IP when we don't know one yet."""
    targets = []
    if config.get("default_speaker_ip"):
        targets.append(config["default_speaker_ip"])
    targets += list(config.get("sonos_seed_ips") or [])
    try:
        from sonos_ctl import speaker_mgr
        targets += speaker_mgr.known_ips()
    except Exception:
        pass
    targets.append("8.8.8.8")
    for target in targets:
        target = str(target).strip()
        if not target:
            continue
        try:
            return _source_ip_for(target)
        except Exception:
            continue
    return "127.0.0.1"


def capture_loop(stop_event):
    """Dispatches to whole-system or per-application capture based on
    config['capture_mode'], and re-dispatches every time the underlying
    loop returns (target app not running yet, or it just closed) so
    switching modes or waiting for an app to launch doesn't require
    restarting the thread from outside."""
    while not stop_event.is_set():
        if config.get("capture_mode") == "process" and config.get("capture_target_name"):
            _capture_loop_process(stop_event)
            if stop_event.is_set():
                return
            time.sleep(2)  # target not running (yet) -- keep checking
            continue
        _capture_loop_system(stop_event)
        return  # only returns on stop_event or a permanently-missing cable


def _capture_loop_process(stop_event):
    """One attempt at per-application capture (see per_app_audio.py) of
    config['capture_target_name']. Returns (without raising) as soon as
    that stops working for any reason -- the app closed, activation was
    refused for it, or capture_loop's caller switched modes -- so the
    caller can decide what to do next; this deliberately does NOT fall
    back to whole-system capture on failure, since silently streaming
    every app's audio when the user asked to scope this to just one
    would violate the whole point of picking a specific app."""
    try:
        import per_app_audio
    except Exception as e:
        print(f"[audio] per-app capture unavailable on this system ({e}); "
              f"switching back to whole-system capture")
        config["capture_mode"] = "system"
        return

    target_name = config["capture_target_name"]
    try:
        sessions = per_app_audio.list_audio_sessions()
    except Exception as e:
        # a real (importable, otherwise working) per_app_audio hit a
        # transient error listing sessions -- e.g. a COM hiccup -- don't
        # punish that by disabling the feature; just retry like "not
        # found yet" does
        print(f"[audio] couldn't list audio sessions: {e}")
        return
    pid = next((s["pid"] for s in sessions if s["name"].lower() == target_name.lower()), None)
    if pid is None:
        return  # not running (yet) -- capture_loop will retry shortly

    configured = {"done": False}

    def on_chunk(pcm, rate, channels, width):
        if not configured["done"]:
            config["sample_rate"] = rate
            config["channels"] = channels
            configured["done"] = True
        broadcaster.publish(pcm)

    print(f"[audio] capturing '{target_name}' (pid {pid}) only -- "
          f"everything else on this PC stays out of the Sonos stream")
    try:
        per_app_audio.capture_loop(pid, stop_event, on_chunk)
    except Exception as e:
        print(f"[audio] per-app capture of '{target_name}' failed: {e}")


def _capture_loop_system(stop_event):
    """Reads PCM from the virtual cable and publishes it to the
    broadcaster -- the single source both the Sonos streams and the local
    delayed-render path draw from, so if this stops, everything downstream
    goes silent no matter what any setting (including the delay) is.

    Self-healing: if the capture stream ever errors out for good (a
    driver hiccup, the device briefly grabbed elsewhere, sleep/wake),
    reopen it from scratch instead of retrying reads against an
    already-dead stream object forever. That used to be exactly what
    happened -- one "Unanticipated host error" and every subsequent read
    just raised "Stream closed" in an infinite loop, silently killing all
    audio (to both Sonos and the local speakers) for the rest of the run
    with no recovery short of relaunching the whole app by hand."""
    attempt = 0
    while not stop_event.is_set():
        idx, info = find_device_index(config["capture_device_substr"], want_input=True)
        if idx is None:
            print(f"[audio] capture device matching '{config['capture_device_substr']}' "
                  f"not found -- install VB-Audio Virtual Cable and set it as your "
                  f"Windows default playback device (see README.md)")
            return

        rate = int(info.get("defaultSampleRate", config["sample_rate"]))
        channels = min(int(info.get("maxInputChannels", 2)), 2)
        config["sample_rate"] = rate
        config["channels"] = channels

        try:
            stream = _pa.open(format=pyaudio.paInt16, channels=channels, rate=rate,
                               input=True, input_device_index=idx, frames_per_buffer=CHUNK)
        except Exception as e:
            attempt += 1
            wait = min(2 * attempt, 10)
            print(f"[audio] capture device open failed ({e}); retrying in {wait}s (attempt {attempt})")
            time.sleep(wait)
            continue

        print(f"[audio] capturing from: {info['name']} @ {rate}Hz x{channels}ch")
        attempt = 0
        consecutive_errors = 0

        while not stop_event.is_set() and consecutive_errors < 5:
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
            except Exception as e:
                consecutive_errors += 1
                print(f"[audio] capture error: {e}")
                time.sleep(0.5)
                continue
            consecutive_errors = 0
            broadcaster.publish(data)

        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass

        if consecutive_errors >= 5:
            print("[audio] capture stream looks dead after repeated errors; reopening it")
            time.sleep(0.5)


current_render_device_name = None  # not persisted -- see get_current_render_device_name()


def get_current_render_device_name():
    """What's actually in use right now, whether auto-picked or explicitly
    chosen -- distinct from config['render_device_substr'], which stays
    blank unless the user picked a device by hand (so a bad auto-pick, like
    grabbing a virtual device, never silently becomes 'sticky')."""
    return current_render_device_name


class _NoRenderDevice(Exception):
    """Raised by _render_session when there's no real output device to use
    at all -- distinct from a transient open/write failure, this shouldn't
    be retried."""


_GAIN_KNEE = 0.7  # start compressing at 70% of full scale (~ -3dBFS)


def _apply_local_gain(pcm_bytes, gain):
    """Amplifies 16-bit PCM by `gain`, soft-limiting instead of hard-
    clipping as the signal approaches full scale.

    A plain linear multiply (audioop.mul) hard-clips: the instant any
    sample crosses the ceiling, it gets slammed flat rather than
    compressed, which sounds like a sudden jump into harsh distortion --
    disproportionately loud to the ear regardless of how small the
    nominal gain increase was. That bites hard here because a lot of
    real source audio already sits close to full scale (apps commonly
    master near 0dBFS), so even a modest boost could push a meaningful
    chunk of samples straight into that wall. A soft knee lets loud
    peaks compress gradually above _GAIN_KNEE instead, so raising the
    slider actually feels like a smooth volume increase across its
    whole range instead of "clean, then suddenly blown out."""
    arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) * (gain / 32768.0)
    mag = np.abs(arr)
    over = mag > _GAIN_KNEE
    if np.any(over):
        excess = mag[over] - _GAIN_KNEE
        compressed = _GAIN_KNEE + np.tanh(excess / (1.0 - _GAIN_KNEE)) * (1.0 - _GAIN_KNEE)
        arr[over] = np.sign(arr[over]) * compressed
    arr = np.clip(arr, -1.0, 1.0) * 32767.0
    return arr.astype(np.int16).tobytes()


def render_loop(stop_event):
    """Plays the SAME audio back out to your real speakers, held behind
    by config['local_delay_ms'] milliseconds, so it lines up with the
    (slower) Sonos playback instead of echoing ahead of it.

    Opening/writing to the real-speaker WASAPI stream can fail at any
    point -- e.g. "Invalid sample rate" right at app startup if the device
    hasn't finished settling into shared-mode format yet, or a write error
    later if the device sleeps/disconnects. Previously any such exception
    just killed this thread for the rest of the run, so local delayed
    playback silently stayed off unless the user happened to nudge the
    delay slider or device dropdown (which calls restart_render() and got a
    fresh, usually-successful attempt). Retry here instead, the same
    self-healing pattern used elsewhere in the app (Sonos discovery, the
    stream watchdog)."""
    global current_render_device_name
    attempt = 0
    while not stop_event.is_set():
        try:
            _render_session(stop_event)
            return  # clean stop_event exit
        except _NoRenderDevice:
            current_render_device_name = None
            return
        except Exception as e:
            attempt += 1
            wait = min(2 * attempt, 10)
            current_render_device_name = None
            print(f"[audio] render loop error ({e}); retrying in {wait}s (attempt {attempt})")
            time.sleep(wait)


def _render_session(stop_event):
    global current_render_device_name
    render_substr = config.get("render_device_substr") or ""
    if render_substr:
        idx, info = find_device_index(render_substr, want_input=False)
    else:
        idx, info = auto_pick_render_device()

    if idx is None:
        print("[audio] no local render device found; delayed local playback disabled")
        raise _NoRenderDevice()

    # wait for capture_loop to settle sample rate/channels
    time.sleep(0.5)
    capture_rate = config["sample_rate"]
    capture_channels = config["channels"]
    sample_width = config["sample_width"]

    # The capture device (the virtual cable) and this render device (your
    # real speakers) are two different endpoints and can have different
    # native sample rates (e.g. the cable at 44100Hz, real speakers at
    # 48000Hz). Windows' WASAPI flatly refuses to open a shared-mode stream
    # at a rate the device doesn't natively run at ("Invalid sample rate"),
    # so we always open at THIS device's own native rate and resample the
    # audio to match before writing to it.
    render_rate = int(info.get("defaultSampleRate", capture_rate))
    render_channels = min(capture_channels, int(info.get("maxOutputChannels", capture_channels)) or capture_channels)

    stream = _pa.open(format=pyaudio.paInt16, channels=render_channels, rate=render_rate,
                       output=True, output_device_index=idx, frames_per_buffer=CHUNK)
    current_render_device_name = info["name"]
    print(f"[audio] rendering (delayed) to: {info['name']} "
          f"@ {render_rate}Hz x{render_channels}ch (capture is {capture_rate}Hz x{capture_channels}ch)")

    needs_resample = (render_rate != capture_rate)
    needs_downmix = (render_channels == 1 and capture_channels == 2)
    resample_state = None

    sid, q = broadcaster.subscribe(maxlen=4000)
    # buffering/delay timing is tracked in terms of the CAPTURE stream's
    # byte rate, since that's the rate audio arrives at from the broadcaster
    bytes_per_ms = capture_rate * capture_channels * sample_width / 1000.0
    buf = bytearray()
    frame_bytes = CHUNK * capture_channels * sample_width

    try:
        while not stop_event.is_set():
            target_bytes = int(bytes_per_ms * config["local_delay_ms"])
            # Windows' own volume mixer only controls what's captured INTO
            # the virtual cable -- it has no effect on what this render
            # path plays back afterward, so an aux/line-level speaker that
            # needs more than that source signal provides has no other
            # knob to turn. Gain is applied (see _apply_local_gain) here,
            # after resampling, right before the device write.
            gain = config.get("local_render_gain", 1.0)
            try:
                chunk = q.get(timeout=1)
            except queue.Empty:
                continue
            buf.extend(chunk)

            # drift guard: if we've drifted more than ~200ms above target
            # (capture outrunning render), trim the excess so the delay
            # doesn't silently grow over a long playback session
            overflow = len(buf) - target_bytes
            if overflow > bytes_per_ms * 200:
                trim = int(overflow - bytes_per_ms * 50)
                trim -= trim % (capture_channels * sample_width)
                if trim > 0:
                    del buf[:trim]

            while len(buf) >= max(target_bytes, 0) + frame_bytes:
                out = bytes(buf[:frame_bytes])
                del buf[:frame_bytes]
                if needs_downmix:
                    out = audioop.tomono(out, sample_width, 0.5, 0.5)
                if needs_resample:
                    out, resample_state = audioop.ratecv(
                        out, sample_width, render_channels,
                        capture_rate, render_rate, resample_state)
                if out and gain != 1.0:
                    out = _apply_local_gain(out, gain)
                if out:
                    stream.write(out)
    finally:
        broadcaster.unsubscribe(sid)
        stream.stop_stream()
        stream.close()


_render_stop_event = None
_render_thread = None
_render_lock = threading.Lock()

_capture_stop_event = None
_capture_thread = None
_capture_lock = threading.Lock()


def start_audio_engine(stop_event):
    global _render_stop_event, _render_thread, _capture_stop_event, _capture_thread
    with _capture_lock:
        _capture_stop_event = threading.Event()
        _capture_thread = threading.Thread(target=capture_loop, args=(_capture_stop_event,), daemon=True)
        _capture_thread.start()

    with _render_lock:
        _render_stop_event = threading.Event()
        _render_thread = threading.Thread(target=render_loop, args=(_render_stop_event,), daemon=True)
        _render_thread.start()


def restart_capture(new_mode=None, new_target_name=None):
    """Stop the current capture thread and start a new one, picking up a
    newly-chosen audio source (whole system vs. one application). Used by
    the dashboard's audio-source picker.

    Whole-system and per-app capture run at different, hardcoded sample
    rates (see per_app_audio.py) -- so switching between them changes the
    actual PCM format on the fly. Restart the render thread (it only reads
    config['sample_rate']/['channels'] once, at the start of each render
    session) and force every currently-streaming Sonos speaker to
    reconnect (its WAV header was generated from whatever the format was
    when THAT connection started, and there's no way to update it mid-
    stream) so nothing downstream is left decoding new-format bytes
    against a stale format."""
    global _capture_stop_event, _capture_thread
    from config import save_config
    if new_mode is not None:
        config["capture_mode"] = new_mode
    if new_target_name is not None:
        config["capture_target_name"] = new_target_name
    if new_mode is not None or new_target_name is not None:
        save_config(config)
    with _capture_lock:
        if _capture_stop_event is not None:
            _capture_stop_event.set()
        if _capture_thread is not None:
            _capture_thread.join(timeout=3)
        _capture_stop_event = threading.Event()
        _capture_thread = threading.Thread(target=capture_loop, args=(_capture_stop_event,), daemon=True)
        _capture_thread.start()
    restart_render()
    try:
        from sonos_ctl import speaker_mgr
        speaker_mgr.reconnect_all_streaming(f"http://{get_lan_ip()}:{config['http_port']}")
    except Exception as e:
        print(f"[audio] couldn't resync Sonos streams after a capture-source change: {e}")


def restart_render(new_device_substr=None):
    """Stop the current delayed-local-playback thread and start a new one,
    picking up either a newly-chosen render device or a changed delay.
    Used when the dashboard's device dropdown or delay slider changes."""
    global _render_stop_event, _render_thread
    from config import save_config
    if new_device_substr is not None:
        config["render_device_substr"] = new_device_substr
        save_config(config)
    with _render_lock:
        if _render_stop_event is not None:
            _render_stop_event.set()
        if _render_thread is not None:
            _render_thread.join(timeout=3)
        _render_stop_event = threading.Event()
        _render_thread = threading.Thread(target=render_loop, args=(_render_stop_event,), daemon=True)
        _render_thread.start()
