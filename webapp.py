"""Flask app: control dashboard + the WAV endpoints Sonos speakers pull from."""

import audioop
import io
import queue
import secrets
import struct
import sys
import threading
import time

from flask import Flask, Response, jsonify, render_template_string, request

from audio_engine import CHUNK, broadcaster, list_output_devices, restart_render, restart_capture, get_current_render_device_name, get_lan_ip
from config import config, save_config, PASSWORD_PATH
from sonos_ctl import speaker_mgr

app = Flask(__name__)

# The rate the Sonos leg is downsampled to in "reduced" streaming-quality
# mode (see config.sonos_stream_quality) -- half of the standard 44100
# capture rate, so the math in stream_wav's audioop.ratecv call is exact.
# The local PC-speaker path is untouched by this; only what's sent to
# Sonos changes. Never used if the capture rate is already at or below it.
REDUCED_SAMPLE_RATE = 22050

# (password_text, mtime_ns) cache so we re-read dashboard_password.txt only
# when it actually changes -- create/edit the file and the next request
# picks it up, no restart.
_pw_cache = (None, None)

# In-memory snapshot of what "100%" means for the master volume slider --
# each enabled speaker's volume + the local boost, captured the moment the
# slider first moves away from 100. Deliberately NOT in config.json: this
# is a live-session convenience (temporarily duck everything, then bring
# it back), not a setting to persist across restarts. Cleared whenever the
# slider returns to exactly 100, so whatever is sitting there at that
# moment becomes the new baseline for the next press, instead of scaling
# relative to itself and drifting further from the real values every time.
_master_volume_baseline = None
# Flask's dev server runs threaded=True (real OS threads), so two
# requests to /api/master_volume close together -- e.g. a slider firing
# on release right as a previous drag's request is still in flight --
# can otherwise both see baseline as None and each capture their own
# snapshot, or one can clear it while the other is still mid-calculation.
# Observed directly while testing: two quick presses left the PC boost
# scaled from the wrong baseline. Guards the whole read-baseline/scale/
# maybe-clear sequence in api_master_volume as one atomic step.
_master_volume_lock = threading.Lock()

# Sleep timer: {"deadline": monotonic_time, "timer": threading.Timer} while
# armed, None otherwise. Session-only like the baseline above -- a timer
# that outlived an app restart would be surprising, not helpful.
_sleep_timer = None
_sleep_timer_lock = threading.Lock()


