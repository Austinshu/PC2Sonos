"""
Audio capture / delay / render engine.

Pipeline:
  Windows apps -> "CABLE Input" (virtual device, becomes your Windows
  default output) -> we capture what's playing into it via WASAPI
  loopback (or, if loopback can't be used, from "CABLE Output", the
  matching virtual recording device -- see _capture_loop_system for why
  loopback is preferred) -> fan out to:
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
import math
import queue
import re
import socket
import sys
import threading
import time

import numpy as np
import audio_backend as pyaudio  # pyaudiowpatch on Windows, sounddevice on macOS

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
        # 0-100 meter reading of the last published chunk, for the
        # dashboard's live level indicator. Computed here rather than in
        # each capture loop so every capture mode (whole-system, per-app)
        # gets it for free from the one point they all already funnel
        # through -- no lock needed, a single float assignment is atomic
        # under the GIL and this is a best-effort UI reading, not
        # something anything downstream depends on being exact.
        self.level_pct = 0.0

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
        self._update_level(chunk)
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

    def _update_level(self, chunk):
        """RMS loudness of this chunk as a 0-100 meter reading. -50dBFS
        (quiet) maps to 0, 0dBFS (full scale) maps to 100 -- audio level
        is perceived logarithmically, so this reads far more like a real
        VU meter than a linear amplitude scale would."""
        try:
            rms = audioop.rms(chunk, 2)  # 2 = 16-bit PCM, the only format used anywhere in this app
        except Exception:
            rms = 0
        if rms <= 0:
            self.level_pct = 0.0
            return
        dbfs = 20 * math.log10(rms / 32768.0)
        self.level_pct = max(0.0, min(100.0, (dbfs + 50.0) * 2.0))


broadcaster = Broadcaster()


def find_device_index(substr, want_input):
    """Look up a device by (substring of) name, as picked from the
    dashboard's dropdown.

    Windows exposes the same physical device once per host API it
    supports (MME, DirectSound, WASAPI, WDM-KS) -- PyAudio enumerates
    all of them under the same name. For output, the dropdown only ever
    shows WASAPI devices (see list_output_devices), so the lookup here
    must stay within WASAPI too, or a name match can silently resolve to
    a different host API's copy of the device (e.g. the legacy MME
    entry, which on some driver stacks opens and writes without error
    but produces no audible output)."""
    substr_l = substr.lower()
    wasapi_info = None
    if not want_input:
        try:
            wasapi_info = _pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except Exception:
            wasapi_info = None
    for i in range(_pa.get_device_count()):
        info = _pa.get_device_info_by_index(i)
        if wasapi_info and info.get("hostApi") != wasapi_info["index"]:
            continue
        name = info.get("name", "")
        if substr_l in name.lower():
            # pyaudiowpatch lists every playback device's loopback endpoint
            # as an input too. That's find_loopback_device's job -- this
            # lookup is for real recording devices, and opening a loopback
            # one through the blocking recording read would hang whenever
            # nothing is playing to it.
            if want_input and info.get("maxInputChannels", 0) > 0 and not info.get("isLoopbackDevice"):
                return i, info
            if not want_input and info.get("maxOutputChannels", 0) > 0:
                return i, info
    return None, None


def find_loopback_device(substr):
    """The WASAPI loopback endpoint of the playback device whose name
    contains `substr`. pyaudiowpatch lists one of these next to every
    playback device, named "<device> [Loopback]" and flagged
    isLoopbackDevice; there is no such flag on macOS's sounddevice
    wrapper, so this simply finds nothing there."""
    substr_l = substr.lower()
    for i in range(_pa.get_device_count()):
        info = _pa.get_device_info_by_index(i)
        if (info.get("isLoopbackDevice") and info.get("maxInputChannels", 0) > 0
                and substr_l in info.get("name", "").lower()):
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
    # macOS virtual/software outputs (BlackHole is our own capture device there)
    "blackhole", "soundflower", "aggregate", "multi-output", "zoomaudio",
    "microsoft teams audio", "existential audio", "background music",
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


_scheduling_status = {"throttling_opt_out": None, "timer_1ms": None, "mmcss_threads": 0}
_scheduling_lock = threading.Lock()


def _harden_audio_scheduling():
    """Windows only: stop Windows treating this as the background app it looks
    like. PC2Sonos has no window, and Windows 11 deprioritises windowless
    background processes -- runs them on slow efficiency cores and coalesces
    their timers -- which is fine for most apps and fatal for one that has to
    finish a chunk of audio every 21ms, forever. On a real PC it made the
    speaker path run a fraction of a percent slower than real time, so the PC
    speakers slid further behind the audio every second (and glitched whenever
    they ran dry), with no window on screen to explain it.

    Three things, all of which audio apps are expected to declare: opt this
    process out of execution-speed throttling, keep its 1ms timer request
    honoured even while it is windowless, and ask for that 1ms timer
    resolution in the first place (the default is 15.6ms, which also makes
    Windows hand the loopback stream over in uneven bursts -- see
    _read_loop_loopback). Every step is best-effort: an older Windows without
    them just carries on as before."""
    if sys.platform != "win32":
        return
    with _scheduling_lock:
        if _scheduling_status["throttling_opt_out"] is not None:
            return  # once per process
        _scheduling_status["throttling_opt_out"] = False
        _scheduling_status["timer_1ms"] = False
        try:
            import ctypes
            from ctypes import wintypes

            class _PowerThrottling(ctypes.Structure):
                _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                            ("StateMask", wintypes.ULONG)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
            # ProcessPowerThrottling = 4; control both EXECUTION_SPEED (0x1) and
            # IGNORE_TIMER_RESOLUTION (0x4) and set neither = throttling off
            state = _PowerThrottling(1, 0x1 | 0x4, 0)
            _scheduling_status["throttling_opt_out"] = bool(kernel32.SetProcessInformation(
                kernel32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state)))
        except Exception as e:
            print(f"[audio] couldn't opt out of Windows background throttling: {e}")
        try:
            import ctypes
            _scheduling_status["timer_1ms"] = ctypes.WinDLL("winmm").timeBeginPeriod(1) == 0
        except Exception as e:
            print(f"[audio] couldn't request a 1ms timer: {e}")
    print(f"[audio] scheduling: background throttling opt-out "
          f"{'on' if _scheduling_status['throttling_opt_out'] else 'unavailable'}, "
          f"1ms timer {'on' if _scheduling_status['timer_1ms'] else 'unavailable'}")


def _register_audio_thread():
    """Windows only: register the calling thread with the multimedia
    scheduler as a "Pro Audio" thread, the way any real-time audio thread is
    meant to (PortAudio does it for its own threads; our capture and render
    threads are separate Python threads and need it too). It keeps them
    running on time when the machine is busy. Best-effort, and a no-op
    anywhere else."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        avrt = ctypes.WinDLL("avrt", use_last_error=True)
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        task_index = wintypes.DWORD(0)
        if avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(task_index)):
            with _scheduling_lock:
                _scheduling_status["mmcss_threads"] += 1
    except Exception:
        pass


