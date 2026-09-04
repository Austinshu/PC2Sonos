# PC2Sonos

Free, local software that replaces the flaky "Sonos desktop app + separate
streaming tool" combo with one thing that just runs at startup:

- Discovers every Sonos speaker on your network automatically, and can
  still find them with one manual step if they're on a separate
  subnet/IoT VLAN that normal discovery can't reach.
- Streams your PC's audio to whichever ones you enable, with individual
  volume control per speaker.
- Choose what gets streamed: the whole system (default), or just one
  application's audio, so the rest of your PC's sound stays off Sonos.
- Delays your PC's *own* local speakers to match Sonos's playback delay,
  so the two don't echo each other -- you set the offset once (or let
  auto-calibration find it) and it stays put, with a periodic background
  resync so it doesn't quietly drift over a long-running session.
- A volume boost for a quiet aux/line-out PC speaker, independent of
  Windows' own volume (which only controls what gets captured, not what
  that device plays back).
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

### Optional: stream just one app instead of the whole system

By default PC2Sonos streams everything your PC plays, the same way the
underlying virtual-cable approach always has. If you'd rather only send
one application's audio to Sonos -- music from a browser tab while
notification sounds and everything else stay off Sonos, say -- open the
**Audio source** card on the dashboard, click **Refresh** to list apps
that have made sound recently, and pick one.

This uses a Windows feature (process-loopback capture) that needs Windows
10 21H2 or newer, works best on Windows 11, and can't capture every
app regardless of Windows version -- copy-protected playback and some
elevated processes aren't capturable this way no matter what. If picking
an app doesn't produce sound, switch back to "Whole system" for that app.
Switching sources briefly reconnects Sonos and the local speaker path,
since the two capture modes run at different audio sample rates.

### Optional: boost a quiet local speaker

Windows' own volume control only affects what PC2Sonos *captures* -- it
has no effect on what PC2Sonos plays back afterward through your real PC
speakers/headphones. If that device sounds too quiet even at 100%
Windows volume (common with a passive speaker on a line-level aux input),
use the **PC speaker volume boost** slider next to the device picker.
100% is the original, unchanged passthrough; above that amplifies the
signal, so very loud peaks can start to clip/distort at high settings --
the same tradeoff as any volume boost pushed past a source's natural
level. This only affects the local speaker path; Sonos speakers keep
their own independent volume control.

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

PC2Sonos's own audio-capture, streaming, and delay-calibration code (this
repository) was written from scratch and does not use or derive from any
third-party project's source code, such as the (GPL-licensed) "Stream What
You Hear" (SWYH) project -- only the general, non-copyrightable idea that
inspired the category ("capture PC audio, send it to a network speaker")
is shared with tools like SWYH.

## Files

- `main.py` -- entrypoint, wires everything together
- `audio_engine.py` -- WASAPI capture from the virtual cable (or, in
  per-app mode, `per_app_audio.py`), delayed + volume-boosted render to
  your real speakers, fan-out to Sonos streams
- `per_app_audio.py` -- per-application capture via Windows' process-
  loopback WASAPI extension, for the "stream just one app" option
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
