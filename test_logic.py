"""
Local logic test harness -- runs on Linux without real audio hardware or
a real Sonos on the LAN, by stubbing out pyaudiowpatch (Windows-only) and
soco discovery. Exercises: config round-trip, Broadcaster fan-out, WAV
header generation, and the Flask dashboard/API routes.
"""

import os
import sys
import types
import threading
import time

# ---- stub pyaudiowpatch before anything imports it ----
fake_pyaudio = types.ModuleType("pyaudiowpatch")
fake_pyaudio.paInt16 = 8
fake_pyaudio.paWASAPI = 13


class FakeStream:
    def read(self, n, exception_on_overflow=False):
        return b"\x00\x00" * n * 2  # silence, stereo 16-bit

    def write(self, data):
        pass

    def stop_stream(self):
        pass

    def close(self):
        pass


class FakePyAudio:
    def get_device_count(self):
        return 3

    def get_device_info_by_index(self, i):
        if i == 0:
            return {"name": "CABLE Output (VB-Audio Virtual Cable)",
                    "maxInputChannels": 2, "maxOutputChannels": 0,
                    "defaultSampleRate": 44100.0, "hostApi": 0}
        if i == 1:
            # a real virtual device that has bitten this project before --
            # auto-pick must skip it, not just "cable"-named devices
            return {"name": "Speakers (Steam Streaming Microphone)",
                    "maxInputChannels": 0, "maxOutputChannels": 2,
                    "defaultSampleRate": 48000.0, "hostApi": 0}
        return {"name": "Speakers (Realtek(R) Audio)",
                "maxInputChannels": 0, "maxOutputChannels": 2,
                "defaultSampleRate": 44100.0, "hostApi": 0}

    def get_host_api_info_by_type(self, t):
        return {"index": 0}

    def open(self, **kwargs):
        return FakeStream()


fake_pyaudio.PyAudio = FakePyAudio
sys.modules["pyaudiowpatch"] = fake_pyaudio

# ---- now safe to import our modules ----
from config import DEFAULT_CONFIG, CONFIG_PATH, load_config, save_config, config  # noqa: E402
import audio_engine  # noqa: E402

# This harness writes to the real config.json (the round-trip test below,
# and every POST route that calls save_config). On a dev box that's the
# live PC2Sonos config -- snapshot it now and put it back on exit no
# matter how the run ends.
import atexit  # noqa: E402
_ORIG_CONFIG_BYTES = CONFIG_PATH.read_bytes() if CONFIG_PATH.exists() else None


@atexit.register
def _restore_real_config():
    if _ORIG_CONFIG_BYTES is None:
        CONFIG_PATH.unlink(missing_ok=True)
    else:
        CONFIG_PATH.write_bytes(_ORIG_CONFIG_BYTES)

print("[test] config round-trip...")
cfg = dict(DEFAULT_CONFIG)
cfg["local_delay_ms"] = 1234
save_config(cfg)
reloaded = load_config()
assert reloaded["local_delay_ms"] == 1234, "config did not persist"
print("  OK")

print("[test] device discovery helpers...")
idx, info = audio_engine.find_device_index("CABLE Output", want_input=True)
assert idx == 0, f"expected capture device index 0, got {idx}"
idx2, info2 = audio_engine.auto_pick_render_device()
assert idx2 == 2, (f"expected render device index 2 (skip CABLE *and* the "
                    f"Steam virtual mic), got {idx2} ({info2})")
print("  OK")

print("[test] list_output_devices flags virtual devices...")
devices = audio_engine.list_output_devices()
by_name = {d["name"]: d for d in devices}
assert by_name["Speakers (Steam Streaming Microphone)"]["likely_virtual"] is True
assert by_name["Speakers (Realtek(R) Audio)"]["likely_virtual"] is False
print("  OK")

print("[test] Broadcaster fan-out...")
b = audio_engine.Broadcaster()
sid1, q1 = b.subscribe()
sid2, q2 = b.subscribe()
b.publish(b"hello")
assert q1.get_nowait() == b"hello"
assert q2.get_nowait() == b"hello"
b.unsubscribe(sid1)
b.publish(b"world")
assert q2.get_nowait() == b"world"
assert q1.empty()
print("  OK")

print("[test] Broadcaster drops oldest when a subscriber is full (no stall)...")
b2 = audio_engine.Broadcaster()
sid3, q3 = b2.subscribe(maxlen=2)
b2.publish(b"1")
b2.publish(b"2")
b2.publish(b"3")  # queue full -> should drop oldest, keep newest
got = [q3.get_nowait(), q3.get_nowait()]
assert b"3" in got, f"expected newest chunk to survive, got {got}"
print("  OK")

print("[test] capture_loop/render_loop run without crashing (short burst)...")
stop_event = threading.Event()
audio_engine.config["local_delay_ms"] = 50
t1 = threading.Thread(target=audio_engine.capture_loop, args=(stop_event,), daemon=True)
t2 = threading.Thread(target=audio_engine.render_loop, args=(stop_event,), daemon=True)
t1.start()
t2.start()
time.sleep(1.0)
stop_event.set()
t1.join(timeout=2)
t2.join(timeout=2)
assert not t1.is_alive() and not t2.is_alive(), "capture/render threads did not stop cleanly"
print("  OK")

print("[test] webapp Flask routes...")
import webapp  # noqa: E402
webapp.app.testing = True
client = webapp.app.test_client()

r = client.get("/")
assert r.status_code == 200 and b"Sonos speakers" in r.data, "expected the full dashboard"
# the PC speaker VOLUME slider (0-100%) lives on the main page, in the PC
# speaker output card; the BOOST (100-500%) is a separate control inside the
# collapsed Advanced card -- so nothing on the main page can be dragged into
# the range that stresses speakers
_page = r.data.decode("utf-8")
assert _page.count('id="localVolume"') == 1 and _page.count('id="localGain"') == 1
assert _page.index('<span class="card-title">PC speaker output') < _page.index('id="localVolume"') \
    < _page.index('<span class="card-title">Advanced:') < _page.index('id="localGain"'), \
    "volume in the PC speaker output card, boost inside Advanced"
# layout: two balanced columns of short cards, with Advanced full-width BELOW them
# (a tall open Advanced card inside the columns left a hole beside it), and its
# summary a plain flex row so a long title can't wrap the header below the arrow
assert _page.count('class="grid-col"') == 2
assert _page.index('Troubleshooting</span>') < _page.index('class="card advanced"'), \
    "Advanced must sit below the two columns, not inside them"
assert 'class="card advanced"' in _page and 'display:inline-flex;">\n      <span class="card-icon">' not in _page
print("  / OK")

r = client.get("/api/speakers")
assert r.status_code == 200 and r.get_json() == []
print("  /api/speakers OK (empty, no real Sonos on this machine)")

r = client.post("/api/delay", json={"delay_ms": 900})
assert r.status_code == 200 and webapp.config["local_delay_ms"] == 900
print("  /api/delay OK")

r = client.get("/api/devices")
body = r.get_json()
# CABLE Output is an input-only (recording) device in the fake, and the
# Steam virtual mic is filtered out as a virtual/software output -- only
# the one real speaker should be offered as a pickable option here.
assert r.status_code == 200 and len(body["devices"]) == 1, body
names = {d["name"] for d in body["devices"]}
assert "Speakers (Steam Streaming Microphone)" not in names, \
    "virtual outputs must not be selectable in the dashboard"
assert "Speakers (Realtek(R) Audio)" in names
print("  /api/devices OK (virtual outputs hidden from the picker)")

r = client.post("/api/render_device", json={"device": "Speakers (Realtek(R) Audio)"})
assert r.status_code == 200
assert webapp.config["render_device_substr"] == "Speakers (Realtek(R) Audio)"
time.sleep(0.3)  # let the restarted render thread open its stream
assert audio_engine.get_current_render_device_name() == "Speakers (Realtek(R) Audio)"
audio_engine._render_stop_event.set()  # clean up the thread this test started
audio_engine._render_thread.join(timeout=2)
print("  /api/render_device OK (explicit device switch works)")

r = client.post("/api/local_gain", json={"percent": 150})
assert r.status_code == 200 and webapp.config["local_render_gain"] == 1.5
r = client.post("/api/local_gain", json={"percent": 999})  # clamps, doesn't error
assert r.status_code == 200 and webapp.config["local_render_gain"] == 5.0
r = client.post("/api/local_gain", json={"percent": 20})  # the boost can't turn things DOWN
assert r.status_code == 200 and webapp.config["local_render_gain"] == 1.0
r = client.post("/api/local_gain", json={"percent": 100})
assert webapp.config["local_render_gain"] == 1.0
print("  /api/local_gain OK (the boost: clamped to 100-500%)")

r = client.post("/api/local_volume", json={"percent": 40})
assert r.status_code == 200 and webapp.config["local_volume"] == 0.4
r = client.post("/api/local_volume", json={"percent": 999})  # volume never goes past 100%
assert r.status_code == 200 and webapp.config["local_volume"] == 1.0
r = client.post("/api/local_volume", json={"percent": -5})
assert r.status_code == 200 and webapp.config["local_volume"] == 0.0
r = client.post("/api/local_volume", json={"percent": 100})  # restore default for later tests
assert webapp.config["local_volume"] == 1.0
print("  /api/local_volume OK (clamped to 0-100%)")

