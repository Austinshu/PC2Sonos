"""Flask app: control dashboard + the WAV endpoints Sonos speakers pull from."""

import io
import queue
import secrets
import struct
import time

from flask import Flask, Response, jsonify, render_template_string, request

from audio_engine import CHUNK, broadcaster, list_output_devices, restart_render, get_current_render_device_name, get_lan_ip
from config import config, save_config, PASSWORD_PATH
from sonos_ctl import speaker_mgr

app = Flask(__name__)

# (password_text, mtime_ns) cache so we re-read dashboard_password.txt only
# when it actually changes -- create/edit the file and the next request
# picks it up, no restart.
_pw_cache = (None, None)


def _dashboard_password():
    """The current dashboard password, or None if the dashboard is open.

    Open (None) when PASSWORD_PATH doesn't exist or is blank -- this is
    the default: the app works on a trusted LAN with no setup, and
    someone who wants a password just drops one line of text in that
    file (path logged at startup)."""
    global _pw_cache
    try:
        mtime = PASSWORD_PATH.stat().st_mtime_ns
    except OSError:
        _pw_cache = (None, None)
        return None
    if mtime != _pw_cache[1]:
        try:
            _pw_cache = (PASSWORD_PATH.read_text(encoding="utf-8").strip() or None, mtime)
        except OSError:
            _pw_cache = (None, None)
    return _pw_cache[0]


@app.before_request
def require_auth():
    # The stream endpoint is fetched directly by Sonos speakers, which
    # can't respond to an HTTP auth challenge -- leave it open, same as
    # every other PC->Sonos streamer. Everything else (the dashboard +
    # /api/*) requires the password IF one has been configured.
    if request.path.startswith("/stream/"):
        return
    password = _dashboard_password()
    if password is None:
        return
    auth = request.authorization
    given = auth.password if auth and auth.password is not None else ""
    # compare as bytes -- str compare_digest raises on non-ASCII input
    if not secrets.compare_digest(given.encode("utf-8"), password.encode("utf-8")):
        return Response(
            "Authentication required", 401,
            {"WWW-Authenticate": 'Basic realm="PC2Sonos"'}
        )

# PC2Sonos is free. This is a "pay what you want" link for anyone who
# finds it useful and wants to support development -- nothing in the app
# is gated behind it. See the /api/donate/* routes below for the once-a-
# week reminder popup, which stops permanently once someone says they've
# already donated (honor system -- there's nothing to verify, and nothing
# at stake if someone just dismisses it forever without paying).
DONATE_URL = "https://buy.stripe.com/eVq7sF9Iv4LjfeM4JScbC01"

DONATE_PROMPT_INTERVAL_SECONDS = 7 * 24 * 3600


STYLE_BLOCK = """
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; background:#111; color:#eee; padding:24px; max-width:640px; margin:0 auto; }
  h1 { font-size:20px; margin-bottom:4px; }
  .sub { color:#888; font-size:13px; margin-bottom:20px; }
  .card { background:#1c1c1c; border-radius:10px; padding:16px 18px; margin-bottom:16px; }
  .speaker { display:flex; align-items:center; gap:14px; flex-wrap:wrap; padding:10px 0; border-bottom:1px solid #2a2a2a; }
  .speaker:last-child { border-bottom:none; }
  .name { font-weight:600; min-width:150px; }
  input[type=range] { width:150px; }
  .toggle { transform: scale(1.3); }
  label { font-size:13px; color:#aaa; display:block; margin-bottom:8px; }
  button { background:#1db954; border:none; color:#000; font-weight:600; padding:6px 14px; border-radius:6px; cursor:pointer; }
  .status { font-size:12px; padding:2px 8px; border-radius:10px; }
  .on { background:#0f3d1f; color:#4caf50; }
  .off { background:#3a2626; color:#888; }
  .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.65); align-items:center; justify-content:center; z-index:1000; }
  .modal-overlay.show { display:flex; }
  .modal-box { background:#1c1c1c; border-radius:12px; padding:22px; max-width:380px; width:90%; box-shadow:0 10px 40px rgba(0,0,0,0.5); }
  .modal-box h3 { margin:0 0 10px 0; font-size:16px; }
  .modal-box p { font-size:13px; color:#ccc; line-height:1.5; margin:0 0 18px 0; }
  .modal-actions { display:flex; justify-content:flex-end; gap:10px; }
  .btn-cancel { background:#333; color:#eee; }
</style>
"""

