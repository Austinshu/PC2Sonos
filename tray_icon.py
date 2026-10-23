import os
import sys
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


def run_tray(config, on_quit=None):
    """Blocks until the tray icon is closed. On macOS this must be called
    on the main thread (AppKit requirement); main.py arranges that. on_quit,
    if given, runs before the process exits (used on macOS to restore the
    default output device)."""
    port = config.get("http_port", 5757)

    def open_dashboard(icon, item):
        webbrowser.open(f"http://127.0.0.1:{port}")

    def export_diagnostics(icon, item):
        # the dashboard's report form (describe the problem, optional reply
        # email, explicit Send) is the one consent gate and the one place
        # that sends -- see diagnostics.py. ?diag=1 opens it straight away.
        webbrowser.open(f"http://127.0.0.1:{port}/?diag=1")

    def quit_app(icon, item):
        if on_quit is not None:
            try:
                on_quit()
            except Exception as e:
                print(f"[tray] quit hook failed: {e}")
        icon.stop()
        os._exit(0)

    items = [
        pystray.MenuItem("Open Dashboard", open_dashboard, default=True),
        pystray.MenuItem("Export Diagnostics...", export_diagnostics),
    ]
    if sys.platform == "darwin":
        # The .app has no Startup-folder shortcut; a Login Items entry is
        # the macOS equivalent (see macos_app.py). Only meaningful from a
        # bundle -- from source, install.sh's LaunchAgent does this job.
        try:
            from macos_app import is_bundled, login_item_enabled, set_login_item
            if is_bundled():
                def toggle_login(icon, item):
                    print(f"[login] {set_login_item(not item.checked)}")

                items.append(pystray.MenuItem("Start at Login", toggle_login,
                                              checked=lambda item: login_item_enabled()))
        except Exception as e:
            print(f"[tray] login item toggle unavailable: {e}")
    items.append(pystray.MenuItem("Quit PC2Sonos", quit_app))

    global _icon
    icon = pystray.Icon("pc2sonos", make_image(), "PC2Sonos", pystray.Menu(*items))
    _icon = icon
    icon.run()