# The level chain is boost (soft-limited) THEN volume (linear). Volume must stay a
# plain proportional control at ANY boost -- when the two were folded into one gain
# ahead of the limiter, a big boost squashed the signal so hard that dragging the
# volume changed the loudness by a couple of dB and the slider seemed to do nothing.
import config as _cfgmod  # noqa: E402
import numpy as _np_lv  # noqa: E402
_loud_music = (_np_lv.sin(_np_lv.linspace(0, 2 * _np_lv.pi * 40, 4800, False)) * 0.8 * 32767).astype(_np_lv.int16)
_loud_music = _np_lv.repeat(_loud_music[:, None], 2, axis=1).flatten().tobytes()


def _rms_of(b):
    a = _np_lv.frombuffer(b, dtype=_np_lv.int16).astype(_np_lv.float64)
    return float(_np_lv.sqrt((a * a).mean()))


for _boost in (1.0, 2.04, 5.0):
    _full = _rms_of(audio_engine._apply_local_levels(_loud_music, 1.0, _boost)) if _boost != 1.0 else _rms_of(_loud_music)
    for _vol in (0.9, 0.56, 0.5, 0.25):
        _got = _rms_of(audio_engine._apply_local_levels(_loud_music, _vol, _boost))
        assert abs(_got / _full - _vol) < 0.01, f"boost {_boost}: volume {_vol} must scale the level by {_vol}, got {_got / _full:.3f}"
# and the boost itself is unchanged: it amplifies, and never hard-clips
_boosted = _np_lv.frombuffer(audio_engine._apply_local_levels(_loud_music, 1.0, 2.04), dtype=_np_lv.int16)
assert _rms_of(audio_engine._apply_local_levels(_loud_music, 1.0, 2.04)) > _rms_of(_loud_music) * 1.15
assert int(_np_lv.abs(_boosted).max()) < 32767, "boosted audio must be soft-limited, not hard-clipped"
# quiet audio passes through untouched at 100% volume and no boost (the render loop skips the stage entirely)
_q = _np_lv.array([100, -100, 250, -250], dtype=_np_lv.int16).tobytes()
assert audio_engine._apply_local_levels(_q, 1.0, 1.0) == _q
print("  PC speaker level chain: boost then a linear volume, proportional at any boost OK")

# an old config had ONE gain (0-500%); a value below 100% was volume, above was boost
_m = {**_cfgmod.DEFAULT_CONFIG, "local_render_gain": 0.5}
_cfgmod._migrate_local_volume({"local_render_gain": 0.5}, _m)
assert _m["local_volume"] == 0.5 and _m["local_render_gain"] == 1.0, _m
_m = {**_cfgmod.DEFAULT_CONFIG, "local_render_gain": 1.9}
_cfgmod._migrate_local_volume({"local_render_gain": 1.9}, _m)
assert _m["local_volume"] == 1.0 and _m["local_render_gain"] == 1.9, "an old boost stays the boost"
_m = {**_cfgmod.DEFAULT_CONFIG, "local_volume": 0.3, "local_render_gain": 0.5}
_cfgmod._migrate_local_volume({"local_volume": 0.3, "local_render_gain": 0.5}, _m)
assert _m["local_volume"] == 0.3, "an already-migrated config must not be migrated again"
assert _cfgmod.DEFAULT_CONFIG["local_volume"] == 1.0 and _cfgmod.DEFAULT_CONFIG["local_render_gain"] == 1.0
print("  old single-gain config migrates into volume + boost OK; defaults are 100% / no boost")

print("[test] /api/master_volume: scales enabled speakers + PC volume, leaves disabled speakers and the boost alone...")
import types as _mv_types  # noqa: E402
_mv_on = _mv_types.SimpleNamespace(volume=0)
_mv_off = _mv_types.SimpleNamespace(volume=0)
webapp.speaker_mgr.speakers["MV_ON"] = _mv_on
webapp.speaker_mgr.speakers["MV_OFF"] = _mv_off
_mv_saved_speakers_cfg = dict(webapp.config["speakers"])
webapp.config["speakers"]["MV_ON"] = {"enabled": True, "volume": 80}
webapp.config["speakers"]["MV_OFF"] = {"enabled": False, "volume": 80}
webapp.config["local_volume"] = 0.8       # PC volume 80%
webapp.config["local_render_gain"] = 2.0  # boost 200% -- master volume must never touch this
try:
    r = client.post("/api/master_volume", json={"percent": 50})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["local_volume_percent"] == 40, body
    assert webapp.config["speakers"]["MV_ON"]["volume"] == 40, \
        "an enabled speaker's volume should scale with the master percentage"
    assert _mv_on.volume == 40, "the scaled volume must actually reach the zone, not just config"
    assert webapp.config["speakers"]["MV_OFF"]["volume"] == 80, \
        "a disabled speaker must be left untouched by master volume"
    assert webapp.config["local_volume"] == 0.4, "the PC volume should scale by the same percentage"
    assert webapp.config["local_render_gain"] == 2.0, "the PC boost must never be touched by master volume"

    r = client.post("/api/master_volume", json={"percent": 999})  # clamps to 500, doesn't error
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert webapp.config["speakers"]["MV_ON"]["volume"] == 100, \
        "scaling a Sonos speaker up by 500% should still clamp its volume to 100"
    assert r.get_json()["local_volume_percent"] == 80, \
        "scaling UP must never raise the PC volume past its 80% baseline"
    assert webapp.config["local_render_gain"] == 2.0, "still no change to the boost"

    r = client.post("/api/master_volume", json={"percent": 100})  # back to 100: baseline restored exactly
    assert webapp.config["speakers"]["MV_ON"]["volume"] == 80 and webapp.config["local_volume"] == 0.8
finally:
    del webapp.speaker_mgr.speakers["MV_ON"]
    del webapp.speaker_mgr.speakers["MV_OFF"]
    webapp.config["speakers"] = _mv_saved_speakers_cfg
    webapp.config["local_volume"] = 1.0
    webapp.config["local_render_gain"] = 1.0
print("  OK")

import numpy as _np  # noqa: E402
import struct as _struct  # noqa: E402

# quiet samples, well under the soft-limiter's knee: gain should apply
# as a plain, undistorted multiply (within the float32-normalization
# round-trip's inherent +/-1-count rounding, not exact int math)
_quiet = _struct.pack("<2h", 100, -100)
_boosted = _struct.unpack("<2h", audio_engine._apply_local_gain(_quiet, 2.0))
assert abs(_boosted[0] - 200) <= 1 and abs(_boosted[1] - (-200)) <= 1, \
    f"local_render_gain must actually scale quiet samples ~linearly, got {_boosted}"

# near-full-scale samples: a hard linear multiply would clip a large
# fraction of these outright (verified: 130% gain hard-clips ~17% of a
# 90%-peak test signal) -- the soft limiter must compress instead,
# landing under the ceiling with zero samples pinned exactly at it
_loud = (_np.sin(_np.linspace(0, 2 * _np.pi, 200, False)) * 0.9 * 32767).astype(_np.int16).tobytes()
_loud_boosted = _np.frombuffer(audio_engine._apply_local_gain(_loud, 1.5), dtype=_np.int16)
assert _loud_boosted.max() < 32767 and _loud_boosted.min() > -32768, \
    "near-full-scale audio boosted 150% must be soft-limited, not hard-clipped to the ceiling"
assert (_np.abs(_loud_boosted) >= 32767).sum() == 0, \
    "the soft limiter should never pin samples exactly at full scale the way a hard clip does"
print("  local_render_gain sample scaling OK (quiet audio scales linearly, loud audio soft-limits)")

# the dashboard volume slider goes down as well as up: turning DOWN must be a
# plain linear scale, not run through the limiter (which would still squash a
# loud peak while making the sound quieter)
_full_peak = _struct.pack("<2h", 32000, -32000)
_down = _struct.unpack("<2h", audio_engine._apply_local_gain(_full_peak, 0.9))
assert _down == (28800, -28800), f"90% volume must scale a loud peak by exactly 0.9, got {_down}"
_half = _np.frombuffer(audio_engine._apply_local_gain(_loud, 0.5), dtype=_np.int16)
_orig = _np.frombuffer(_loud, dtype=_np.int16)
assert _np.abs(_half.astype(_np.int32) * 2 - _orig).max() <= 2, "50% volume must be a straight halving"
assert not any(audio_engine._apply_local_gain(_loud, 0.0)), "0% volume is silence"
print("  local volume below 100% is a plain linear scale OK")

r = client.post("/api/local_eq", json={"bass": 6, "mid": -3, "treble": 999})  # treble clamps
assert r.status_code == 200
assert webapp.config["local_eq_bass_db"] == 6.0
assert webapp.config["local_eq_mid_db"] == -3.0
assert webapp.config["local_eq_treble_db"] == 24.0, "EQ bands must clamp to +/-24dB"
r = client.post("/api/local_eq", json={"bass": 0, "mid": 0, "treble": 0})  # restore flat for later tests
assert webapp.config["local_eq_bass_db"] == 0.0
print("  /api/local_eq OK (clamped to +/-24dB)")

print("[test] _ThreeBandEQ: flat is a byte-exact passthrough, each band only affects its own range...")
_RATE = 44100
_t = _np.linspace(0, 0.3, int(_RATE * 0.3), False)