VB_CREDIT_LINE = """
<div style="font-size:11px; color:#666; text-align:center; margin-top:4px;">
  Uses <a href="https://vb-audio.com/Cable/" target="_blank" style="color:#888;">VB-CABLE</a>
  by VB-Audio Software (Vincent Burel) &mdash; free donationware, not affiliated with
  PC2Sonos. If it's useful to you, consider
  <a href="https://vb-audio.com/Cable/" target="_blank" style="color:#888;">donating to VB-Audio</a>.
</div>
"""

DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
<title>PC2Sonos</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
""" + STYLE_BLOCK + """
</head>
<body>
<h1>PC2Sonos</h1>
<div class="sub">Free. Local, no account. Runs at startup. &mdash;
  <a href="{{donate_url}}" target="_blank" style="color:#1db954;">&hearts; Support this project</a>
</div>

<div class="card">
  <label>PC speaker output device &mdash; the real speakers/headphones PC2Sonos should play the delayed audio to (virtual devices like game-streaming mics are flagged and best avoided)</label>
  <select id="renderDevice" onchange="setDevice()" style="width:100%; padding:6px; background:#111; color:#eee; border:1px solid #333; border-radius:6px;"></select>
</div>

<div class="card">
  <label>Local PC-speaker sync delay &mdash; raise until your PC speakers and Sonos play together, with no echo</label>
  <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
    <input type="range" min="0" max="4000" step="1" id="delay" value="{{delay}}"
           oninput="syncDelay('slider')" style="flex:1; min-width:150px;">
    <input type="number" min="0" max="4000" step="1" id="delayNum" value="{{delay}}"
           oninput="syncDelay('number')"
           style="width:70px; padding:4px; background:#111; color:#eee; border:1px solid #333; border-radius:6px;">
    <span>ms</span>
    <button onclick="setDelay()">Apply</button>
    <button onclick="autoCalibrate('silent')" style="background:#2b6cb0; color:#fff;">Auto</button>
  </div>
  <div id="calibResult" style="margin-top:8px; font-size:12px; color:#888;"></div>
  <div style="margin-top:14px; padding-top:12px; border-top:1px solid #2a2a2a; font-size:12px;">
    <div style="color:#ccc; font-weight:600; margin-bottom:6px;">Prefer a test tone + microphone instead?</div>
    <div style="color:#aaa; line-height:1.5;">
      Put the microphone (built-in laptop mic, or any USB/headset mic) somewhere it
      can clearly hear <strong>both</strong> your PC speakers and the Sonos speaker(s)
      you're syncing to at once &mdash; roughly the midpoint between them, not sitting
      right next to either one. A headset mic worn while sitting at the PC usually
      only hears the PC speakers well and will give a bad reading. Works best in a
      quiet room.
      <div style="margin-top:8px;">
        <button onclick="autoCalibrate('acoustic')" style="background:#2b6cb0; color:#fff;">Calibrate with test tone</button>
      </div>
    </div>
  </div>
</div>

<div class="card">
  <div style="display:flex; align-items:center; justify-content:space-between;">
    <label style="margin-bottom:0;">Sonos speakers</label>
    <button onclick="rescan()" style="background:#333; color:#eee; font-weight:400; padding:4px 10px; font-size:12px;">Rescan</button>
  </div>
  <div style="font-size:11px; color:#777; margin:2px 0 8px;">
    &#9733; = default speaker: streamed to the instant PC2Sonos starts, before
    a network scan finishes. Click a star to set it.
  </div>
  <div id="speakers"></div>
  <div id="rescanResult" style="margin-top:6px; font-size:12px; color:#888;"></div>
  <details style="margin-top:12px; font-size:12px; color:#aaa;">
    <summary style="cursor:pointer; color:#ccc;">Speakers not showing up? (different subnet / IoT VLAN)</summary>
    <div style="margin-top:10px; line-height:1.5;">
      Automatic discovery uses network multicast, which most routers don't
      pass between VLANs. If your Sonos speakers are on a separate (e.g.
      IoT) network, type <strong>one speaker's IP address</strong> below &mdash;
      the app will reach it directly and find the rest from it. Give that
      speaker a DHCP reservation so its IP doesn't change. Comma-separate
      to list more than one.
      <div style="display:flex; gap:8px; margin-top:8px; flex-wrap:wrap;">
        <input type="text" id="seedIps" placeholder="10.0.20.41, 10.0.20.42"
               style="flex:1; min-width:180px; padding:6px; background:#111; color:#eee; border:1px solid #333; border-radius:6px;">
        <button onclick="saveSeedIps()">Save &amp; scan</button>
      </div>
      <div id="seedResult" style="margin-top:8px; color:#888;"></div>
    </div>
  </details>
