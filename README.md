# PC2Sonos

<img alt="Windows" title="Windows 10 or newer" src="https://img.shields.io/badge/Windows-10%2B-0078D6?logo=windows&logoColor=white">
<img alt="macOS" title="macOS 12.0 (Monterey) or newer" src="https://img.shields.io/badge/macOS-12.0%2B-000000?logo=apple&logoColor=white">

PC2Sonos plays your computer's audio on your Sonos speakers, in sync with
your computer's own speakers. It's free, runs entirely on your machine, and
starts by itself when you log in.

Sonos speakers always play a second or two behind whatever sends them
audio. That's built into Sonos so a group of speakers stays in step, and no
software can remove it. So if your PC speakers and your Sonos play together,
you hear everything twice. PC2Sonos fixes that by holding your PC speakers
back by the same amount, the way the lip-sync setting on an AV receiver
does. You set the delay once, or press **Auto** and it's measured for you,
and it stays in sync from then on.

## What it does

- Finds every Sonos speaker on your network by itself, and streams to
  whichever ones you turn on, each with its own volume.
- Plays a delayed copy to your PC's own speakers or headphones (more than
  one at once if you like) so they line up with Sonos.
- Streams the whole system, just the apps you pick, or for a while an
  audio file or a device plugged into the PC.
- Has a dashboard in your browser (`http://127.0.0.1:5757`) showing what's
  playing where, your sync delay and a live level meter.
- Includes a sleep timer, a scale-everything volume control, a PC speaker
  EQ and boost, and a low-bandwidth mode for a Sonos on weak Wi-Fi.
- Never uses your microphone on Windows (see below).
- Needs no account and sends no telemetry. The only thing it sends anywhere
  is one check per launch for a newer version.

## Install