def _fire_sleep_timer():
    """Runs on the threading.Timer's own thread when the countdown reaches
    zero: turns off every currently-enabled speaker the same way the
    dashboard's own toggle would, so the watchdog respects it (an enabled
    speaker that merely stopped gets auto-restarted -- see
    sonos_ctl.watchdog_tick -- so this has to actually disable them, not
    just call stop())."""
    global _sleep_timer
    with _sleep_timer_lock:
        _sleep_timer = None
    base_url = f"http://{get_lan_ip()}:{config['http_port']}"
    for s in speaker_mgr.list():
        if s["enabled"]:
            speaker_mgr.set_enabled(s["uid"], False, base_url)
    print("[sleep timer] fired -- turned off every enabled speaker")


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
  * { box-sizing:border-box; }
  body {
    font-family: -apple-system, "Segoe UI", sans-serif;
    background:#0e0e0e; color:#eee; padding:28px 24px; max-width:1080px; margin:0 auto;
    -webkit-font-smoothing:antialiased;
  }
  h1 { font-size:20px; font-weight:700; letter-spacing:-0.02em; margin:0 0 2px; }
  .sub { color:#888; font-size:13px; }
  .sub a { text-decoration:none; }
  .sub a:hover { text-decoration:underline; }

  /* top identity bar: icon + name/tagline on the left, support link on
     the right -- replaces the old bare <h1> so the page reads as an app
     with a header, not a scrolling settings form */
  .topbar {
    display:flex; align-items:center; justify-content:space-between;
    gap:16px; flex-wrap:wrap; margin-bottom:20px;
  }
  .brand { display:flex; align-items:center; gap:12px; }
  .brand-icon {
    width:40px; height:40px; border-radius:11px; background:#132a1c;
    border:1px solid #1f4d2e; display:flex; align-items:center; justify-content:center;
    flex-shrink:0; color:#1db954;
  }
  .brand-icon svg { width:22px; height:22px; }
  .donate-link { color:#1db954; font-size:13px; text-decoration:none; white-space:nowrap; }
  .donate-link:hover { text-decoration:underline; }

  /* at-a-glance status strip -- the "dashboard" part of the dashboard:
     the handful of numbers you'd otherwise have to read the whole page
     to piece together, up front and large */
  .stat-row {
    display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr));
    gap:12px; margin-bottom:16px;
  }
  .stat-tile {
    background:#1a1a1a; border:1px solid #262626; border-radius:12px;
    padding:13px 16px;
  }
  .stat-label {
    font-size:10.5px; color:#888; text-transform:uppercase; letter-spacing:.06em;
    margin-bottom:5px; font-weight:600;
  }
  .stat-value {
    font-size:19px; font-weight:700; color:#eee;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  .stat-value.good { color:#4caf50; }
  .stat-value.warn { color:#e0a030; }

  /* platformBanner's two states: something actually needs fixing (warn,
     the original orange) vs. just worth knowing (info, calm blue) -- see
     checkPlatform(). A permission granted + working is NOT the same as
     nothing to say, so this banner stays visible either way on macOS
     instead of disappearing the moment there's no error left to show. */
  #platformBanner.banner-warn { background:#3a2a10; border:1px solid #6b4a12; }
  #platformBanner.banner-info { background:#12233a; border:1px solid #1f3f6b; }

  /* update banner: one compact row (text + Download) with the release notes
     tucked behind a "What's new" toggle, so it stays a single line until
     asked. Reading them never needs a download, and the notes scroll inside
     a capped height so a long changelog can't push the rest of the dashboard
     off a standard-height screen. */
  #updateBanner .update-row { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
  #updateBanner .update-notes summary { cursor:pointer; color:#8fd3a5; font-size:12px; margin-top:8px; }
  #updateNotesBody { margin-top:8px; max-height:min(45vh, 340px); overflow-y:auto; padding-right:8px; font-size:12px; line-height:1.55; color:#cfd8d2; }
  #updateNotesBody p { margin:0 0 8px; }
  #updateNotesBody ul { margin:0 0 8px; padding-left:18px; }
  #updateNotesBody li { margin-bottom:4px; }
  #updateNotesBody code { background:#0d1a12; padding:1px 4px; border-radius:4px; font-size:11px; }
  #updateNotesBody a { color:#8fd3a5; }

  /* live input level meter, in the stat-row's 4th tile -- a bar instead
     of a number since "how loud" is more legible as a glance-length than
     a percentage would be. Width is set inline per-reading by JS. */
  .level-track { background:#333; border-radius:99px; height:10px; margin-top:3px; overflow:hidden; }
  .level-fill { background:#1db954; height:100%; width:0%; border-radius:99px; transition:width .12s linear; }

  /* Two balanced columns, grouped by what the cards are about: the PC-speaker
     cards (output + volume, sync delay) on the left and the Sonos/session
     cards (streaming quality, sleep timer, troubleshooting) on the right come
     out within ~25px of each other's height, so nothing is left with a void
     beneath it. The earlier auto-fit grid sized each ROW to its tallest
     card, which stranded the shorter ones with a gap below them, and made
     the tall Advanced card leave a hole beside it whenever it was open --
     so Advanced now stands on its own below the columns, full width.
     Speakers stays above, on its own, for the same reason (variable-length
     list). Stacks to one column below the media query at the bottom. */
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; align-items:stretch; margin-bottom:14px; }
  .grid-col { display:flex; flex-direction:column; gap:14px; min-width:0; }
  /* the two columns stretch to the same height, and the last card in the
     shorter one takes up the (small) difference -- the bottoms line up flush */
  .grid-col > .card:last-child { flex:1 1 auto; }

  .card {
    background:#1a1a1a; border:1px solid #262626; border-radius:12px;
    padding:16px 18px; margin-bottom:0;
  }
  .card-header { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
  .card-icon {
    width:26px; height:26px; border-radius:8px; background:#132a1c;
    color:#1db954; display:flex; align-items:center; justify-content:center; flex-shrink:0;
  }
  .card-icon svg { width:15px; height:15px; }
  .card-title { font-size:14px; font-weight:700; color:#eee; }
  .card-desc { font-size:12px; color:#999; line-height:1.5; margin:0; }
  details.card .card-header { margin-bottom:0; }
  details.card { padding:0; }
  /* the summary is a flex row -- arrow, then icon + title -- rather than
     inline content: with a long title the inline version wrapped the whole
     header below the arrow, leaving the arrow stranded on a line of its own */
  details.card > summary {
    list-style:none; cursor:pointer; padding:16px 18px;
    display:flex; align-items:center; gap:8px;
  }
  details.card > summary::-webkit-details-marker { display:none; }
  details.card > summary::before {
    content:"\\25B8"; flex-shrink:0; width:12px; text-align:center; color:#777;
    transition:transform .15s ease;
  }
  details.card[open] > summary::before { transform:rotate(90deg); }
  details.card > summary .card-header { flex:1; min-width:0; }

  /* small "what's this?" disclosure for the longer explanatory copy --
     collapsed by default so a tile shows its controls first and the
     paragraph explaining them only when asked for, instead of every
     card being a wall of text before you reach anything clickable. */
  .info-toggle { margin:-4px 0 12px; }
  .info-toggle > summary {
    cursor:pointer; list-style:none; font-size:11px; color:#777;
    display:inline-flex; align-items:center; gap:4px; user-select:none;
  }
  .info-toggle > summary::-webkit-details-marker { display:none; }
  .info-toggle > summary:hover { color:#aaa; }
  .info-toggle[open] > summary { color:#999; margin-bottom:6px; }
  .info-toggle .card-desc { padding-left:1px; }

  /* Advanced: a full-width card whose sections sit side by side instead of
     one long narrow column -- two across on a normal window, one on a phone */
  .adv-body { padding:0 18px 18px; }
  .adv-note { font-size:11px; color:#999; line-height:1.5; }
  .adv-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)); gap:0 36px; align-items:start; }
  .adv-section { margin-top:14px; padding-top:12px; border-top:1px solid #2a2a2a; min-width:0; }

  label { font-size:13px; color:#aaa; display:block; margin-bottom:8px; line-height:1.4; }

  button {
    background:#1db954; border:none; color:#000; font-weight:600;
    font-size:13px; padding:7px 15px; border-radius:8px; cursor:pointer;
    transition:filter .12s ease, transform .08s ease;
  }
  button:hover { filter:brightness(1.12); }
  button:active { transform:scale(0.97); }
  button:disabled { filter:grayscale(0.6) brightness(0.8); cursor:default; }

  /* range sliders: flat, custom-styled track + thumb instead of the
     browser's raw OS-default control. Horizontal ones (delay, boost,
     per-speaker volume) get a green fill up to the current value via
     paintRange() in the page script; the vertical EQ faders below skip
     the fill since they're bidirectional around a 0dB center. */
  input[type=range] {
    width:150px; height:20px; -webkit-appearance:none; appearance:none;
    background:#333; border-radius:99px; outline:none; cursor:pointer;
  }
  input[type=range]::-webkit-slider-runnable-track { height:6px; border-radius:99px; background:transparent; }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance:none; appearance:none; width:16px; height:16px;
    margin-top:-5px; border-radius:50%; background:#fff;
    box-shadow:0 1px 3px rgba(0,0,0,0.5); border:none;
    transition:transform .1s ease;
  }
  input[type=range]:hover::-webkit-slider-thumb { transform:scale(1.15); }
  input[type=range]:active::-webkit-slider-thumb { transform:scale(1.25); }

  input[type=number], input[type=text], select {
    background:#111; color:#eee; border:1px solid #333; border-radius:8px;
    padding:6px 8px; font-size:13px; transition:border-color .12s ease;
  }
  input[type=number]:focus, input[type=text]:focus, select:focus {
    outline:none; border-color:#1db954;
  }

  .speaker { display:flex; align-items:center; gap:14px; flex-wrap:wrap; padding:11px 0; border-bottom:1px solid #232323; }
  .speaker:last-child { border-bottom:none; }
  .name { font-weight:600; min-width:150px; }

  /* toggle switch, replacing a bare scaled-up checkbox */
  .switch { position:relative; display:inline-block; width:38px; height:22px; flex-shrink:0; }
  .switch input { position:absolute; opacity:0; width:100%; height:100%; margin:0; cursor:pointer; }
  .switch-track {
    position:absolute; inset:0; background:#333; border-radius:22px;
    transition:background-color .15s ease; pointer-events:none;
  }
  .switch-track::before {
    content:""; position:absolute; width:16px; height:16px; left:3px; top:3px;
    background:#eee; border-radius:50%; transition:transform .15s ease;
    box-shadow:0 1px 2px rgba(0,0,0,0.4);
  }
  .switch input:checked + .switch-track { background:#1db954; }
  .switch input:checked + .switch-track::before { transform:translateX(16px); background:#0a2914; }
  .switch input:focus-visible + .switch-track { outline:2px solid #1db954; outline-offset:2px; }

  .status { font-size:11px; padding:3px 9px; border-radius:99px; font-weight:600; letter-spacing:.02em; }
  .on { background:#0f3d1f; color:#4caf50; }
  .off { background:#2a2a2a; color:#888; }

  .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7); align-items:center; justify-content:center; z-index:1000; }
  .modal-overlay.show { display:flex; }
  .modal-box { background:#1a1a1a; border:1px solid #2a2a2a; border-radius:14px; padding:22px; max-width:380px; width:90%; box-shadow:0 20px 60px rgba(0,0,0,0.6); }
  .modal-box h3 { margin:0 0 10px 0; font-size:16px; }
  .modal-box p { font-size:13px; color:#ccc; line-height:1.5; margin:0 0 18px 0; }
  .modal-actions { display:flex; justify-content:flex-end; gap:10px; }
  .btn-cancel { background:#2a2a2a; color:#eee; }

  .btn-ghost { background:#333; color:#eee; font-weight:400; padding:4px 10px; font-size:12px; }
  .btn-blue { background:#2b6cb0; color:#fff; }

  @media (max-width:860px) {
    body { max-width:640px; }
    .grid { grid-template-columns:1fr; }
  }
</style>
"""

BLACKHOLE_CREDIT_LINE = """
<div style="font-size:11px; color:#666; text-align:center; margin-top:4px;">
  Uses <a href="https://existential.audio/blackhole/" target="_blank" style="color:#888;">BlackHole</a>
  by Existential Audio (free, open source) as the virtual audio device &mdash; not affiliated with
  PC2Sonos. macOS support contributed by Michael Shapiro.
</div>
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
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2032%2032'%3E%3Crect%20width='32'%20height='32'%20rx='7'%20fill='%230e0e0e'/%3E%3Cg%20transform='translate(3,4)'%3E%3Cpolygon%20points='2,9%206,9%2011,4%2011,20%206,15%202,15'%20fill='%231db954'/%3E%3Cpath%20d='M15%208%20A%206%206%200%200%201%2015%2016'%20stroke='%231db954'%20stroke-width='2.2'%20fill='none'%20stroke-linecap='round'/%3E%3Cpath%20d='M18.5%204.5%20A%2011%2011%200%200%201%2018.5%2019.5'%20stroke='%231db954'%20stroke-width='2.2'%20fill='none'%20stroke-linecap='round'%20opacity='.55'/%3E%3C/g%3E%3C/svg%3E">
""" + STYLE_BLOCK + """
</head>
<body>
<div class="topbar">
  <div class="brand">
    <div class="brand-icon">
      <svg viewBox="0 0 24 24" fill="currentColor"><polygon points="2,9 6,9 11,4 11,20 6,15 2,15"/><path d="M15 8 A 6 6 0 0 1 15 16" stroke="currentColor" stroke-width="2.2" fill="none" stroke-linecap="round"/><path d="M18.5 4.5 A 11 11 0 0 1 18.5 19.5" stroke="currentColor" stroke-width="2.2" fill="none" stroke-linecap="round" opacity=".55"/></svg>
    </div>
    <div>
      <h1>PC2Sonos</h1>
      <div class="sub">Free. Local, no account. Runs at startup.</div>
      <div id="nowPlayingText" style="display:none; font-size:12px; color:#1db954; margin-top:2px; max-width:420px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></div>
    </div>
  </div>
  <a href="{{donate_url}}" target="_blank" class="donate-link">&hearts; Support this project</a>
</div>

<div class="card" id="platformBanner" style="display:none;">
  <span id="platformText" style="color:#ddd; font-size:13px;"></span>
  <button id="platformButton" onclick="platformAction()" class="btn-ghost" style="margin-top:8px;"></button>
</div>

<div class="card" id="updateBanner" style="display:none; background:#132a1c; border:1px solid #1f4d2e;">
  <div class="update-row">
    <span id="updateText" style="color:#ddd; font-size:13px;"></span>
    <button onclick="downloadUpdate()">Download</button>
  </div>
  <details class="update-notes" id="updateNotes" style="display:none;">
    <summary>What's new in <span id="updateNotesVersion"></span></summary>
    <div id="updateNotesBody"></div>
  </details>
</div>

<div class="stat-row">
  <div class="stat-tile">
    <div class="stat-label">Streaming</div>
    <div class="stat-value" id="statStreaming">&mdash;</div>
  </div>
  <div class="stat-tile">
    <div class="stat-label">Sync delay</div>
    <div class="stat-value" id="statDelay">{{delay}} ms</div>
  </div>
  <div class="stat-tile">
    <div class="stat-label">PC output device</div>
    <div class="stat-value" id="statDevice">&mdash;</div>
  </div>
  <div class="stat-tile">
    <div class="stat-label">Input level</div>
    <div class="level-track"><div class="level-fill" id="levelFill"></div></div>
  </div>
</div>

<div class="card" style="margin-bottom:14px;">
  <div class="card-header">
    <span class="card-icon"><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="2,9 6,9 11,4 11,20 6,15 2,15"/><path d="M15 8 A 6 6 0 0 1 15 16" stroke="currentColor" stroke-width="2.2" fill="none" stroke-linecap="round"/><path d="M18.5 4.5 A 11 11 0 0 1 18.5 19.5" stroke="currentColor" stroke-width="2.2" fill="none" stroke-linecap="round" opacity=".55"/></svg></span>
    <span class="card-title">Sonos speakers</span>
    <button onclick="rescan()" class="btn-ghost" style="margin-left:auto;">Rescan</button>
  </div>
  <details class="info-toggle">
    <summary>&#9432; What does &#9733; mean?</summary>
    <div class="card-desc">&#9733; = default speaker: streamed to the instant PC2Sonos starts, before
    a network scan finishes. Click a star to set it.</div>
  </details>
  <div style="padding:10px 0 12px; border-bottom:1px solid #232323; margin-bottom:4px;">
    <label style="margin-bottom:6px;">Scale everything together</label>
    <details class="info-toggle">
      <summary>&#9432; How does this work?</summary>
      <div class="card-desc">Scales every enabled Sonos speaker and the PC boost from wherever they're each set right now (individual volumes below stay fully adjustable afterward). 100% is a no-op. Below 100% turns everything down together, including the PC boost. Above 100% turns Sonos speakers up together (up to 100% each, a Sonos limit) -- but never the PC boost, which only ever moves down through this control. Raising the PC boost itself always needs the dedicated slider below, since that one carries its own hardware-risk warning that this control shouldn't be able to trigger as a side effect.</div>
    </details>
    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
      <input type="range" min="0" max="500" step="1" id="masterVolume" value="100"
             oninput="document.getElementById('masterVolumeNum').value = this.value; paintRange(this)"
             onchange="applyMasterVolume()" style="flex:1; min-width:150px;">
      <input type="number" min="0" max="500" step="1" id="masterVolumeNum" value="100"
             oninput="const s=document.getElementById('masterVolume'); s.value=this.value; paintRange(s)"
             onchange="applyMasterVolume()"
             style="width:60px; padding:4px; background:#111; color:#eee; border:1px solid #333; border-radius:8px;">
      <span>%</span>
    </div>
    <div id="masterVolumeResult" style="margin-top:6px; font-size:12px; color:#888;"></div>
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

<div class="grid">
<div class="grid-col">

<div class="card">
  <div class="card-header">
    <span class="card-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14v-2a8 8 0 0 1 16 0v2"/><rect x="2.5" y="14" width="5" height="7" rx="2"/><rect x="16.5" y="14" width="5" height="7" rx="2"/></svg></span>
    <span class="card-title">PC speaker output</span>
  </div>
  <details class="info-toggle">
    <summary>&#9432; What does this do?</summary>
    <div class="card-desc">This is the key setting for keeping your PC's own speakers in sync with Sonos: it's WHICH physical speaker/headphones PC2Sonos plays the delayed audio to. PC2Sonos auto-picks the first real output it finds, which is usually right -- but if your PC speakers don't seem to be playing the delayed feed at all, or you have more than one output connected (headphones + speakers, a monitor's speakers, etc.), check this first before touching anything else below. (Virtual/software outputs, including PC2Sonos's own VB-Cable, are left out of this list -- they're never a real speaker.)</div>
  </details>
  <select id="renderDevice" onchange="setDevice()" style="width:100%; padding:6px; background:#111; color:#eee; border:1px solid #333; border-radius:6px;"></select>
  <div style="margin-top:14px; padding-top:12px; border-top:1px solid #2a2a2a;">
    <label style="margin-bottom:2px;">Volume</label>
    <details class="info-toggle">
      <summary>&#9432; What does this do?</summary>
      <div class="card-desc">How loud PC2Sonos plays the delayed audio through the device above, on top of Windows' own volume for it. 100% is the original level and lower turns it down. This only affects your PC speakers &mdash; each Sonos speaker has its own volume in the Sonos speakers card at the top. Note that Windows' own volume keys and taskbar slider control the virtual cable, not your speakers; the speakers' own Windows volume is under Settings &gt; System &gt; Sound, on your speakers' entry. (If an aux/line-out speaker is still too quiet with that turned up, there's a separate boost under Advanced.)</div>
    </details>
    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
      <input type="range" min="0" max="100" step="1" id="localVolume" value="{{local_volume_percent}}"
             oninput="syncLocalVolume('slider')" onchange="setLocalVolume()" style="flex:1; min-width:150px;">
      <input type="number" min="0" max="100" step="1" id="localVolumeNum" value="{{local_volume_percent}}"
             oninput="syncLocalVolume('number')" onchange="setLocalVolume()"
             style="width:70px; padding:4px; background:#111; color:#eee; border:1px solid #333; border-radius:6px;">
      <span>%</span>
    </div>
  </div>
</div>

<div class="card">
  <div class="card-header">
    <span class="card-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></svg></span>
    <span class="card-title">Local PC-speaker sync delay</span>
  </div>
  <div class="card-desc">Raise until your PC speakers and Sonos play together, with no echo.</div>
  <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
    <input type="range" min="0" max="4000" step="1" id="delay" value="{{delay}}"
           oninput="syncDelay('slider')" onchange="setDelay()" style="flex:1; min-width:150px;">
    <input type="number" min="0" max="4000" step="1" id="delayNum" value="{{delay}}"
           oninput="syncDelay('number')" onchange="setDelay()"
           style="width:70px; padding:4px; background:#111; color:#eee; border:1px solid #333; border-radius:6px;">
    <span>ms</span>
    <button onclick="autoCalibrate('silent')" class="btn-blue">Auto</button>
  </div>
  <div id="calibResult" style="margin-top:8px; font-size:12px; color:#888;"></div>
  <details style="margin-top:12px; font-size:12px; color:#aaa;">
    <summary style="cursor:pointer; color:#ccc;">Prefer a test tone + microphone instead?</summary>
    <div style="margin-top:10px; line-height:1.5;">
      Put the microphone (built-in laptop mic, or any USB/headset mic) somewhere it
      can clearly hear <strong>both</strong> your PC speakers and the Sonos speaker(s)
      you're syncing to at once &mdash; roughly the midpoint between them, not sitting
      right next to either one. A headset mic worn while sitting at the PC usually
      only hears the PC speakers well and will give a bad reading. Works best in a
      quiet room.
      <div style="margin-top:8px;">
        <button onclick="autoCalibrate('acoustic')" class="btn-blue">Calibrate with test tone</button>
      </div>
    </div>
  </details>
</div>

</div>
<div class="grid-col">

<div class="card">
  <div class="card-header">
    <span class="card-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.55a11 11 0 0 1 14 0"/><path d="M8.5 16a6 6 0 0 1 7 0"/><circle cx="12" cy="19" r="1" fill="currentColor" stroke="none"/></svg></span>
    <span class="card-title">Sonos streaming quality</span>
  </div>
  <details class="info-toggle">
    <summary>&#9432; What does this do?</summary>
    <div class="card-desc">Full quality sends Sonos the exact captured audio (typically 44.1kHz) -- the same as always. Reduced halves the sample rate sent to Sonos only; your PC speakers are never affected. Lower bandwidth means less for a weak Wi-Fi link to a speaker to keep up with, at the cost of slightly less crisp highs -- worth trying if a speaker keeps cutting in and out.</div>
  </details>
  <select id="streamQuality" onchange="setStreamQuality()" style="width:100%; padding:6px; background:#111; color:#eee; border:1px solid #333; border-radius:6px;">
    <option value="full">Full quality</option>
    <option value="reduced">Reduced bandwidth</option>
  </select>
  <div id="streamQualityResult" style="margin-top:8px; font-size:12px; color:#888;"></div>
</div>

<div class="card">
  <div class="card-header">
    <span class="card-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/></svg></span>
    <span class="card-title">Sleep timer</span>
  </div>
  <div class="card-desc" style="margin-bottom:8px;">Stops every currently-streaming Sonos speaker after the chosen time (your PC speakers, and any speaker you turn on afterward, are unaffected).</div>
  <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
    <select id="sleepMinutes" style="padding:6px; background:#111; color:#eee; border:1px solid #333; border-radius:6px;">
      <option value="15">15 min</option>
      <option value="30">30 min</option>
      <option value="45">45 min</option>
      <option value="60" selected>60 min</option>
      <option value="90">90 min</option>
    </select>
    <button onclick="startSleepTimer()">Start</button>
    <button id="cancelSleepBtn" onclick="cancelSleepTimer()" class="btn-ghost" style="display:none;">Cancel</button>
  </div>
  <div id="sleepTimerStatus" style="margin-top:8px; font-size:12px; color:#888;"></div>
</div>

<div class="card">
  <div class="card-header">
    <span class="card-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="3" width="16" height="18" rx="2"/><line x1="8" y1="8" x2="16" y2="8"/><line x1="8" y1="12" x2="16" y2="12"/><line x1="8" y1="16" x2="12" y2="16"/></svg></span>
    <span class="card-title">Troubleshooting</span>
  </div>
  <button onclick="exportDiag()">Export Diagnostics</button>
  <div id="diagResult" style="margin-top:8px; font-size:12px; color:#888;"></div>
</div>

</div>
</div>

<details class="card advanced">
  <summary>
    <span class="card-header">
      <span class="card-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="4" x2="5" y2="20"/><circle cx="5" cy="9" r="2" fill="currentColor" stroke="none"/><line x1="12" y1="4" x2="12" y2="20"/><circle cx="12" cy="15" r="2" fill="currentColor" stroke="none"/><line x1="19" y1="4" x2="19" y2="20"/><circle cx="19" cy="7" r="2" fill="currentColor" stroke="none"/></svg></span>
      <span class="card-title">Advanced: EQ, audio source &amp; capture method</span>
    </span>
  </summary>
  <div class="adv-body">
    <div class="adv-note">
      The boost and EQ below can push your speakers harder than their
      intended level, and pushing either far enough can stress or damage
      underpowered speakers/amps over time. <strong>The defaults (100%
      boost, 0dB EQ) are what we recommend</strong> -- adjusting past them
      is at your own risk to your hardware, not just audio quality.
    </div>
    <div class="adv-grid">
    <div class="adv-section">
      <label style="margin-bottom:2px;">PC speaker boost</label>
      <details class="info-toggle">
        <summary>&#9432; What does this do?</summary>
        <div class="card-desc">For an aux/line-out speaker that's too quiet even with the PC speaker Volume at 100% and Windows' volume up: amplifies the signal with a soft limiter, so loud peaks compress gradually instead of clipping. It is applied first, and the Volume slider then turns the boosted sound down, so Volume does exactly what it says at any boost setting. Nothing else changes the boost &mdash; not even the master volume slider.</div>
      </details>
      <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
        <input type="range" min="100" max="500" step="1" id="localGain" value="{{local_gain_percent}}"
               oninput="syncLocalGain('slider')" onchange="setLocalGain()" style="flex:1; min-width:150px;">
        <input type="number" min="100" max="500" step="1" id="localGainNum" value="{{local_gain_percent}}"
               oninput="syncLocalGain('number')" onchange="setLocalGain()"
               style="width:70px; padding:4px; background:#111; color:#eee; border:1px solid #333; border-radius:6px;">
        <span>%</span>
      </div>
      <div style="font-size:11px; color:#777; margin-top:6px;">
        100% = no boost (the default). Above that amplifies the signal &mdash; loud peaks compress gradually instead of clipping, so it stays clean well past 100%.
      </div>
      <div id="localGainWarning" style="display:none; font-size:11px; color:#e0a030; margin-top:4px;">
        &#9888; The boost is on. The higher you go, the more the limiter has to compress to stay clean, and the harder your speakers are pushed.
      </div>
    </div>
    <div class="adv-section">
      <label style="margin-bottom:2px;">PC speaker EQ</label>
      <details class="info-toggle">
        <summary>&#9432; What does this do?</summary>
        <div class="card-desc">Bass/mid/treble for the local speaker path only (Sonos speakers keep their own EQ in the Sonos app).</div>
      </details>
      <div id="eqSliders" style="display:flex; gap:16px; flex-wrap:wrap; margin-top:6px;">
        <div style="display:flex; flex-direction:column; align-items:center; gap:4px;">
          <input type="range" class="eq-fader" min="-24" max="24" step="1" id="eqBass" value="{{eq_bass_db}}"
                 oninput="setLocalEq()" orient="vertical"
                 style="writing-mode: vertical-lr; direction: rtl; width:24px; height:100px;">
          <span id="eqBassVal" style="font-size:11px; color:#aaa;">{{eq_bass_db}} dB</span>
          <span style="font-size:11px; color:#777;">Bass</span>
        </div>
        <div style="display:flex; flex-direction:column; align-items:center; gap:4px;">
          <input type="range" class="eq-fader" min="-24" max="24" step="1" id="eqMid" value="{{eq_mid_db}}"
                 oninput="setLocalEq()" orient="vertical"
                 style="writing-mode: vertical-lr; direction: rtl; width:24px; height:100px;">
          <span id="eqMidVal" style="font-size:11px; color:#aaa;">{{eq_mid_db}} dB</span>
          <span style="font-size:11px; color:#777;">Mid</span>
        </div>
        <div style="display:flex; flex-direction:column; align-items:center; gap:4px;">
          <input type="range" class="eq-fader" min="-24" max="24" step="1" id="eqTreble" value="{{eq_treble_db}}"
                 oninput="setLocalEq()" orient="vertical"
                 style="writing-mode: vertical-lr; direction: rtl; width:24px; height:100px;">
          <span id="eqTrebleVal" style="font-size:11px; color:#aaa;">{{eq_treble_db}} dB</span>
          <span style="font-size:11px; color:#777;">Treble</span>
        </div>
        <button onclick="resetLocalEq()" class="btn-ghost" style="align-self:flex-start;">Reset</button>
      </div>
      <div id="eqWarning" style="display:none; font-size:11px; color:#e0a030; margin-top:6px;">
        &#9888; Past &plusmn;6dB starts sounding less like "more/less bass" and more like a different speaker -- large boosts can also introduce noise.
      </div>
    </div>
    <div class="adv-section">
      <div style="display:flex; align-items:center; justify-content:space-between;">
        <label style="margin-bottom:0;">Audio source &mdash; what PC2Sonos sends to Sonos</label>
        <button onclick="loadAudioSessions()" class="btn-ghost">Refresh</button>
      </div>
      <details class="info-toggle">
        <summary>&#9432; How does this work?</summary>
        <div class="card-desc">An app only shows up here once it's made some sound since PC2Sonos started (or since the last Refresh). Windows can't always capture a specific app this way &mdash; copy-protected playback and some elevated apps aren't capturable regardless. Check one or more apps to mix just those into the Sonos stream, or leave none checked to send everything.</div>
      </details>
      <label style="display:flex; align-items:center; gap:8px; font-weight:400; font-size:13px; padding:4px 0;">
        <input type="checkbox" id="captureWholeSystem" onchange="onWholeSystemToggle()">
        Whole system (default)
      </label>
      <div id="captureAppList" style="display:flex; flex-direction:column; gap:2px; max-height:180px; overflow-y:auto; margin-top:4px; padding:6px; background:#111; border:1px solid #333; border-radius:6px;"></div>
      <div id="captureSourceResult" style="margin-top:8px; font-size:12px; color:#888;"></div>
    </div>
    <div id="captureMethodBlock" class="adv-section" style="display:none;">
      <label style="margin-bottom:2px;">Capture method &mdash; how PC2Sonos reads your PC's audio</label>
      <details class="info-toggle">
        <summary>&#9432; What's the difference?</summary>
        <div class="card-desc"><strong>Loopback</strong> listens to what Windows is already playing into the virtual cable. Windows doesn't count that as microphone access, so PC2Sonos isn't listed under Privacy &amp; security &gt; Microphone and the mic never shows as in use. The <strong>recording device</strong> method opens the cable as if it were a microphone: it carries the identical audio, but Windows treats it as mic access the whole time PC2Sonos runs. Switch to it only if loopback ever gives you silence &mdash; PC2Sonos also falls back to it by itself if loopback can't be used. Switching may reconnect your Sonos speakers for a few seconds.</div>
      </details>
      <select id="captureMethod" onchange="setCaptureMethod()" style="padding:6px; background:#111; color:#eee; border:1px solid #333; border-radius:6px; max-width:100%;">
        <option value="loopback">Loopback &mdash; no microphone access (recommended)</option>
        <option value="recording">Recording device &mdash; Windows shows the mic in use</option>
      </select>
      <div id="captureMethodStatus" style="margin-top:8px; font-size:12px; color:#888;"></div>
    </div>
    </div>
  </div>
</details>
""" + (BLACKHOLE_CREDIT_LINE if sys.platform == "darwin" else VB_CREDIT_LINE) + """

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
function formatAgo(seconds){
  if (seconds === null || seconds === undefined) return 'unknown';
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
  return Math.floor(seconds / 3600) + 'h ago';
}
function paintRange(el){
  // colors the track up to the current value, so the slider shows
  // progress at a glance instead of a bare thumb on a flat bar -- only
  // makes sense for one-directional 0-at-the-low-end sliders (delay,
  // boost, per-speaker volume); the bidirectional EQ faders skip this
  const min = parseFloat(el.min) || 0, max = parseFloat(el.max) || 100;
  const pct = max > min ? 100 * (parseFloat(el.value) - min) / (max - min) : 0;
  el.style.background = `linear-gradient(to right, #1db954 0%, #1db954 ${pct}%, #333 ${pct}%, #333 100%)`;
}
function paintAllRanges(){
  document.querySelectorAll('input[type=range]:not(.eq-fader)').forEach(paintRange);
}
async function refresh(){
  const res = await fetch('/api/speakers');
  const data = await res.json();
  const el = document.getElementById('speakers');
  el.innerHTML = '';
  const statStreaming = document.getElementById('statStreaming');
  if (data.length === 0) {
    el.innerHTML = '<div style="color:#888; padding:10px 0;">Searching for Sonos speakers...</div>';
    statStreaming.textContent = '—';
    statStreaming.className = 'stat-value';
    return;
  }
  const streaming = data.filter(s => s.streaming).length;
  statStreaming.textContent = streaming + ' / ' + data.length;
  statStreaming.className = 'stat-value ' + (streaming > 0 ? 'good' : 'warn');
  data.forEach(s => {
    const div = document.createElement('div');
    div.className = 'speaker';
    const grouped = s.grouped_with && s.grouped_with.length;
    const star = s.is_default ? '&#9733;' : '&#9734;';
    const starTitle = s.is_default ? 'Default speaker (click to unset)' : 'Set as default speaker';
    const health = s.dropout_count > 0
      ? `<span style="flex-basis:100%; font-size:11px; color:#888;">&#9888; ${s.dropout_count} dropout${s.dropout_count === 1 ? '' : 's'} this session &mdash; last ${formatAgo(s.last_dropout_seconds_ago)}</span>`
      : '';
    div.innerHTML = `
      <span onclick="setDefault('${s.uid}', ${s.is_default})" title="${starTitle}"
            style="cursor:pointer; font-size:16px; color:${s.is_default ? '#f5c518' : '#666'};">${star}</span>
      <label class="switch">
        <input type="checkbox" ${s.enabled ? 'checked' : ''} onchange="toggle('${s.uid}', this.checked)">
        <span class="switch-track"></span>
      </label>
      <span class="name">${s.name}${grouped ? ` <span style="font-weight:400; color:#888; font-size:12px;">(grouped with ${s.grouped_with.join(', ')} &mdash; this also controls them)</span>` : ''}</span>
      <input type="range" min="0" max="100" value="${s.volume}"
             oninput="paintRange(this); this.nextElementSibling.textContent = this.value + '%'"
             onchange="setVol('${s.uid}', this.value)">
      <span style="width:36px; display:inline-block;">${s.volume}%</span>
      <span class="status ${s.streaming ? 'on' : 'off'}">${s.streaming ? 'streaming' : 'idle'}</span>
      ${health}
    `;
    el.appendChild(div);
  });
  paintAllRanges();
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
async function applyMasterVolume(){
  const el = document.getElementById('masterVolumeResult');
  const percent = parseInt(document.getElementById('masterVolume').value);
  el.textContent = 'Scaling everything to ' + percent + '%...';
  const res = await fetch('/api/master_volume', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({percent})});
  const data = await res.json();
  if (!data.ok) { el.textContent = 'Failed.'; return; }
  // slider deliberately stays put -- drag it back toward 100 to bring
  // everything back to where it was before you started turning it down
  // (the backend scales from a fixed baseline, not from whatever the
  // last press left things at, so this is always reversible)
  if (data.local_volume_percent !== undefined) {
    document.getElementById('localVolume').value = data.local_volume_percent;
    syncLocalVolume('slider');
  }
  el.textContent = 'Set to ' + percent + '% -- individual volumes below are updated.';
  refresh();
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
  paintRange(slider);
  document.getElementById('statDelay').textContent = slider.value + ' ms';
}
async function setDelay(){
  const v = document.getElementById('delay').value;
  await fetch('/api/delay', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({delay_ms: parseInt(v)})});
}
function syncLocalGain(source){
  const slider = document.getElementById('localGain');
  const num = document.getElementById('localGainNum');
  if (source === 'slider') {
    num.value = slider.value;
  } else {
    let v = parseInt(num.value);
    if (isNaN(v)) return;
    v = Math.max(100, Math.min(500, v));
    slider.value = v;
  }
  document.getElementById('localGainWarning').style.display = (parseInt(slider.value) > 100) ? 'block' : 'none';
  paintRange(slider);
}
function syncLocalVolume(source){
  const slider = document.getElementById('localVolume');
  const num = document.getElementById('localVolumeNum');
  if (source === 'slider') {
    num.value = slider.value;
  } else {
    let v = parseInt(num.value);
    if (isNaN(v)) return;
    v = Math.max(0, Math.min(100, v));
    slider.value = v;
  }
  paintRange(slider);
}
async function setLocalVolume(){
  const v = document.getElementById('localVolume').value;
  await fetch('/api/local_volume', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({percent: parseInt(v)})});
}
async function setLocalGain(){
  const v = document.getElementById('localGain').value;
  await fetch('/api/local_gain', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({percent: parseInt(v)})});
}
let eqDebounce = null;
function setLocalEq(){
  const bass = parseInt(document.getElementById('eqBass').value);
  const mid = parseInt(document.getElementById('eqMid').value);
  const treble = parseInt(document.getElementById('eqTreble').value);
  document.getElementById('eqBassVal').textContent = bass + ' dB';
  document.getElementById('eqMidVal').textContent = mid + ' dB';
  document.getElementById('eqTrebleVal').textContent = treble + ' dB';
  const extreme = Math.max(Math.abs(bass), Math.abs(mid), Math.abs(treble)) > 6;
  document.getElementById('eqWarning').style.display = extreme ? 'block' : 'none';
  // debounce -- dragging a slider fires oninput continuously, no need
  // to POST every single intermediate value
  if (eqDebounce) clearTimeout(eqDebounce);
  eqDebounce = setTimeout(() => {
    fetch('/api/local_eq', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({bass, mid, treble})});
  }, 200);
}
function resetLocalEq(){
  document.getElementById('eqBass').value = 0;
  document.getElementById('eqMid').value = 0;
  document.getElementById('eqTreble').value = 0;
  setLocalEq();
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
        paintRange(document.getElementById('delay'));
        document.getElementById('statDelay').textContent = s.result_ms + ' ms';
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
    opt.textContent = d.name;
    if (d.name === data.current) opt.selected = true;
    sel.appendChild(opt);
  });
  document.getElementById('statDevice').textContent = data.current || '—';
}
async function setDevice(){
  const sel = document.getElementById('renderDevice');
  document.getElementById('statDevice').textContent = sel.value || '—';
  await fetch('/api/render_device', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({device: sel.value})});
}
async function loadAudioSessions(){
  const res = await fetch('/api/audio_sessions');
  const data = await res.json();
  const selected = new Set(data.mode === 'apps' ? data.targets : []);
  const list = document.getElementById('captureAppList');
  list.innerHTML = '';
  data.sessions.forEach(s => {
    const row = document.createElement('label');
    row.style.cssText = 'display:flex; align-items:center; gap:8px; font-weight:400; font-size:13px; padding:2px 0;';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = s.name;
    cb.checked = selected.has(s.name);
    cb.onchange = setCaptureSource;
    row.appendChild(cb);
    row.appendChild(document.createTextNode(s.name));
    list.appendChild(row);
  });
  const wholeCb = document.getElementById('captureWholeSystem');
  wholeCb.checked = selected.size === 0;
  list.style.display = wholeCb.checked ? 'none' : 'flex';
}
function onWholeSystemToggle(){
  const wholeCb = document.getElementById('captureWholeSystem');
  const list = document.getElementById('captureAppList');
  if (wholeCb.checked) {
    list.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
  }
  list.style.display = wholeCb.checked ? 'none' : 'flex';
  setCaptureSource();
}
async function setCaptureSource(){
  const el = document.getElementById('captureSourceResult');
  const wholeCb = document.getElementById('captureWholeSystem');
  const list = document.getElementById('captureAppList');
  const checked = Array.from(list.querySelectorAll('input[type=checkbox]:checked')).map(cb => cb.value);
  if (checked.length > 0) wholeCb.checked = false;  // picking an app implicitly turns off "whole system"
  const targets = wholeCb.checked ? [] : checked;
  list.style.display = wholeCb.checked ? 'none' : 'flex';
  el.textContent = 'Switching...';
  const res = await fetch('/api/capture_source', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode: targets.length ? 'apps' : 'system', targets})});
  const data = await res.json();
  el.textContent = data.ok
    ? (targets.length ? ('Now mixing ' + targets.join(', ') + ' into the Sonos stream.') : 'Now capturing the whole system again.')
    : ('Failed: ' + data.error);
}
let _captureMethodBusy = false;
function showCaptureMethodStatus(d){
  const el = document.getElementById('captureMethodStatus');
  const c = d.capture || {};
  const rate = c.rate ? (' at ' + (c.rate / 1000) + ' kHz') : '';
  el.style.color = '#888';
  if (!c.method) {
    el.textContent = 'Not capturing yet (waiting for the virtual cable).';
  } else if (c.method === 'apps') {
    el.textContent = 'Currently mixing selected apps (see Audio source above), which is separate from this setting.';
  } else if (c.method === 'loopback') {
    el.textContent = 'In use: loopback capture' + rate + '.';
  } else if (c.fallback_reason) {
    el.style.color = '#e0a030';
    el.textContent = 'Loopback could not be used (' + c.fallback_reason + '), so the recording device is in use instead' + rate + '. Windows will show the mic as in use.';
  } else {
    el.textContent = 'In use: recording device' + rate + '. Windows will show the mic as in use.';
  }
}
async function loadCaptureMethod(){
  try {
    const res = await fetch('/api/capture_method');
    const d = await res.json();
    const block = document.getElementById('captureMethodBlock');
    if (!d.supported) { block.style.display = 'none'; return; }
    block.style.display = 'block';
    if (!_captureMethodBusy) document.getElementById('captureMethod').value = d.configured;
    if (!_captureMethodBusy) showCaptureMethodStatus(d);
  } catch (e) {}
}
async function setCaptureMethod(){
  const sel = document.getElementById('captureMethod');
  const el = document.getElementById('captureMethodStatus');
  _captureMethodBusy = true;
  sel.disabled = true;
  el.style.color = '#888';
  el.textContent = 'Switching... your Sonos speakers may reconnect for a few seconds.';
  try {
    const res = await fetch('/api/capture_method', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({method: sel.value})});
    const d = await res.json();
    if (d.ok) { showCaptureMethodStatus(d); } else { el.textContent = 'Failed: ' + d.error; }
  } catch (e) {
    el.textContent = 'Failed: ' + e;
  } finally {
    sel.disabled = false;
    _captureMethodBusy = false;
  }
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
let _platformAction = null;
async function checkPlatform(){
  // Only ever shows on macOS. Two different states, not one: something
  // actually broken (BlackHole missing, permission denied -- capture
  // silently produces nothing, with no error anywhere else) vs. nothing
  // broken but still worth explaining (the Microphone indicator staying
  // lit the whole time PC2Sonos runs, which is expected -- see the
  // info branch below -- but looks alarming with zero context).
  try {
    const res = await fetch('/api/platform_status');
    const s = await res.json();
    const banner = document.getElementById('platformBanner');
    const text = document.getElementById('platformText');
    const btn = document.getElementById('platformButton');
    banner.classList.remove('banner-warn', 'banner-info');
    if (s.platform !== 'darwin') {
      // Windows: nothing to say while loopback is working. The one case
      // worth a banner is loopback having quietly fallen back to the
      // recording device, because that brings back the microphone
      // indicator this app otherwise no longer causes -- explain it,
      // rather than leaving the person to wonder why it's lit again.
      const c = s.capture || {};
      if (s.platform === 'win32' && c.configured === 'loopback' && c.method === 'recording' && c.fallback_reason) {
        banner.classList.add('banner-warn');
        text.textContent = `PC2Sonos couldn't use loopback capture (${c.fallback_reason}), so it is reading the virtual cable as a recording device instead. Audio works normally, but Windows will show the microphone as in use while PC2Sonos runs. That is the cable, not your real microphone.`;
        btn.style.display = 'none';
        banner.style.display = 'block';
      } else {
        banner.style.display = 'none';
      }
      return;
    }
    if (!s.capture_device_present) {
      banner.classList.add('banner-warn');
      text.textContent = 'BlackHole (the virtual audio device PC2Sonos captures from) is not installed. Install it with: brew install --cask blackhole-2ch';
      btn.style.display = 'inline-block';
      btn.textContent = 'Open BlackHole website';
      _platformAction = () => window.open('https://existential.audio/blackhole/', '_blank');
      banner.style.display = 'block';
    } else if (['denied', 'restricted', 'not determined'].includes(s.microphone_permission)) {
      banner.classList.add('banner-warn');
      text.textContent = 'macOS is blocking audio capture (Microphone permission for PC2Sonos is ' + s.microphone_permission + '). BlackHole counts as a microphone. Turn on PC2Sonos under System Settings > Privacy & Security > Microphone, then quit and reopen PC2Sonos.';
      btn.style.display = 'inline-block';
      btn.textContent = 'Open Microphone Settings';
      _platformAction = () => fetch('/api/microphone_settings', {method:'POST'});
      banner.style.display = 'block';
    } else {
      // permission is fine and audio is flowing -- nothing to FIX, but
      // the Microphone indicator being lit the entire time this runs is
      // real and needs an explanation, not silence, or it just looks
      // like the app is spying (see webapp.api_platform_status)
      banner.classList.add('banner-info');
      text.textContent = `macOS may show a Microphone indicator the whole time PC2Sonos runs -- that's expected, not a bug: reading from BlackHole (the virtual audio device this app streams your system audio through) counts as "Microphone" access to macOS, even though BlackHole only ever carries your Mac's own audio, never your room or your voice. Your real microphone is only ever touched if you press "Calibrate with test tone" on the dashboard, which is optional, and listens for about 6 seconds.`;
      btn.style.display = 'none';
      banner.style.display = 'block';
    }
  } catch (e) {}
}
function platformAction(){ if (_platformAction) _platformAction(); }
let _updateDownloadUrl = null;
let _updatePoll = null;
function addInline(parent, str){
  // just enough markdown for release notes -- **bold**, `code`, [text](https://link)
  // -- built with DOM calls (never innerHTML), so nothing in the notes can
  // inject markup into the dashboard
  let i = 0;
  while (i < str.length) {
    let next = -1, kind = '';
    for (const [pos, k] of [[str.indexOf('**', i), 'b'], [str.indexOf('`', i), 'c'], [str.indexOf('[', i), 'l']]) {
      if (pos !== -1 && (next === -1 || pos < next)) { next = pos; kind = k; }
    }
    if (next === -1) { parent.appendChild(document.createTextNode(str.slice(i))); return; }
    if (next > i) parent.appendChild(document.createTextNode(str.slice(i, next)));
    if (kind === 'b') {
      const end = str.indexOf('**', next + 2);
      if (end === -1) { parent.appendChild(document.createTextNode(str.slice(next))); return; }
      const el = document.createElement('strong');
      el.textContent = str.slice(next + 2, end);
      parent.appendChild(el);
      i = end + 2;
    } else if (kind === 'c') {
      const end = str.indexOf('`', next + 1);
      if (end === -1) { parent.appendChild(document.createTextNode(str.slice(next))); return; }
      const el = document.createElement('code');
      el.textContent = str.slice(next + 1, end);
      parent.appendChild(el);
      i = end + 1;
    } else {
      const close = str.indexOf('](', next);
      const end = close === -1 ? -1 : str.indexOf(')', close + 2);
      const url = end === -1 ? '' : str.slice(close + 2, end);
      if (end === -1 || !(url.startsWith('https://') || url.startsWith('http://'))) {
        parent.appendChild(document.createTextNode('['));
        i = next + 1;
        continue;
      }
      const a = document.createElement('a');
      a.textContent = str.slice(next + 1, close);
      a.href = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      parent.appendChild(a);
      i = end + 1;
    }
  }
}
function renderNotes(box, text){
  box.textContent = '';
  let list = null;
  for (const raw of String(text || '').split(String.fromCharCode(10))) {
    const line = raw.split(String.fromCharCode(13)).join('').trim();
    if (!line) { list = null; continue; }
    if (line.startsWith('- ') || line.startsWith('* ')) {
      if (!list) { list = document.createElement('ul'); box.appendChild(list); }
      const li = document.createElement('li');
      addInline(li, line.slice(2));
      list.appendChild(li);
      continue;
    }
    list = null;
    const p = document.createElement('p');
    let h = line;
    while (h.startsWith('#')) h = h.slice(1);
    h = h.trim();
    if (h !== line) {
      const strong = document.createElement('strong');
      addInline(strong, h);
      p.appendChild(strong);
    } else {
      addInline(p, line);
    }
    box.appendChild(p);
  }
}
async function checkUpdate(){
  // Polls OUR OWN local /api/update_status, not GitHub -- the one real
  // GitHub request already happened once at startup (see updater.py).
  // This just waits for that background result to land, then stops.
  try {
    const res = await fetch('/api/update_status');
    const s = await res.json();
    if (s.checked) {
      if (_updatePoll) clearInterval(_updatePoll);
      if (s.update_available) {
        _updateDownloadUrl = s.download_url;
        document.getElementById('updateText').textContent =
          'PC2Sonos ' + s.latest_version + ' is available — you have ' + s.current_version + '.';
        document.getElementById('updateBanner').style.display = 'block';
        // the changelog stays one click away for as long as an update is
        // pending -- reading it doesn't require downloading anything, so it
        // can inform the decision to update rather than come after it
        if (s.notes) {
          document.getElementById('updateNotesVersion').textContent = s.latest_version;
          const body = document.getElementById('updateNotesBody');
          renderNotes(body, s.notes);
          if (s.release_url && s.release_url.startsWith('https://')) {
            const p = document.createElement('p');
            const a = document.createElement('a');
            a.textContent = 'Open the full release page';
            a.href = s.release_url;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            p.appendChild(a);
            body.appendChild(p);
          }
          document.getElementById('updateNotes').style.display = 'block';
        }
      }
    }
  } catch (e) {
    // a transient fetch hiccup shouldn't permanently give up -- just try
    // again on the next tick instead of clearing the interval here
  }
}
function downloadUpdate(){
  if (_updateDownloadUrl) window.open(_updateDownloadUrl, '_blank');
}
async function loadLevel(){
  try {
    const res = await fetch('/api/level');
    const data = await res.json();
    document.getElementById('levelFill').style.width = data.level + '%';
  } catch (e) {}
}
async function loadStreamQuality(){
  const res = await fetch('/api/stream_quality');
  const data = await res.json();
  document.getElementById('streamQuality').value = data.quality;
}
async function setStreamQuality(){
  const el = document.getElementById('streamQualityResult');
  const quality = document.getElementById('streamQuality').value;
  el.textContent = 'Applying -- Sonos speakers will briefly reconnect...';
  const res = await fetch('/api/stream_quality', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({quality})});
  const data = await res.json();
  el.textContent = data.ok ? 'Applied.' : ('Failed: ' + (data.error || 'unknown error'));
}
let sleepCountdownInterval = null;
let sleepRemainingSeconds = null;
function formatMMSS(totalSeconds){
  const m = Math.floor(totalSeconds / 60), s = totalSeconds % 60;
  return m + ':' + String(s).padStart(2, '0');
}
function renderSleepStatus(){
  const el = document.getElementById('sleepTimerStatus');
  const cancelBtn = document.getElementById('cancelSleepBtn');
  if (sleepRemainingSeconds === null) {
    el.textContent = '';
    cancelBtn.style.display = 'none';
    return;
  }
  el.textContent = 'Stopping Sonos playback in ' + formatMMSS(sleepRemainingSeconds) + '...';
  cancelBtn.style.display = 'inline-block';
}
function tickSleepCountdown(){
  if (sleepRemainingSeconds === null) return;
  sleepRemainingSeconds = Math.max(0, sleepRemainingSeconds - 1);
  renderSleepStatus();
  if (sleepRemainingSeconds === 0) {
    clearInterval(sleepCountdownInterval);
    sleepCountdownInterval = null;
    sleepRemainingSeconds = null;
    setTimeout(refresh, 1500);  // give the backend a moment to actually stop things, then reflect it
  }
}
function armSleepCountdown(remainingSeconds){
  sleepRemainingSeconds = remainingSeconds;
  if (sleepCountdownInterval) clearInterval(sleepCountdownInterval);
  sleepCountdownInterval = remainingSeconds === null ? null : setInterval(tickSleepCountdown, 1000);
  renderSleepStatus();
}
async function startSleepTimer(){
  const minutes = parseInt(document.getElementById('sleepMinutes').value);
  const res = await fetch('/api/sleep_timer', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({minutes})});
  const data = await res.json();
  armSleepCountdown(data.remaining_seconds);
}
async function cancelSleepTimer(){
  await fetch('/api/sleep_timer', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({minutes: 0})});
  armSleepCountdown(null);
}
async function loadSleepTimer(){
  const res = await fetch('/api/sleep_timer');
  const data = await res.json();
  armSleepCountdown(data.active ? data.remaining_seconds : null);
}
async function loadNowPlaying(){
  const el = document.getElementById('nowPlayingText');
  try {
    const res = await fetch('/api/now_playing');
    const data = await res.json();
    if (!data.available || !data.playing) { el.style.display = 'none'; return; }
    el.textContent = '♪ Now playing: ' + data.title + (data.artist ? ' — ' + data.artist : '');
    el.style.display = 'block';
  } catch (e) { el.style.display = 'none'; }
}
refresh();
loadDevices();
loadAudioSessions();
loadCaptureMethod();
setInterval(loadCaptureMethod, 5000);
loadSeedIps();
loadStreamQuality();
loadSleepTimer();
loadNowPlaying();
setInterval(loadNowPlaying, 4000);
checkDonatePrompt();
checkUpdate();
checkPlatform();
setInterval(checkPlatform, 5000);
syncLocalVolume('slider');
syncLocalGain('slider');  // shows the warning immediately if the saved boost is already past 100%
setLocalEq();  // shows the warning immediately if a saved EQ band is already past +/-6dB
_updatePoll = setInterval(checkUpdate, 3000);
setInterval(refresh, 4000);
setInterval(loadLevel, 300);
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return render_template_string(
        DASHBOARD_HTML, delay=config["local_delay_ms"], donate_url=DONATE_URL,
        local_volume_percent=round(config.get("local_volume", 1.0) * 100),
        local_gain_percent=round(max(1.0, config.get("local_render_gain", 1.0)) * 100),
        eq_bass_db=round(config.get("local_eq_bass_db", 0.0)),
        eq_mid_db=round(config.get("local_eq_mid_db", 0.0)),
        eq_treble_db=round(config.get("local_eq_treble_db", 0.0)))


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


@app.route("/api/master_volume", methods=["POST"])
def api_master_volume():
    """Scale every enabled Sonos speaker's volume AND the PC speaker volume
    together, relative to a fixed baseline -- whatever they were set to the
    last time this slider sat at 100% -- rather than to whatever they
    currently are. Scaling from the live current values (the old behavior)
    compounds on every press (50% twice lands on 25%, not back to the first
    50%) and can never be undone by sliding back up. Moving the slider back
    to 100 restores the baseline exactly, and that restored state becomes
    the new baseline for next time.

    0-500% for Sonos speakers -- no hardware risk there, Sonos enforces
    its own 100% ceiling per speaker regardless of scale. The PC speaker
    volume only ever scales DOWN through this control (capped at its
    baseline, never above 100%): turning it up is the dedicated slider's
    job. The PC BOOST (Advanced) is not touched at all -- it is the one
    setting that can stress speakers, so nothing that gets dragged around
    casually is allowed to move it; an earlier version that scaled it
    pushed a real boost to its 500% ceiling from a single press."""
    global _master_volume_baseline
    data = request.get_json(force=True)
    percent = max(0, min(500, int(data.get("percent", 100))))

    with _master_volume_lock:
        if _master_volume_baseline is None:
            _master_volume_baseline = {
                "speakers": {s["uid"]: s["volume"] for s in speaker_mgr.list() if s["enabled"]},
                "local_volume_percent": round(config.get("local_volume", 1.0) * 100),
            }

        scale = percent / 100.0
        volume_scale = min(1.0, scale)  # PC volume: down only, never above its baseline
        for uid, base_volume in _master_volume_baseline["speakers"].items():
            speaker_mgr.set_volume(uid, round(base_volume * scale))
        new_volume_percent = max(0, min(100, round(_master_volume_baseline["local_volume_percent"] * volume_scale)))
        config["local_volume"] = new_volume_percent / 100.0
        save_config(config)

        if percent == 100:
            _master_volume_baseline = None

    return jsonify({"ok": True, "local_volume_percent": new_volume_percent})


@app.route("/api/level")
def api_level():
    """Live input level (0-100) for the dashboard's meter -- one shared
    reading since every enabled speaker (and the local path) gets the
    same captured signal. Polled frequently (see the dashboard's own
    poll interval), so this must stay cheap: just reads an already-
    computed float, no audio work happens on this request."""
    return jsonify({"level": round(broadcaster.level_pct, 1)})


@app.route("/api/now_playing")
def api_now_playing():
    """What Windows itself currently shows as "now playing" (taskbar/
    lock-screen media controls) -- Windows-only, see now_playing.py for
    why this can't work the same way on macOS. This is a query, not a
    guarantee of what's actually going to Sonos right now (e.g. in per-
    app capture mode the selected app and the system's media session
    aren't necessarily the same thing) -- it's a convenience display,
    not a claim about the stream's exact contents."""
    from now_playing import get_now_playing
    info = get_now_playing()
    if info is None:
        return jsonify({"available": False})
    return jsonify({"available": True, **info})


@app.route("/api/sleep_timer", methods=["GET", "POST"])
def api_sleep_timer():
    """GET reports the countdown (for a page load/reload mid-countdown);
    POST {minutes: N>0} arms it, replacing any timer already running,
    and {minutes: 0} cancels. See _fire_sleep_timer for what firing
    actually does."""
    global _sleep_timer
    if request.method == "GET":
        with _sleep_timer_lock:
            if _sleep_timer is None:
                return jsonify({"active": False, "remaining_seconds": None})
            remaining = max(0, int(_sleep_timer["deadline"] - time.monotonic()))
        return jsonify({"active": True, "remaining_seconds": remaining})

    data = request.get_json(force=True)
    minutes = max(0, min(600, int(data.get("minutes", 0))))
    with _sleep_timer_lock:
        if _sleep_timer is not None:
            _sleep_timer["timer"].cancel()
            _sleep_timer = None
        if minutes > 0:
            t = threading.Timer(minutes * 60, _fire_sleep_timer)
            t.daemon = True
            t.start()
            _sleep_timer = {"deadline": time.monotonic() + minutes * 60, "timer": t}
        active = _sleep_timer is not None
        remaining = minutes * 60 if active else None
    return jsonify({"ok": True, "active": active, "remaining_seconds": remaining})


@app.route("/api/stream_quality", methods=["GET", "POST"])
def api_stream_quality():
    """The Sonos-only bandwidth setting (see REDUCED_SAMPLE_RATE and
    stream_wav) -- separate from local_gain/local_eq's config routes
    because changing it has to force every currently-streaming speaker
    to reconnect (a live connection's WAV header already declared the
    old sample rate; there's no way to change that mid-stream), the same
    way switching capture mode already does."""
    if request.method == "GET":
        return jsonify({"quality": config.get("sonos_stream_quality", "full")})
    data = request.get_json(force=True)
    quality = data.get("quality")
    if quality not in ("full", "reduced"):
        return jsonify({"ok": False, "error": "quality must be 'full' or 'reduced'"}), 400
    config["sonos_stream_quality"] = quality
    save_config(config)
    base_url = f"http://{get_lan_ip()}:{config['http_port']}"
    speaker_mgr.reconnect_all_streaming(base_url)
    return jsonify({"ok": True})


@app.route("/api/delay", methods=["POST"])
def api_set_delay():
    data = request.get_json(force=True)
    config["local_delay_ms"] = max(0, int(data.get("delay_ms", config["local_delay_ms"])))
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/local_gain", methods=["POST"])
def api_set_local_gain():
    # The BOOST: percent 100-500, mapped to a 1.0-5.0 multiplier that
    # audio_engine's render loop applies on top of the volume. Read fresh
    # every chunk there, so this takes effect immediately -- no render
    # restart needed. 100% (no boost) is the safe/no-warning setting; the
    # dashboard shows a warning above that.
    data = request.get_json(force=True)
    percent = max(100, min(500, int(data.get("percent", 100))))
    config["local_render_gain"] = percent / 100.0
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/local_volume", methods=["POST"])
def api_set_local_volume():
    # The PC speaker VOLUME: percent 0-100, the plain slider on the main
    # page (100 = the original level). Read fresh every chunk in the render
    # loop, so it takes effect immediately.
    data = request.get_json(force=True)
    percent = max(0, min(100, int(data.get("percent", 100))))
    config["local_volume"] = percent / 100.0
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/local_eq", methods=["POST"])
def api_set_local_eq():
    # Each band is dB, clamped to +/-24 -- read fresh every chunk in
    # audio_engine's _ThreeBandEQ, so this takes effect immediately with
    # no render restart. Local speaker path only; never touches the
    # Sonos-facing stream. The EQ stage soft-limits its own output (see
    # _soft_limit in audio_engine.py), so a big boost on one band
    # compresses gracefully instead of hard-clipping before the gain
    # stage's limiter would even get a chance to help.
    data = request.get_json(force=True)
    for key, config_key in (("bass", "local_eq_bass_db"),
                             ("mid", "local_eq_mid_db"),
                             ("treble", "local_eq_treble_db")):
        if key in data:
            config[config_key] = max(-24.0, min(24.0, float(data[key])))
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/devices")
def api_devices():
    # Virtual/software outputs (VB-Cable included) are never a real
    # speaker a person would pick here -- PC2Sonos uses the cable
    # internally regardless of what shows in this list. Diagnostics
    # (diagnostics.py) still reports the full list with a "looks virtual"
    # flag, for troubleshooting; this is just the dashboard's picker.
    devices = [d for d in list_output_devices() if not d.get("likely_virtual")]
    return jsonify({
        "devices": devices,
        "current": get_current_render_device_name(),
    })


@app.route("/api/platform_status")
def api_platform_status():
    """macOS-specific health the dashboard can't otherwise see: is the
    capture device (BlackHole) present, and has macOS granted the
    audio-input permission that reading from it requires (without it
    CoreAudio delivers silence with no error). On Windows both report
    n/a."""
    from audio_engine import find_device_index, get_capture_status
    idx, _ = find_device_index(config["capture_device_substr"], want_input=True)
    mic = "n/a"
    if sys.platform == "darwin":
        try:
            from macos_app import microphone_status
            mic = microphone_status()
        except Exception as e:
            mic = f"unknown ({e})"
    return jsonify({"platform": sys.platform, "capture_device_present": idx is not None,
                    "capture_device_substr": config["capture_device_substr"],
                    "microphone_permission": mic,
                    # how the audio is actually being read right now (Windows: loopback
                    # vs. the microphone-class recording device) -- see /api/capture_method
                    "capture": get_capture_status()})


@app.route("/api/capture_method", methods=["GET", "POST"])
def api_capture_method():
    """Windows: read the virtual cable by WASAPI loopback (default, no
    microphone access) or as a recording device (the original method,
    which Windows treats as the microphone). `capture` reports what is
    actually in use, which differs from `configured` whenever loopback
    had to fall back. Not applicable on macOS (`supported` is false and
    the dashboard hides the control there)."""
    from audio_backend import BACKEND
    from audio_engine import get_capture_status
    if request.method == "POST":
        method = (request.get_json(force=True) or {}).get("method")
        if method not in ("loopback", "recording"):
            return jsonify({"ok": False, "error": "method must be 'loopback' or 'recording'"}), 400
        if method != config.get("capture_method"):
            restart_capture(new_method=method)
            # Let the restarted capture thread open its stream before
            # replying, so the answer describes the new state rather than
            # a blank in-between one.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and get_capture_status()["method"] is None:
                time.sleep(0.1)
    return jsonify({"ok": True, "supported": BACKEND == "pyaudiowpatch",
                    "configured": config.get("capture_method"),
                    "capture": get_capture_status()})


@app.route("/api/microphone_settings", methods=["POST"])
def api_microphone_settings():
    try:
        from macos_app import open_microphone_settings
        open_microphone_settings()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/update_status")
def api_update_status():
    # Local-only -- reads updater.py's in-memory cache from the single
    # check that ran once at startup. Never triggers a new GitHub request.
    from updater import get_status
    return jsonify(get_status())


@app.route("/api/render_device", methods=["POST"])
def api_set_render_device():
    data = request.get_json(force=True)
    device_name = data.get("device", "")
    restart_render(new_device_substr=device_name)
    return jsonify({"ok": True})


@app.route("/api/audio_sessions")
def api_audio_sessions():
    # Apps with an audio session open right now -- the "capture just this
    # app" picker. Not available at all on non-Windows/older Windows (see
    # per_app_audio.py's Windows-version notes), so this degrades to an
    # empty list rather than a 500 in that case; the dashboard just shows
    # "whole system" as the only option.
    try:
        from per_app_audio import list_audio_sessions
        sessions = list_audio_sessions()
    except Exception as e:
        print(f"[audio] can't list per-app audio sessions: {e}")
        sessions = []
    return jsonify({
        "sessions": sessions,
        "mode": config.get("capture_mode", "system"),
        "targets": config.get("capture_target_names", []),
    })


@app.route("/api/capture_source", methods=["POST"])
def api_set_capture_source():
    data = request.get_json(force=True)
    mode = data.get("mode")
    if mode not in ("system", "apps"):
        return jsonify({"ok": False, "error": "mode must be 'system' or 'apps'"}), 400
    targets = data.get("targets", [])
    if not isinstance(targets, list):
        return jsonify({"ok": False, "error": "targets must be a list"}), 400
    targets = [str(t).strip() for t in targets if str(t).strip()]
    if mode == "apps" and not targets:
        return jsonify({"ok": False, "error": "pick at least one application first"}), 400
    restart_capture(new_mode=mode, new_target_names=targets)
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
        in_rate = config["sample_rate"]
        channels = config["channels"]
        sample_width = config["sample_width"]
        # "reduced" halves the rate sent to THIS speaker only -- the local
        # PC-speaker path reads straight from the broadcaster untouched,
        # so this never affects it. Decided once per connection: a change
        # mid-stream forces a reconnect anyway (api_stream_quality), which
        # starts a fresh generate() call and picks up the new setting.
        out_rate = in_rate
        if config.get("sonos_stream_quality") == "reduced" and in_rate > REDUCED_SAMPLE_RATE:
            out_rate = REDUCED_SAMPLE_RATE
        resample_state = None

        yield wav_header(out_rate, channels, sample_width)
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
        last_trim_log = 0.0
        try:
            while True:
                chunk = q.get()
                backlog = q.qsize()
                if backlog > max_backlog_chunks:
                    dropped = 0
                    for _ in range(backlog - trim_to_chunks):
                        try:
                            chunk = q.get_nowait()
                            dropped += 1
                        except queue.Empty:
                            break
                    # this IS an audible skip/glitch on the Sonos side --
                    # was previously silent, so a real stutter left no
                    # trace anywhere to diagnose it from. Throttled to
                    # once per 5s so a bad stretch doesn't flood the log.
                    now = time.monotonic()
                    if dropped and now - last_trim_log > 5:
                        last_trim_log = now
                        print(f"[stream] backlog hit {backlog} chunks "
                              f"(~{backlog * chunk_ms:.0f}ms) for {uid}; "
                              f"dropped {dropped} chunks (~{dropped * chunk_ms:.0f}ms) "
                              f"-- this is an audible skip on the Sonos side")
                if out_rate != in_rate:
                    chunk, resample_state = audioop.ratecv(
                        chunk, sample_width, channels, in_rate, out_rate, resample_state)
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