</div>

<div class="card">
  <label style="margin-bottom:0;">Troubleshooting</label>
  <button onclick="exportDiag()">Export Diagnostics</button>
  <div id="diagResult" style="margin-top:8px; font-size:12px; color:#888;"></div>
</div>
""" + VB_CREDIT_LINE + """

<div class="modal-overlay" id="diagModal">
  <div class="modal-box">
    <h3>Export Diagnostics?</h3>
    <p>This saves a diagnostics file (system info, the app log, and your
       settings &mdash; no personal files, no browsing history) to your
       Desktop, then opens an email pre-addressed to
       <strong>austin1235@gmail.com</strong> so you can attach it and send
       it yourself if you want help. <strong>Nothing leaves your
       computer unless you choose to hit send.</strong></p>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="cancelExportDiag()">Cancel</button>
      <button onclick="confirmExportDiag()">Accept</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="donateModal">
  <div class="modal-box">
    <h3>Enjoying PC2Sonos?</h3>
    <p>It's free and always will be. If it's useful to you, consider
       chipping in whatever it's worth to you &mdash; totally optional.
       This won't ask again for a week, and never again if you've
       already donated.</p>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="dismissDonate(false)">Maybe later</button>
      <button class="btn-cancel" onclick="dismissDonate(true)">I've already donated</button>
      <button onclick="goDonate()">Donate</button>
    </div>
  </div>
</div>