def _tone(freq):
    sig = (0.3 * _np.sin(2 * _np.pi * freq * _t) * 32767).astype(_np.int16)
    return _np.repeat(sig[:, None], 2, axis=1).flatten().tobytes()


def _rms(pcm):
    return _np.sqrt(_np.mean(_np.frombuffer(pcm, dtype=_np.int16).astype(_np.float64) ** 2))


_bass_tone, _treble_tone = _tone(100), _tone(8000)

_flat_eq = audio_engine._ThreeBandEQ(_RATE, 2)
assert _flat_eq.process(_bass_tone, 0.0, 0.0, 0.0) == _bass_tone, \
    "a flat (0,0,0) EQ must be a byte-exact passthrough, not just numerically close"

_bass_boost = audio_engine._ThreeBandEQ(_RATE, 2)
bass_before, bass_after = _rms(_bass_tone), _rms(_bass_boost.process(_bass_tone, 12.0, 0.0, 0.0))
assert bass_after > bass_before * 3, "boosting bass +12dB should strongly affect a 100Hz tone"

_bass_boost_hf = audio_engine._ThreeBandEQ(_RATE, 2)
treble_before, treble_after_bassboost = _rms(_treble_tone), _rms(_bass_boost_hf.process(_treble_tone, 12.0, 0.0, 0.0))
assert 0.8 < treble_after_bassboost / treble_before < 1.2, \
    "boosting bass should NOT meaningfully affect an 8000Hz tone (frequency selectivity)"

_treble_boost = audio_engine._ThreeBandEQ(_RATE, 2)
treble_after = _rms(_treble_boost.process(_treble_tone, 0.0, 0.0, 12.0))
assert treble_after > treble_before * 1.5, "boosting treble +12dB should strongly affect an 8000Hz tone"
print("  OK (bass/treble shelves are frequency-selective, flat setting is a true no-op)")

# an extreme boost on an already-loud signal is exactly the "make it
# shudder" case a hard clip would ruin -- confirm the EQ's own output
# stays soft-limited (no sample pinned exactly at the ceiling), not
# hard-clipped, at the maximum +24dB the dashboard now allows
_loud_bass_tone = (_np.sin(_np.linspace(0, 2 * _np.pi * 30, int(_RATE * 0.3), False))
                    * 0.9 * 32767).astype(_np.int16)
_loud_bass_tone = _np.repeat(_loud_bass_tone[:, None], 2, axis=1).flatten().tobytes()
_extreme_eq = audio_engine._ThreeBandEQ(_RATE, 2)
_extreme_out = _np.frombuffer(_extreme_eq.process(_loud_bass_tone, 24.0, 0.0, 0.0), dtype=_np.int16)
assert (_np.abs(_extreme_out) >= 32767).sum() == 0, \
    "even a +24dB boost on already-loud audio must soft-limit, never hard-clip to the ceiling"
print("  OK (a +24dB boost on loud audio soft-limits, doesn't hard-clip)")

print("[test] _ThreeBandEQ: the fast (FFT) filter matches the sample-by-sample biquad chain it replaced...")


def _reference_eq_chunks(chunks, channels, rate, bass, mid, treble):
    """The original implementation, kept here as the reference: three biquads
    per channel run one sample at a time in Python, state carried across chunks."""
    bands = [[audio_engine._Biquad(), audio_engine._Biquad(), audio_engine._Biquad()] for _ in range(channels)]
    for chain in bands:
        chain[0].set_coeffs(*audio_engine._low_shelf_coeffs(audio_engine._EQ_BASS_HZ, rate, bass))
        chain[1].set_coeffs(*audio_engine._peaking_coeffs(audio_engine._EQ_MID_HZ, rate, mid, audio_engine._EQ_MID_Q))
        chain[2].set_coeffs(*audio_engine._high_shelf_coeffs(audio_engine._EQ_TREBLE_HZ, rate, treble))
    outs = []
    for c in chunks:
        arr = _np.frombuffer(c, dtype=_np.int16).astype(_np.float64).reshape(-1, channels)
        out = _np.empty_like(arr)
        for ch in range(channels):
            b_f, m_f, t_f = bands[ch]
            for i in range(arr.shape[0]):
                out[i, ch] = t_f.process(m_f.process(b_f.process(arr[i, ch])))
        limited = audio_engine._soft_limit((out / 32768.0).astype(_np.float32))
        outs.append((limited * 32767.0).astype(_np.int16))
    return outs


_rng = _np.random.default_rng(7)
_EQ_RATE = 48000
_tt = _np.arange(int(_EQ_RATE * 1.5)) / _EQ_RATE
_music = _np.stack([
    sum(0.15 * _np.sin(2 * _np.pi * f * _tt + p) for f, p in ((60, 0.3), (440, 1.1), (3000, 2.0), (9000, 0.7))),
    sum(0.15 * _np.sin(2 * _np.pi * f * _tt + p) for f, p in ((90, 1.9), (700, 0.2), (5000, 0.5), (12000, 2.4))),
], axis=1) + _rng.standard_normal((len(_tt), 2)) * 0.02
_music_pcm = (_np.clip(_music, -1, 1) * 32767).astype(_np.int16)
# chunk sizes that differ from each other and from the FFT size, as the real
# render loop's do (1024 frames, or 1114/1115 after resampling from 44.1kHz)
_sizes, _pos, _eq_chunks = [1024, 1114, 1115, 700, 1024, 1024, 333], 0, []
while _pos < len(_music_pcm):
    _n = _sizes[len(_eq_chunks) % len(_sizes)]
    _eq_chunks.append(_music_pcm[_pos:_pos + _n].tobytes())
    _pos += _n
for _bands in ((13.0, 0.0, 0.0), (6.0, -3.0, 4.0), (-8.0, 5.0, -6.0), (24.0, 24.0, 24.0), (0.5, 0.0, 0.0)):
    _ref = _reference_eq_chunks(_eq_chunks, 2, _EQ_RATE, *_bands)
    _fast_eq = audio_engine._ThreeBandEQ(_EQ_RATE, 2)
    _worst = 0
    for _c, _r in zip(_eq_chunks, _ref):
        _got = _np.frombuffer(_fast_eq.process(_c, *_bands), dtype=_np.int16).reshape(-1, 2)
        assert _got.shape == _r.shape, (_got.shape, _r.shape)
        _worst = max(_worst, int(_np.abs(_got.astype(_np.int32) - _r.astype(_np.int32)).max()))
    assert _worst <= 1, f"EQ {_bands}: fast filter is {_worst} steps away from the biquad chain (must be <= 1)"
print("  matches to within one 16-bit step at every setting, across uneven chunk boundaries OK")

_t0 = time.perf_counter()
_reference_eq_chunks(_eq_chunks[:20], 2, _EQ_RATE, 13.0, 0.0, 0.0)
_t_ref = time.perf_counter() - _t0
_fast_eq = audio_engine._ThreeBandEQ(_EQ_RATE, 2)
_fast_eq.process(_eq_chunks[0], 13.0, 0.0, 0.0)  # first call builds the impulse response
_t0 = time.perf_counter()
for _c in _eq_chunks[:20]:
    _fast_eq.process(_c, 13.0, 0.0, 0.0)
_t_fast = time.perf_counter() - _t0
# the whole point: this stage must leave the render loop nearly all of each
# 21ms chunk (measured ~15x faster; 3x leaves room for a slow CI machine)
assert _t_fast * 3 < _t_ref, f"fast EQ {_t_fast * 1000:.1f}ms vs per-sample {_t_ref * 1000:.1f}ms for 20 chunks"
print(f"  {_t_ref / _t_fast:.0f}x faster than the per-sample loop OK")

# changing the settings mid-stream must not click: no jump between the last
# sample of one chunk and the first of the next beyond what the music itself does
_sw_eq = audio_engine._ThreeBandEQ(_EQ_RATE, 2)
_sw_out = []
for _i, _c in enumerate(_eq_chunks[:16]):
    _bands = (13.0, 0.0, 0.0) if _i < 8 else (2.0, 4.0, -5.0)
    _sw_out.append(_np.frombuffer(_sw_eq.process(_c, *_bands), dtype=_np.int16).reshape(-1, 2))
_sw = _np.concatenate(_sw_out).astype(_np.int32)
_steps = _np.abs(_np.diff(_sw, axis=0)).max()
_music_steps = _np.abs(_np.diff(_np.concatenate([_np.frombuffer(c, dtype=_np.int16).reshape(-1, 2)
                                                for c in _eq_chunks[:16]]).astype(_np.int32), axis=0)).max()
assert _steps < _music_steps * 4 + 2000, f"EQ change clicked: biggest sample step {_steps} (music's own: {_music_steps})"
print("  changing the EQ mid-stream doesn't click OK")

print("[test] _StandingBacklogGuard: bursts are fine, a queue that never drains is trimmed...")
_g = audio_engine._StandingBacklogGuard(min_backlog=3, window_s=0.5)
_now, _drops = 0.0, 0
for _i in range(200):                     # 4s of bursty-but-draining arrival
    _drops += _g.check([0, 2, 1, 0, 3, 0][_i % 6], _now)
    _now += 0.02
