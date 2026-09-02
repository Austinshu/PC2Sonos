"""
PC2Sonos-Setup.exe -- the one-download installer.

Built with PyInstaller --onefile --uac-admin, with these payloads
bundled inside it (see build_installer.ps1):
    payload/PC2Sonos.exe          the app itself (self-contained)
    payload/VBCABLE/              VB-Audio Virtual Cable driver pack
                                  (unmodified, (c) VB-Audio / Vincent
                                  Burel, donationware -- vb-audio.com)

What one double-click does on a fresh x64 PC:
    1. asks where to install (defaults to Program Files\\PC2Sonos -- a
       normal, non-cloud-synced location, same as any other Windows app)
       and installs PC2Sonos.exe (+ PC2Sonos-Uninstall.exe) there
    2. installs the VB-CABLE virtual audio driver if it's missing
       (silent; falls back to VB-Audio's visible installer window)
    3. sets "CABLE Input" as the Windows default output device
    4. registers PC2Sonos to start at every login + Desktop shortcut
    5. registers PC2Sonos in Windows' "Apps & Features" so it can be
       removed the normal way, not via manual instructions
    6. starts it now and opens the dashboard

No Python, no pip, no manual audio settings on the target machine.
"""

import ctypes
import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

APP_NAME = "PC2Sonos"
DASHBOARD = "http://127.0.0.1:5757"
UNINSTALL_KEY = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"

# The small, frequently-rewritten config.json/log -- kept out of wherever
# the app binaries end up (Program Files by default, or wherever the user
# picks) because that location may not be writable by the app once it's
# running unelevated as a normal user. ProgramData always is, and -- the
# whole reason this exists as a separate constant -- it's never touched by
# OneDrive's Known Folder Move the way Documents/Desktop/Pictures can be.
DATA_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / APP_NAME

# Every location the app (binaries) and its data have lived in across
# versions, oldest first, so an upgrade from any of them can be found and
# cleaned up/migrated instead of left behind as dead weight.
_OLD_DIRS = [
    Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / APP_NAME,
    Path(os.environ.get("LOCALAPPDATA", "")) / APP_NAME,
]

# Well-known SID for the builtin "Users" group -- locale-independent
# (unlike the string "Users", which is only correct on English Windows).
_USERS_SID = "*S-1-5-32-545"


def say(msg):
    print(msg, flush=True)


def payload_dir():
    # PyInstaller onefile unpacks bundled data next to the module
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "payload"


def default_install_dir():
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    return Path(program_files) / APP_NAME


def prompt_install_dir():
    """Asks where to put the app binaries, defaulting to Program Files.
    Falls back to the default silently if a folder picker can't be shown
    (no display, tkinter unavailable) or the user cancels -- this should
    never be the thing that blocks an otherwise-working install."""
    default = default_install_dir()
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            title="Choose where to install PC2Sonos (Cancel for the default)",
            initialdir=str(default.parent),
            mustexist=False,
        )
        root.destroy()
        if chosen:
            # a folder picker returns the SELECTED folder -- put our own
            # named subfolder inside it, same as picking "C:\Program
            # Files" would otherwise dump loose files straight into it
            chosen_path = Path(chosen)
            return chosen_path if chosen_path.name == APP_NAME else chosen_path / APP_NAME
    except Exception as e:
        say(f"  (couldn't show a folder picker: {e}; using the default)")
    return default


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


def setup_data_dir():
    """Creates %ProgramData%\\PC2Sonos and grants standard users write
    access to it -- ProgramData is normally writable by any user for
    files/folders they create themselves, but that first mkdir happens
    here, while we're elevated, so explicitly granting access removes any
    dependence on the machine's particular default ACLs. Without this,
    the app could install fine and then fail to save its own config the
    first time someone drags the delay slider, as a standard (non-admin)
    user."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["icacls", str(DATA_DIR), "/grant", f"{_USERS_SID}:(OI)(CI)M"],
            capture_output=True, timeout=15)
    except Exception as e:
        say(f"  (couldn't set permissions on {DATA_DIR}: {e} -- should still work for you)")


def migrate_and_cleanup_old_dirs():
    """Every build before this one kept the app binaries AND config.json
    together in one folder -- first %LOCALAPPDATA%\\PC2Sonos, then (very
    briefly) Documents\\PC2Sonos, which turned out to still be inside
    OneDrive on any PC with Documents redirected there. Carries the
    config forward into the new, separate DATA_DIR before wiping each old
    folder -- config.py's own migration only runs once PC2Sonos.exe
    itself starts, which happens after this, so doing it here too means
    an upgrade never has a moment where the old settings exist nowhere."""
    new_config = DATA_DIR / "config.json"
    for old_dir in _OLD_DIRS:
        if not old_dir.exists():
            continue
        try:
            old_config = old_dir / "config.json"
            if old_config.exists() and not new_config.exists():
                shutil.copy2(old_config, new_config)
                say("  carried your existing settings over from the old install location")
            shutil.rmtree(old_dir, ignore_errors=True)
            say(f"  removed old install left over at {old_dir}")
        except Exception as e:
            say(f"  couldn't clean up old install at {old_dir}: {e}")


def install_app_files(dst_dir):
    src = payload_dir() / f"{APP_NAME}.exe"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{APP_NAME}.exe"
    stop_running_app()
    time.sleep(1.0)
    shutil.copy2(src, dst)
    say(f"  installed {dst}")

    setup_data_dir()
    migrate_and_cleanup_old_dirs()

    # the uninstaller ships as its own small exe (built the same way as
    # this installer) so it can be run standalone later, long after this
    # setup exe's own temp extraction has been cleaned up -- copy it in
    # persistently rather than relying on anything from this payload dir
    uninstall_src = payload_dir() / f"{APP_NAME}-Uninstall.exe"
    uninstall_dst = dst_dir / f"{APP_NAME}-Uninstall.exe"
    if uninstall_src.exists():
        shutil.copy2(uninstall_src, uninstall_dst)
        say(f"  installed {uninstall_dst}")
    else:
        uninstall_dst = None
        say("  !! PC2Sonos-Uninstall.exe missing from payload -- Apps & Features entry skipped")

    return dst, uninstall_dst


def register_uninstaller(exe_path, uninstall_exe_path):
    """Adds PC2Sonos to Windows' "Apps & Features" list with a working
    Uninstall button, instead of leaving removal as something someone
    has to be handed manual instructions for."""
    if not uninstall_exe_path:
        return
    try:
        with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, "1.2.0")
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{uninstall_exe_path}"')
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(exe_path))
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(exe_path.parent))
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, 90000)
        say("  registered in Settings > Apps (uninstall the normal way, anytime)")
    except Exception as e:
        say(f"  couldn't register Apps & Features entry: {e}")


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

    install_dir = prompt_install_dir()

    say("[1/7] Installing app files...")
    say(f"  installing to {install_dir}")
    exe, uninstall_exe = install_app_files(install_dir)

    say("[2/7] Virtual audio cable...")
    ok = install_cable_driver()

    say("[3/7] Audio routing...")
    if ok:
        set_default_output()
    else:
        say("  skipped (driver not installed)")

    say("[4/7] Firewall...")
    open_firewall(exe)

    say("[5/7] Autostart...")
    make_shortcuts(exe)

    say("[6/7] Registering with Windows...")
    register_uninstaller(exe, uninstall_exe)

    say("[7/7] Starting PC2Sonos...")
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
