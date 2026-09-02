"""Sonos discovery and control via SoCo."""

import threading
import time
from xml.sax.saxutils import escape

import soco.config as soco_config
from soco import SoCo
from soco.discovery import discover, scan_network

from config import config, save_config

# SoCo's default is 20s. A speaker that's asleep or briefly off-network
# then stalls every code path that touches it for 20s at a time -- and
# with cross-subnet (unicast) discovery there's no multicast "who's
# alive" to prune the dead ones, so this happens routinely. 4s is plenty
# for a speaker that's actually there on the LAN.
soco_config.REQUEST_TIMEOUT = 4.0


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
        self.names = {}      # uid -> str, cached at discovery so list()
                             # never has to make a (possibly hanging)
                             # network call just to render the dashboard
        self.groups = {}     # uid -> [other member names], same reason:
                             # refreshed from the background threads only
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
        # IPs of speakers we've successfully reached this run, seeded from
        # config but grown as we discover more. Used as extra entry points
        # for the unicast (cross-VLAN) discovery path so that if the one
        # configured seed is down, any other speaker we've seen still gets
        # us back to the whole household.
        self._known_ips = set()
        # set by the dashboard's "Rescan" button (and used to break the
        # discovery loop out of its wait in on_demand mode)
        self.rescan_requested = threading.Event()

    def known_ips(self):
        """A locked snapshot (sorted list) of the speaker IPs reached so
        far this run. `_known_ips` is mutated from the discovery and
        prime paths under `self._lock`; callers here run on other threads
        (get_lan_ip on the watchdog thread, the diagnostics snapshot, the
        next discovery pass), and iterating the set unlocked while it's
        being added to raises 'Set changed size during iteration'."""
        with self._lock:
            return sorted(self._known_ips)

    def _default_enabled_for(self, uid):
        """Whether a newly-seen speaker should start out enabled. The
        configured default speaker always is; everything else follows
        new_speakers_default_enabled (true on a fresh install)."""
        if uid and uid == config.get("default_speaker_uid"):
            return True
        return bool(config.get("new_speakers_default_enabled", True))

    def prime_default_speaker(self):
        """Reach config['default_speaker_ip'] directly and register it,
        so the watchdog can boot-start it without waiting for a discovery
        pass. Returns the uid on success, None otherwise (blank config,
        speaker unreachable, or the IP now answers as a different device).

        Safe to call repeatedly -- it's a no-op once the speaker is
        already known."""
        ip = str(config.get("default_speaker_ip") or "").strip()
        if not ip:
            return None
        # A speaker waking from standby can take longer than the 4s
        # REQUEST_TIMEOUT to answer its first request. Give it a few
        # tries before falling back to normal discovery, otherwise a
        # cold-boot default speaker silently misses the fast-start path.
        zone = uid = None
        for attempt in range(3):
            try:
                zone = SoCo(ip)
                uid = zone.uid
                break
            except Exception as e:
                if attempt == 2:
                    print(f"[sonos] default speaker {ip} not reachable at "
                          f"launch after {attempt + 1} tries: {e}")
                    return None
                time.sleep(2)

        expected = str(config.get("default_speaker_uid") or "").strip()
        if expected and expected != uid:
            print(f"[sonos] {ip} is now {uid}, not the configured default "
                  f"speaker {expected} -- ignoring it, letting discovery take over")
            return None

        name, groups = self._zone_meta(zone)  # network I/O -- keep it off the lock
        with self._lock:
            if not expected:
                config["default_speaker_uid"] = uid
            self._known_ips.add(ip)
            if name:
                self.names[uid] = name
            if groups is not None:
                self.groups[uid] = groups
            self.speakers.setdefault(uid, zone)
            config["speakers"].setdefault(
                uid, {"enabled": True, "volume": 50})["enabled"] = True
        save_config(config)
        print(f"[sonos] default speaker ready at launch: {self.names.get(uid, uid)}")
        return uid

    def _discover_zones(self):
        """The set of Sonos zones to track, from every source that applies
        -- results are UNIONed, not taken from the first that returns
        something (a SoCo set dedupes by uid).

        - SSDP multicast (`discover`): on a flat network this alone is
          complete -- and on a hit `soco.discovery.discover` already
          returns the responder's full `visible_zones`, so a single
          household that meshes across VLANs over SonosNet comes back
          whole here too.
        - Configured `sonos_seed_ips`: always walked when set. A mixed
          setup -- some speakers on the LAN, a separate Sonos system on
          an IoT VLAN reachable only by unicast -- would otherwise never
          find the VLAN speakers once SSDP returns the LAN ones. Reaching
          any one speaker yields its whole household via ZoneGroupTopology.
        - `sonos_scan_cidrs`: a unicast subnet sweep. Last resort (it's
          the expensive one) -- only when nothing else turned anything up.
        - Previously-seen IPs (`known_ips()`): a recovery path, used only
          when SSDP and any configured seeds all came back empty."""
        zones = set()
        try:
            zones |= discover(timeout=5) or set()
        except Exception as e:
            print(f"[sonos] SSDP discovery error: {e}")

        seed_ips = [str(s).strip() for s in (config.get("sonos_seed_ips") or [])
                    if str(s).strip()]
        scan_cidrs = [str(c).strip() for c in (config.get("sonos_scan_cidrs") or [])
                      if str(c).strip()]

        seeds = list(seed_ips)
        if not zones and not seeds:
            seeds = self.known_ips()
        tried = set()
        for ip in seeds:
            if ip in tried:
                continue
            tried.add(ip)
            try:
                device = SoCo(ip)
                # Warm the household's shared ZoneGroupState cache from
                # THIS speaker, which we know we just reached -- otherwise
                # the first .player_name access later might land on a
                # speaker that's currently asleep and stall/return blank.
                _ = device.player_name
                zones |= (device.visible_zones or {device})
            except Exception as e:
                print(f"[sonos] seed speaker {ip} unreachable: {e}")

        if scan_cidrs and not zones:
            for cidr in scan_cidrs:
                try:
                    print(f"[sonos] nothing found yet; unicast-scanning {cidr}")
                    zones |= (scan_network(networks_to_scan=[cidr],
                                           multi_household=True) or set())
                except Exception as e:
                    print(f"[sonos] subnet scan of {cidr} failed: {e}")
        return zones

    def rediscover(self):
        """Run a discovery pass and merge the results. Returns True if it
        completed, False if something went wrong along the way (the
        dashboard's rescan routes surface this)."""
        try:
            found = self._discover_zones()
        except Exception as e:
            print(f"[sonos] discovery error: {e}")
            return False

        # Resolve each zone's name + group membership -- which is network
        # I/O, up to a REQUEST_TIMEOUT per unreachable zone -- BEFORE
        # taking self._lock. list() needs that same lock for every
        # /api/speakers poll (every 4s), so doing this under it would let
        # one asleep speaker stall the whole dashboard.
        resolved = []  # (uid, zone, ip, name, groups)
        for zone in found:
            try:
                uid = zone.uid
            except Exception:
                continue
            try:
                ip = zone.ip_address or ""
            except Exception:
                ip = ""
            name, groups = self._zone_meta(zone)
            resolved.append((uid, zone, ip, name, groups))

        # everything below used to run unguarded -- a single flaky zone
        # (a bad .volume read, a save_config hiccup) would raise straight
        # out of rediscover() and kill the discovery thread for the rest
        # of the run, with no more speakers ever found again and no
        # obvious reason why. Guard it so that can't happen.
        try:
            with self._lock:
                for uid, zone, ip, name, groups in resolved:
                    if ip:
                        self._known_ips.add(ip)
                    if name:
                        self.names[uid] = name
                    if groups is not None:
                        self.groups[uid] = groups
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
                            config["speakers"][uid] = {
                                "enabled": self._default_enabled_for(uid),
                                "volume": 50,
                            }
                        print(f"[sonos] found: {self.names.get(uid, uid)}")
            save_config(config)
        except Exception as e:
            print(f"[sonos] discovery bookkeeping error: {e}")
            return False
        return True

    def _zone_meta(self, zone):
        """(display name, sorted list of visible group-member names) for a
        zone. Makes network calls (player_name / group topology), so this
        must run OUTSIDE self._lock and the caller merges the result in
        afterwards. Returns (None, None) if the zone can't be reached;
        (name, None) if the name resolved but the group didn't."""
        try:
            name = zone.player_name or None
        except Exception:
            return None, None
        try:
            grp = zone.group
            others = []
            for m in (grp.members if grp is not None else []):
                # skip the zone itself and any bonded satellites/subs --
                # those aren't separately-controllable "speakers", they'd
                # just show up as a confusing "grouped with Sub, Sub"
                if m.uid == zone.uid:
                    continue
                try:
                    if not m.is_visible:
                        continue
                except Exception:
                    continue
                if m.player_name:
                    others.append(m.player_name)
            return name, sorted(others)
        except Exception:
            return name, None

    def list(self):
        with self._lock:
            out = []
            for uid, zone in self.speakers.items():
                cfg = config["speakers"].get(uid, {"enabled": True, "volume": 50})
                # Reads cached state ONLY -- nothing here makes a network
                # call. The dashboard polls this every few seconds, and a
                # single asleep/off-network speaker used to make the whole
                # call hang, then 500, on SoCo's request timeout.
                out.append({
                    "uid": uid,
                    "name": self.names.get(uid, uid),
                    "enabled": cfg.get("enabled", True),
                    "volume": cfg.get("volume", 50),
                    "streaming": self.streams.get(uid, False),
                    "ip": getattr(zone, "ip_address", "") or "",
                    "is_default": uid == config.get("default_speaker_uid"),
                    # surfaced so the dashboard can show "grouped with X, Y"
                    # -- toggling any one of them affects the whole group
                    # (see _effective_zone), so the UI shouldn't imply
                    # otherwise
                    "grouped_with": self.groups.get(uid, []),
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

    def set_default_speaker(self, uid):
        """Pin (or, with a falsy uid, clear) the speaker PC2Sonos talks
        to directly at launch. Returns True on success, False if the uid
        isn't a speaker we currently know an IP for."""
        with self._lock:
            if not uid:
                config["default_speaker_ip"] = ""
                config["default_speaker_uid"] = ""
                save_config(config)
                return True
            ip = getattr(self.speakers.get(uid), "ip_address", "") or ""
            if not ip:
                return False
            config["default_speaker_ip"] = ip
            config["default_speaker_uid"] = uid
            config["speakers"].setdefault(
                uid, {"enabled": True, "volume": 50})["enabled"] = True
            save_config(config)
        return True

    def request_rescan(self):
        """One immediate discovery pass, plus a nudge for the on_demand
        loop (which is otherwise idle). Returns rediscover()'s status."""
        self.rescan_requested.set()
        return self.rediscover()

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