**Windows 10 or newer:** download `PC2Sonos-Setup.exe` from the
[latest release](https://github.com/Austinshu/PC2Sonos/releases/latest)
and run it. It installs the free VB-Audio Virtual Cable, sets it as your
default playback device, adds the firewall rules Sonos needs, and starts
PC2Sonos. Uninstall it from Settings > Apps like anything else.

**macOS 12 or newer:** download the `.dmg` for your Mac (Apple Silicon or
Intel) from the same page and drag PC2Sonos to Applications. The app isn't
notarized, so the first launch shows "Apple could not verify PC2Sonos is
free of malware": click Done, then System Settings > Privacy & Security >
**Open Anyway** (on macOS 14 and older, right-click the app > Open). It
then offers to install the free BlackHole audio driver and asks for
Microphone access. See [macOS](#macos) for why.

Then open the dashboard, turn on the Sonos speakers you want, play
something, and press **Auto** next to the sync delay. (Or drag the slider
by ear until the echo disappears.) That's the whole setup. From then on
PC2Sonos starts quietly at login, in the system tray or menu bar.

What's new in each version is on the
[Releases](https://github.com/Austinshu/PC2Sonos/releases) page.

## Using it

**PC speaker volume.** On Windows, the Volume slider in the PC speaker
output card is your speakers' own Windows volume, the same setting as in
Settings > System > Sound. It's on the dashboard because Windows' volume
keys can't reach your speakers while PC2Sonos runs; they control the
virtual cable instead. On macOS it's PC2Sonos's own volume for the Mac's
speakers. Sonos speakers have their own volume sliders.

**Boost and EQ (Advanced).** If a speaker is still too quiet at full
volume, as a passive speaker on an aux input can be, there's a boost of up
to 500%, plus a bass/mid/treble EQ of up to &plusmn;24dB. Both affect only
the PC speakers and soft-limit instead of clipping. The defaults (100%
boost, 0dB EQ) are what we recommend: pushing either far enough can damage
small speakers or amps over time. That's why nothing else on the dashboard,
including the scale-everything slider, can change the boost.

**Scale everything together.** The slider at the top of the Sonos card
turns every enabled Sonos speaker and your PC speakers up or down together.
Drag it back to 100% and everything returns to exactly where it was. Above
100% it only raises Sonos, never your PC speakers.

**Stream only some apps (Windows).** Under **Audio source**, click
**Refresh** and tick the apps you want on Sonos; everything else stays off
it. This needs Windows 10 21H2 or newer, and copy-protected playback can't
be captured this way. If a ticked app is silent, use "Whole system".

**Sleep timer.** Pick 15 to 90 minutes and press Start. Every streaming
Sonos speaker turns off when it runs out. Your PC speakers aren't affected.

**Reduced bandwidth.** If a Sonos speaker on weak Wi-Fi keeps cutting out,
turn on **Reduced bandwidth** in the Sonos streaming quality card. It halves
the sample rate sent to Sonos. Your PC speakers stay at full quality.

**Faster start after a reboot.** Click the **&#9733;** next to the speaker
you use most to make it the default. PC2Sonos then connects to it straight
away at login instead of waiting 15 to 20 seconds for a network scan. Give
that speaker a fixed address (a DHCP reservation) in your router. In
`config.json`, `"discovery_mode": "on_demand"` stops the repeating
background scan, and `"new_speakers_default_enabled": false` keeps newly
found speakers off until you turn them on.

**Speakers on another subnet or IoT VLAN.** If your Sonos speakers don't
show up because they're on a separate network, type one speaker's IP
address under "Speakers not showing up?" on the dashboard. PC2Sonos
reaches it directly and finds the rest from it.

**Password.** The dashboard has no password by default. To add one, put it
on the first line of a file called `dashboard_password.txt` in the
PC2Sonos data folder (`%ProgramData%\PC2Sonos` on Windows,
`~/Library/Application Support/PC2Sonos` on macOS). This keeps housemates
and guests out, but it isn't strong security: it's sent over plain HTTP,
and the audio stream itself stays open because Sonos can't log in.

## Does PC2Sonos use your microphone?

**On Windows, no.** PC2Sonos reads your audio by *loopback* from the
virtual cable's playback side, which Windows doesn't count as microphone
access. So it isn't listed under Privacy > Microphone and the mic indicator
stays off. The cable only ever carries your PC's own audio, never your room
or your voice. Auto calibration doesn't use a microphone either; it reads
Sonos's own playback clock. The one exception is if loopback can't be used
(the cable is missing its loopback device or is set to surround) or you
chose **Recording device** under Advanced > Capture method. Then PC2Sonos
reads the cable's recording side, which Windows labels a microphone, and
the dashboard tells you so.

**On macOS, the indicator stays on**, because macOS treats reading from
BlackHole as microphone access and has no loopback equivalent. It still
only carries your Mac's own audio.

## macOS

PC2Sonos on macOS works the same way, with
[BlackHole 2ch](https://existential.audio/blackhole/) (free, open source)
in place of VB-Audio Virtual Cable. It makes BlackHole the default output
while it runs and puts your previous output back when you quit from the
menu bar. It starts at login through Login Items. Picking individual apps
to stream isn't available on macOS yet.

To install from source instead of the `.dmg`:

```bash
git clone https://github.com/Austinshu/PC2Sonos.git && cd PC2Sonos
./install.sh
```

`install.sh` installs BlackHole through Homebrew if needed, sets up a
virtualenv, adds a firewall rule if the firewall is on, runs the app once
so macOS shows the Microphone prompt, and installs a LaunchAgent.
`./uninstall.sh` reverses it.

## Running from source on Windows

For working on PC2Sonos itself:

1. Install [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) and set
   **CABLE Input (VB-Audio Virtual Cable)** as your default playback
   device.
2. Run `./install.ps1` in PowerShell from this folder. It needs Python 3.10
   or newer, installs the dependencies, builds `PC2Sonos.exe`, adds a
   Startup shortcut and launches it.

`build_installer.ps1` builds `PC2Sonos-Setup.exe`, the one-download
installer that bundles VB-Audio Virtual Cable and the uninstaller.
`test_logic.py` and `test_macos.py` run without audio hardware or a Sonos.

## Support

PC2Sonos is free, with nothing held back behind a payment. If it's useful
to you, the dashboard has an optional, pay-what-you-want "Support this
project" link, and a small reminder pops up at most once a week (never
again if you say you've donated).

## Credits and third-party software

PC2Sonos bundles, unmodified, **VB-CABLE**, a virtual audio driver made and
owned by **VB-Audio Software / Vincent Burel** (https://vb-audio.com/Cable/).
It isn't open source; it's donationware, distributed here under VB-Audio's
terms for bundling it with free or commercial applications, which ask that
you can see it's VB-Audio's product and know you're free to donate for it.
If PC2Sonos is useful to you, please consider donating to VB-Audio too.

On macOS, PC2Sonos uses **BlackHole** by Existential Audio Inc.
(https://existential.audio/blackhole/, GPL-3.0). It's a separate driver
PC2Sonos talks to through CoreAudio, not bundled or linked. The macOS
support was contributed by **Michael Shapiro**
([Shap-Code](https://github.com/Shap-Code)).

PC2Sonos's own capture, streaming and calibration code was written from
scratch and doesn't use or derive from other projects' source code, such as
the GPL-licensed "Stream What You Hear".

## Files

- `main.py`: entry point that wires everything together
- `audio_engine.py`: captures the virtual cable, plays the delayed copy to
  your PC speakers, and feeds the Sonos streams
- `audio_backend.py`: one audio interface over pyaudiowpatch (Windows) and
  sounddevice (macOS)
- `per_app_audio.py`: per-app capture on Windows
- `file_playback.py`: the "play a file or an external device" source
- `sonos_ctl.py`: Sonos discovery and control, plus the watchdog that
  restarts dropped streams and resyncs long-running ones
- `calibration.py`: automatic sync-delay measurement from Sonos's playback
  clock
- `webapp.py`: the dashboard and the audio streams Sonos pulls from
- `config.py`: settings (`C:\ProgramData\PC2Sonos\config.json` on Windows)
- `windows_audio.py` / `windows_firewall.py`: default output device,
  speaker volume and firewall rules on Windows
- `macos_audio.py` / `macos_firewall.py` / `macos_app.py`: the macOS
  counterparts, plus the Microphone permission and Login Items
- `updater.py` / `version.py`: the once-per-launch update check
- `diagnostics.py`: crash logging and the dashboard's Export Diagnostics
- `tray_icon.py`: the system tray icon
- `setup_installer.py` / `uninstall.py` / `build_installer.ps1` /
  `install.ps1`: the Windows installer, uninstaller and dev setup
- `install.sh` / `uninstall.sh` / `build_macos_app.sh`: macOS install and
  app build
- `test_logic.py` / `test_macos.py`: tests that run without audio hardware