<script>
async function refresh(){
  const res = await fetch('/api/speakers');
  const data = await res.json();
  const el = document.getElementById('speakers');
  el.innerHTML = '';
  if (data.length === 0) {
    el.innerHTML = '<div style="color:#888; padding:10px 0;">Searching for Sonos speakers...</div>';
    return;
  }
  data.forEach(s => {
    const div = document.createElement('div');
    div.className = 'speaker';
    const grouped = s.grouped_with && s.grouped_with.length;
    const star = s.is_default ? '&#9733;' : '&#9734;';
    const starTitle = s.is_default ? 'Default speaker (click to unset)' : 'Set as default speaker';
    div.innerHTML = `
      <span onclick="setDefault('${s.uid}', ${s.is_default})" title="${starTitle}"
            style="cursor:pointer; font-size:16px; color:${s.is_default ? '#f5c518' : '#666'};">${star}</span>
      <input type="checkbox" class="toggle" ${s.enabled ? 'checked' : ''} onchange="toggle('${s.uid}', this.checked)">
      <span class="name">${s.name}${grouped ? ` <span style="font-weight:400; color:#888; font-size:12px;">(grouped with ${s.grouped_with.join(', ')} &mdash; this also controls them)</span>` : ''}</span>
      <input type="range" min="0" max="100" value="${s.volume}" onchange="setVol('${s.uid}', this.value)">
      <span style="width:36px; display:inline-block;">${s.volume}%</span>
      <span class="status ${s.streaming ? 'on' : 'off'}">${s.streaming ? 'streaming' : 'idle'}</span>
    `;
    el.appendChild(div);
  });
}
async function rescan(){
  const el = document.getElementById('rescanResult');
  el.textContent = 'Scanning the network...';
  try {
    const res = await fetch('/api/rescan', {method:'POST'});
    const data = await res.json();
    el.textContent = 'Found ' + data.found + ' speaker' + (data.found === 1 ? '' : 's') + '.'
      + (data.ok ? '' : ' (a scan step hit an error -- see the log)');
  } catch (e) {
    el.textContent = 'Scan failed: ' + e;
  }
  refresh();
}
async function setDefault(uid, isDefault){
  await fetch('/api/default_speaker', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({uid: isDefault ? null : uid})});
  refresh();
}
async function loadSeedIps(){
  const res = await fetch('/api/sonos_seed');
  const data = await res.json();
  const el = document.getElementById('seedIps');
  if (document.activeElement !== el) el.value = (data.seed_ips || []).join(', ');
}
async function saveSeedIps(){
  const el = document.getElementById('seedResult');
  el.textContent = 'Saving and looking for speakers...';
  const raw = document.getElementById('seedIps').value;
  const ips = raw.split(',').map(s => s.trim()).filter(Boolean);
  const res = await fetch('/api/sonos_seed', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({seed_ips: ips})});
  const data = await res.json();
  if (data.error) {
    el.textContent = 'Failed: ' + data.error;
  } else {
    el.textContent = 'Saved. Speakers found so far: ' + data.found
      + (data.ok ? '' : ' (a scan step hit an error -- see the log)');
  }
  refresh();
}
async function toggle(uid, enabled){
  await fetch('/api/speaker/' + uid + '/enabled', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled})});
  refresh();
}
async function setVol(uid, volume){
  await fetch('/api/speaker/' + uid + '/volume', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({volume: parseInt(volume)})});
}
function syncDelay(source){
  const slider = document.getElementById('delay');
  const num = document.getElementById('delayNum');
  if (source === 'slider') {
    num.value = slider.value;
  } else {
    let v = parseInt(num.value);
    if (isNaN(v)) return;
    v = Math.max(0, Math.min(4000, v));
    slider.value = v;
  }
}
async function setDelay(){
  const v = document.getElementById('delay').value;
  await fetch('/api/delay', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({delay_ms: parseInt(v)})});
}
let calibPolling = null;
async function autoCalibrate(method){
  const el = document.getElementById('calibResult');
  el.textContent = method === 'acoustic'
    ? 'Starting -- you will hear a short test tone...'
    : 'Starting -- measuring Sonos playback timing (no sound needed)...';
  await fetch('/api/calibrate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({method})});
  if (calibPolling) clearInterval(calibPolling);
  calibPolling = setInterval(async () => {
    const res = await fetch('/api/calibrate/status');
    const s = await res.json();
    el.textContent = s.detail || s.state;
    if (s.state === 'done' || s.state === 'error') {
      clearInterval(calibPolling);
      calibPolling = null;
      if (s.state === 'done' && s.result_ms !== null && s.result_ms !== undefined) {
        document.getElementById('delay').value = s.result_ms;
        document.getElementById('delayNum').value = s.result_ms;
      }
    }
  }, 700);
}
async function loadDevices(){
  const res = await fetch('/api/devices');
  const data = await res.json();
  const sel = document.getElementById('renderDevice');
  sel.innerHTML = '';
  data.devices.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.name;
    opt.textContent = d.name + (d.likely_virtual ? '  (looks virtual -- probably not real speakers)' : '');
    if (d.name === data.current) opt.selected = true;
    sel.appendChild(opt);
  });
}
async function setDevice(){
  const sel = document.getElementById('renderDevice');
  await fetch('/api/render_device', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({device: sel.value})});
}
function exportDiag(){
  document.getElementById('diagModal').classList.add('show');
}
function cancelExportDiag(){
  document.getElementById('diagModal').classList.remove('show');
}
async function confirmExportDiag(){
  document.getElementById('diagModal').classList.remove('show');
  const el = document.getElementById('diagResult');
  el.textContent = 'Generating...';
  try {
    const res = await fetch('/api/diagnostics', {method:'POST'});
    const data = await res.json();
    el.textContent = data.ok
      ? ('Saved ' + data.filename + ' to your Desktop and opened an email to ' + data.email + ' -- attach it and hit send.')
      : ('Failed: ' + data.error);
  } catch (e) {
    el.textContent = 'Failed: ' + e;
  }
}
function goDonate(){
  window.open('{{donate_url}}', '_blank');
  document.getElementById('donateModal').classList.remove('show');
}
async function dismissDonate(donated){
  document.getElementById('donateModal').classList.remove('show');
  await fetch('/api/donate/dismiss', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({donated})});
}
async function checkDonatePrompt(){
  const res = await fetch('/api/donate/status');
  const s = await res.json();
  if (s.should_prompt) {
    document.getElementById('donateModal').classList.add('show');
  }
}
refresh();
loadDevices();
loadSeedIps();
checkDonatePrompt();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML, delay=config["local_delay_ms"], donate_url=DONATE_URL)


