"""
PC2Sonos-Setup.exe -- the one-download installer.

Built with PyInstaller --onefile --uac-admin, with these payloads
bundled inside it (see build_installer.ps1):
    payload/PC2Sonos.exe          the app itself (self-contained)
    payload/VBCABLE/              VB-Audio Virtual Cable driver pack
                                  (unmodified, (c) VB-Audio / Vincent
                                  Burel, donationware -- vb-audio.com)

What one double-click does on a fresh x64 PC:
    1. installs PC2Sonos.exe to  %LOCALAPPDATA%\\PC2Sonos\\app
    2. installs the VB-CABLE virtual audio driver if it's missing
       (silent; falls back to VB-Audio's visible installer window)
    3. sets "CABLE Input" as the Windows default output device
    4. registers PC2Sonos to start at every login + Desktop shortcut
    5. starts it now and opens the dashboard

No Python, no pip, no manual audio settings on the target machine.
"""

import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = "PC2Sonos"
DASHBOARD = "http://127.0.0.1:5757"


def say(msg):
    print(msg, flush=True)


def payload_dir():
    # PyInstaller onefile unpacks bundled data next to the module
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "payload"


def install_dir():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME / "app"


def cable_present():
    try:
        from windows_audio import list_playback_devices
        return any("cable input" in (n or "").lower()
                   for _, n in list_playback_devices())
    except Exception:
        return False


def stop_running_app():
    subprocess.run(
        ["taskkill", "/f", "/im", f"{APP_NAME}.exe"],
        capture_output=True)


def install_app_files():
    src = payload_dir() / f"{APP_NAME}.exe"
    dst_dir = install_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{APP_NAME}.exe"
    stop_running_app()
    time.sleep(1.0)
    shutil.copy2(src, dst)
    say(f"  installed {dst}")
    return dst


def install_cable_driver():
    if cable_present():
        say("  VB-CABLE already installed -- skipping driver step")
        return True
    setup = payload_dir() / "VBCABLE" / "VBCABLE_Setup_x64.exe"
    if not setup.exists():
        say("  !! VBCABLE_Setup_x64.exe missing from payload")
        return False
    # VB-Audio's redistribution terms require the end user to be able to
    # see and identify VB-CABLE as VB-Audio's own product (not something
    # of ours) and know they're free to donate for it -- so we say that
    # plainly here even though the driver install itself runs silently.
    say("  installing VB-CABLE (virtual audio driver by VB-Audio Software /")
    say("  Vincent Burel -- free donationware, https://vb-audio.com/Cable/)...")
    # -i install, -h hidden: the community-standard unattended flags.
    # We're already elevated (the setup exe requests admin via manifest).
    try:
        subprocess.run([str(setup), "-i", "-h"], cwd=str(setup.parent),
                       timeout=180)
    except Exception as e:
        say(f"  silent driver install failed to run: {e}")
    # driver enumeration can lag; poll for the endpoint to appear
    for _ in range(30):
        if cable_present():
            say("  VB-CABLE installed")
            return True
        time.sleep(1)
    # fall back to VB-Audio's own visible installer so a human can
    # click "Install Driver" -- never leave the machine half-done
    say("  silent install didn't take; opening VB-Audio's installer --")
    say("  click 'Install Driver' in the window that appears.")
    try:
        subprocess.run([str(setup)], cwd=str(setup.parent), timeout=600)
    except Exception as e:
        say(f"  couldn't run visible installer either: {e}")
        return False
    for _ in range(15):
        if cable_present():
            say("  VB-CABLE installed")
            return True
        time.sleep(1)
    return cable_present()


def set_default_output():
    try:
        from windows_audio import ensure_cable_is_default
        say(f"  {ensure_cable_is_default()}")
    except Exception as e:
        say(f"  couldn't set default output automatically ({e}).")
        say("  You can do it once by hand: Settings > System > Sound >")
        say("  Output > 'CABLE Input (VB-Audio Virtual Cable)'.")


def open_firewall(exe_path):
    try:
        from windows_firewall import ensure_firewall_rules
        say(f"  {ensure_firewall_rules(str(exe_path), port=5757)}")
    except Exception as e:
        say(f"  couldn't add firewall rule automatically ({e}).")
        say("  If Sonos can't fetch the stream, allow PC2Sonos.exe through")
        say("  Windows Firewall by hand (Private and Public networks).")


def make_shortcuts(exe_path):
    ps = f'''
$W = New-Object -ComObject WScript.Shell
foreach ($where in @(
    [Environment]::GetFolderPath('Startup'),
    [Environment]::GetFolderPath('Desktop'))) {{
  $s = $W.CreateShortcut((Join-Path $where "{APP_NAME}.lnk"))
  $s.TargetPath = "{exe_path}"
  $s.WorkingDirectory = "{exe_path.parent}"
  $s.Description = "PC audio to Sonos, in sync"
  $s.Save()
}}
'''
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True)
    if r.returncode == 0:
        say("  start-at-login + Desktop shortcuts created")
    else:
        say(f"  shortcut creation problem: {r.stderr.strip()[:200]}")


def launch_unelevated(exe_path):
    # We're running elevated; launching the app via explorer.exe makes
    # Windows start it as the normal desktop user instead (so the tray
    # icon and audio session land in the right place).
    try:
        subprocess.Popen(["explorer.exe", str(exe_path)])
    except Exception:
        subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent))
    say("  PC2Sonos started")


def main():
    say("")
    say("==============================================")
    say(f"   {APP_NAME} Setup")
    say("   PC audio on your Sonos speakers, in sync.")
    say("==============================================")
    say("")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        say("Please run this installer as Administrator (it needs to")
        say("install the virtual audio driver once).")
        input("Press Enter to exit...")
        return 1

    say("[1/6] Installing app files...")
    exe = install_app_files()

    say("[2/6] Virtual audio cable...")
    ok = install_cable_driver()

    say("[3/6] Audio routing...")
    if ok:
        set_default_output()
    else:
        say("  skipped (driver not installed)")

    say("[4/6] Firewall...")
    open_firewall(exe)

    say("[5/6] Autostart...")
    make_shortcuts(exe)

    say("[6/6] Starting PC2Sonos...")
    launch_unelevated(exe)

    say("")
    say("Done! Your dashboard: " + DASHBOARD)
    say("Sonos speakers on your network are found automatically;")
    say("use the dashboard to tune the sync delay to your room.")
    try:
        os.startfile(DASHBOARD)  # noqa
    except Exception:
        pass
    say("")
    input("Press Enter to close this window...")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        input("Setup hit an error (details above). Press Enter to close...")
        sys.exit(1)