def get_scheduling_status():
    with _scheduling_lock:
        return dict(_scheduling_status)


def capture_loop(stop_event):
    """Dispatches to whole-system or per-application capture based on
    config['capture_mode'], and re-dispatches every time the underlying
    loop returns (no selected app running yet, or the last one just
    closed) so switching modes or waiting for an app to launch doesn't
    require restarting the thread from outside."""
    _register_audio_thread()
    while not stop_event.is_set():
        if config.get("capture_mode") == "apps" and config.get("capture_target_names"):
            _capture_loop_apps(stop_event)
            if stop_event.is_set():
                return
            time.sleep(2)  # nothing selected is running (yet) -- keep checking
            continue
        _capture_loop_system(stop_event)
        return  # only returns on stop_event or a permanently-missing cable


# Every process-loopback capture uses this same hardcoded format (see
# activate_process_loopback_client in per_app_audio.py -- GetMixFormat()
# isn't available on this kind of stream, so Windows' own internal mix
# format is used for all of them) -- meaning multiple selected apps can
# always be mixed by simple sample-by-sample addition, with no resampling
# step needed between sources.
_APP_CAPTURE_RATE = 48000
_APP_CAPTURE_CHANNELS = 2
_APP_CAPTURE_WIDTH = 2  # bytes (16-bit PCM, after per_app_audio's own float->int16 conversion)


class _AppSource:
    """One selected app's live capture: its own background thread reading
    from per_app_audio, feeding a small jitter buffer that _capture_loop_apps'
    mixer drains at a fixed cadence. Kept separate per app so one app
    stalling, closing, or never having been capturable in the first place
    can't affect any of the others still in the mix."""

    def __init__(self, name):
        self.name = name
        self.buf = bytearray()
        self.buf_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def on_chunk(self, pcm, *_rate_channels_width):
        with self.buf_lock:
            self.buf.extend(pcm)
            # If the mixer ever falls behind (shouldn't happen in normal
            # operation), don't let one slow/stalled source grow without
            # bound -- cap at ~1s and drop the oldest audio.
            max_bytes = _APP_CAPTURE_RATE * _APP_CAPTURE_CHANNELS * _APP_CAPTURE_WIDTH
            if len(self.buf) > max_bytes:
                del self.buf[:len(self.buf) - max_bytes]

    def take(self, n_bytes):
        """Returns exactly n_bytes, silence-padding if this source hasn't
        buffered enough yet (e.g. it just started) rather than stalling
        the whole mix waiting for it."""
        with self.buf_lock:
            if len(self.buf) >= n_bytes:
                data = bytes(self.buf[:n_bytes])
                del self.buf[:n_bytes]
                return data
            data = bytes(self.buf) + b"\x00" * (n_bytes - len(self.buf))
            self.buf.clear()
            return data


_APP_RESCAN_INTERVAL_S = 1.0  # how often to check for selected apps launching/closing