def _should_prompt_donation():
    if config.get("donated"):
        return False
    last = config.get("last_donate_prompt_at", 0) or 0
    return (time.time() - last) >= DONATE_PROMPT_INTERVAL_SECONDS


@app.route("/api/donate/status")
def api_donate_status():
    should_prompt = _should_prompt_donation()
    if should_prompt:
        # mark as shown now, not just on dismiss -- so reloading the page
        # (the dashboard polls this on every load) doesn't re-show it in
        # a loop before the user has a chance to click anything
        config["last_donate_prompt_at"] = time.time()
        save_config(config)
    return jsonify({"should_prompt": should_prompt, "donated": bool(config.get("donated"))})


@app.route("/api/donate/dismiss", methods=["POST"])
def api_donate_dismiss():
    data = request.get_json(force=True)
    config["last_donate_prompt_at"] = time.time()
    if data.get("donated"):
        config["donated"] = True
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/speakers")
def api_speakers():
    return jsonify(speaker_mgr.list())


@app.route("/api/speaker/<uid>/enabled", methods=["POST"])
def api_set_enabled(uid):
    data = request.get_json(force=True)
    # IMPORTANT: never use request.url_root here. The dashboard is often
    # opened via http://127.0.0.1:<port>/ (or "localhost"), and that host
    # is only meaningful on THIS PC -- a Sonos speaker is a separate
    # physical device, and "127.0.0.1" on ITS end means itself, not us.
    # Sonos would then try to fetch the stream from its own loopback and
    # fail with "unable to connect". Always build the URL from this PC's
    # real LAN IP instead, regardless of how the dashboard was reached.
    base_url = f"http://{get_lan_ip()}:{config['http_port']}"
    speaker_mgr.set_enabled(uid, bool(data.get("enabled")), base_url)
    return jsonify({"ok": True})


@app.route("/api/sonos_seed", methods=["GET", "POST"])
def api_sonos_seed():
    """Manual speaker IPs for when the speakers are on a subnet SSDP
    multicast can't cross (e.g. an IoT VLAN). Saving triggers an
    immediate rediscover so the user sees the result without waiting for
    the 15s loop."""
    if request.method == "GET":
        return jsonify({"seed_ips": config.get("sonos_seed_ips", [])})
    data = request.get_json(force=True)
    ips = data.get("seed_ips", [])
    if not isinstance(ips, list):
        return jsonify({"ok": False, "error": "seed_ips must be a list"}), 400
    config["sonos_seed_ips"] = [str(ip).strip() for ip in ips if str(ip).strip()]
    save_config(config)
    # rediscover() swallows its own errors and reports success/failure via
    # its return value -- pass that straight through rather than always
    # claiming ok
    ok = speaker_mgr.rediscover()
    return jsonify({"ok": ok, "found": len(speaker_mgr.list())})


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    """One immediate discovery pass. Useful in auto mode (skip the wait
    for the next 15s tick) and required in on_demand mode (the background
    loop is idle until asked)."""
    ok = speaker_mgr.request_rescan()
    return jsonify({"ok": ok, "found": len(speaker_mgr.list())})


@app.route("/api/default_speaker", methods=["POST"])
def api_default_speaker():
    """Pin the speaker PC2Sonos streams to immediately at launch (by its
    current IP), or clear it with {"uid": null}."""
    data = request.get_json(force=True)
    uid = data.get("uid")
    if speaker_mgr.set_default_speaker(uid):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "no known IP for that speaker yet"}), 400


