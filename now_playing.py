"""Windows-only: reads the system's "Now Playing" info (the same source
Windows itself uses for the taskbar/lock-screen media controls) via the
public GlobalSystemMediaTransportControlsSessionManager WinRT API, so the
dashboard can show what's actually playing without touching the Sonos
connection at all -- unlike pushing metadata into the Sonos stream itself
(which would need a reconnect, and an audible blip, every time a track
changes), this is a pure read of state Windows already tracks, no
different from asking "what does the taskbar show right now."

Not available on macOS -- there's no equivalent public API there (only
the private, undocumented MediaRemote framework), so get_now_playing()
always returns None on that platform rather than reach for something
that could break silently on a future OS update.
"""

import asyncio
import sys


def get_now_playing():
    """{"title": str, "artist": str, "playing": bool} for whatever
    Windows currently considers the active media session, or None if
    there isn't one / this isn't Windows / anything about the query
    failed. Deliberately broad exception handling -- this is a "nice to
    have" dashboard display, never something anything else depends on,
    so any failure here should be invisible rather than surfaced."""
    if sys.platform != "win32":
        return None
    try:
        return asyncio.run(_get_now_playing_async())
    except Exception:
        return None


async def _get_now_playing_async():
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )

    manager = await MediaManager.request_async()
    session = manager.get_current_session()
    if session is None:
        return None
    info = await session.try_get_media_properties_async()
    title = (info.title or "").strip()
    if not title:
        return None
    playback_info = session.get_playback_info()
    playing = (playback_info is not None
               and playback_info.playback_status == PlaybackStatus.PLAYING)
    return {"title": title, "artist": (info.artist or "").strip(), "playing": playing}