def _capture_loop_apps(stop_event):
    """Per-application capture of one or more selected apps at once (see
    config['capture_target_names']), mixed together into a single stream.
    Each configured app gets its own dedicated per_app_audio capture
    thread that starts as soon as that app is found running and stops
    (without affecting any others still active) when it exits or its
    capture fails -- so, unlike whole-system capture, a missing or
    uncapturable app is never a fatal error, just one fewer source in the
    mix. Falls back to whole-system capture if per-app capture isn't
    available on this system at all.

    Returns (without raising, exactly like _capture_loop_process used to)
    as soon as NONE of the selected apps are currently running/capturable
    -- both right at the start, and later if every source that had joined
    the mix has since dropped out -- so the caller's retry-every-2s loop
    takes over instead of this busy-polling session lists on its own
    forever."""
    try:
        import per_app_audio
    except Exception as e:
        print(f"[audio] per-app capture unavailable on this system ({e}); "
              f"switching back to whole-system capture")
        config["capture_mode"] = "system"
        return

    targets = list(config.get("capture_target_names") or [])
    if not targets:
        return

    try:
        sessions = per_app_audio.list_audio_sessions()
    except Exception as e:
        # a real (importable, otherwise working) per_app_audio hit a
        # transient error listing sessions -- e.g. a COM hiccup -- don't
        # punish that by disabling the feature; just retry like "not
        # found yet" does
        print(f"[audio] couldn't list audio sessions: {e}")
        return
    live_pids = {s["name"].lower(): s["pid"] for s in sessions}

    _publish_capture_format(_APP_CAPTURE_RATE, _APP_CAPTURE_CHANNELS)

    def run_source(src, pid):
        try:
            per_app_audio.capture_loop(pid, src.stop_event, src.on_chunk)
        except Exception as e:
            print(f"[audio] per-app capture of '{src.name}' failed: {e}")

    def _try_start(name, sources):
        key = name.lower()
        if key in sources:
            return
        pid = live_pids.get(key)
        if pid is None:
            return
        src = _AppSource(name)
        src.thread = threading.Thread(target=run_source, args=(src, pid), daemon=True)
        sources[key] = src
        src.thread.start()
        print(f"[audio] '{name}' (pid {pid}) joined the mix")

    sources = {}  # lowercased exe name -> _AppSource
    for name in targets:
        _try_start(name, sources)
    if not sources:
        return  # none of the selected apps are running (yet) -- caller retries shortly

    frame_bytes = _APP_CAPTURE_CHANNELS * _APP_CAPTURE_WIDTH
    chunk_bytes = CHUNK * frame_bytes
    chunk_seconds = CHUNK / _APP_CAPTURE_RATE

    print(f"[audio] mixing {len(sources)}/{len(targets)} selected app(s) into the Sonos "
          f"stream -- everything else on this PC stays out of it")
    _set_capture_status("apps", "selected apps (process loopback)",
                        _APP_CAPTURE_RATE, _APP_CAPTURE_CHANNELS)
    try:
        next_tick = time.monotonic()
        next_rescan = next_tick + _APP_RESCAN_INTERVAL_S
        while not stop_event.is_set():
            now = time.monotonic()
            if now >= next_rescan:
                next_rescan = now + _APP_RESCAN_INTERVAL_S
                try:
                    live_pids = {s["name"].lower(): s["pid"]
                                 for s in per_app_audio.list_audio_sessions()}
                except Exception as e:
                    print(f"[audio] couldn't list audio sessions: {e}")
                    live_pids = {}
                for name in targets:
                    _try_start(name, sources)
                for key in list(sources):
                    src = sources[key]
                    if not src.thread.is_alive():
                        del sources[key]
                        print(f"[audio] '{src.name}' dropped out of the mix")
                if not sources:
                    return  # every source that was in the mix is gone -- caller retries

            mixed = np.zeros(CHUNK * _APP_CAPTURE_CHANNELS, dtype=np.int32)
            for src in sources.values():
                mixed += np.frombuffer(src.take(chunk_bytes), dtype=np.int16).astype(np.int32)
            # Summing multiple full-scale sources can exceed int16 range --
            # soft-limit (see _soft_limit) rather than hard-clip, the same
            # treatment already used for the local gain/EQ path.
            normalized = mixed.astype(np.float32) / 32768.0
            broadcaster.publish((_soft_limit(normalized) * 32767.0).astype(np.int16).tobytes())

            next_tick += chunk_seconds
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()  # fell behind -- resync instead of free-running
    finally:
        _set_capture_status()
        for src in sources.values():
            src.stop_event.set()
        for src in sources.values():
            if src.thread:
                src.thread.join(timeout=2)


# What the system-capture loop is reading from right now, for the
# dashboard and the diagnostics snapshot. "method" is what is actually in
# use ("loopback", "recording", or "apps"), which differs from
# config["capture_method"] whenever loopback had to fall back -- and
# "fallback_reason" says why.
_capture_status = {"method": None, "device": None, "rate": None,
                   "channels": None, "fallback_reason": None}


def _set_capture_status(method=None, device=None, rate=None, channels=None, fallback_reason=None):
    _capture_status.update(method=method, device=device, rate=rate,
                           channels=channels, fallback_reason=fallback_reason)


def _restart_downstream():
    """Restart everything that latched the capture format when it started:
    the delayed local render session (it reads config['sample_rate'] and
    ['channels'] once per session) and every Sonos stream currently
    playing ours (its WAV header was written from the format at the moment
    THAT connection began, and can't be corrected mid-stream). Without
    this, they'd keep decoding the new capture bytes as the old format.

    Sonos goes first. At startup nothing is streaming yet, so that's a
    no-op, and doing it BEFORE the slower render restart means a stream the
    launch sequence is just about to start begins with the right format,
    instead of being knocked over halfway through starting."""
    try:
        from sonos_ctl import speaker_mgr
        speaker_mgr.reconnect_all_streaming(f"http://{get_lan_ip()}:{config['http_port']}")
    except Exception as e:
        print(f"[audio] couldn't resync Sonos streams after a capture format change: {e}")
    try:
        restart_render()
    except Exception as e:
        print(f"[audio] couldn't restart the local speaker path after a capture format change: {e}")


def _publish_capture_format(rate, channels):
    """Record the sample rate/channel count the capture stream is really
    delivering, once it is actually open. If that differs from what
    config held a moment ago, whatever already started (the local render
    path, a Sonos stream) latched the old format -- restart those.

    This is the one place the format changes, and it matters more now than
    it used to: the recording device always arrived at 44.1kHz, matching
    the value config.json was left holding, but WASAPI loopback arrives at
    the cable's own mix rate (48kHz here), and a fallback from one method
    to the other changes the rate mid-run."""
    changed = config.get("sample_rate") != rate or config.get("channels") != channels
    config["sample_rate"] = rate
    config["channels"] = channels
    if changed:
        # saved, so only the first launch after a change (e.g. upgrading from
        # the 44.1kHz recording device to 48kHz loopback) has anything to
        # restart -- every later launch already finds the right format here
        from config import save_config
        try:
            save_config(config)
        except Exception as e:
            print(f"[audio] couldn't save the capture format: {e}")
        print(f"[audio] capture format is {rate}Hz x{channels}ch -- restarting the local "
              f"speaker path and Sonos streams so they use it")
        threading.Thread(target=_restart_downstream, daemon=True).start()


def get_capture_status():
    status = dict(_capture_status)
    status["configured"] = config.get("capture_method", "recording")
    return status


def _loopback_wanted():
    # Only pyaudiowpatch can do WASAPI loopback; macOS's sounddevice wrapper
    # (audio_backend.py) has nothing equivalent.
    return pyaudio.BACKEND == "pyaudiowpatch" and config.get("capture_method") == "loopback"


