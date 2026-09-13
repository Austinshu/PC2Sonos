"""The audio device layer, chosen per platform behind one PyAudio-shaped
interface so audio_engine.py and calibration.py don't need to know
which OS they're on.

  * Windows: pyaudiowpatch (PyAudio with the WASAPI loopback patches
    this project has always used). Re-exported unchanged.
  * macOS (and anything else where pyaudiowpatch doesn't exist): a thin
    wrapper around python-sounddevice (PortAudio over CoreAudio) that
    exposes the handful of PyAudio calls the rest of the code makes --
    device enumeration, host-API queries, and blocking read/write
    streams -- with PyAudio's method names and dict keys.

Only the methods actually used elsewhere are implemented; anything
else raises AttributeError, on purpose, so a new PyAudio call added to
audio_engine.py fails loudly on macOS instead of silently doing nothing.
"""

try:
    import pyaudiowpatch as _pa  # Windows (or the test harness's stub)
except ImportError:
    _pa = None

if _pa is not None:
    BACKEND = "pyaudiowpatch"
    PyAudio = _pa.PyAudio
    paInt16 = _pa.paInt16
    paWASAPI = _pa.paWASAPI
else:
    import sounddevice as _sd

    BACKEND = "sounddevice"
    # PyAudio's constants; the values only matter for equality checks.
    paInt16 = 8
    paWASAPI = 13

    class _Stream:
        """Blocking stream with PyAudio's read/write/stop/close surface."""

        def __init__(self, raw_stream):
            self._s = raw_stream
            self._s.start()

        def read(self, frames, exception_on_overflow=True):
            data, overflowed = self._s.read(frames)
            if overflowed and exception_on_overflow:
                raise IOError("input overflowed")
            return bytes(data)

        def write(self, data):
            self._s.write(data)

        def stop_stream(self):
            try:
                self._s.stop()
            except Exception:
                pass

        def close(self):
            try:
                self._s.close()
            except Exception:
                pass

    class PyAudio:
        """Just enough of pyaudio.PyAudio, backed by sounddevice."""

        def _devices(self):
            return list(_sd.query_devices())

        def get_device_count(self):
            return len(self._devices())

        def get_device_info_by_index(self, index):
            d = self._devices()[index]
            return {
                "index": index,
                "name": d.get("name") or "",
                "maxInputChannels": int(d.get("max_input_channels") or 0),
                "maxOutputChannels": int(d.get("max_output_channels") or 0),
                "defaultSampleRate": float(d.get("default_samplerate") or 44100.0),
                "hostApi": int(d.get("hostapi") or 0),
            }

        def get_host_api_info_by_type(self, host_type):
            # There is no WASAPI here. audio_engine.py already treats a
            # failure as "don't filter by host API", which is right for
            # CoreAudio's single host API.
            raise ValueError(f"host API type {host_type} not available on this platform")

        def get_host_api_info_by_index(self, index):
            apis = list(_sd.query_hostapis())
            if 0 <= index < len(apis):
                return {"index": index, "name": apis[index].get("name", "unknown")}
            return {"index": index, "name": "unknown"}

        def get_default_input_device_info(self):
            d = _sd.query_devices(kind="input")
            idx = d.get("index")
            if idx is None:
                default = _sd.default.device[0]
                idx = int(default) if default is not None and int(default) >= 0 else 0
            info = self.get_device_info_by_index(int(idx))
            info["name"] = d.get("name") or info["name"]
            return info

        def open(self, format=paInt16, channels=2, rate=44100, input=False, output=False,
                 input_device_index=None, output_device_index=None, frames_per_buffer=1024):
            if format != paInt16:
                raise ValueError("only 16-bit PCM is supported")
            if input:
                raw = _sd.RawInputStream(samplerate=rate, blocksize=frames_per_buffer,
                                         device=input_device_index, channels=channels, dtype="int16")
            elif output:
                raw = _sd.RawOutputStream(samplerate=rate, blocksize=frames_per_buffer,
                                          device=output_device_index, channels=channels, dtype="int16")
            else:
                raise ValueError("open() needs input=True or output=True")
            return _Stream(raw)

        def terminate(self):
            pass
