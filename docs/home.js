/* PC2Sonos home: hero signal ribbons, echo lab (Web Audio beat + lanes), scroll-lit statement, reveals. */
(function () {
  "use strict";
  var reduce = false; // motion stays on everywhere, including phones with Reduce Motion on
  var DPR = Math.min(window.devicePixelRatio || 1, 2);

  /* ---------------- audio ---------------- */
  // The beat is two pre-rendered looping WAVs (in sync, and with the echo) played through plain <audio>
  // elements, the same way the DJ Koma site plays its music. Phones start an <audio> element from the
  // first touch, and iPhones play it even with the silent switch on. The pattern below matches the files
  // so the waveform lanes line up with what you hear.
  var BPM = 116, STEP = 60 / BPM / 4, ECHO = 0.19, BARS = 4, LOOP = 16 * STEP * BARS;
  // 16-step pattern: k kick, s snare, h hat, bass notes (semitones from A1)
  var KICK = [1,0,0,0, 1,0,0,0, 1,0,1,0, 1,0,0,0];
  var SNARE= [0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,1];
  var HAT  = [0,0,1,0, 0,0,1,0, 0,0,1,0, 0,1,1,0];
  var BASS = [0,null,null,0, null,null,12,null, 3,null,null,3, null,5,null,7];
  var playing = false, starting = false, synced = true, userStopped = false;

  function makeEl(src) { var a = new Audio(src); a.loop = true; a.preload = "auto"; a.setAttribute("playsinline", ""); return a; }
  var dryEl = makeEl("beat.wav"), echoEl = makeEl("beat-echo.wav");
  function applyMute() { dryEl.muted = !synced; echoEl.muted = synced; }
  applyMute();
  // Must be called from inside a touch, click or key press. If the browser blocks it, the next one retries.
  function start() {
    if (playing || starting) return;
    starting = true;
    try { if (navigator.audioSession) navigator.audioSession.type = "playback"; } catch (e) {}
    echoEl.currentTime = dryEl.currentTime;
    var ps = [dryEl.play(), echoEl.play()].map(function (x) { return x && x.then ? x : Promise.resolve(); });
    Promise.all(ps).then(function () { starting = false; playing = true; paintButtons(); stopListening(); },
      function () { starting = false; dryEl.pause(); echoEl.pause(); });
  }
  function stop() { dryEl.pause(); echoEl.pause(); playing = false; paintButtons(); }
  function setPlaying(on) { on ? start() : stop(); }
  function setSynced(on) { synced = on; applyMute(); paintButtons(); }
  function audioTime() { return dryEl.currentTime || 0; }

  var soundBtn = document.getElementById("soundBtn"), soundLbl = document.getElementById("soundLbl");
  var playBtn = document.getElementById("labPlay"), sws = Array.prototype.slice.call(document.querySelectorAll(".sync-switch"));
  var hint = document.getElementById("tapHint");
  var lab = document.getElementById("lab");
  function paintButtons() {
    if (soundBtn) { soundBtn.setAttribute("aria-pressed", playing ? "true" : "false"); soundLbl.textContent = playing ? "Sound off" : "Sound on"; }
    if (playBtn) playBtn.innerHTML = playing ? "&#10074;&#10074; Stop the beat" : "&#9654; Play the beat";
    sws.forEach(function (sw) { sw.setAttribute("aria-checked", synced ? "true" : "false"); sw.querySelector(".sync-lbl").textContent = synced ? "In sync" : "Echo on"; });
    if (hint && playing) hint.textContent = synced ? "Flip the switch to hear the echo you'd get without PC2Sonos." : "That's the echo. Flip it back to fix it.";
    if (lab) lab.classList.toggle("synced", synced);
  }
  function toggle() { userStopped = playing; setPlaying(!playing); }
  if (soundBtn) soundBtn.addEventListener("click", toggle);
  if (playBtn) playBtn.addEventListener("click", toggle);
  sws.forEach(function (sw) { sw.addEventListener("click", function () { setSynced(!synced); if (!playing) { userStopped = false; start(); } }); });
  // Start the beat on the first interaction of any kind (tap, swipe, click, key, wheel, scroll), like the
  // DJ Koma site. On phones the finger lifting off after a tap or a scroll swipe is what unlocks sound.
  // Listeners stay on until the beat is actually playing, so a blocked attempt retries on the next one.
  var FIRST = ["click", "touchstart", "touchend", "touchmove", "pointerdown", "pointerup", "keydown", "wheel", "scroll"];
  function firstTouch(e) {
    if (e.target && e.target.closest && e.target.closest("[data-audio-ctl]")) return;
    if (userStopped) return stopListening();
    start();
  }
  function stopListening() { FIRST.forEach(function (t) { removeEventListener(t, firstTouch, { capture: true, passive: true }); }); }
  FIRST.forEach(function (t) { addEventListener(t, firstTouch, { capture: true, passive: true }); });
  // iOS sometimes pauses page audio (a call, another app); pick the beat back up on the next scroll.
  addEventListener("scroll", function () { if (playing && dryEl.paused) { var r = dryEl.play(); if (r && r.catch) r.catch(function () {}); r = echoEl.play(); if (r && r.catch) r.catch(function () {}); } }, { passive: true });

  /* ---------------- lanes ---------------- */
  // amplitude of the beat at time t (seconds), same pattern the synth plays
  function amp(t) {
    var bar = 16 * STEP, tb = ((t % bar) + bar) % bar, a = 0;
    for (var i = 0; i < 16; i++) {
      var dt = tb - i * STEP; if (dt < 0) dt += bar;
      if (KICK[i]) a += Math.exp(-dt * 11) * 1.0;
      if (SNARE[i]) a += Math.exp(-dt * 22) * 0.7;
      if (HAT[i]) a += Math.exp(-dt * 60) * 0.35;
      if (BASS[i] !== null) a += Math.exp(-dt * 7) * 0.25;
    }
    return Math.min(a, 1.3);
  }
  var LAG = 1.48, WINDOW = 4.2, pcShift = LAG, gapShown = 0;
  var lanes = [document.getElementById("lanePc"), document.getElementById("laneSonos")].filter(Boolean);
  function fit(c) { var r = c.getBoundingClientRect(); c.width = Math.max(1, r.width * DPR); c.height = Math.max(1, r.height * DPR); }
  function grad(g, w) { var l = g.createLinearGradient(0, 0, w, 0); l.addColorStop(0, "#ffb23f"); l.addColorStop(.52, "#ff3d7f"); l.addColorStop(1, "#7b5cff"); return l; }
  function drawLane(c, now, shift, dim) {
    var g = c.getContext("2d"), w = c.width, h = c.height, mid = h / 2;
    g.clearRect(0, 0, w, h);
    // beat grid
    g.fillStyle = "rgba(255,255,255,.06)";
    var beat = STEP * 4, t0 = now - WINDOW;
    for (var bt = Math.ceil(t0 / beat) * beat; bt < now; bt += beat) { var gx = (bt - t0) / WINDOW * w; g.fillRect(gx, 0, 1 * DPR, h); }
    g.fillStyle = dim ? "rgba(244,239,231,.55)" : grad(g, w);
    var bw = 3 * DPR, gap = 2 * DPR;
    for (var x = 0; x < w; x += bw + gap) {
      var t = t0 + (x / w) * WINDOW - shift;
      var a = amp(t) * (0.75 + 0.25 * Math.sin(t * 97.3) * Math.sin(t * 31.1));
      var bh = Math.max(1.5 * DPR, Math.min(1, a) * (h * 0.44));
      g.fillRect(x, mid - bh, bw, bh * 2);
    }
    // playhead
    g.fillStyle = "#f4efe7"; g.fillRect(w - 2 * DPR, 0, 2 * DPR, h);
  }
  var gapNum = document.getElementById("gapNum");

  /* ---------------- hero ribbons ---------------- */
  var hero = document.getElementById("heroWave"), freq = null;
  function drawHero(now) {
    if (!hero) return;
    var g = hero.getContext("2d"), w = hero.width, h = hero.height;
    g.clearRect(0, 0, w, h);
    var level = 0;
    if (playing) {
      level = Math.min(1, amp(now) * 0.8);
    }
    var lines = 22, gr = grad(g, w);
    g.strokeStyle = gr; g.lineWidth = 1.2 * DPR;
    for (var L = 0; L < lines; L++) {
      var p = L / (lines - 1);
      g.globalAlpha = 0.08 + 0.5 * Math.pow(1 - Math.abs(p - 0.5) * 2, 2);
      g.beginPath();
      for (var x = 0; x <= w; x += 8 * DPR) {
        var u = x / w;
        var y = h * 0.62
          + Math.sin(u * 6.0 + now * 0.6 + p * 2.2) * h * (0.08 + level * 0.12)
          + Math.sin(u * 13.0 - now * 1.1 + p * 5.0) * h * (0.025 + level * 0.08)
          + (p - 0.5) * h * 0.22 * Math.sin(u * 3.1 + now * 0.3);
        x ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.stroke();
    }
    g.globalAlpha = 1;
  }

  function resize() { lanes.forEach(fit); if (hero) fit(hero); }
  window.addEventListener("resize", resize); resize();

  var t0 = performance.now(), last = t0;
  function frame(ms) {
    var dt = Math.min(0.05, (ms - last) / 1000); last = ms;
    var now = (ms - t0) / 1000;
    // when audio runs, lock lanes to the audio clock
    if (playing) {
      now = audioTime();
      // keep the two loops locked together
      if (Math.abs(echoEl.currentTime - dryEl.currentTime) > 0.06) echoEl.currentTime = dryEl.currentTime;
    }
    var target = synced ? LAG : 0;
    pcShift += (target - pcShift) * Math.min(1, dt * 3.2);
    if (lanes[0]) drawLane(lanes[0], now, pcShift, false);
    if (lanes[1]) drawLane(lanes[1], now, LAG, true);
    var gapMs = Math.round(Math.abs(LAG - pcShift) * 1000);
    if (gapNum && gapMs !== gapShown) { gapShown = gapMs; gapNum.textContent = gapMs.toLocaleString("en-US"); }
    drawHero(now);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  /* ---------------- scroll-lit statement ---------------- */
  var st = document.getElementById("statement");
  if (st) {
    // wrap each word of plain text nodes; keep the italic <em> as one unit
    var nodes = Array.prototype.slice.call(st.childNodes), words = [];
    nodes.forEach(function (n) {
      if (n.nodeType === 3) {
        var frag = document.createDocumentFragment();
        n.nodeValue.split(/(\s+)/).forEach(function (part) {
          if (!part) return;
          if (/^\s+$/.test(part)) { frag.appendChild(document.createTextNode(part)); return; }
          var s = document.createElement("span"); s.className = "w"; s.textContent = part; frag.appendChild(s); words.push(s);
        });
        st.replaceChild(frag, n);
      } else { n.classList.add("w"); words.push(n); }
    });
    var lit = function () {
      var r = st.getBoundingClientRect(), vh = innerHeight;
      var p = (vh * 0.85 - r.top) / (r.height + vh * 0.35);
      var k = reduce ? words.length : Math.round(Math.max(0, Math.min(1, p)) * words.length);
      words.forEach(function (w, i) { w.classList.toggle("on", i < k); });
    };
    addEventListener("scroll", lit, { passive: true }); lit();
  }

  /* ---------------- reveals (only for things below the first screen) ---------------- */
  if ("IntersectionObserver" in window && !reduce) {
    var io = new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.remove("pre"); io.unobserve(e.target); } }); }, { rootMargin: "0px 0px -8% 0px" });
    Array.prototype.forEach.call(document.querySelectorAll(".reveal"), function (el, i) {
      if (el.getBoundingClientRect().top > innerHeight) { el.classList.add("pre"); el.style.transitionDelay = (i % 4) * 70 + "ms"; io.observe(el); }
    });
  }
})();