def _loopback_render_substr():
    """The playback side of the cable whose recording side is named by
    capture_device_substr: VB-Audio pairs "CABLE Output" (recording) with
    "CABLE Input" (playback), and the same naming holds for its other
    cables ("CABLE-A Output" / "CABLE-A Input")."""
    return re.sub("output", "Input", config["capture_device_substr"], flags=re.IGNORECASE)


def _pick_loopback_device():
    """(index, info, None) for the cable's loopback endpoint, or
    (None, None, why-not)."""
    substr = _loopback_render_substr()
    idx, info = find_loopback_device(substr)
    if idx is None:
        return None, None, f"no loopback device matching '{substr}'"
    channels = int(info.get("maxInputChannels", 0))
    if channels not in (1, 2):
        # Windows hands loopback capture the endpoint's own channel layout
        # (unlike the recording device, which it converts to stereo for
        # us). The virtual cable is stereo unless someone changed its
        # format in the Sound control panel; not worth a surround downmix.
        return None, None, f"'{info['name']}' is set to {channels} channels (needs stereo)"
    return idx, info, None


# Loopback only: how long the reader can go without a packet from Windows
# before we start feeding the streams silence ourselves, and how often the
# watcher checks.
_LOOPBACK_IDLE_GRACE_S = 0.3
# How often the watcher looks. While audio is flowing, a look every 50ms is
# plenty to notice the stream going quiet within the grace period above --
# and every look is a thread wake-up that has to take the interpreter lock
# off the render loop, so on a busy background process fewer is better.
# Once it IS idle it is feeding silence in real time and needs the finer
# tick to keep that on schedule.
_LOOPBACK_WATCH_BUSY_S = 0.05
_LOOPBACK_WATCH_S = 0.01
_LOOPBACK_OPEN_FAILURES_BEFORE_FALLBACK = 3


def _read_loop_recording(stream, stop_event):
    """Blocking reads from the cable's recording device, which always
    delivers -- a stream of silence when nothing is playing. Returns the
    number of consecutive read errors it ended on (5 = the stream is dead)."""
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
    return consecutive_errors


