# PC2Sonos

<img alt="Windows" title="Windows 10 or newer" src="https://img.shields.io/badge/Windows-10%2B-0078D6?logo=windows&logoColor=white">
<img alt="macOS" title="macOS 12.0 (Monterey) or newer" src="https://img.shields.io/badge/macOS-12.0%2B-000000?logo=apple&logoColor=white">

Free, local software that replaces the flaky "Sonos desktop app + separate
streaming tool" combo with one thing that just runs at startup:

- Discovers every Sonos speaker on your network automatically, and can
  still find them with one manual step if they're on a separate
  subnet/IoT VLAN that normal discovery can't reach.
- Streams your PC's audio to whichever ones you enable, with individual
  volume control per speaker.
- Doesn't touch your microphone: on Windows it reads what's playing by
  loopback rather than opening a recording device, so the microphone
  indicator stays off (see "Does PC2Sonos use your microphone?" below).
- Choose what gets streamed: the whole system (default), or just one
  application's audio, so the rest of your PC's sound stays off Sonos.
- Delays your PC's *own* local speakers to match Sonos's playback delay,
  so the two don't echo each other -- you set the offset once (or let
  auto-calibration find it) and it stays put, with a periodic background
  resync so it doesn't quietly drift over a long-running session.
- A volume slider for your PC speakers, right next to the output picker on
  the dashboard, that also goes past 100% to boost a quiet aux/line-out
  speaker.
- A dashboard that shows what's actually happening at a glance --
  how many speakers are streaming, your current sync delay, which PC
  output device is active, and a live input level meter -- instead of
  just a settings form you have to read to know anything's working.
- A one-click sleep timer: pick a duration and every streaming Sonos
  speaker turns itself off when it runs out, without touching your PC
  speakers or needing you to remember.
- A reduced-bandwidth streaming option for a Sonos speaker on a weak or
  distant Wi-Fi link, independent of your PC speakers' quality.
- Checks once per launch whether a newer version exists and shows a
  download link if so -- see "Why the update checker exists" below.
- Starts automatically when you log into Windows. No app to remember to
  open.

Everything runs on your machine. There's no cloud dependency, no
telemetry, no account, and no license check -- it's a local web dashboard
(default `http://127.0.0.1:5757`) plus a background audio/streaming
engine, fully offline apart from that one-time-per-launch update check.

## Why the delay slider exists

Sonos speakers buffer incoming audio by roughly a second or two so that
multiple speakers in your system can stay perfectly in sync with each
other. That floor is inside Sonos's own firmware and no PC software (this
one included) can remove it. What *this* software fixes is the side
effect: without it, your PC's local speakers play instantly while Sonos
lags behind, so you hear the same audio twice. PC2Sonos holds your local
speaker output back by a matching amount so both play together -- same
idea as the "audio delay" / lip-sync offset setting on an AV receiver.

## Does PC2Sonos use your microphone? (Windows)

No. PC2Sonos gets your PC's audio by *loopback* capture of VB-Audio
Virtual Cable's playback side ("CABLE Input" -- the device Windows sends
your sound to), which Windows does not treat as microphone access. So
PC2Sonos isn't listed under Settings > Privacy & security > Microphone as
an app that's using it, and the microphone-in-use indicator stays off.
It also never captures your room or your voice: the only thing the cable
ever carries is your PC's own audio on its way to Sonos.

Up to and including v1.4.5, PC2Sonos captured differently: it opened the
cable's *recording* side ("CABLE Output"). That carries the identical
audio, but Windows classes every recording device as a microphone, so it
showed the microphone as in use for as long as PC2Sonos was running -- a
fair thing to find alarming, even though it was never a real microphone.
You can still see that behavior in two cases, and the dashboard says so:

- PC2Sonos falls back to the recording device by itself if loopback can't
  be used (the cable's loopback device is missing or won't open, or the
  cable has been switched to a surround format). Audio keeps working, and
  an orange notice at the top of the dashboard says why.
- You picked **Recording device** under Advanced > Capture method. There's
  no reason to unless loopback ever gives you silence.

The one thing that *does* briefly use a real microphone is the optional
"Calibrate with test tone" button on the dashboard: a manual, ~6-second
recording, off by default (the default Auto calibration measures Sonos's
own playback clock instead and never touches a microphone, real or
virtual, at all). Per-app capture ("Audio source" under Advanced) doesn't
use a microphone either.

macOS is different -- it has no loopback equivalent, so it still has to
read BlackHole as an input; see the macOS section below.

