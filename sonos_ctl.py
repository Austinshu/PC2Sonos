"""Sonos discovery and control via SoCo."""

import threading
import time
from xml.sax.saxutils import escape

from soco.discovery import discover

from config import config, save_config


def _track_metadata(title):
    """DIDL-Lite metadata for our stream, built explicitly instead of
    relying on SoCo's play_uri(title=...) shortcut.

    That shortcut tags anything it builds as upnp:class
    'object.item.audioItem.audioBroadcast' with a TuneIn service id
    (SA_RINCON65031_) baked in -- i.e. it tells Sonos "this is a TuneIn
    radio station", not real audio from a device on your LAN. That's
    very likely why the Sonos app locks out Bass/Sub EQ while PC Audio
    plays: Sonos restricts tone controls for some broadcast-tagged
    sources. Tagging this as a plain track (musicTrack) with no
    third-party service id instead should make Sonos treat it like
    normal audio, and let EQ controls work just like they do for other
    sources."""
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="1" parentID="0" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        "</item></DIDL-Lite>"
    )


def _reltime_to_seconds(s):
    try:
        h, m, sec = s.split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec)
    except Exception:
        return None


def measure_transport_delay(zone, base_url, uid, settle_seconds=8.0, poll_interval=0.1):
    """Silent alternative to the microphone/test-tone calibration: measures
    how far behind wall-clock time Sonos's OWN reported playback position
    (RelTime, from GetPositionInfo) is, right after telling it to
    (re)start our stream. No test tone, no microphone, no quiet room
    needed -- just the UPnP transport clock the speaker already reports.

    Restarting the stream gives a precisely-known zero point (the instant
    play_uri() returns, on our clock) to measure from. RelTime only has
    1-second resolution, so a single reading has up to ~1s of quantization
    error; polling through several tick-overs and taking the median of
    (wall_clock_at_tick - RelTime_at_tick) across all of them cancels most
    of that out.

    Caveat this doesn't capture: this measures when Sonos's transport
    clock starts counting, which is presumably close to but not
    necessarily bit-for-bit identical to the exact instant sound becomes
    audible (there could be additional internal decode/buffer-priming
    time Sonos doesn't expose over UPnP). Treat the result as a strong,
    repeatable starting point rather than a guaranteed-perfect one.

    Returns delay_ms (int), or None if fewer than 3 ticks were observed
    (e.g. this speaker/content doesn't report RelTime, or the connection
    dropped) -- the caller should treat that as "couldn't measure this
    speaker" rather than trust a near-empty sample."""
    meta = _track_metadata("PC Audio")
    url = f"{base_url}/stream/{uid}.wav"
    t0 = time.monotonic()
    try:
        zone.play_uri(url, meta=meta)
    except Exception:
        return None

    last = None
    samples = []
    deadline = time.monotonic() + settle_seconds
    while time.monotonic() < deadline:
        now = time.monotonic()
        try:
            info = zone.avTransport.GetPositionInfo([("InstanceID", 0)])
            rt = _reltime_to_seconds(info.get("RelTime", ""))
        except Exception:
            rt = None
        if rt is not None and rt != last:
            samples.append(now - rt - t0)
            last = rt
        time.sleep(poll_interval)

    if len(samples) < 3:
        return None
    samples.sort()
    mid = len(samples) // 2
    median = samples[mid] if len(samples) % 2 else (samples[mid - 1] + samples[mid]) / 2
    return int(round(median * 1000))


def _effective_zone(zone):
    """The zone that actually accepts transport commands (play_uri, stop)
    for whatever group `zone` currently belongs to.

    Sonos requires those commands to be sent to a group's COORDINATOR --
    SoCo raises SoCoSlaveException if you call zone.play_uri()/zone.stop()
    directly on a different member of a group someone made in the Sonos
    app. Before this, that's exactly what happened: every non-coordinator
    member's checkbox silently did nothing (the exception was caught and
    logged, nothing else). The member was often still audibly playing our
    audio anyway, because grouped speakers all replicate the coordinator's
    stream automatically -- so the actual "off switch" for a whole grouped
    set of speakers was hidden behind whichever one happened to be the
    coordinator, indistinguishable in the dashboard from any other row.
    That's the likely explanation for someone enabling/disabling what
    looked like one speaker and getting (or being unable to stop) audio
    across an entire Sonos group.

    Routing every command through the coordinator instead means toggling
    ANY member of a group in the dashboard now actually starts/stops
    playback for that whole group, matching what the Sonos app itself
    shows as one unit."""
    try:
        group = zone.group
        if group is not None and group.coordinator is not None:
            return group.coordinator
    except Exception:
        pass
    return zone