class _LoopbackReader(threading.Thread):
    """Blocking reads of one WASAPI loopback stream, on a thread of their own.

    Blocking reads are the only way of reading this stream that loses
    nothing. Polling it with get_read_available() instead delivered only
    ~99.3% of real time on a real PC with no overflow ever reported --
    which drained each Sonos speaker's buffer about every 100 seconds (the
    speaker then stops and has to be restarted) and put faint clicks in the
    PC speakers -- where blocking reads delivered 99.9%+.

    A blocking read has two hazards, hence the separate thread and the
    watcher in _read_loop_loopback: Windows only sends loopback data while
    something is playing to that endpoint (real playback devices send
    nothing at all when idle, and a read on one never returns), and a read
    stuck inside PortAudio can't be interrupted -- closing the stream from
    another thread returns at once but leaves that read, and this thread,
    stuck for good."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.last_data = time.monotonic()
        self.errors = 0          # consecutive read errors; 5 = the stream is dead
        self.abandoned = False   # set when nobody is listening any more

    def run(self):
        _register_audio_thread()
        while not self.abandoned and self.errors < 5:
            try:
                data = self.stream.read(CHUNK, exception_on_overflow=False)
            except Exception as e:
                if self.abandoned:
                    return
                self.errors += 1
                print(f"[audio] capture error: {e}")
                time.sleep(0.5)
                continue
            if self.abandoned:
                return  # a read that finally returned after we moved on: drop it
            self.errors = 0
            self.last_data = time.monotonic()
            broadcaster.publish(data)


def _read_loop_loopback(stream, stop_event, rate, channels):
    """Publishes a WASAPI loopback stream, feeding real-time silence
    whenever Windows has sent nothing for a moment (see _LoopbackReader) so
    every Sonos stream and the local render path stay alive on an idle
    system. Returns the number of consecutive errors it ended on, like
    _read_loop_recording (5 = the stream is dead)."""
    reader = _LoopbackReader(stream)
    reader.start()
    silence = b"\x00" * (CHUNK * channels * 2)  # 16-bit samples
    chunk_seconds = CHUNK / float(rate)
    next_silence = None
    announced_idle = False
    try:
        while not stop_event.is_set() and reader.errors < 5 and reader.is_alive():
            now = time.monotonic()
            if now - reader.last_data >= _LOOPBACK_IDLE_GRACE_S:
                if not announced_idle:
                    announced_idle = True
                    print("[audio] loopback stream is idle (Windows sends no audio while nothing "
                          "is playing) -- feeding silence so the speakers' streams stay alive")
                if next_silence is None or now - next_silence > 0.5:
                    next_silence = now  # first idle chunk, or we were suspended: don't replay the gap
                while next_silence <= now:
                    broadcaster.publish(silence)
                    next_silence += chunk_seconds
                nap = _LOOPBACK_WATCH_S
            else:
                next_silence = None
                nap = _LOOPBACK_WATCH_BUSY_S
            time.sleep(nap)
    finally:
        reader.abandoned = True
        # A healthy reader returns from its current read within ~20ms and
        # exits, after which closing the stream is safe. One stuck in a read
        # on a silent device won't; give up on it after a moment rather than
        # hold up a restart (it's a daemon thread and exits with the app).
        reader.join(timeout=0.5)
    if reader.errors >= 5 or (not reader.is_alive() and not stop_event.is_set()):
        return 5
    return reader.errors


def _capture_loop_system(stop_event):
    """Reads PCM from the virtual cable and publishes it to the
    broadcaster -- the single source both the Sonos streams and the local
    delayed-render path draw from, so if this stops, everything downstream
    goes silent no matter what any setting (including the delay) is.

    On Windows there are two ways to read the cable, chosen by
    config["capture_method"]:

      * "loopback" (default): WASAPI loopback of "CABLE Input", the
        playback device Windows apps are already sending audio to.
        Windows does not count this as microphone access.
      * "recording": open "CABLE Output", the cable's *recording* device.
        It carries exactly the same audio, but Windows treats every
        recording device as a microphone -- listing the app under
        Privacy > Microphone and showing the mic as in use for as long
        as PC2Sonos runs, which people rightly find alarming. This is
        also what loopback falls back to if it can't be used (no such
        loopback device, it won't open, or it isn't stereo), so audio
        never stops working just because the newer method didn't.

    On macOS BlackHole is read as an input device, the "recording" path.

    Self-healing: if the capture stream ever errors out for good (a
    driver hiccup, the device briefly grabbed elsewhere, sleep/wake),
    reopen it from scratch instead of retrying reads against an
    already-dead stream object forever. That used to be exactly what
    happened -- one "Unanticipated host error" and every subsequent read
    just raised "Stream closed" in an infinite loop, silently killing all
    audio (to both Sonos and the local speakers) for the rest of the run
    with no recovery short of relaunching the whole app by hand."""
    attempt = 0
    warned_missing = False
    loopback_open_failures = 0
    loopback_gave_up = None  # why this run stopped trying loopback, if it did
    while not stop_event.is_set():
        fallback_reason = None
        use_loopback = _loopback_wanted() and loopback_gave_up is None
        if use_loopback:
            idx, info, fallback_reason = _pick_loopback_device()
            use_loopback = idx is not None
        elif _loopback_wanted():
            fallback_reason = loopback_gave_up
        if not use_loopback:
            idx, info = find_device_index(config["capture_device_substr"], want_input=True)

        if idx is None:
            _set_capture_status()
            if sys.platform == "darwin":
                hint = "install BlackHole (brew install --cask blackhole-2ch)"
            else:
                hint = ("install VB-Audio Virtual Cable and set it as your Windows "
                        "default playback device")
            if not warned_missing:
                warned_missing = True
                print(f"[audio] capture device matching '{config['capture_device_substr']}' "
                      f"not found -- {hint} (see README.md)")
            # Keep checking rather than giving up for the whole run: on
            # macOS the first launch offers to install BlackHole and audio
            # should start as soon as the driver appears, no restart needed.
            stop_event.wait(5)
            continue

        warned_missing = False
        rate = int(info.get("defaultSampleRate", config["sample_rate"]))
        channels = min(int(info.get("maxInputChannels", 2)), 2)

        try:
            stream = _pa.open(format=pyaudio.paInt16, channels=channels, rate=rate,
                               input=True, input_device_index=idx, frames_per_buffer=CHUNK)
        except Exception as e:
            attempt += 1
            if use_loopback:
                loopback_open_failures += 1
                if loopback_open_failures >= _LOOPBACK_OPEN_FAILURES_BEFORE_FALLBACK:
                    loopback_gave_up = f"'{info['name']}' would not open ({e})"
                    print(f"[audio] loopback capture failed {loopback_open_failures} times "
                          f"({e}); switching to the recording device for this run")
                    continue
            wait = min(2 * attempt, 10)
            print(f"[audio] capture device open failed ({e}); retrying in {wait}s (attempt {attempt})")
            time.sleep(wait)
            continue

        if use_loopback:
            print(f"[audio] capturing from: {info['name']} @ {rate}Hz x{channels}ch (WASAPI loopback)")
        else:
            why = f" -- loopback unavailable: {fallback_reason}" if fallback_reason else ""
            print(f"[audio] capturing from: {info['name']} @ {rate}Hz x{channels}ch{why}")
        _publish_capture_format(rate, channels)
        _set_capture_status("loopback" if use_loopback else "recording",
                            info["name"], rate, channels, fallback_reason)
        attempt = 0
        loopback_open_failures = 0

        if use_loopback:
            consecutive_errors = _read_loop_loopback(stream, stop_event, rate, channels)
        else:
            consecutive_errors = _read_loop_recording(stream, stop_event)

        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        _set_capture_status()

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


def _soft_limit(normalized):
    """normalized: a float array of samples roughly in [-1, 1] but
    possibly well beyond it (e.g. after a large gain or EQ boost).
    Returns a float array softly compressed back toward [-1, 1] instead
    of hard-clipped.

    A plain hard clip -- flattening anything over the ceiling straight
    to the ceiling -- turns the INSTANT any sample crosses it into a
    jump from clean to harshly distorted, disproportionately loud/harsh
    to the ear regardless of how small the push past the ceiling was.
    This is shared by every stage that can push a sample past full scale
    (the volume boost, and the EQ boosting a band hard enough to do the
    same on its own) so nothing downstream ever has to clean up after a
    hard clip that already happened upstream -- once a signal's been
    hard-clipped, that distortion can't be undone later in the chain.

    Uses excess/(excess+width) rather than tanh(excess/width): tanh
    saturates to (numerically) exactly 1.0 within about 3-4x the knee
    width, so anything past that -- easily reached by a large EQ boost
    stacked on already-loud audio, verified directly: a +24dB bass boost
    on a 90%-of-full-scale tone pinned 77% of all samples to the exact
    same ceiling value with tanh -- collapses to one repeated value for
    a long stretch, which is audibly indistinguishable from a hard clip
    no matter how smooth the math generating it was. The rational curve
    below never actually reaches 1.0 for any finite input, so it keeps
    differentiating samples (and therefore keeps sounding like
    compression, not a flat top) even at extreme gain."""
    mag = np.abs(normalized)
    over = mag > _GAIN_KNEE
    out = np.array(normalized, copy=True)
    if np.any(over):
        width = 1.0 - _GAIN_KNEE
        excess = mag[over] - _GAIN_KNEE
        compressed = _GAIN_KNEE + (excess / (excess + width)) * width
        out[over] = np.sign(normalized[over]) * compressed
    return np.clip(out, -1.0, 1.0)


def _apply_local_levels(pcm_bytes, volume, boost):
    """The PC-speaker level chain, in the order a real amplifier has it: the
    BOOST first (config['local_render_gain'], >=1, under Advanced -- for an
    aux speaker too quiet even at full volume), soft-limited so it can't
    hard-clip, and THEN the VOLUME (config['local_volume'], 0-1, the plain
    slider on the main page) as a straight linear scale of whatever came out.

    Volume comes last on purpose. The two used to be multiplied into a single
    gain that went through the limiter, and with a big boost the limiter
    squashed the signal so hard that dragging the volume down changed the
    loudness by only a couple of dB -- the slider looked like it did nothing.
    After the limiter, 50% is always exactly half the amplitude, whatever the
    boost is set to."""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    if boost > 1.0:
        samples = _soft_limit(samples * (boost / 32768.0)) * 32767.0
    if volume < 1.0:
        samples = samples * volume
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


def _apply_local_gain(pcm_bytes, gain):
    """Amplifies 16-bit PCM by `gain`, soft-limiting (see _soft_limit)
    instead of hard-clipping as the signal approaches full scale. Real
    source audio commonly already sits close to full scale (apps master
    near 0dBFS), so even a modest boost could push a meaningful chunk of
    samples straight into a hard ceiling -- the soft knee means raising
    the slider actually feels like a smooth volume increase across its
    whole range instead of clean, then suddenly blown out.

    At or below 1.0 this is the dashboard's plain volume control, so it is
    a straight linear scale: nothing can exceed full scale when turning
    down, and running the limiter anyway would squash loud peaks even
    while making the sound quieter (a 90% volume would still compress a
    full-scale peak to about 82%)."""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    if gain <= 1.0:
        return np.clip(samples * gain, -32768, 32767).astype(np.int16).tobytes()
    return (_soft_limit(samples * (gain / 32768.0)) * 32767.0).astype(np.int16).tobytes()


# Bass/mid/treble EQ for the LOCAL speaker path only. Sonos speakers
# already have their own Bass/Treble controls in the Sonos app and
# hardware, so this never touches what gets sent to Sonos -- only what
# audio_engine.py plays out to your real PC speakers/headphones.
_EQ_BASS_HZ = 200.0
_EQ_MID_HZ = 1000.0
_EQ_MID_Q = 0.9
_EQ_TREBLE_HZ = 5000.0
_EQ_SHELF_SLOPE = 0.9  # S in the RBJ cookbook shelf formulas -- a gentle, musical slope


class _Biquad:
    """One second-order IIR filter section (direct form I). Stateful --
    each instance remembers the last two input/output samples, so a
    single instance must never be shared between channels (that would
    smear the stereo image) or reused across a totally different filter
    without resetting."""
    __slots__ = ("b0", "b1", "b2", "a1", "a2", "x1", "x2", "y1", "y2")

    def __init__(self):
        self.b0, self.b1, self.b2 = 1.0, 0.0, 0.0
        self.a1, self.a2 = 0.0, 0.0
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def set_coeffs(self, b0, b1, b2, a0, a1, a2):
        self.b0, self.b1, self.b2 = b0 / a0, b1 / a0, b2 / a0
        self.a1, self.a2 = a1 / a0, a2 / a0

    def process(self, x):
        y = (self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2
             - self.a1 * self.y1 - self.a2 * self.y2)
        self.x2, self.x1 = self.x1, x
        self.y2, self.y1 = self.y1, y
        return y


def _low_shelf_coeffs(freq, rate, gain_db):
    """RBJ Audio EQ Cookbook low-shelf -- boosts/cuts everything below
    `freq`. This is the standard, decades-old textbook biquad formula
    used throughout DSP (not derived from or copied out of any specific
    project's source)."""
    a = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / rate
    cos_w0, sin_w0 = np.cos(w0), np.sin(w0)
    alpha = sin_w0 / 2.0 * np.sqrt((a + 1 / a) * (1.0 / _EQ_SHELF_SLOPE - 1) + 2)
    sqrt_a = np.sqrt(a)
    b0 = a * ((a + 1) - (a - 1) * cos_w0 + 2 * sqrt_a * alpha)
    b1 = 2 * a * ((a - 1) - (a + 1) * cos_w0)
    b2 = a * ((a + 1) - (a - 1) * cos_w0 - 2 * sqrt_a * alpha)
    a0 = (a + 1) + (a - 1) * cos_w0 + 2 * sqrt_a * alpha
    a1 = -2 * ((a - 1) + (a + 1) * cos_w0)
    a2 = (a + 1) + (a - 1) * cos_w0 - 2 * sqrt_a * alpha
    return b0, b1, b2, a0, a1, a2


def _high_shelf_coeffs(freq, rate, gain_db):
    """RBJ Audio EQ Cookbook high-shelf -- boosts/cuts everything above `freq`."""
    a = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / rate
    cos_w0, sin_w0 = np.cos(w0), np.sin(w0)
    alpha = sin_w0 / 2.0 * np.sqrt((a + 1 / a) * (1.0 / _EQ_SHELF_SLOPE - 1) + 2)
    sqrt_a = np.sqrt(a)
    b0 = a * ((a + 1) + (a - 1) * cos_w0 + 2 * sqrt_a * alpha)
    b1 = -2 * a * ((a - 1) + (a + 1) * cos_w0)
    b2 = a * ((a + 1) + (a - 1) * cos_w0 - 2 * sqrt_a * alpha)
    a0 = (a + 1) - (a - 1) * cos_w0 + 2 * sqrt_a * alpha
    a1 = 2 * ((a - 1) - (a + 1) * cos_w0)
    a2 = (a + 1) - (a - 1) * cos_w0 - 2 * sqrt_a * alpha
    return b0, b1, b2, a0, a1, a2


def _peaking_coeffs(freq, rate, gain_db, q):
    """RBJ Audio EQ Cookbook peaking (bell) filter -- boosts/cuts a band
    centered on `freq`, width controlled by `q`."""
    a = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / rate
    cos_w0, sin_w0 = np.cos(w0), np.sin(w0)
    alpha = sin_w0 / (2 * q)
    b0 = 1 + alpha * a
    b1 = -2 * cos_w0
    b2 = 1 - alpha * a
    a0 = 1 + alpha / a
    a1 = -2 * cos_w0
    a2 = 1 - alpha / a
    return b0, b1, b2, a0, a1, a2


# How far the EQ's impulse response is followed before it is cut off: never
# shorter than the minimum, stops as soon as a whole block of it has decayed
# below the floor (relative to its peak -- ~-180dB, far below what 16-bit
# audio can even represent), never longer than the maximum. A 200Hz shelf at
# 48kHz needs on the order of a thousand taps to get there.
_EQ_IR_MIN_TAPS = 256
_EQ_IR_MAX_TAPS = 4096
_EQ_IR_BLOCK = 256
_EQ_IR_FLOOR = 1e-9


def _cascade_impulse_response(coeff_sets):
    """The response of the given biquads in series to a single unit impulse,
    computed with exactly the recursion _Biquad runs (direct form I), so the
    FIR built from it IS the same filter, just evaluated all at once instead
    of one sample at a time. Cut off where what's left has decayed away --
    see _EQ_IR_FLOOR. Runs only when the EQ settings change."""
    sections = [(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)
                for b0, b1, b2, a0, a1, a2 in coeff_sets]
    state = [[0.0, 0.0, 0.0, 0.0] for _ in sections]  # x1, x2, y1, y2 per section
    out = []
    peak = 0.0
    while len(out) < _EQ_IR_MAX_TAPS:
        block = []
        for i in range(len(out), len(out) + _EQ_IR_BLOCK):
            x = 1.0 if i == 0 else 0.0
            for (b0, b1, b2, a1, a2), st in zip(sections, state):
                y = b0 * x + b1 * st[0] + b2 * st[1] - a1 * st[2] - a2 * st[3]
                st[1], st[0] = st[0], x
                st[3], st[2] = st[2], y
                x = y
            block.append(x)
        out.extend(block)
        tail = max(abs(v) for v in block)
        peak = max(peak, tail)
        if len(out) >= _EQ_IR_MIN_TAPS and tail < _EQ_IR_FLOOR * max(peak, 1.0):
            break
    return np.array(out, dtype=np.float64)


class _ThreeBandEQ:
    """Bass/mid/treble EQ, one independent filter per channel so stereo
    channels never share (and smear) filter state.

    The three bands are biquads in series. They used to be run one sample at a
    time in a Python loop -- about 6000 filter steps per 21ms of audio, which
    on a fast PC is 10% of a core and is exactly what fell behind real time
    whenever Windows deprioritised the app (it runs windowless in the
    background): the speakers then slid further and further behind the audio,
    and the backlog never cleared. The same filter is now evaluated in one go
    per chunk: the cascade's impulse response is computed once whenever the
    settings change, and each chunk is convolved with it by FFT (overlap-save,
    keeping the last few thousand input samples from the previous chunk so
    the result is continuous across chunk boundaries). It matches the
    sample-by-sample filter to within a single 16-bit step on real music, and
    costs about a fifteenth as much."""

    def __init__(self, rate, channels):
        self.rate = rate
        self.channels = channels
        self._last = (0.0, 0.0, 0.0)
        self._ir = None
        self._spectra = {}  # FFT size -> the impulse response's spectrum at that size
        self._hist = np.zeros((0, channels))

    def _update(self, bass_db, mid_db, treble_db):
        self._ir = _cascade_impulse_response((
            _low_shelf_coeffs(_EQ_BASS_HZ, self.rate, bass_db),
            _peaking_coeffs(_EQ_MID_HZ, self.rate, mid_db, _EQ_MID_Q),
            _high_shelf_coeffs(_EQ_TREBLE_HZ, self.rate, treble_db)))
        self._spectra = {}
        need = len(self._ir) - 1  # input history the new response looks back over
        if len(self._hist) < need:
            self._hist = np.concatenate(
                [np.zeros((need - len(self._hist), self.channels)), self._hist], axis=0)
        else:
            self._hist = self._hist[len(self._hist) - need:]
        self._last = (bass_db, mid_db, treble_db)

    def process(self, pcm_bytes, bass_db, mid_db, treble_db):
        if bass_db == 0.0 and mid_db == 0.0 and treble_db == 0.0:
            # flat -- skip the work, and start from silence when it is next
            # switched on rather than filtering audio from long ago
            self._hist = np.zeros((0, self.channels))
            self._last = (0.0, 0.0, 0.0)
            return pcm_bytes
        if (bass_db, mid_db, treble_db) != self._last:
            self._update(bass_db, mid_db, treble_db)
        arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
        frames = len(arr) // self.channels
        if frames == 0:
            return b""
        x = arr[:frames * self.channels].reshape(frames, self.channels)
        taps = len(self._ir)
        seg = np.concatenate([self._hist, x], axis=0)
        size = 1 << (len(seg) - 1).bit_length()
        spectrum = self._spectra.get(size)
        if spectrum is None:
            spectrum = self._spectra[size] = np.fft.rfft(self._ir, size)
        filtered = np.fft.irfft(np.fft.rfft(seg, size, axis=0) * spectrum[:, None], size, axis=0)
        out = filtered[taps - 1:taps - 1 + frames]
        self._hist = seg[len(seg) - (taps - 1):]
        # soft-limit, not hard-clip: a large boost on one band can push
        # samples well past full scale on its own, and a hard clip here
        # would introduce harsh distortion before the gain stage's own
        # soft limiter (_apply_local_gain) ever gets a chance to help --
        # once a sample's been hard-clipped, nothing downstream can
        # undo it, so this has to be soft-limited at the source
        limited = _soft_limit((out / 32768.0).astype(np.float32))
        return (limited * 32767.0).astype(np.int16).tobytes()


class _StandingBacklogGuard:
    """Notices when a queue has stopped draining, as opposed to merely
    arriving in bursts.

    Audio reaches the render loop in bursts (Windows hands the loopback stream
    over in chunks on its own timer), so the queue is routinely two or three
    chunks deep for a moment and then empty again. A queue that NEVER gets
    anywhere near empty is different: the loop has fallen behind real time (a
    stall, the app being deprioritised, sleep/resume) and every chunk in the
    queue is lag the speakers are adding on top of the audio. Nothing ever
    gives that lag back -- audio arrives and is consumed at the same rate --
    so without this the PC speakers stayed behind (by seconds, in the worst
    case) until the app was restarted, and every underrun on the way there
    was an audible glitch.

    Feed it the depth left behind after each get(); every `window_s` it
    answers how many chunks to throw away to get back to (nearly) real time:
    the low-water mark of that window, less one. Zero in normal operation."""

    def __init__(self, min_backlog=3, window_s=0.5):
        self.min_backlog = min_backlog
        self.window_s = window_s
        self._low = None
        self._window_end = None

    def check(self, depth_left, now):
        if self._window_end is None:
            self._window_end = now + self.window_s
        self._low = depth_left if self._low is None else min(self._low, depth_left)
        if now < self._window_end:
            return 0
        low, self._low, self._window_end = self._low, None, now + self.window_s
        return low - 1 if low >= self.min_backlog else 0


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
    _register_audio_thread()
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
    try:
        host_api_name = _pa.get_host_api_info_by_index(info["hostApi"])["name"]
    except Exception:
        host_api_name = "unknown"
    print(f"[audio] rendering (delayed) to: {info['name']} "
          f"@ {render_rate}Hz x{render_channels}ch (capture is {capture_rate}Hz x{capture_channels}ch) "
          f"[hostApi={host_api_name}]")

    needs_resample = (render_rate != capture_rate)
    needs_downmix = (render_channels == 1 and capture_channels == 2)
    resample_state = None
    eq = _ThreeBandEQ(render_rate, render_channels)

    sid, q = broadcaster.subscribe(maxlen=4000)
    # buffering/delay timing is tracked in terms of the CAPTURE stream's
    # byte rate, since that's the rate audio arrives at from the broadcaster
    bytes_per_ms = capture_rate * capture_channels * sample_width / 1000.0
    buf = bytearray()
    frame_bytes = CHUNK * capture_channels * sample_width
    backlog_guard = _StandingBacklogGuard()
    chunk_ms = CHUNK / float(capture_rate) * 1000.0
    last_catch_up_log = 0.0

    try:
        while not stop_event.is_set():
            target_bytes = int(bytes_per_ms * config["local_delay_ms"])
            # Windows' volume keys and taskbar slider control the DEFAULT
            # playback device, which is the virtual cable -- not the speakers
            # this path plays to (those have their own Windows volume,
            # applied after this), so the dashboard needs its own controls
            # for them. Boost then volume are applied (see
            # _apply_local_levels) here, after resampling, right before the
            # device write.
            volume = config.get("local_volume", 1.0)
            boost = max(1.0, config.get("local_render_gain", 1.0))
            bass_db = config.get("local_eq_bass_db", 0.0)
            mid_db = config.get("local_eq_mid_db", 0.0)
            treble_db = config.get("local_eq_treble_db", 0.0)
            try:
                chunk = q.get(timeout=1)
            except queue.Empty:
                continue

            # The delay above is held in `buf`, which the loop below always
            # empties down to it -- so the drift guard after this can never
            # see lag that piled up in the QUEUE, which is where it actually
            # collects when the loop falls behind. This is the one that
            # bounds it.
            now = time.monotonic()
            skip = backlog_guard.check(q.qsize(), now)
            if skip:
                for _ in range(skip):
                    try:
                        chunk = q.get_nowait()
                    except queue.Empty:
                        break
                if now - last_catch_up_log > 5:
                    last_catch_up_log = now
                    print(f"[audio] the local speaker path had fallen ~{skip * chunk_ms:.0f}ms behind "
                          f"real time; skipped ahead to catch up")
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
                if out:
                    out = eq.process(out, bass_db, mid_db, treble_db)
                if out and (volume != 1.0 or boost != 1.0):
                    out = _apply_local_levels(out, volume, boost)
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
    _harden_audio_scheduling()
    with _capture_lock:
        _capture_stop_event = threading.Event()
        _capture_thread = threading.Thread(target=capture_loop, args=(_capture_stop_event,), daemon=True)
        _capture_thread.start()

    with _render_lock:
        _render_stop_event = threading.Event()
        _render_thread = threading.Thread(target=render_loop, args=(_render_stop_event,), daemon=True)
        _render_thread.start()


def restart_capture(new_mode=None, new_target_names=None, new_method=None):
    """Stop the current capture thread and start a new one, picking up a
    newly-chosen audio source (whole system vs. one or more selected
    apps) or capture method (Windows loopback vs. the recording device).
    Used by the dashboard's audio-source picker and capture-method switch.

    Whole-system and per-app capture run at different sample rates (see
    per_app_audio.py), and loopback at the cable's own mix rate rather than
    the 44.1kHz Windows converts the recording device to -- so switching
    can change the actual PCM format on the fly. That is handled where the
    new format is actually known, once the new stream is open (see
    _publish_capture_format): it restarts the local render path and the
    Sonos streams only if the format really changed, so a switch between
    two sources with the same format doesn't interrupt anything."""
    global _capture_stop_event, _capture_thread
    from config import save_config
    if new_mode is not None:
        config["capture_mode"] = new_mode
    if new_target_names is not None:
        config["capture_target_names"] = new_target_names
    if new_method is not None:
        config["capture_method"] = new_method
    if new_mode is not None or new_target_names is not None or new_method is not None:
        save_config(config)
    with _capture_lock:
        if _capture_stop_event is not None:
            _capture_stop_event.set()
        if _capture_thread is not None:
            _capture_thread.join(timeout=3)
        _capture_stop_event = threading.Event()
        _capture_thread = threading.Thread(target=capture_loop, args=(_capture_stop_event,), daemon=True)
        _capture_thread.start()


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