## Why the update checker exists

The early releases of PC2Sonos were, honestly, not good at the one thing
the app exists to do: sync. The delay calibration was rough, the render
path had format/resample bugs that threw timing off, and there was no
mechanism to correct for a long-running session's audio gradually
drifting out of sync with Sonos -- so "close enough" on day one could be
audibly off by the end of the day. A lot of the work since then has gone
directly at that problem: more accurate silent calibration (measured from
Sonos's own playback clock, not guesswork), fixes to the capture/render
pipeline's sample-rate and format handling, and a background watchdog
that now periodically resyncs Sonos automatically so drift never has a
chance to accumulate. The practical result is that most setups can now
land right around **0ms** of extra delay and stay there indefinitely,
instead of needing a compensating offset that only stayed correct for a
few minutes.

None of that helps anyone still running an old install, though. This app
has no telemetry and no account system by design -- which also means
there's no way to reach existing users to tell them "the sync problem you
gave up on is fixed now." A silent, no-account tool that never phones
home is also a tool with no way to say "hey, this got better." The update
checker is the one deliberate exception to "fully offline": once per
launch, it makes a single request to GitHub's public release API (no
data about you or your setup goes with it) to compare your version
against the latest release, and shows a banner with a direct download
link if you're behind. After that one check, it's silent again for the
rest of the session -- no polling, no background checking, nothing else
sent anywhere.

## One-time setup

