import os
import webbrowser

import pystray
from PIL import Image, ImageDraw

# Set once the tray icon is live, so other threads (main.announce_startup)
# can pop a startup balloon without threading an icon reference around.
_icon = None


def make_image():
    img = Image.new("RGB", (64, 64), "black")
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=(29, 185, 84))
    return img


def notify(message, title="PC2Sonos"):
    """Best-effort tray balloon. No-op if the tray isn't up or the
    platform backend doesn't support notifications."""
    icon = _icon
    if icon is None:
        return
    try:
        icon.notify(message, title)
    except Exception:
        pass


def run_tray(config):
    port = config.get("http_port", 5757)

    def open_dashboard(icon, item):
        webbrowser.open(f"http://127.0.0.1:{port}")

    def export_diagnostics(icon, item):
        # the one thing a non-technical person on some machine we've
        # never seen can actually hand back to us when something's
        # wrong -- see diagnostics.py. Never sent anywhere by itself;
        # it's up to the person to share the file if they want help.
        # Ask first, plainly, before touching anything -- same consent
        # gate the dashboard's "Export Diagnostics" button shows.
        try:
            import ctypes
            from diagnostics import SUPPORT_EMAIL
            MB_OKCANCEL = 0x1
            IDOK = 1
            confirm = (
                "This saves a diagnostics file (system info, the app log, "
                "and your settings -- no personal files, no browsing "
                f"history) to your Desktop, then opens an email pre-"
                f"addressed to {SUPPORT_EMAIL} so you can attach it and "
                "send it yourself if you want help.\n\n"
                "Nothing leaves your computer unless you choose to hit "
                "send.\n\nClick OK to continue, or Cancel to back out."
            )
            if ctypes.windll.user32.MessageBoxW(
                    0, confirm, "Export Diagnostics?", MB_OKCANCEL) != IDOK:
                return
        except Exception:
            pass  # no ctypes (e.g. non-Windows test run) -- just proceed

        try:
            from diagnostics import export_diagnostics_zip, open_diagnostics_email, SUPPORT_EMAIL
            path = export_diagnostics_zip()
            open_diagnostics_email(path)
            msg = (f"Diagnostics saved to your Desktop:\n{path.name}\n\n"
                   f"An email to {SUPPORT_EMAIL} should have opened -- "
                   f"just attach that file and hit send.")
        except Exception as e:
            msg = f"Couldn't create the diagnostics file: {e}"
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "PC2Sonos Diagnostics", 0x40)
        except Exception:
            print(f"[diagnostics] {msg}")

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", open_dashboard, default=True),
        pystray.MenuItem("Export Diagnostics...", export_diagnostics),
        pystray.MenuItem("Quit PC2Sonos", quit_app),
    )
    global _icon
    icon = pystray.Icon("pc2sonos", make_image(), "PC2Sonos", menu)
    _icon = icon
    icon.run()
