# PC2Sonos

Free, local software that replaces the flaky "Sonos desktop app + separate
streaming tool" combo with one thing that just runs at startup:

- Discovers every Sonos speaker on your network automatically.
- Streams your PC's system audio to whichever ones you enable, with
  individual volume control per speaker.
- Delays your PC's *own* local speakers to match Sonos's playback delay,
  so the two don't echo each other -- you set the offset once with a
  slider and it stays put.
- Starts automatically when you log into Windows. No app to remember to
  open.

Everything runs on your machine. There's no cloud dependency, no
telemetry, no account, and no license check -- it's a local web dashboard
(default `http://127.0.0.1:5757`) plus a background audio/streaming
engine, fully offline from the moment it's installed.

## Why the delay slider exists

Sonos speakers buffer incoming audio by roughly a second or two so that
multiple speakers in your system can stay perfectly in sync with each
other. That floor is inside Sonos's own firmware and no PC software (this
one included) can remove it. What *this* software fixes is the side
effect: without it, your PC's local speakers play instantly while Sonos
lags behind, so you hear the same audio twice. PC2Sonos holds your local
speaker output back by a matching amount so both play together -- same
idea as the "audio delay" / lip-sync offset setting on an AV receiver.

## One-time setup

1. **Install VB-Audio Virtual Cable** (free): https://vb-audio.com/Cable/
   This creates a virtual audio device Windows apps can output to, which
   PC2Sonos then reads from -- it's what makes delaying your local
   speakers possible at all (Windows won't let software "un-play" audio
   that's already reached a real speaker).
2. Set your Windows default **playback** device to **"CABLE Input (VB-Audio
   Virtual Cable)"** (Settings > System > Sound > Output).
3. Run `install.ps1` from this folder (right-click > Run with PowerShell,
   or open PowerShell here and run `./install.ps1`). It installs the
   Python dependencies, creates a Windows Startup shortcut, and launches
   the app.
4. Open the dashboard: http://127.0.0.1:5757
   - Enable the Sonos speakers you want.
   - Play something on your PC.
   - Drag the "local sync delay" slider up from 0 until your PC speakers
     and Sonos land together with no echo (typically somewhere around
     1000-2000ms, but it depends on your network and speakers -- dial it
     in by ear).

After that, it's fully automatic: log into Windows, PC2Sonos starts
quietly in the system tray, and every Sonos speaker you enabled starts
receiving audio.

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
- `audio_engine.py` -- WASAPI capture from the virtual cable, delayed
  render to your real speakers, fan-out to Sonos streams
- `sonos_ctl.py` -- Sonos discovery/control via SoCo
- `webapp.py` -- Flask dashboard + the WAV endpoints Sonos speakers pull
  audio from
- `tray_icon.py` -- system tray icon (Open Dashboard / Quit)
- `install.ps1` -- one-time setup script