1. Download `PC2Sonos-Setup.exe` from the
   [latest release](https://github.com/Austinshu/PC2Sonos/releases/latest)
   and run it. It installs VB-Audio Virtual Cable, sets it as your
   default playback device, adds the Windows Firewall rules Sonos needs
   to reach the stream, registers PC2Sonos in Settings > Apps so it can
   be uninstalled the normal way, and starts the app -- one click,
   nothing to configure by hand.
2. Open the dashboard: http://127.0.0.1:5757
   - Enable the Sonos speakers you want.
   - Play something on your PC, then click **Auto** to measure the sync
     delay automatically (or drag the slider by ear until your PC
     speakers and Sonos land together with no echo).

After that, it's fully automatic: log into Windows, PC2Sonos starts
quietly in the system tray, and every Sonos speaker you enabled starts
receiving audio. The dashboard opens in your browser on the very first
run only; after that a small tray notification just confirms it started,
and the dashboard is one click away on the tray icon.

### Optional: stream only specific apps instead of the whole system

By default PC2Sonos streams everything your PC plays, the same way the
underlying virtual-cable approach always has. If you'd rather only send
particular applications' audio to Sonos -- music from a browser tab plus
a game, while notification sounds and everything else stay off Sonos,
say -- open the **Audio source** card on the dashboard, click **Refresh**
to list apps that have made sound recently, and check as many as you
want. They're mixed together into one stream; leave every box unchecked
(or check "Whole system") to go back to sending everything.

This uses a Windows feature (process-loopback capture) that needs Windows
10 21H2 or newer, works best on Windows 11, and can't capture every
app regardless of Windows version -- copy-protected playback and some
elevated processes aren't capturable this way no matter what. If checking
an app doesn't produce sound, uncheck it and use "Whole system" instead.
Each checked app joins or drops out of the mix independently as you open
and close it -- one app not being capturable, or not running yet, doesn't
stop the others from streaming. Switching between "Whole system" and any
app selection briefly reconnects Sonos and the local speaker path only if
the two capture modes end up at different audio sample rates (per-app
capture is always 48 kHz; whole-system capture runs at your virtual
cable's own rate, which is usually 48 kHz too, in which case nothing
interrupts).

### PC speaker volume, and boosting a quiet local speaker

The **Volume** slider in the PC speaker output card (right under the
output picker) sets how loud PC2Sonos plays the delayed audio through
your real PC speakers/headphones, on top of Windows' own volume for that
device. 100% is the original, unchanged level and dragging down simply
turns it down. It only affects the local speaker path; Sonos speakers
keep their own independent volume control.

If that device still sounds too quiet at full Windows volume (common
with a passive speaker on a line-level aux input), go above 100%: the
slider goes up to 500%, using a soft limiter rather than a hard clip so
loud peaks compress gradually as they approach full scale instead of
slamming flat. 100% is also the ceiling of the safe/no-warning zone; the
dashboard shows a warning past it as a reminder that you're past the
source's natural level.

There's also a **bass/mid/treble EQ** under Advanced, local speaker path
only (Sonos speakers keep their own EQ in the Sonos app) -- a low shelf
at 200Hz, a peak at 1000Hz, and a high shelf at 5000Hz, each up to
&plusmn;24dB: a fully adjustable bass shelf well beyond the fixed,
modest boost most built-in Windows/driver audio enhancements offer.
0dB on all three is a true passthrough; a warning appears past
&plusmn;6dB on any band, since a plain 3-band shelf/peak EQ pushed that
far starts sounding like a different speaker rather than "more/less
bass." The EQ soft-limits its own output too (same as the boost), so
even a large boost on one band compresses gracefully instead of
hard-clipping.

**100% volume / 0dB EQ (the defaults) is what we recommend.** Both
controls go well past that on purpose, for cases like an underpowered
aux speaker that genuinely needs it -- but pushing either far enough can
stress or damage underpowered speakers/amps over time, not just change
how the audio sounds. Adjusting past the defaults is at your own risk to
your hardware.

### Optional: scale everything up or down at once

The **master volume** slider on the Sonos speakers card scales every
enabled speaker's volume and the PC boost together, from wherever each
one is currently set. It's relative, not absolute: drag it to 50% and
everything drops by half, PC boost included; drag it back to 100% and
you get back exactly what you started with, not just whatever the last
press happened to leave behind. Above 100% turns your Sonos speakers up
together (up to 100% each, same as always) -- but deliberately **not**
the PC boost, which only ever moves down through this slider. Raising
the boost itself still needs its own dedicated slider below, since that
one carries a hardware-risk warning this control shouldn't be able to
trigger as a side effect of an innocuous "turn everything up" press.
Handy for a quick "turn it all down" (or Sonos up) moment without
losing track of each speaker's individual level -- the per-speaker
sliders below stay fully adjustable the whole time. Once you're back at
100%, that becomes the
new baseline for the next time you use it.

### Optional: sleep timer

Click the **Sleep timer** card, pick a duration (15-90 minutes), and hit
**Start** -- every currently-streaming Sonos speaker turns itself off
when the countdown reaches zero, the same as flipping its toggle off by
hand. Only Sonos is affected; your PC's own speakers, and anything you
turn back on afterward, are untouched. **Cancel** stops the countdown
early. The timer lives only in the running app and doesn't survive a
restart -- it's a "falling asleep to a podcast" tool, not a schedule.

### Optional: reduce Sonos bandwidth on a weak Wi-Fi link

If a Sonos speaker is on a flaky or distant Wi-Fi connection and its
playback keeps cutting in and out, try **Reduced bandwidth** in the
**Sonos streaming quality** card. It halves the sample rate sent to
Sonos (44.1kHz -> 22kHz) -- meaningfully less data for a weak link to
keep up with, at the cost of slightly less crisp highs. This only
changes what's sent to Sonos; your PC's own speakers always stay at full
quality. Switching it forces every currently-streaming speaker to
reconnect at the new rate, so expect one short glitch right after you
change it.

### Optional: start faster after a reboot

Normally PC2Sonos runs a network scan at startup to find your speakers,
which takes 15-20 seconds before audio begins. If you mostly stream to
one speaker, click the **&#9733;** next to it in the dashboard to make it
the *default speaker*: PC2Sonos then talks to that speaker directly at
launch and starts playing in a second or two, and the full scan happens
in the background. Give that speaker a DHCP reservation in your router so
its address doesn't change.

To go further, set `"discovery_mode": "on_demand"` in `config.json`: once
the default speaker is up, the repeating background scan stops entirely
(it runs once as a safety net, then only when you press **Rescan**).

By default a newly-discovered speaker starts out enabled (a fresh install
lights up everything and you turn off what you don't want). Set
`"new_speakers_default_enabled": false` in `config.json` to have new
speakers stay off until you enable them -- handy on a busy network.

### Optional: password-protect the dashboard

By default the dashboard has no password -- PC2Sonos assumes it's running
on a network you control.

To require one, create a file named `dashboard_password.txt` in the
PC2Sonos data folder -- the same folder that holds `config.json` and
`pc2sonos.log` (its full path is printed at the top of `pc2sonos.log`
every time the app starts; on a normal install it's
`%ProgramData%\PC2Sonos`). Put the password on the first line and save.

It takes effect on the next page load -- the browser will ask for a
username (anything works) and the password. Delete the file to remove the
password again.

What this is and isn't: it's a low-effort gate to keep other people on
the same LAN (housemates, guests on the wifi) from opening the dashboard
and toggling your speakers or changing settings. It is **not** strong
security. The password sits in a plain text file, it's sent over plain
HTTP (base64-encoded, not encrypted -- anyone who can capture traffic on
your network can read it), and the audio stream endpoint stays open
because Sonos speakers can't authenticate. Don't reuse a password you
care about, and don't rely on this if the dashboard is somehow reachable
from outside your home network. The file is deliberately kept out of the
diagnostics export so it isn't shared by accident.

## macOS

PC2Sonos runs on macOS too, with the same dashboard and the same design:
a virtual audio device stands in for VB-CABLE, the app captures from it,
plays a delayed copy to the Mac's own speakers and streams to Sonos.

- **Virtual device:** [BlackHole 2ch](https://existential.audio/blackhole/)
  (free, open source) instead of VB-CABLE. PC2Sonos makes it the default
  output while running (via the public CoreAudio HAL API, `macos_audio.py`)
  and restores your previous output when you quit from the menu bar.
- **Audio:** PortAudio/CoreAudio through `sounddevice`, behind the same
  PyAudio-shaped interface the Windows build uses (`audio_backend.py`), so
  `audio_engine.py` and `calibration.py` are shared unchanged.
- **Permissions:** macOS treats reading from BlackHole as *microphone*
  access -- there's no separate "virtual audio input" permission, so the
  Microphone privacy indicator stays lit the entire time PC2Sonos runs,
  the same as any other Mac app that routes system audio through a
  virtual device (Loopback, Audio Hijack, etc). That indicator does
  *not* mean your actual mic/room audio is being captured -- BlackHole
  only ever carries your Mac's own audio. The one thing that does touch
  your real microphone is the optional "Calibrate with test tone" button
  on the dashboard, a manual, ~6-second recording, off by default (the
  default Auto calibration measures Sonos's own playback clock instead
  and never touches the mic at all). The app asks for the permission on
  launch (`macos_app.py`); without it CoreAudio delivers silence with no
  error, so the dashboard shows a banner explaining that -- and keeps
  showing a calmer one afterward, explaining what the indicator means,
  since granted-and-working is not the same as nothing left worth
  saying.
- **Startup:** a LaunchAgent (from `install.sh`) or a Login Items entry
  (from the .app's menu bar item) instead of a Startup-folder shortcut.
- **Not available on macOS:** per-application capture. The "Audio source"
  picker only offers "Whole system". macOS 14.2+ has Core Audio process
  taps that could do this and would also remove the need for BlackHole
  and the microphone permission; that is a possible future direction.

### Install on macOS

Either build/run from source:

```bash
git clone https://github.com/Austinshu/PC2Sonos.git && cd PC2Sonos
./install.sh
```

`install.sh` installs BlackHole via Homebrew if needed, creates a
virtualenv, adds a firewall rule if the application firewall is on, runs
the app once in the foreground so macOS can show the Microphone prompt
(launchd-started processes never get one), and installs the LaunchAgent.
`./uninstall.sh` reverses it.

Or use a prebuilt `PC2Sonos-macOS-<arch>.dmg` from a release (built by
`build_macos_app.sh` / the `macOS` GitHub Actions workflow): drag
PC2Sonos to Applications. The app is not notarized (that needs a paid
Apple Developer account), so the first launch shows "Apple could not
verify PC2Sonos is free of malware": click Done, then System Settings >
Privacy & Security > **Open Anyway** (macOS 15+), or right-click > Open
(macOS 14 and older). The first launch offers to install BlackHole and to
start at login, then asks for Microphone access; click Allow.

Data lives in `~/Library/Application Support/PC2Sonos/` (`config.json`,
`pc2sonos.log`, the optional `dashboard_password.txt`).

## Running from source (for development)

The steps above are what an end user needs -- nothing else. This section
is only for building/modifying PC2Sonos itself, where you don't get the
installer's automation for free:

1. Install VB-Audio Virtual Cable by hand (free): https://vb-audio.com/Cable/
   This creates a virtual audio device Windows apps can output to, which
   PC2Sonos then reads from -- it's what makes delaying your local
   speakers possible at all (Windows won't let software "un-play" audio
   that's already reached a real speaker). `PC2Sonos-Setup.exe` installs
   this for you; running from source, you have to do it yourself.
2. Set your Windows default **playback** device to **"CABLE Input (VB-Audio
   Virtual Cable)"** (Settings > System > Sound > Output).
3. Run `install.ps1` from this folder (right-click > Run with PowerShell,
   or open PowerShell here and run `./install.ps1`). Needs Python 3.10+
   on PATH -- it installs the Python dependencies, builds `PC2Sonos.exe`,
   creates a Windows Startup shortcut, and launches it.

To build the actual one-download installer (`PC2Sonos-Setup.exe`,
bundling VB-CABLE and the uninstaller), see `build_installer.ps1`.

## Support

PC2Sonos is free, with no restrictions and nothing gated behind payment.
If it's useful to you, the dashboard has a "Support this project" link to
an optional, pay-what-you-want donation -- and once a week (never again
if you say you've already donated), a small popup offers the same thing.
It's entirely honor-system; dismissing it forever costs nothing and
changes nothing about how the app works.

## Credits / third-party software

PC2Sonos bundles (unmodified) **VB-CABLE**, a virtual audio driver made and
owned by **VB-Audio Software / Vincent Burel** (https://vb-audio.com/Cable/).
It is *not* open source -- it's donationware, distributed here under
VB-Audio's terms for bundling the single VB-CABLE package with free or
commercial applications, on the condition that end users can plainly see it
as VB-Audio's own product and know they're free to donate for it if they
find it useful. This project does not modify, resell, or claim any
ownership over VB-CABLE; if PC2Sonos is useful to you, please consider
donating to VB-Audio directly at the link above.

On macOS, PC2Sonos uses **BlackHole** by Existential Audio Inc.
(https://existential.audio/blackhole/, GPL-3.0) as the virtual audio
device. It is a separate driver the app talks to through CoreAudio, not
bundled with or linked into PC2Sonos. The macOS support (audio backend,
CoreAudio integration, installer, app bundle) was contributed by
**Michael Shapiro** ([Shap-Code](https://github.com/Shap-Code)).

PC2Sonos's own audio-capture, streaming, and delay-calibration code (this
repository) was written from scratch and does not use or derive from any
third-party project's source code, such as the (GPL-licensed) "Stream What
You Hear" (SWYH) project -- only the general, non-copyrightable idea that
inspired the category ("capture PC audio, send it to a network speaker")
is shared with tools like SWYH.

## Files

- `main.py` -- entrypoint, wires everything together
- `audio_backend.py` -- the per-platform audio device layer behind one
  PyAudio-shaped interface (pyaudiowpatch on Windows, sounddevice on macOS)
- `macos_audio.py` / `macos_firewall.py` / `macos_app.py` -- macOS
  counterparts of the windows_* helpers: default output device, firewall
  status, microphone permission, Login Items, native dialogs
- `install.sh` / `uninstall.sh` / `build_macos_app.sh` -- macOS install and
  .app build (see "macOS" above)
- `test_macos.py` -- Linux-runnable tests for the macOS layer
- `audio_engine.py` -- WASAPI loopback capture of the virtual cable (with
  a fallback to its recording device, and `per_app_audio.py` in per-app
  mode), delayed + volume-boosted render to your real speakers, fan-out to
  Sonos streams
- `per_app_audio.py` -- per-application capture via Windows' process-
  loopback WASAPI extension, for the "stream only specific apps" option
- `sonos_ctl.py` -- Sonos discovery/control via SoCo, including the
  background watchdog that restarts dropped streams and periodically
  resyncs long-running ones to prevent drift
- `calibration.py` -- automatic sync-delay measurement (silent, from
  Sonos's own playback clock, and an optional test-tone + microphone method)
- `updater.py` / `version.py` -- the once-per-launch update check (see
  "Why the update checker exists" above)
- `webapp.py` -- Flask dashboard + the WAV endpoints Sonos speakers pull
  audio from
- `tray_icon.py` -- system tray icon (Open Dashboard / Quit)
- `config.py` -- settings load/save (`C:\ProgramData\PC2Sonos\config.json`
  -- deliberately not Documents or AppData, see the comment at the top of
  the file)
- `diagnostics.py` -- crash logging + the dashboard's "Export Diagnostics" bundle
- `windows_audio.py` / `windows_firewall.py` -- Windows-specific helpers
  (default output device, firewall rules)
- `install.ps1` -- source-based dev setup script (see "Running from source" above)
- `setup_installer.py` / `uninstall.py` -- source for the packaged
  `PC2Sonos-Setup.exe` / `PC2Sonos-Uninstall.exe`
- `build_installer.ps1` -- builds the one-download `PC2Sonos-Setup.exe`
