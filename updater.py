"""Update check -- see main.py, which calls check_for_update_async() exactly
once, right at process startup. That single request is the ONLY thing
PC2Sonos itself ever sends over the internet (everything else is either LAN
traffic to a Sonos speaker, or a link the user clicks open in their own
browser). The result is cached in memory for the rest of the run; webapp.py's
/api/update_status route only ever reads that cache, so reloading the
dashboard never triggers another GitHub request."""

import threading

import requests

from version import VERSION

_GITHUB_API_URL = "https://api.github.com/repos/Austinshu/PC2Sonos/releases/latest"
_ASSET_NAME = "PC2Sonos-Setup.exe"

_status = {
    "checked": False,
    "update_available": False,
    "current_version": VERSION,
    "latest_version": None,
    "download_url": None,
}
_lock = threading.Lock()


def _parse_version(v):
    """'v1.2.10' -> (1, 2, 10), so 1.2.10 correctly compares as newer than
    1.2.9 (a plain string compare would get that backwards)."""
    v = (v or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def get_status():
    """Local-only, synchronous, no network -- safe to call from a Flask
    request handler on every dashboard load."""
    with _lock:
        return dict(_status)


def _check(timeout):
    result = {
        "checked": True,
        "update_available": False,
        "current_version": VERSION,
        "latest_version": None,
        "download_url": None,
    }
    try:
        resp = requests.get(
            _GITHUB_API_URL, timeout=timeout,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"PC2Sonos-UpdateCheck/{VERSION}"})
        resp.raise_for_status()
        data = resp.json()
        latest = str(data.get("tag_name") or "")
        result["latest_version"] = latest
        download_url = None
        for asset in data.get("assets") or []:
            if asset.get("name") == _ASSET_NAME:
                download_url = asset.get("browser_download_url")
                break
        # fall back to the release page itself if the asset ever gets
        # renamed -- a page to click "download" from beats no link at all
        result["download_url"] = download_url or data.get("html_url")
        if _parse_version(latest) > _parse_version(VERSION):
            result["update_available"] = True
    except Exception as e:
        # offline, DNS failure, GitHub rate-limited us, whatever -- this is
        # a best-effort background check and never something that should
        # interrupt startup or show up as an error to the user
        print(f"[update] check skipped: {e}")
    with _lock:
        _status.update(result)


def check_for_update_async(timeout=5):
    threading.Thread(target=_check, args=(timeout,), daemon=True).start()
