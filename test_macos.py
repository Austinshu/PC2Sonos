"""
Tests for the macOS layer that run anywhere (Linux CI included): the
sounddevice-backed audio_backend shim presents the PyAudio surface
audio_engine.py/calibration.py rely on, the CoreAudio helper fails soft
off-macOS, and config picks platform defaults. Companion to
test_logic.py, which exercises the Windows path with a pyaudiowpatch stub.

Run:  python test_macos.py
"""

import importlib
import os
import sys
import tempfile
import types

# isolated data dir so this never touches a real install
os.environ.setdefault("PC2SONOS_TEST_HOME", tempfile.mkdtemp(prefix="pc2sonos-test-"))

# ---- make pyaudiowpatch unimportable and stub sounddevice ----
sys.modules["pyaudiowpatch"] = None  # documented way to force ImportError

fake_sd = types.ModuleType("sounddevice")
_DEVICES = [
    {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2,
     "default_samplerate": 48000.0, "hostapi": 0},
    {"name": "ZoomAudioDevice", "max_input_channels": 2, "max_output_channels": 2,
     "default_samplerate": 48000.0, "hostapi": 0},
    {"name": "Mac mini Speakers", "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 44100.0, "hostapi": 0},
    {"name": "Mac mini Microphone", "max_input_channels": 1, "max_output_channels": 0,
     "default_samplerate": 48000.0, "hostapi": 0},
]


class _Raw:
    opened = []

    def __init__(self, **kw):
        self.kw = kw
        _Raw.opened.append(kw)
        self.channels = kw.get("channels", 2)

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass

    def read(self, n):
        return b"\x00\x00" * n * self.channels, False

    def write(self, data):
        pass


def _query_devices(device=None, kind=None):
    if kind == "input":
        d = dict(_DEVICES[3])
        d["index"] = 3
        return d
    return list(_DEVICES)


fake_sd.query_devices = _query_devices
fake_sd.query_hostapis = lambda index=None: [{"name": "Core Audio"}]
fake_sd.RawInputStream = _Raw
fake_sd.RawOutputStream = _Raw
fake_sd.default = types.SimpleNamespace(device=(3, 0))
sys.modules["sounddevice"] = fake_sd

print("[test] audio_backend falls back to sounddevice when pyaudiowpatch is missing...")
import audio_backend  # noqa: E402
assert audio_backend.BACKEND == "sounddevice", audio_backend.BACKEND
pa = audio_backend.PyAudio()
assert pa.get_device_count() == 4
info = pa.get_device_info_by_index(0)
assert info["name"] == "BlackHole 2ch" and info["maxInputChannels"] == 2 and info["maxOutputChannels"] == 2
assert info["defaultSampleRate"] == 48000.0 and info["hostApi"] == 0
try:
    pa.get_host_api_info_by_type(audio_backend.paWASAPI)
    raise AssertionError("WASAPI lookup must fail so audio_engine skips host-API filtering")
except ValueError:
    pass
assert pa.get_host_api_info_by_index(0)["name"] == "Core Audio"
mic = pa.get_default_input_device_info()
assert mic["index"] == 3 and mic["name"] == "Mac mini Microphone"
print("  OK")

print("[test] audio_backend streams present PyAudio's read/write surface...")
s = pa.open(format=audio_backend.paInt16, channels=2, rate=48000, input=True,
            input_device_index=0, frames_per_buffer=512)
assert _Raw.opened[-1]["device"] == 0 and _Raw.opened[-1]["blocksize"] == 512
data = s.read(512, exception_on_overflow=False)
assert isinstance(data, bytes) and len(data) == 512 * 2 * 2
s.stop_stream()
s.close()
o = pa.open(format=audio_backend.paInt16, channels=2, rate=44100, output=True, output_device_index=2)
o.write(b"\x00" * 4096)
o.close()
try:
    pa.open(format=1, channels=2, rate=44100, output=True)
    raise AssertionError("non-16-bit formats must be rejected")
except ValueError:
    pass
print("  OK")

print("[test] audio_engine works on top of the shim (device pick skips virtual outputs)...")
import audio_engine  # noqa: E402
audio_engine.config["capture_device_substr"] = "BlackHole"
idx, _ = audio_engine.find_device_index("BlackHole", want_input=True)
assert idx == 0
idx2, info2 = audio_engine.auto_pick_render_device()
assert idx2 == 2, f"should skip BlackHole and Zoom's virtual device, got {info2}"
names = {d["name"]: d for d in audio_engine.list_output_devices()}
assert names["BlackHole 2ch"]["likely_virtual"] and names["ZoomAudioDevice"]["likely_virtual"]
assert not names["Mac mini Speakers"]["likely_virtual"]
print("  OK")

print("[test] macOS helpers fail soft off-macOS...")
import macos_audio  # noqa: E402
import macos_firewall  # noqa: E402
import macos_app  # noqa: E402
if sys.platform != "darwin":
    assert macos_audio.current_default_output() == (None, "")
    ok, why = macos_audio.set_default_output("BlackHole")
    assert not ok and "macOS" in why
    changed, status, prev = macos_audio.ensure_blackhole_is_default()
    assert not changed and prev == ""
    assert macos_firewall.firewall_state().startswith("n/a")
    assert macos_app.microphone_status().startswith("n/a")
    assert macos_app.request_microphone_access() is False
    assert macos_app.ask("x", ["OK"]) is None
    assert macos_app.is_bundled() is False and macos_app.app_bundle_path() is None
assert macos_audio._fourcc("dOut") == 0x644F7574
print("  OK")

print("[test] platform defaults in config...")
import config as cfg  # noqa: E402
expected = "BlackHole" if sys.platform == "darwin" else "CABLE Output"
assert cfg.DEFAULT_CONFIG["capture_device_substr"] == expected
assert "manage_default_output" in cfg.DEFAULT_CONFIG and "previous_default_output" in cfg.DEFAULT_CONFIG
print("  OK")

print("[test] /api/platform_status reports the capture device...")
import webapp  # noqa: E402
webapp.app.testing = True
r = webapp.app.test_client().get("/api/platform_status")
body = r.get_json()
assert r.status_code == 200 and body["capture_device_present"] is True, body
assert body["platform"] == sys.platform
print("  OK")

importlib.invalidate_caches()
print("\nall macOS-layer tests passed")