assert _drops == 0, "a queue that keeps returning to empty is bursts, not a backlog"
_g = audio_engine._StandingBacklogGuard(min_backlog=3, window_s=0.5)
_now, _drops = 0.0, []
for _i in range(120):                     # 2.4s with the queue never below 4 chunks
    _drops.append(_g.check(4, _now))
    _now += 0.02
assert [d for d in _drops if d] and sum(_drops) >= 3, _drops
assert _drops.count(3) >= 1 and max(_drops) == 3, "should drop all but one of the standing backlog"
_g = audio_engine._StandingBacklogGuard(min_backlog=3, window_s=0.5)
_now, _drops = 0.0, 0
for _i in range(100):                     # deep, but it does reach 1 every window: not standing
    _drops += _g.check(9 if _i % 20 else 1, _now)
    _now += 0.02
assert _drops == 0, "a queue that dips low each window is draining"
print("  OK")

print("[test] render loop: lag that piles up in the queue is trimmed, not kept forever...")


class _PacedStream(FakeStream):
    """An output device that, like a real one, takes real time to play what it is
    given -- on a deadline of its own, so the coarse Windows sleep tick can't make
    it play slower than real time on average."""

    def __init__(self):
        self.written = 0
        self._free_at = time.monotonic()

    def write(self, data):
        self.written += len(data)
        self._free_at = max(self._free_at, time.monotonic() - 0.05) + len(data) / 4 / 44100.0
        time.sleep(max(0.0, self._free_at - time.monotonic()))


class _PacedPyAudio(FakePyAudio):
    def open(self, **kwargs):
        self.stream = _PacedStream()
        return self.stream


_real_pa_for_render = audio_engine._pa
_paced = _PacedPyAudio()
audio_engine._pa = _paced
_saved_render_cfg = {k: audio_engine.config.get(k) for k in
                     ("local_delay_ms", "render_device_substr", "local_eq_bass_db", "sample_rate", "channels")}
audio_engine.config.update(local_delay_ms=0, render_device_substr="", local_eq_bass_db=0.0,
                           sample_rate=44100, channels=2)
_before = set(audio_engine.broadcaster._subs)
_stop = threading.Event()
_rt = threading.Thread(target=audio_engine.render_loop, args=(_stop,), daemon=True)
_rt.start()
for _ in range(100):
    _new = set(audio_engine.broadcaster._subs) - _before
    if _new:
        break
    time.sleep(0.05)
assert _new, "the render session never subscribed"
_rq = audio_engine.broadcaster._subs[_new.pop()]
_c = b"\x10\x00" * 1024 * 2
for _ in range(40):                       # a sudden ~0.9s of lag
    audio_engine.broadcaster.publish(_c)
_feeding = threading.Event()


def _feed():                              # ...and audio keeps arriving at exactly real time after it
    nxt = time.monotonic()
    while not _feeding.is_set():
        audio_engine.broadcaster.publish(_c)
        nxt += 1024 / 44100.0
        time.sleep(max(0.0, nxt - time.monotonic()))


_ft = threading.Thread(target=_feed, daemon=True)
_ft.start()
time.sleep(0.1)
_peak = _rq.qsize()
_depths = []
for _ in range(125):                      # 2.5s, sampled every 20ms
    time.sleep(0.02)
    _depths.append(_rq.qsize())
_left = _depths[-1]
_after_trim = max(_depths[60:])           # from 1.2s on, well after the first trim
_feeding.set()
_stop.set()
_rt.join(timeout=3)
_ft.join(timeout=1)
audio_engine._pa = _real_pa_for_render
audio_engine.config.update(_saved_render_cfg)
assert _peak >= 20, f"test setup: expected a big backlog to start with, saw {_peak}"
assert _after_trim <= 8, (f"a {_peak}-chunk backlog was still up to {_after_trim} chunks deep 1.2s later -- "
                          f"that lag would have been permanent")
assert _paced.stream.written > 40 * 4096, "audio must keep playing while the backlog is trimmed"
print(f"  {_peak}-chunk backlog trimmed to at most {_after_trim} within seconds, playback continued OK")

r = client.get("/api/audio_sessions")
body = r.get_json()
assert r.status_code == 200 and "sessions" in body and body["mode"] == "system" and body["targets"] == []
print("  /api/audio_sessions OK")

r = client.post("/api/capture_source", json={"mode": "bogus"})
assert r.status_code == 400
r = client.post("/api/capture_source", json={"mode": "apps", "targets": []})
assert r.status_code == 400, "apps mode with no targets should be rejected"
print("  /api/capture_source OK (rejects invalid input)")

print("[test] per-app audio dispatcher: three failure modes, three different responses...")
import audio_engine  # noqa: E402  (already imported above; re-import is a no-op)
import types as _types
_orig_mode = webapp.config.get("capture_mode")
_orig_targets = webapp.config.get("capture_target_names")


def _with_fake_per_app_audio(fake_module_or_none, target_names, body):
    webapp.config["capture_mode"] = "apps"
    webapp.config["capture_target_names"] = target_names
    if fake_module_or_none is None:
        sys.modules["per_app_audio"] = None  # the documented way to make `import x` raise ImportError
    else:
        sys.modules["per_app_audio"] = fake_module_or_none
    try:
        body()
    finally:
        sys.modules.pop("per_app_audio", None)
        webapp.config["capture_mode"] = _orig_mode
        webapp.config["capture_target_names"] = _orig_targets


try:
    # (a) can't even be imported (e.g. pycaw/comtypes missing -- a non-
    # Windows box, or an old Windows without process-loopback support):
    # this is permanent for the rest of the run, so fall back for good.
    def _check_a():
        audio_engine._capture_loop_apps(threading.Event())  # must not raise
        assert webapp.config["capture_mode"] == "system", \
            "an unimportable per-app backend should fall back to whole-system capture"
    _with_fake_per_app_audio(None, ["spotify.exe"], _check_a)
    print("  OK (unimportable backend falls back to system mode)")

    # (b) imports fine, but listing sessions blew up right now (e.g. a
    # transient COM error): don't punish that with a permanent fallback.
    def _check_b():
        audio_engine._capture_loop_apps(threading.Event())  # must not raise
        assert webapp.config["capture_mode"] == "apps", \
            "a transient listing failure must NOT force a fallback to system mode"
    broken = _types.ModuleType("per_app_audio")
    broken.list_audio_sessions = lambda: (_ for _ in ()).throw(OSError("simulated COM hiccup"))
    _with_fake_per_app_audio(broken, ["spotify.exe"], _check_b)
    print("  OK (a transient listing error is a clean no-op, not a fallback)")

    # (c) backend works fine, none of the selected apps are running yet.
    def _check_c():
        audio_engine._capture_loop_apps(threading.Event())  # not found -- clean no-op
        assert webapp.config["capture_mode"] == "apps", \
            "selected apps simply not running (yet) must NOT force a fallback to system mode"
    fake = _types.ModuleType("per_app_audio")
    fake.list_audio_sessions = lambda: [{"pid": 123, "name": "chrome.exe"}]
    _with_fake_per_app_audio(fake, ["definitely_not_a_running_app.exe"], _check_c)
    print("  OK (no selected app currently running is a clean no-op, not a fallback)")

    # (d) two selected apps both running: each should get mixed in, and
    # a source that stops producing frames should drop out of the mix
    # without affecting the other.
    def _check_d():
        stop = threading.Event()
        t = threading.Thread(target=audio_engine._capture_loop_apps, args=(stop,), daemon=True)
        t.start()
        time.sleep(0.3)
        stop.set()
        t.join(timeout=3)
        assert not t.is_alive(), "mixer thread should stop promptly when told to"
    per_app_audio_fake = _types.ModuleType("per_app_audio")
    per_app_audio_fake.list_audio_sessions = lambda: [
        {"pid": 111, "name": "spotify.exe"}, {"pid": 222, "name": "chrome.exe"}]

    def _fake_capture_loop(pid, stop_event, on_chunk, include_tree=True):
        pcm = (_np.ones(1024 * 2, dtype=_np.int16) * 1000).tobytes()
        while not stop_event.is_set():
            on_chunk(pcm, 48000, 2, 2)
            time.sleep(0.01)
    per_app_audio_fake.capture_loop = _fake_capture_loop
    _with_fake_per_app_audio(per_app_audio_fake, ["spotify.exe", "chrome.exe"], _check_d)
    print("  OK (multiple selected apps mix and stop cleanly)")
finally:
    webapp.config["capture_mode"] = _orig_mode
    webapp.config["capture_target_names"] = _orig_targets

print("[test] Windows loopback capture: default, silence fill, fallbacks, switching...")
# On Windows the cable can be read two ways: WASAPI loopback of "CABLE Input"
# (Windows doesn't treat that as microphone use) or by opening the cable's
# *recording* device "CABLE Output" (which it does). These drive the real
# capture loop against a fake PyAudio that has both, and check which one it
# opens, what it publishes, and that it never gets stuck.
import audio_backend as _ab  # noqa: E402

assert _ab.BACKEND == "pyaudiowpatch"  # the stub above stands in for it, even on macOS CI
_CH = audio_engine.CHUNK


_all_loopback_fakes = []