@app.route("/api/speaker/<uid>/volume", methods=["POST"])
def api_set_volume(uid):
    data = request.get_json(force=True)
    speaker_mgr.set_volume(uid, data.get("volume", 50))
    return jsonify({"ok": True})


@app.route("/api/delay", methods=["POST"])
def api_set_delay():
    data = request.get_json(force=True)
    config["local_delay_ms"] = max(0, int(data.get("delay_ms", config["local_delay_ms"])))
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/devices")
def api_devices():
    return jsonify({
        "devices": list_output_devices(),
        "current": get_current_render_device_name(),
    })


@app.route("/api/render_device", methods=["POST"])
def api_set_render_device():
    data = request.get_json(force=True)
    device_name = data.get("device", "")
    restart_render(new_device_substr=device_name)
    return jsonify({"ok": True})


@app.route("/api/calibrate", methods=["POST"])
def api_calibrate():
    from calibration import start_calibration_async
    data = request.get_json(silent=True) or {}
    method = "acoustic" if data.get("method") == "acoustic" else "silent"
    start_calibration_async(method)
    return jsonify({"ok": True})


@app.route("/api/calibrate/status")
def api_calibrate_status():
    from calibration import get_status
    return jsonify(get_status())


@app.route("/api/diagnostics", methods=["POST"])
def api_diagnostics():
    # same bundle the tray icon's "Export Diagnostics..." makes -- exposed
    # here too since plenty of people never right-click the tray icon.
    try:
        from diagnostics import export_diagnostics_zip, open_diagnostics_email, SUPPORT_EMAIL
        path = export_diagnostics_zip()
        open_diagnostics_email(path)
        return jsonify({"ok": True, "path": str(path), "filename": path.name, "email": SUPPORT_EMAIL})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


def wav_header(sample_rate, channels, sample_width):
    # Declares a very large data size so Sonos treats this as a long-
    # running live stream rather than a fixed-length file (same trick
    # used by other PC->Sonos streamers).
    big_size = 0x7FFFFFFF
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", big_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                           sample_rate * channels * sample_width,
                           channels * sample_width, sample_width * 8))
    buf.write(b"data")
    buf.write(struct.pack("<I", big_size))
    return buf.getvalue()


@app.route("/stream/<uid>.wav")
def stream_wav(uid):
    def generate():
        yield wav_header(config["sample_rate"], config["channels"], config["sample_width"])
        sid, q = broadcaster.subscribe(maxlen=200)
        # Sonos pulls this over HTTP in real time. If this generator ever
        # falls a little behind for a moment -- a GC pause, another
        # request briefly hogging the GIL on Flask's dev server, a slow
        # socket write -- the queue quietly backs up. Unlike the local
        # delayed-render path (which has its own drift guard, see
        # audio_engine.render_loop), nothing here ever undoes that: every
        # chunk still gets yielded, just later and later, so a transient
        # stall becomes permanent extra Sonos-side delay that keeps
        # compounding for as long as the app runs -- which is exactly
        # what repeated auto-calibration runs were measuring (641ms, then
        # 911, 1090, 1270 on the same otherwise-idle system). Mirror the
        # local path's ~200ms drift guard here too: once backlog exceeds
        # that, skip ahead to near "now" instead of dutifully draining it
        # in order.
        chunk_ms = CHUNK / config["sample_rate"] * 1000
        max_backlog_chunks = max(1, int(200 / chunk_ms))
        trim_to_chunks = max(1, int(50 / chunk_ms))
        try:
            while True:
                chunk = q.get()
                backlog = q.qsize()
                if backlog > max_backlog_chunks:
                    for _ in range(backlog - trim_to_chunks):
                        try:
                            chunk = q.get_nowait()
                        except queue.Empty:
                            break
                yield chunk
        finally:
            broadcaster.unsubscribe(sid)

    return Response(generate(), mimetype="audio/wav")


def run_web(host="0.0.0.0", port=None):
    port = port or config["http_port"]
    if _dashboard_password() is None:
        print(f"[web] dashboard has no password. To set one, put it on a "
              f"single line in: {PASSWORD_PATH}")
    else:
        print("[web] dashboard is password-protected")
    app.run(host=host, port=port, threaded=True, use_reloader=False)
