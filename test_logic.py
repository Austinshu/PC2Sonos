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
print("  / OK")

r = client.get("/api/speakers")
assert r.status_code == 200 and r.get_json() == []
print("  /api/speakers OK (empty, no real Sonos on this machine)")

r = client.post("/api/delay", json={"delay_ms": 900})
assert r.status_code == 200 and webapp.config["local_delay_ms"] == 900
print("  /api/delay OK")

r = client.get("/api/devices")
body = r.get_json()
# CABLE Output is an input-only (recording) device in the fake, so only
# the 2 real OUTPUT-capable devices should be listed here.
assert r.status_code == 200 and len(body["devices"]) == 2, body
names = {d["name"] for d in body["devices"]}
assert "Speakers (Steam Streaming Microphone)" in names
print("  /api/devices OK")

r = client.post("/api/render_device", json={"device": "Speakers (Realtek(R) Audio)"})
assert r.status_code == 200
assert webapp.config["render_device_substr"] == "Speakers (Realtek(R) Audio)"
time.sleep(0.3)  # let the restarted render thread open its stream
assert audio_engine.get_current_render_device_name() == "Speakers (Realtek(R) Audio)"
audio_engine._render_stop_event.set()  # clean up the thread this test started
audio_engine._render_thread.join(timeout=2)
print("  /api/render_device OK (explicit device switch works)")

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
mgr._last_auto_restart[uid_a] = 0  # simulate cooldown elapsed
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
else:
    assert _h2 is not None, "non-Windows: lock is a no-op, always granted"
print("  OK")

print("[test] diagnostics module...")
import diagnostics  # noqa: E402

# system_snapshot() must never raise, even on a non-Windows box with no
# real Sonos/audio hardware -- every Windows-only lookup inside it is
# supposed to degrade to a "n/a (not Windows)" string instead.
snap = diagnostics.system_snapshot()
assert isinstance(snap, str) and "OS:" in snap and "LAN IP:" in snap
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

print("[test] run_calibration() fails clean with no Sonos speakers enabled...")
import sonos_ctl as _sc  # noqa: E402
_sc.speaker_mgr.speakers.clear()
webapp.config["speakers"].clear()
calibration.run_calibration()
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
