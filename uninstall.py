"""
PC2Sonos-Uninstall.exe -- removes everything the installer added.

Built the same way as setup_installer.py (PyInstaller --onefile
--uac-admin) and placed at %LOCALAPPDATA%\\PC2Sonos\\app alongside
PC2Sonos.exe itself, then registered in Windows' "Apps & Features" so
uninstalling is a normal Settings > Apps > Uninstall click, not a set
of manual instructions someone has to be handed.

Every step below is independent and skips cleanly if its target was
never there -- safe to run even on a partial/broken install.

Removes:
  1. Any running PC2Sonos.exe
  2. Windows default playback device, switched back to a real
     (non-virtual) device *before* the virtual cable disappears out
     from under it -- otherwise Windows can be left with no usable
     default output
  3. The VB-CABLE virtual audio driver (via VB-Audio's own installed
     uninstaller -- see VBCABLE_UNINSTALL_HINT below)
  4. The two PC2Sonos Windows Firewall rules
  5. The Startup + Desktop shortcuts
  6. The leftover HKCU\\Software\\PC2Sonos registry key, if present
     (used by an earlier trial system; harmless but no longer used)
  7. %LOCALAPPDATA%\\PC2Sonos entirely (app files, config, logs)
  8. The Windows "Apps & Features" entry for PC2Sonos itself

Does NOT touch: your Sonos speakers (there's nothing on them to
undo), or any other app that happens to also use VB-CABLE for
something unrelated to PC2Sonos.
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
UNINSTALL_KEY = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"

# Virtual/software output devices that are never "the real speakers to
# fall back to" -- same blocklist audio_engine.py uses for auto-pick,
# duplicated here (rather than imported) so this uninstaller doesn't
# need pyaudiowpatch as a dependency at all.
_VIRTUAL_DEVICE_BLOCKLIST = [
    "cable", "vb-audio", "steam streaming", "voicemeeter", "virtual",
    "voicemod", "nvidia broadcast", "wave link", "loopback",
]


def say(msg):
    print(msg, flush=True)


def install_dir():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME


def stop_running_app():
    subprocess.run(["taskkill", "/f", "/im", f"{APP_NAME}.exe"], capture_output=True)
    time.sleep(0.5)


def restore_real_default_output():
    """Switch Windows default playback off the virtual cable and onto a
    real device *before* uninstalling the driver -- if we uninstall
    first, Windows can be left pointing at a default device that no
    longer exists, which is a worse state than just leaving CABLE Input
    selected would have been."""
    try:
        from pycaw.utils import AudioUtilities
        from windows_audio import _policy_config

        devices = []
        for d in AudioUtilities.GetAllDevices():
            try:
                devices.append((d.id, d.FriendlyName or ""))
            except Exception:
                continue

        real = next(
            (did for did, name in devices
             if not any(bad in name.lower() for bad in _VIRTUAL_DEVICE_BLOCKLIST)),
            None,
        )
        if not real:
            say("  no real output device found to switch to -- leaving as-is")
            return
        pc = _policy_config()
        for role in (0, 1, 2):
            pc.SetDefaultEndpoint(real, role)
        say("  switched default playback device off the virtual cable")
    except Exception as e:
        say(f"  couldn't switch default output automatically ({e}) --")
        say("  you may need to pick a real device in Settings > Sound > Output.")


def uninstall_cable_driver():
    """VB-CABLE keeps its own persistent uninstaller (installed to its
    own Program Files location, independent of anything PC2Sonos does)
    and registers it in HKLM's Uninstall key -- we look that up rather
    than assuming a fixed path, since VB-Audio could change it."""
    setup_path = None
    for hive_path in (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hive_path) as root:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(root, subkey_name) as sub:
                            name, _ = winreg.QueryValueEx(sub, "DisplayName")
                            if "cable" in name.lower() and "vb" in name.lower():
                                path, _ = winreg.QueryValueEx(sub, "UninstallString")
                                setup_path = path.strip('"')
                                break
                    except OSError:
                        continue
        except OSError:
            continue
        if setup_path:
            break

    if not setup_path or not Path(setup_path).exists():
        say("  VB-CABLE's own uninstaller wasn't found -- it may already be")
        say("  removed, or you'll need to remove it yourself via Settings > Apps.")
        return

    say(f"  found VB-CABLE's uninstaller at {setup_path}")
    try:
        # -u uninstall, -h hidden: the same flag convention this project's
        # own installer already uses for -i (install), just the other
        # direction. Falls back to the visible installer window (where a
        # human can click "Uninstall Driver") if the silent path doesn't
        # take, same defensive pattern as setup_installer.py's install step.
        subprocess.run([setup_path, "-u", "-h"], cwd=str(Path(setup_path).parent), timeout=180)
        say("  VB-CABLE driver uninstalled")
    except Exception as e:
        say(f"  silent uninstall failed to run ({e}); opening VB-Audio's")
        say("  installer -- click 'Uninstall Driver' in the window that appears.")
        try:
            subprocess.run([setup_path], cwd=str(Path(setup_path).parent), timeout=600)
        except Exception as e2:
            say(f"  couldn't run it either: {e2}")


def remove_firewall_rules():
    for name in ("PC2Sonos (app)", "PC2Sonos (port)"):
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"],
            capture_output=True, text=True)
        if r.returncode == 0:
            say(f"  removed firewall rule: {name}")


def remove_shortcuts():
    for where in (
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup",
        Path(os.path.expanduser("~/Desktop")),
    ):
        shortcut = where / f"{APP_NAME}.lnk"
        if shortcut.exists():
            try:
                shortcut.unlink()
                say(f"  removed {shortcut}")
            except Exception as e:
                say(f"  couldn't remove {shortcut}: {e}")


def remove_registry_leftovers():
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\PC2Sonos")
        say("  removed leftover HKCU\\Software\\PC2Sonos registry key")
    except FileNotFoundError:
        pass
    except Exception as e:
        say(f"  couldn't remove registry leftovers: {e}")


def remove_app_files():
    d = install_dir()
    if not d.exists():
        return
    # can't delete the running exe's own directory while it's the
    # current process's cwd/module path -- schedule it via a detached
    # cmd that waits a moment after we exit, so the exe deletes itself
    # cleanly instead of failing with "file in use"
    try:
        subprocess.Popen(
            ["cmd", "/c", "timeout /t 2 >nul & rmdir /s /q " + f'"{d}"'],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        say(f"  scheduled removal of {d}")
    except Exception as e:
        say(f"  couldn't schedule folder removal ({e}) -- delete")
        say(f"  {d} by hand once this window closes.")


def remove_uninstall_registration():
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY)
    except FileNotFoundError:
        pass
    except Exception as e:
        say(f"  couldn't remove Apps & Features entry: {e}")


def main():
    say("")
    say("==============================================")
    say(f"   {APP_NAME} Uninstall")
    say("==============================================")
    say("")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        say("Please run this uninstaller as Administrator (it needs to")
        say("remove the virtual audio driver and firewall rules cleanly).")
        input("Press Enter to exit...")
        return 1

    say("[1/7] Stopping PC2Sonos...")
    stop_running_app()

    say("[2/7] Restoring default playback device...")
    restore_real_default_output()

    say("[3/7] Removing VB-CABLE virtual audio driver...")
    uninstall_cable_driver()

    say("[4/7] Removing firewall rules...")
    remove_firewall_rules()

    say("[5/7] Removing shortcuts...")
    remove_shortcuts()

    say("[6/7] Cleaning up registry...")
    remove_registry_leftovers()
    remove_uninstall_registration()

    say("[7/7] Removing app files...")
    remove_app_files()

    say("")
    say("Done. PC2Sonos has been removed.")
    say("(Your Sonos speakers themselves need nothing undone.)")
    input("Press Enter to close this window...")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        input("Uninstall hit an error (details above). Press Enter to close...")
        sys.exit(1)