class _LoopbackFakeStream:
    """A loopback endpoint whose blocking read() returns the `chunks` it has
    ready and then blocks forever -- like Windows, which sends nothing while
    nothing is playing, and like PortAudio, where a stuck read is NOT freed by
    close(). feed() makes more audio available; release() (test cleanup only)
    lets a stuck reader thread finish."""

    def __init__(self, chunks, fail=False):
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self.remaining = chunks
        self.fail = fail
        self.released = False
        _all_loopback_fakes.append(self)

    def feed(self, chunks):
        with self._lock:
            self.remaining += chunks
        self._wake.set()

    def release(self):
        self.released = True
        self._wake.set()

    def read(self, n, exception_on_overflow=False):
        if self.fail:
            time.sleep(0.005)
            raise OSError("simulated: Unanticipated host error")
        while True:
            with self._lock:
                if self.remaining > 0:
                    self.remaining -= 1
                    return b"\x01\x00" * n * 2  # non-silent
                self._wake.clear()
            self._wake.wait()  # nothing playing: block, indefinitely
            if self.released:
                raise OSError("released")

    def stop_stream(self):
        pass

    def close(self):
        pass  # deliberately does NOT free a stuck read, exactly like the real thing


class _RecordingFakeStream(FakeStream):
    def read(self, n, exception_on_overflow=False):
        time.sleep(0.004)
        return b"\x02\x00" * n * 2


class _CableFakePyAudio:
    def __init__(self, cable_channels=2):
        def dev(name, inch, outch, rate, loop=False):
            d = {"name": name, "maxInputChannels": inch, "maxOutputChannels": outch,
                 "defaultSampleRate": rate, "hostApi": 0}
            if loop:
                d["isLoopbackDevice"] = True
            return d
        self.devices = [
            dev("CABLE Output (VB-Audio Virtual Cable)", 2, 0, 44100.0),
            dev("CABLE Input (VB-Audio Virtual Cable) [Loopback]", cable_channels, 0, 48000.0, loop=True),
            dev("Speakers (Realtek(R) Audio)", 0, 2, 48000.0),
            dev("Speakers (Realtek(R) Audio) [Loopback]", 2, 0, 48000.0, loop=True),
        ]
        self.opened = []  # input_device_index of each capture stream opened, in order
        self.fail_loopback_open = False
        self.loopback_read_fails = False
        self.loopback_chunks = 0

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, i):
        d = dict(self.devices[i])
        d["index"] = i
        return d

    def get_host_api_info_by_type(self, t):
        return {"index": 0}

    def get_host_api_info_by_index(self, i):
        return {"name": "Windows WASAPI"}

    def open(self, **kw):
        if not kw.get("input"):
            return FakeStream()
        idx = kw["input_device_index"]
        self.opened.append(idx)
        if self.devices[idx].get("isLoopbackDevice"):
            if self.fail_loopback_open:
                raise OSError("simulated: Invalid sample rate")
            return _LoopbackFakeStream(self.loopback_chunks, fail=self.loopback_read_fails)
        return _RecordingFakeStream()


_real_pa = audio_engine._pa
_real_engine_time = audio_engine.time
_saved_capture_cfg = {k: audio_engine.config.get(k) for k in
                      ("capture_method", "capture_device_substr", "capture_mode", "capture_target_names")}


