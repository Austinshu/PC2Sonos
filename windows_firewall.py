"""
Windows Firewall allow-rules for PC2Sonos.

Sonos speakers are separate physical devices on the LAN -- they connect
IN to this PC's HTTP server (the audio stream + dashboard). If Windows
Firewall is blocking inbound connections, Sonos can never reach us, and
there is no error anywhere on this machine to say why: the dashboard
looks fine, the app looks fine, Sonos just silently never plays. This
is a confirmed real failure mode (it happened on this project's own
first test machine whenever the network profile was "Public"), and a
fresh machine's network profile is outside our control.

We add two rules, both profile=any so it doesn't matter what Windows
calls the current network (Public/Private/Domain):
  1. a program rule -- covers the exe however Windows decides to filter
  2. a TCP port rule -- covers it even if the exe gets moved/reinstalled

Both are idempotent (skipped if already present) and fail soft: if we
aren't elevated or netsh isn't happy, we return a status string instead
of raising, and the caller just logs it.
"""

import subprocess

RULE_NAME_PROGRAM = "PC2Sonos (app)"
RULE_NAME_PORT = "PC2Sonos (port)"


def _run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def _rule_exists(name):
    try:
        r = _run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"])
        return r.returncode == 0 and "No rules match" not in (r.stdout or "")
    except Exception:
        return False


def ensure_firewall_rules(exe_path, port=5757):
    """Adds the program+port allow rules if missing. Safe to call on
    every startup -- cheap no-op once the rules exist. Returns a
    human-readable status line for the log."""
    results = []
    try:
        if not _rule_exists(RULE_NAME_PROGRAM):
            r = _run([
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={RULE_NAME_PROGRAM}", "dir=in", "action=allow",
                f"program={exe_path}", "enable=yes", "profile=any",
            ])
            results.append("program rule added" if r.returncode == 0
                            else f"program rule failed: {(r.stderr or r.stdout or '').strip()[:150]}")
        else:
            results.append("program rule already present")

        if not _rule_exists(RULE_NAME_PORT):
            r = _run([
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={RULE_NAME_PORT}", "dir=in", "action=allow",
                "protocol=TCP", f"localport={port}", "enable=yes", "profile=any",
            ])
            results.append("port rule added" if r.returncode == 0
                            else f"port rule failed: {(r.stderr or r.stdout or '').strip()[:150]}")
        else:
            results.append("port rule already present")
        return "; ".join(results)
    except Exception as e:
        return f"firewall rule setup failed: {type(e).__name__}: {e}"
