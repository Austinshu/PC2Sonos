"""macOS application-firewall helper.

Sonos speakers connect IN to this Mac's HTTP server. macOS ships with
the application firewall off, in which case nothing here matters. When
it is on, an unsigned interpreter gets a "Do you want the application
to accept incoming network connections?" prompt the first time it
listens -- fine when a human is watching, invisible when launched at
login by launchd. install.sh adds an allow rule with sudo; this module
only reports state so diagnostics can show it. Never raises."""

import subprocess
import sys

SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def _run(args, timeout=10):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def firewall_state():
    """'off', 'on', or a short reason it couldn't be determined."""
    if sys.platform != "darwin":
        return "n/a (not macOS)"
    try:
        out = _run([SOCKETFILTERFW, "--getglobalstate"]).lower()
        if "disabled" in out or "off" in out:
            return "off"
        if "enabled" in out or "on" in out:
            return "on"
        return f"unknown ({out.strip()[:80]})"
    except Exception as e:
        return f"couldn't check ({type(e).__name__}: {e})"


def app_allowed(exe_path):
    """Whether exe_path already has an allow rule. Only meaningful when
    the firewall is on."""
    if sys.platform != "darwin":
        return "n/a (not macOS)"
    try:
        out = _run([SOCKETFILTERFW, "--listapps"])
        if exe_path in out:
            return "allowed" if "allow incoming" in out.lower() else "listed"
        return "not listed"
    except Exception as e:
        return f"couldn't check ({type(e).__name__}: {e})"