class SpeakerManager:
    def __init__(self):
        self.speakers = {}   # uid -> soco.SoCo
        self.streams = {}    # uid -> bool
        self._lock = threading.Lock()
        # uid -> monotonic time of our last automatic restart, so the
        # watchdog can't get into a rapid-fire restart fight with a user
        # who is deliberately stopping playback on the speaker itself
        self._last_auto_restart = {}
        # speakers we've done the boot-time force-start on (or that got
        # a manual dashboard start, which counts)
        self._boot_started = set()
        # speakers we've already logged a query failure for (log noise cap)
        self._warned = set()

    def rediscover(self):
        try:
            found = discover(timeout=5) or set()
        except Exception as e:
            print(f"[sonos] discovery error: {e}")
            return
        # everything below used to run unguarded -- a single flaky zone
        # (a bad .volume read, a save_config hiccup) would raise straight
        # out of rediscover() and kill the discovery thread for the rest
        # of the run, with no more speakers ever found again and no
        # obvious reason why. Guard it so that can't happen.
        try:
            with self._lock:
                for zone in found:
                    uid = zone.uid
                    if uid not in self.speakers:
                        self.speakers[uid] = zone
                        if uid not in config["speakers"]:
                            # deliberately NOT reading the speaker's current
                            # real volume here -- this is only the starting
                            # position of OUR dashboard slider, not a change
                            # to the physical speaker (nothing is sent to
                            # the zone until someone actually touches the
                            # slider). 50% is a sane, unsurprising starting
                            # point for every newly-discovered speaker.
                            config["speakers"][uid] = {"enabled": True, "volume": 50}
                        print(f"[sonos] found: {zone.player_name}")
            save_config(config)
        except Exception as e:
            print(f"[sonos] discovery bookkeeping error: {e}")

    def list(self):
        with self._lock:
            out = []
            for uid, zone in self.speakers.items():
                cfg = config["speakers"].get(uid, {"enabled": True, "volume": 50})
                # surfaced so the dashboard can show "grouped with X, Y" --
                # toggling any one of them actually affects the whole group
                # (see _effective_zone), so the UI shouldn't imply otherwise
                group_members = []
                try:
                    grp = zone.group
                    if grp is not None and len(grp.members) > 1:
                        group_members = sorted(
                            m.player_name for m in grp.members if m.player_name != zone.player_name)
                except Exception:
                    pass
                out.append({
                    "uid": uid,
                    "name": zone.player_name,
                    "enabled": cfg.get("enabled", True),
                    "volume": cfg.get("volume", 50),
                    "streaming": self.streams.get(uid, False),
                    "grouped_with": group_members,
                })
            return sorted(out, key=lambda s: s["name"])

    def set_enabled(self, uid, enabled, base_url):
        zone = self.speakers.get(uid)
        if not zone:
            return
        config["speakers"].setdefault(uid, {})["enabled"] = enabled
        save_config(config)
        if enabled:
            self.start_stream(uid, zone, base_url)
        else:
            self.stop_stream(uid, zone)

    def set_volume(self, uid, volume):
        zone = self.speakers.get(uid)
        if not zone:
            return
        volume = max(0, min(100, int(volume)))
        config["speakers"].setdefault(uid, {})["volume"] = volume
        save_config(config)
        try:
            zone.volume = volume
        except Exception as e:
            print(f"[sonos] volume set failed for {uid}: {e}")

    def start_stream(self, uid, zone, base_url):
        self._boot_started.add(uid)
        self._last_auto_restart[uid] = time.monotonic()
        try:
            url = f"{base_url}/stream/{uid}.wav"
            _effective_zone(zone).play_uri(url, meta=_track_metadata("PC Audio"))
            self.streams[uid] = True
            print(f"[sonos] streaming to {zone.player_name}")
        except Exception as e:
            print(f"[sonos] failed to start stream on {zone.player_name}: {e}")

    def stop_stream(self, uid, zone):
        try:
            _effective_zone(zone).stop()
        except Exception:
            pass
        self.streams[uid] = False

    def start_all_enabled(self, base_url):
        with self._lock:
            items = list(self.speakers.items())
        for uid, zone in items:
            if config["speakers"].get(uid, {}).get("enabled", True):
                self.start_stream(uid, zone, base_url)

    def measure_delay_ms(self, base_url):
        """Silent (no test tone) calibration: measures each enabled
        speaker's own transport-reported startup latency via
        measure_transport_delay(). Returns {uid: delay_ms} for whichever
        speakers gave a usable reading -- callers take the max across
        enabled speakers as the delay to apply, since matching PC audio
        to the SLOWEST enabled speaker keeps every one of them close to
        in sync rather than just one."""
        with self._lock:
            items = [(uid, zone) for uid, zone in self.speakers.items()
                     if config["speakers"].get(uid, {}).get("enabled", True)]
        results = {}
        for uid, zone in items:
            ms = measure_transport_delay(zone, base_url, uid)
            if ms is not None:
                results[uid] = ms
        return results

    def watchdog_tick(self, base_url):
        """Self-healing: called every few seconds from main.

        The old behavior only ever sent the stream to a speaker at app
        startup or on a dashboard toggle -- so any time Sonos dropped the
        connection (wifi blip, PC sleep, user briefly switching sources,
        Sonos deciding a silent stream is over), playback silently died
        until a human noticed and re-toggled. This checks each enabled
        speaker's actual transport state and:

          * our stream loaded but STOPPED  -> restart it (at most once
            per 45s per speaker, so a user deliberately hitting stop on
            the speaker doesn't have to fight us more than once a
            minute -- turning the speaker off in the dashboard stops
            the restarts entirely)
          * a different source loaded      -> leave it alone (the user
            chose it); just reflect "idle" in the dashboard
          * our stream PLAYING             -> reflect "streaming"
        """
        with self._lock:
            items = list(self.speakers.items())
        for uid, zone in items:
            if not config["speakers"].get(uid, {}).get("enabled", True):
                continue
            try:
                # query the COORDINATOR's transport, not this zone's own --
                # a grouped, non-coordinator member mirrors the group's
                # playback but reports its own CurrentURI as a pointer back
                # to the coordinator (x-rincon:...), not our stream URL, so
                # checking against this zone directly would never see "ours"
                target = _effective_zone(zone)
                uri = target.avTransport.GetMediaInfo([("InstanceID", 0)]).get("CurrentURI") or ""
                ours = f"/stream/{uid}.wav" in uri
                state = target.get_current_transport_info().get("current_transport_state", "")
            except Exception as e:
                # speaker unreachable / query failed; try again next tick
                if uid not in self._warned:
                    self._warned.add(uid)
                    print(f"[sonos] watchdog: can't query {getattr(zone, 'player_name', uid)}: {e}")
                continue

            if uid not in self._boot_started:
                # first time we've seen this speaker since the app
                # launched (covers Windows startup): claim it for PC
                # audio -- unless it's actively PLAYING something the
                # user chose, in which case leave that alone
                self._boot_started.add(uid)
                if ours or state != "PLAYING":
                    print(f"[sonos] boot-start: sending stream to {zone.player_name}")
                    self.start_stream(uid, zone, base_url)
                    continue

            if ours and state == "PLAYING":
                self.streams[uid] = True
            elif ours:
                self.streams[uid] = False
                now = time.monotonic()
                if now - self._last_auto_restart.get(uid, 0) >= 45:
                    self._last_auto_restart[uid] = now
                    print(f"[sonos] watchdog: {zone.player_name} stopped "
                          f"(state={state}); restarting stream")
                    self.start_stream(uid, zone, base_url)
            else:
                # user is playing something else on this speaker
                self.streams[uid] = False


speaker_mgr = SpeakerManager()