class _FastSleepTime:
    """audio_engine's `time`, with sleeps shortened so the open-failure retry
    waits (2s, 4s) don't make the test slow. Only audio_engine sees this."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def sleep(s):
        time.sleep(min(s, 0.02))


def _capture_for(fake_pa, seconds, fast_sleep=False):
    """Run the real system-capture loop against `fake_pa` for `seconds`;
    returns (published chunks, status while running, thread stopped in time)."""
    audio_engine._pa = fake_pa
    if fast_sleep:
        audio_engine.time = _FastSleepTime()
    stop = threading.Event()
    sid, q = audio_engine.broadcaster.subscribe(maxlen=100000)
    th = threading.Thread(target=audio_engine._capture_loop_system, args=(stop,), daemon=True)
    th.start()
    time.sleep(seconds)
    status = audio_engine.get_capture_status()
    stop.set()
    th.join(timeout=2)
    audio_engine.broadcaster.unsubscribe(sid)
    audio_engine._pa = _real_pa
    audio_engine.time = _real_engine_time
    chunks = []
    while not q.empty():
        chunks.append(q.get_nowait())
    return chunks, status, not th.is_alive()


_real_restart_downstream = audio_engine._restart_downstream
_restart_calls = []
_saved_format = (audio_engine.config.get("sample_rate"), audio_engine.config.get("channels"))

try:
    audio_engine.config["capture_device_substr"] = "CABLE Output"
    audio_engine.config["capture_mode"] = "system"
    audio_engine.config["capture_target_names"] = []
    audio_engine.config["capture_method"] = "loopback"
    # the format config.json was left holding by earlier (44.1kHz recording-device) runs
    audio_engine.config["sample_rate"], audio_engine.config["channels"] = 44100, 2
    audio_engine._restart_downstream = lambda: _restart_calls.append(1)

    # (0) the render path and Sonos streams latch the capture format when they
    # start, so they must be restarted exactly when the format really changes
    audio_engine._publish_capture_format(44100, 2)
    time.sleep(0.1)
    assert _restart_calls == [], "same format: nothing should restart"
    audio_engine._publish_capture_format(48000, 2)
    time.sleep(0.1)
    assert len(_restart_calls) == 1 and audio_engine.config["sample_rate"] == 48000
    assert load_config()["sample_rate"] == 48000,         "the new format must be saved, so later launches find it and have nothing to restart"
    audio_engine._publish_capture_format(48000, 2)
    time.sleep(0.1)
    assert len(_restart_calls) == 1, "unchanged format must not restart again"
    audio_engine._publish_capture_format(48000, 1)
    time.sleep(0.1)
    assert len(_restart_calls) == 2, "a channel-count change is a format change too"
    _restart_calls.clear()
    audio_engine.config["sample_rate"], audio_engine.config["channels"] = 44100, 2
    print("  format changes restart downstream exactly once OK")

    # (a) lookups: loopback is found by its playback-side name; the recording
    # lookup never resolves to a loopback device (opening one with the
    # blocking recording read would hang whenever nothing plays to it).
    audio_engine._pa = _CableFakePyAudio()
    assert audio_engine.find_loopback_device("CABLE Input")[0] == 1
    assert audio_engine.find_loopback_device("no such device")[0] is None
    assert audio_engine.find_device_index("CABLE Output", want_input=True)[0] == 0
    assert audio_engine.find_device_index("Realtek", want_input=True)[0] is None, \
        "the recording lookup must not return a [Loopback] device"
    assert audio_engine._loopback_render_substr() == "CABLE Input"
    audio_engine.config["capture_device_substr"] = "CABLE-A Output"
    assert audio_engine._loopback_render_substr() == "CABLE-A Input"
    audio_engine.config["capture_device_substr"] = "CABLE Output"
    audio_engine._pa = _real_pa
    print("  device lookups OK")

    # (b) default path: opens the LOOPBACK device (not the microphone-class
    # recording one), at the loopback's own rate, and publishes what it reads
    fake = _CableFakePyAudio()
    fake.loopback_chunks = 6
    chunks, status, stopped = _capture_for(fake, 0.25)
    assert fake.opened == [1], f"loopback should be the only device opened, got {fake.opened}"
    assert status["method"] == "loopback" and status["rate"] == 48000 and status["channels"] == 2, status
    assert status["fallback_reason"] is None
    assert audio_engine.config["sample_rate"] == 48000
    real = [c for c in chunks if any(c)]
    assert len(real) == 6 and all(len(c) == _CH * 4 for c in real), (len(real), len(chunks))
    assert stopped
    time.sleep(0.1)
    assert len(_restart_calls) == 1, "44.1kHz config -> 48kHz loopback must restart downstream once"
    print("  loopback is the default and never opens the recording device OK")

    # (c) Windows sends nothing while idle: the loop keeps the streams alive
    # with real-time silence instead of blocking, then stops promptly
    fake = _CableFakePyAudio()
    fake.loopback_chunks = 0
    chunks, status, stopped = _capture_for(fake, 1.0)
    assert stopped, "an idle loopback must not stop the thread from exiting"
    assert chunks and not any(any(c) for c in chunks), "idle fill must be silence"
    assert all(len(c) == _CH * 4 for c in chunks)
    expected = (1.0 - audio_engine._LOOPBACK_IDLE_GRACE_S) * 48000 / _CH   # ~33 chunks
    assert expected * 0.5 <= len(chunks) <= expected * 1.5, \
        f"silence fill should run at real time (~{expected:.0f} chunks), got {len(chunks)}"
    assert len(_restart_calls) == 1, "same 48kHz format again: no restart"
    print(f"  idle loopback fills real-time silence ({len(chunks)} chunks in 1s) OK")

    # (d) real audio resumes after an idle stretch: silence stops, audio flows
    fake = _CableFakePyAudio()
    stream = _LoopbackFakeStream(0)
    fake.open = lambda **kw: (fake.opened.append(kw["input_device_index"]) or stream)
    audio_engine._pa = fake
    stop = threading.Event()
    sid, q = audio_engine.broadcaster.subscribe(maxlen=100000)
    th = threading.Thread(target=audio_engine._capture_loop_system, args=(stop,), daemon=True)
    th.start()
    time.sleep(0.7)            # idle: silence is being fed
    stream.feed(5)             # playback starts
    time.sleep(0.3)
    stop.set(); th.join(timeout=2)
    audio_engine.broadcaster.unsubscribe(sid)
    audio_engine._pa = _real_pa
    got = []
    while not q.empty():
        got.append(q.get_nowait())
    loud = [i for i, c in enumerate(got) if any(c)]
    assert len(loud) == 5, f"expected the 5 real chunks, got {len(loud)}"
    assert loud[0] > 0, "silence should have been fed before the audio started"
    assert not th.is_alive()
    print("  audio after an idle stretch comes through OK")

    # (e) fallbacks: every reason loopback can't be used ends up on the
    # recording device (with the reason recorded), never on silence
    fake = _CableFakePyAudio()
    del fake.devices[1]            # no loopback device at all (e.g. cable renamed)
    chunks, status, _ = _capture_for(fake, 0.2)
    assert fake.opened == [0] and status["method"] == "recording", (fake.opened, status)
    assert status["fallback_reason"] and "no loopback device" in status["fallback_reason"], status
    assert status["rate"] == 44100
    time.sleep(0.1)
    assert len(_restart_calls) == 2, "falling back to 44.1kHz changes the rate: downstream must restart"
    print("  no loopback device -> recording device, reason reported OK")

    fake = _CableFakePyAudio(cable_channels=6)   # cable switched to a surround format
    chunks, status, _ = _capture_for(fake, 0.2)
    assert fake.opened == [0] and status["method"] == "recording", (fake.opened, status)
    assert "6 channels" in status["fallback_reason"], status
    print("  non-stereo loopback -> recording device OK")

    fake = _CableFakePyAudio()
    fake.fail_loopback_open = True
    chunks, status, _ = _capture_for(fake, 1.0, fast_sleep=True)
    n_fail = audio_engine._LOOPBACK_OPEN_FAILURES_BEFORE_FALLBACK
    assert fake.opened == [1] * n_fail + [0], f"expected {n_fail} loopback attempts then the recording device, got {fake.opened}"
    assert status["method"] == "recording" and "would not open" in status["fallback_reason"], status
    assert chunks, "audio must flow on the fallback device"
    print("  loopback that won't open -> recording device after retries OK")

    # a loopback stream that keeps erroring is treated as dead and reopened,
    # not read forever (5 consecutive errors, same as the recording path)
    fake = _CableFakePyAudio()
    fake.loopback_read_fails = True
    chunks, status, stopped = _capture_for(fake, 1.5, fast_sleep=True)
    assert stopped and fake.opened.count(1) >= 2, \
        f"a dead loopback stream must be reopened, opened: {fake.opened}"
    print("  dead loopback stream is reopened OK")

    # (f) the explicit "recording" setting never touches loopback
    audio_engine.config["capture_method"] = "recording"
    fake = _CableFakePyAudio()
    chunks, status, _ = _capture_for(fake, 0.2)
    assert fake.opened == [0] and status["method"] == "recording" and status["fallback_reason"] is None, (fake.opened, status)
    print("  capture_method=recording opens only the recording device OK")

    # (g) dashboard API: validation, switching (which restarts capture), status
    audio_engine.config["capture_method"] = "loopback"
    fake = _CableFakePyAudio()
    fake.loopback_chunks = 10 ** 9
    audio_engine._pa = fake
    r = client.post("/api/capture_method", json={"method": "bogus"})
    assert r.status_code == 400
    r = client.post("/api/capture_method", json={"method": "recording"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["configured"] == "recording", body
    assert body["capture"]["method"] == "recording", body
    assert webapp.config["capture_method"] == "recording"
    r = client.post("/api/capture_method", json={"method": "loopback"})
    body = r.get_json()
    assert body["configured"] == "loopback" and body["capture"]["method"] == "loopback", body
    assert body["capture"]["device"].endswith("[Loopback]") and body["capture"]["rate"] == 48000, body
    r = client.get("/api/capture_method")
    assert r.get_json()["supported"] is True
    r = client.get("/api/platform_status")
    assert r.get_json()["capture"]["method"] == "loopback"
    import diagnostics as _diagnostics_mod  # noqa: E402
    snap = _diagnostics_mod.system_snapshot()
    assert "Audio capture: loopback from 'CABLE Input" in snap and "capture_method=loopback" in snap, snap
    assert b'id="captureMethod"' in client.get("/").data
    print("  /api/capture_method, /api/platform_status, diagnostics OK")
finally:
    for _fake_stream in _all_loopback_fakes:
        _fake_stream.release()
    audio_engine._pa = _real_pa
    audio_engine.time = _real_engine_time
    audio_engine._restart_downstream = _real_restart_downstream
    audio_engine.config["sample_rate"], audio_engine.config["channels"] = _saved_format
    for _t, _ev in ((audio_engine._capture_thread, audio_engine._capture_stop_event),
                    (audio_engine._render_thread, audio_engine._render_stop_event)):
        if _ev is not None:
            _ev.set()
        if _t is not None:
            _t.join(timeout=3)
    for _k, _v in _saved_capture_cfg.items():
        audio_engine.config[_k] = _v

assert DEFAULT_CONFIG["capture_method"] == ("loopback" if sys.platform == "win32" else "recording"), \
    "Windows defaults to loopback (no microphone access); other platforms have no such option"

# the real downstream restart (render path + Sonos reconnect) must run cleanly with no speakers around
audio_engine._restart_downstream()
time.sleep(0.2)
audio_engine._render_stop_event.set()
audio_engine._render_thread.join(timeout=3)
print("  OK")

wav = webapp.wav_header(44100, 2, 2)
assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and b"fmt " in wav and b"data" in wav
print("  wav_header() OK")

# dashboard password: open by default, enforced once PASSWORD_PATH exists,
# stream endpoint always open (Sonos can't do HTTP auth)
def _test_dashboard_password():
    import base64
    from config import PASSWORD_PATH
    if PASSWORD_PATH.exists():
        print(f"  dashboard password test SKIPPED ({PASSWORD_PATH} exists -- won't touch a real one)")
        return
    assert webapp._dashboard_password() is None
    assert client.get("/").status_code == 200, "open when no password file"
    PASSWORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    PASSWORD_PATH.write_text("  s3cret \n", encoding="utf-8")  # whitespace stripped
    try:
        assert webapp._dashboard_password() == "s3cret"
        assert client.get("/").status_code == 401, "protected once the file exists"
        assert client.get("/api/speakers").status_code == 401
        hdr = lambda u, p: {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
        assert client.get("/", headers=hdr("x", "s3cret")).status_code == 200
        assert client.get("/", headers=hdr("x", "nope")).status_code == 401
        assert client.get("/stream/RINCON_X.wav", headers=hdr("x", "nope")).status_code != 401, \
            "stream endpoint must stay open for Sonos"
    finally:
        PASSWORD_PATH.unlink()
    assert webapp._dashboard_password() is None, "removing the file re-opens the dashboard"
    print("  dashboard password OK (open by default, enforced when set)")


_test_dashboard_password()

print("[test] watchdog restarts dropped streams, respects other sources + cooldown...")
import sonos_ctl  # noqa: E402


class FakeAV:
    def __init__(self, uri):
        self.uri = uri

    def GetMediaInfo(self, args):
        return {"CurrentURI": self.uri}

    def SetAVTransportURI(self, args):
        pass


class FakeZone:
    def __init__(self, uid, uri, state):
        self.uid = uid
        self.player_name = f"Fake {uid[-4:]}"
        self.avTransport = FakeAV(uri)
        self._state = state
        self.play_count = 0

    def get_current_transport_info(self):
        return {"current_transport_state": self._state}

    def play_uri(self, uri="", meta="", title="", **kw):
        self.play_count += 1
        self.avTransport.uri = uri
        self._state = "PLAYING"


mgr = sonos_ctl.SpeakerManager()
base = "http://192.168.1.5:5757"
_wd_saved_speakers = dict(webapp.config["speakers"])  # restored at end of this block

uid_a = "RINCON_AAAA0001"
za = FakeZone(uid_a, f"{base}/stream/{uid_a}.wav", "STOPPED")  # our stream, dropped
uid_b = "RINCON_BBBB0002"
zb = FakeZone(uid_b, "x-sonos-spotify:some_song", "PLAYING")   # user picked Spotify
mgr.speakers[uid_a] = za
mgr.speakers[uid_b] = zb
webapp.config["speakers"][uid_a] = {"enabled": True, "volume": 50}
webapp.config["speakers"][uid_b] = {"enabled": True, "volume": 50}

mgr.watchdog_tick(base)
assert za.play_count == 1, "watchdog should have restarted the dropped stream"
assert mgr.streams[uid_a] is True
assert zb.play_count == 0, "watchdog must NOT hijack a speaker playing another source"
assert mgr.streams[uid_b] is False

za._state = "STOPPED"  # drops again immediately
mgr.watchdog_tick(base)
assert za.play_count == 1, "cooldown should prevent an immediate second restart"
# simulate cooldown elapsed. Not a plain 0: the check is
# time.monotonic() - last >= 45, and monotonic() counts from boot, so on a
# freshly booted CI VM (uptime < 45s) 0 would look like "just restarted".
mgr._last_auto_restart[uid_a] = time.monotonic() - 3600
mgr.watchdog_tick(base)
assert za.play_count == 2, "watchdog should restart again after the cooldown"

webapp.config["speakers"][uid_a]["enabled"] = False
za._state = "STOPPED"
mgr._last_auto_restart[uid_a] = 0
mgr.watchdog_tick(base)
assert za.play_count == 2, "disabled speakers must never be auto-restarted"

# boot-claim: a newly-seen speaker sitting idle on some OLD source gets
# claimed for PC audio (this is the Windows-startup case), while one
# actively playing another source (zb, Spotify) stays untouched
uid_c = "RINCON_CCCC0003"
zc = FakeZone(uid_c, "x-sonos-spotify:stale_from_yesterday", "STOPPED")
mgr.speakers[uid_c] = zc
webapp.config["speakers"][uid_c] = {"enabled": True, "volume": 50}
mgr.watchdog_tick(base)
assert zc.play_count == 1, "idle speaker should be claimed at boot"
assert zb.play_count == 0, "actively-playing other source must stay untouched"
webapp.config["speakers"] = _wd_saved_speakers  # don't leak fake speakers to config.json
print("  OK")

print("[test] watchdog periodic resync: reconnects a long-running stream to reset drift...")
uid_d = "RINCON_DDDD0004"
zd = FakeZone(uid_d, f"{base}/stream/{uid_d}.wav", "PLAYING")  # already streaming ours
mgr2 = sonos_ctl.SpeakerManager()
mgr2.speakers[uid_d] = zd
mgr2._boot_started.add(uid_d)  # as if this speaker has been running for a while already
mgr2._last_auto_restart[uid_d] = time.monotonic()  # just (re)started -- fresh connection
webapp.config["speakers"][uid_d] = {"enabled": True, "volume": 50}
try:
    mgr2.watchdog_tick(base)
    assert zd.play_count == 0, "a freshly-(re)started stream must not be reconnected yet"
    assert mgr2.streams[uid_d] is True

    mgr2._last_auto_restart[uid_d] = time.monotonic() - sonos_ctl.RESYNC_INTERVAL_SECONDS - 1
    mgr2.watchdog_tick(base)
    assert zd.play_count == 1, \
        "a stream running past RESYNC_INTERVAL_SECONDS should be silently reconnected"
    assert mgr2.streams[uid_d] is True
finally:
    webapp.config["speakers"].pop(uid_d, None)
print("  OK")

print("[test] reconnect_all_streaming: only touches speakers actually streaming ours...")
uid_e = "RINCON_EEEE0005"
ze = FakeZone(uid_e, f"{base}/stream/{uid_e}.wav", "PLAYING")
uid_f = "RINCON_FFFF0006"
zf = FakeZone(uid_f, "x-sonos-spotify:some_song", "PLAYING")  # enabled, but playing something else
mgr3 = sonos_ctl.SpeakerManager()
mgr3.speakers[uid_e] = ze
mgr3.speakers[uid_f] = zf
mgr3.streams[uid_e] = True
mgr3.streams[uid_f] = False
mgr3.reconnect_all_streaming(base)
assert ze.play_count == 1, "a speaker actively streaming our audio must be reconnected"
assert zf.play_count == 0, "a speaker not currently streaming ours must be left alone"
print("  OK")

print("[test] startup: default speaker + new-speaker-enabled default...")
_cfg = sonos_ctl.config
_saved = {k: _cfg.get(k) for k in
          ("default_speaker_ip", "default_speaker_uid", "new_speakers_default_enabled")}
_saved_speakers = dict(_cfg["speakers"])  # these tests call save_config()
try:
    m2 = sonos_ctl.SpeakerManager()

    # _default_enabled_for: follows the config flag, but the default
    # speaker is always enabled
    _cfg["new_speakers_default_enabled"] = False
    _cfg["default_speaker_uid"] = "RINCON_DEFAULT"
    assert m2._default_enabled_for("RINCON_OTHER") is False
    assert m2._default_enabled_for("RINCON_DEFAULT") is True
    _cfg["new_speakers_default_enabled"] = True
    assert m2._default_enabled_for("RINCON_OTHER") is True

    # prime_default_speaker: reachable IP -> registered + uid recorded
    class FakeSoCo:
        def __init__(self, ip):
            if ip == "10.0.0.9":
                raise OSError("unreachable")
            self.ip_address = ip
            self.uid = "RINCON_PRIMED" if ip == "10.0.0.5" else "RINCON_SOMEONEELSE"
            self.player_name = "Primed"
        @property
        def group(self):
            return None
    _orig_soco = sonos_ctl.SoCo
    _orig_sleep = sonos_ctl.time.sleep
    sonos_ctl.SoCo = FakeSoCo
    sonos_ctl.time.sleep = lambda _s: None   # don't wait out the prime retry
    try:
        _cfg["default_speaker_ip"] = ""
        _cfg["default_speaker_uid"] = ""
        assert m2.prime_default_speaker() is None, "blank IP -> nothing"

        _cfg["default_speaker_ip"] = "10.0.0.5"
        _cfg["default_speaker_uid"] = ""
        assert m2.prime_default_speaker() == "RINCON_PRIMED"
        assert _cfg["default_speaker_uid"] == "RINCON_PRIMED", "uid auto-recorded"
        assert "RINCON_PRIMED" in m2.speakers
        assert _cfg["speakers"]["RINCON_PRIMED"]["enabled"] is True

        # IP now answers as a different device -> ignored
        m2b = sonos_ctl.SpeakerManager()
        _cfg["default_speaker_ip"] = "10.0.0.7"  # -> RINCON_SOMEONEELSE
        assert m2b.prime_default_speaker() is None, "uid mismatch -> ignore the IP"

        # unreachable IP -> None, no crash
        m2c = sonos_ctl.SpeakerManager()
        _cfg["default_speaker_uid"] = ""
        _cfg["default_speaker_ip"] = "10.0.0.9"
        assert m2c.prime_default_speaker() is None
    finally:
        sonos_ctl.SoCo = _orig_soco
        sonos_ctl.time.sleep = _orig_sleep

    # set_default_speaker: pin by uid (needs a known IP), and clear
    m3 = sonos_ctl.SpeakerManager()
    m3.speakers["RINCON_X"] = FakeZone("RINCON_X", "", "STOPPED")
    m3.speakers["RINCON_X"].ip_address = "10.0.0.42"
    assert m3.set_default_speaker("RINCON_X") is True
    assert _cfg["default_speaker_ip"] == "10.0.0.42"
    assert _cfg["default_speaker_uid"] == "RINCON_X"
    assert m3.set_default_speaker("RINCON_UNKNOWN") is False, "no IP -> refuse"
    assert m3.set_default_speaker(None) is True
    assert _cfg["default_speaker_ip"] == "" and _cfg["default_speaker_uid"] == ""
    print("  OK")
finally:
    _cfg.update(_saved)
    _cfg["speakers"] = _saved_speakers
    sonos_ctl.save_config(_cfg)

print("[test] on_demand discovery loop: one pass, then idle until asked...")
import main as _main  # noqa: E402
_calls = []
_orig_rd = sonos_ctl.speaker_mgr.rediscover
sonos_ctl.speaker_mgr.rediscover = lambda: _calls.append(1)
try:
    _stop = threading.Event()
    # shrink the 20s safety-net wait so the test is quick
    _t = threading.Thread(target=_main.sonos_discovery_loop, args=(_stop, True), daemon=True)
    # monkeypatch stop_event.wait so the initial 20s becomes instant
    _real_wait = _stop.wait
    _stop.wait = lambda t=None: _real_wait(0.05 if t == 20 else (t or 0))
    _t.start()
    time.sleep(0.6)
    assert len(_calls) == 1, f"on_demand should scan exactly once up front, got {len(_calls)}"
    sonos_ctl.speaker_mgr.rescan_requested.set()  # simulate the Rescan button
    time.sleep(0.4)
    assert len(_calls) == 2, "a rescan request should trigger exactly one more scan"
    _stop.set()
    sonos_ctl.speaker_mgr.rescan_requested.set()
finally:
    sonos_ctl.speaker_mgr.rediscover = _orig_rd
    sonos_ctl.speaker_mgr.rescan_requested.clear()
print("  OK")

print("[test] single-instance lock: second acquire is refused...")
# unique name so this doesn't collide with a real running PC2Sonos.exe
_lock_name = f"PC2Sonos-test-{os.getpid()}"
_h1 = _main.acquire_single_instance_lock(_lock_name)
assert _h1 is not None, "first acquire should succeed"
_h2 = _main.acquire_single_instance_lock(_lock_name)
if sys.platform == "win32":
    assert _h2 is None, "second acquire (same name) must be refused"
    import ctypes as _ct
    _ct.WinDLL("kernel32").CloseHandle(_h1)  # release so nothing wedges
elif sys.platform == "darwin":
    assert _h2 is None, "second acquire (same name) must be refused (flock)"
    _h1.close()  # releases the flock so nothing wedges
else:
    assert _h2 is not None, "Linux: lock is a no-op, always granted"
print("  OK")

print("[test] update checker: version compare, and the route never touches the network...")
import updater  # noqa: E402
assert updater._parse_version("v1.2.10") > updater._parse_version("1.2.9"), \
    "1.2.10 must sort newer than 1.2.9 (a plain string compare gets this backwards)"
assert updater._parse_version("v1.2.5") == updater._parse_version("1.2.5")
assert not (updater._parse_version("1.2.5") > updater._parse_version("1.2.5"))
# check_for_update_async() is never called anywhere in this test run, so the
# cache must still be in its untouched default state -- and the route must
# just read that cache, not reach out to GitHub itself.
status = updater.get_status()
assert status["checked"] is False and status["update_available"] is False
r = client.get("/api/update_status")
assert r.status_code == 200 and r.get_json() == status
print("  OK")

# The release notes ride along in the SAME response the version check already
# fetches (no extra request), are capped, and reach the dashboard through the
# route -- so the changelog can be read without downloading anything.
class _FakeGitHubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


_real_requests_get = updater.requests.get
_gh_calls = []


def _fake_github(url, **kw):
    _gh_calls.append(url)
    return _FakeGitHubResponse({
        "tag_name": "v99.0.0", "name": "PC2Sonos v99.0.0",
        "html_url": "https://github.com/Austinshu/PC2Sonos/releases/tag/v99.0.0",
        "body": "- **Fixed** a thing\r\n- second\r\n" + "x" * 20000,
        "assets": [{"name": updater._ASSET_NAME, "browser_download_url": "https://example.invalid/PC2Sonos-Setup.exe"}],
    })


try:
    updater.requests.get = _fake_github
    updater._check(5)
    _st = updater.get_status()
    assert len(_gh_calls) == 1, "the notes must come from the one request the version check already makes"
    assert _st["update_available"] and _st["latest_version"] == "v99.0.0"
    assert _st["notes"].startswith("- **Fixed** a thing") and len(_st["notes"]) == updater._MAX_NOTES_CHARS
    assert _st["release_url"].endswith("/tag/v99.0.0") and _st["download_url"].endswith("PC2Sonos-Setup.exe")
    assert client.get("/api/update_status").get_json()["notes"] == _st["notes"]
    updater.requests.get = lambda url, **kw: _FakeGitHubResponse({"tag_name": "v99.0.1", "assets": []})
    updater._check(5)  # a release with no body: empty string, never None
    assert updater.get_status()["notes"] == ""
finally:
    updater.requests.get = _real_requests_get
    with updater._lock:
        updater._status.update(checked=False, update_available=False, latest_version=None,
                               download_url=None, release_name=None, release_url=None, notes="")
_page = client.get("/").data.decode("utf-8")
assert 'id="updateNotes"' in _page and "function renderNotes" in _page and "function addInline" in _page
print("  release notes are captured from the one request, capped, and served to the dashboard OK")

print("[test] diagnostics module...")
import diagnostics  # noqa: E402

# system_snapshot() must never raise, even on a non-Windows box with no
# real Sonos/audio hardware -- every Windows-only lookup inside it is
# supposed to degrade to a "n/a (not Windows)" string instead. On an
# actual Windows box (every real PC2Sonos install, and this test suite's
# own dev machine), those same lookups correctly return real data
# instead -- so the assertion has to check for the opposite thing
# depending on which platform is actually running the suite.
snap = diagnostics.system_snapshot()
assert isinstance(snap, str) and "OS:" in snap and "LAN IP:" in snap
if sys.platform == "win32":
    assert "not Windows" not in snap, \
        "on Windows, every lookup should return real data, never the non-Windows placeholder"
elif sys.platform == "darwin":
    # macOS has its own real lookups in place of the Windows-only ones
    assert "BlackHole" in snap and "Microphone" in snap, snap
else:
    assert "not Windows" in snap  # confirms the win32-only branches are hit and handled
print("  system_snapshot() OK")

zpath = diagnostics.export_diagnostics_zip()
assert zpath.exists(), "diagnostics zip was not created"
import zipfile as _zipfile
with _zipfile.ZipFile(zpath) as z:
    names = z.namelist()
    assert "snapshot.txt" in names, names
zpath.unlink()  # clean up -- this test doesn't get its own Desktop
print("  export_diagnostics_zip() OK")

# install_global_exception_logging() must not itself raise, and an
# uncaught exception in a background thread must not propagate/crash
# the process -- it should just print and let the thread die quietly.
diagnostics.install_global_exception_logging()


def _boom():
    raise RuntimeError("simulated crash for the excepthook test")


t = threading.Thread(target=_boom, daemon=True)
t.start()
t.join(timeout=2)
assert not t.is_alive()
print("  threading.excepthook installed and doesn't crash the process OK")

print("[test] discovery loop survives a bad rediscover() call...")


def _bad_rediscover():
    raise RuntimeError("simulated discovery failure")


orig_rediscover = sonos_ctl.speaker_mgr.rediscover
sonos_ctl.speaker_mgr.rediscover = _bad_rediscover
import main  # noqa: E402
loop_stop = threading.Event()
loop_thread = threading.Thread(target=main.sonos_discovery_loop, args=(loop_stop,), daemon=True)
loop_thread.start()
time.sleep(0.5)
assert loop_thread.is_alive(), "discovery loop must survive a single bad rediscover() call"
loop_stop.set()
loop_thread.join(timeout=2)
sonos_ctl.speaker_mgr.rediscover = orig_rediscover
print("  OK")

print("[test] calibration DSP core (chirp + cross-correlation)...")
import numpy as np  # noqa: E402
import calibration  # noqa: E402

np.random.seed(12345)  # deterministic -- this test must not be flaky
RATE = 44100
true_delay_ms = 1730  # a made-up "Sonos took this long" ground truth
pc_offset_ms = 40      # a made-up "PC hardware/driver latency" ground truth

sig = calibration.make_chirp_signal(RATE)
n_total = int(RATE * 6.0)
recording = np.random.normal(0, 0.01, n_total)  # background hiss, like a real room


def _stamp(rec, at_ms, amplitude):
    start = int(RATE * at_ms / 1000)
    end = start + len(sig)
    if end <= len(rec):
        rec[start:end] += sig * amplitude


_stamp(recording, pc_offset_ms, 0.8)      # the "PC speaker" echo, quieter/closer
_stamp(recording, true_delay_ms, 1.0)     # the "Sonos speaker" echo, further away in time

hits = calibration.find_echo_times(recording, sig, RATE)
assert len(hits) >= 2, f"expected to find both echoes, got {hits}"
measured_delay_ms = (hits[1][0] - hits[0][0]) * 1000
expected = true_delay_ms - pc_offset_ms
assert abs(measured_delay_ms - expected) < 5, (
    f"expected ~{expected}ms between echoes, measured {measured_delay_ms}ms")
print(f"  correctly recovered {measured_delay_ms:.1f}ms (expected ~{expected}ms) OK")

# pure silence/noise (no chirp at all) must NOT produce two confident
# fake hits -- a failed calibration should say so, not report a bogus number
noise_only = np.random.normal(0, 0.01, n_total)
hits_noise = calibration.find_echo_times(noise_only, sig, RATE)
assert len(hits_noise) < 2, f"noise-only recording should not yield 2 confident hits, got {hits_noise}"
print("  correctly reports no confident match on pure noise OK")

print("[test] run_calibration_silent() fails clean with no Sonos speakers enabled...")
import sonos_ctl as _sc  # noqa: E402
_sc.speaker_mgr.speakers.clear()
webapp.config["speakers"].clear()
calibration.run_calibration_silent()
st = calibration.get_status()
assert st["state"] == "error" and "Enable at least one Sonos speaker" in st["detail"], st
print("  OK")

print("[test] donation prompt: routes are always reachable (nothing is gated)...")
r = client.get("/")
assert r.status_code == 200 and b"Sonos speakers" in r.data, "PC2Sonos is free -- / must always show the real dashboard"
print("  / OK")

r = client.get("/stream/fake-uid.wav")
assert r.status_code == 200, "streaming must never be gated"
print("  /stream/<uid>.wav reachable OK")

print("[test] donation prompt: once-a-week throttling and permanent opt-out...")
webapp.config["donated"] = False
webapp.config["last_donate_prompt_at"] = 0

r = client.get("/api/donate/status")
body = r.get_json()
assert body["should_prompt"] is True, "never prompted before -- should prompt now"
print("  first check (never prompted) -> should_prompt True OK")

r = client.get("/api/donate/status")
assert r.get_json()["should_prompt"] is False, "checking again immediately shouldn't re-prompt within the same week"
print("  immediate re-check -> should_prompt False (marked shown) OK")

webapp.config["last_donate_prompt_at"] = time.time() - webapp.DONATE_PROMPT_INTERVAL_SECONDS - 1
r = client.get("/api/donate/status")
assert r.get_json()["should_prompt"] is True, "a full week later, should prompt again"
print("  a week later -> should_prompt True again OK")

r = client.post("/api/donate/dismiss", json={"donated": True})
assert r.status_code == 200 and webapp.config["donated"] is True
webapp.config["last_donate_prompt_at"] = 0  # even "never prompted" shouldn't matter now
r = client.get("/api/donate/status")
assert r.get_json()["should_prompt"] is False, "once donated=True, never prompt again regardless of timing"
print("  dismiss(donated=True) -> permanently suppressed OK")
webapp.config["donated"] = False  # reset for anything running after this
print("  OK")

print("\nALL LOGIC TESTS PASSED")
