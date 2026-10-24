"""Read Bear Tracker's public sighting feed without submitting any reports.

The timer HTTP API supplies the death baseline. Actual sightings arrive on the
website's Socket.IO v4 namespace; death timers and window estimates are never
treated as sightings. aiohttp handles WebSocket transport, while the small
Engine.IO/Socket.IO text framing used by this namespace is handled here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from typing import AsyncIterator

if __package__:
    from .listening_scope import resolve_scope
else:
    from listening_scope import resolve_scope

DATA_CENTER = "豆豆柴"
WORLDS = ("水晶塔", "银泪湖", "太阳海岸", "伊修加德", "红茶川")
SOCKET_URL = "wss://tracker-api.beartoolkit.com/socket/?EIO=4&transport=websocket"
NAMESPACE = "/HuntUpdate"
LOG = logging.getLogger(__name__)


def _log_info(message, *args):
    # Route diagnostics through the plugin logger when running inside AstrBot.
    try:
        from astrbot.api import logger
    except ImportError:
        logger = LOG
    logger.info(message, *args)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value):
    number = _number(value)
    # This namespace and lastDeathTimers use Unix seconds, unlike the unrelated
    # world_hunt event whose death times are milliseconds.
    return number if number is not None and 0 < number < 10_000_000_000 else None


def _position(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    x, y = (_number(v) for v in value)
    if x is None or y is None or x < 1 or y < 1:
        return None
    return [x, y]


def decode_event(frame):
    """Decode only named events from the hunt namespace, never other feeds."""
    prefix = "42" + NAMESPACE + ","
    if not isinstance(frame, str) or not frame.startswith(prefix):
        return None
    try:
        event = json.loads(frame[len(prefix):])
    except (ValueError, TypeError):
        return None
    if (not isinstance(event, list) or len(event) != 2
            or not isinstance(event[0], str) or not isinstance(event[1], dict)):
        return None
    return event[0], event[1]


class SightingState:
    """Validate sightings and retain death baselines over reconnects."""

    def __init__(self, resources, *, started_at=None, max_age=300, listen_scopes=None):
        self.resources = resources
        self.listen_scopes = listen_scopes
        self.world_centers, self.centers = resolve_scope(resources, listen_scopes)
        self.started_at = time.time() if started_at is None else started_at
        self.max_age = max_age
        self.deaths = {}
        self.aliases = {}
        self.ignored_baseline = {}
        self.retracted_at = {}
        self.active = {}

    def _record_death(self, identity, death_at):
        previous = self.deaths.get(identity, 0.0)
        living = self.active.get(identity)
        self.deaths[identity] = max(previous, death_at)
        if death_at > previous:
            self.ignored_baseline.pop(identity, None)
        if self.active.get(identity, {}).get("seen_at", float("inf")) <= death_at:
            self.active.pop(identity, None)
        return death_at > previous and (not living or death_at >= living["seen_at"])

    def _death_notice(self, identity, *, retracted=False):
        return {"type": "death", "world": identity[0], "hunt_name": identity[1],
                "death_at": self.deaths.get(identity, 0.0), "retracted": retracted}

    def _identity(self, world, hunt_name, key=None):
        if world not in self.world_centers or not isinstance(hunt_name, str):
            return None
        definitions = self.resources.get("DatabaseHunt", {})
        key = key or self.aliases.get((world, hunt_name))
        if not key:
            key = hunt_name if hunt_name in definitions else re.sub(r" \d+$", "", hunt_name)
        definition = definitions.get(key, {})
        if definition.get("Rank") != 3:
            return None
        return (world, hunt_name), key, definition

    def _sighting(self, identity, key, definition, seen_at, position, now):
        death_at = self.deaths.get(identity, 0.0)
        if (seen_at is None or seen_at <= death_at
                or seen_at < self.started_at or now - seen_at > self.max_age
                or seen_at > now + 60 or seen_at <= self.retracted_at.get(identity, 0)):
            return None
        # Different reports of the same living S carry different sighting times.
        # The latest preceding death identifies the spawn throughout its life.
        spawn_key = json.dumps([*identity, death_at], ensure_ascii=False, separators=(",", ":"))
        if self.ignored_baseline.get(identity) == death_at:
            return None
        previous = self.active.get(identity)
        if previous and previous["seen_at"] > death_at:
            # A delayed update of the preceding death must not rename an
            # already reported living spawn or cancel its pending delivery.
            death_at = previous["death_at"]
            spawn_key = previous["spawn_key"]
        if position is None and previous and previous["spawn_key"] == spawn_key:
            position = previous["position"]
        suffix = re.search(r" (\d+)$", identity[1])
        sighting = {
            "hunt_key": key, "hunt_name": identity[1], "world": identity[0], "data_center": self.world_centers[identity[0]],
            "seen_at": seen_at, "death_at": death_at, "position": position,
            "instance": int(suffix[1]) if suffix else None,
            "region_key": definition.get("Region"), "spawn_key": spawn_key,
        }
        self.active[identity] = sighting
        return sighting

    def snapshot(self, payload, *, initial=False, now=None):
        now = time.time() if now is None else now
        rows = payload.get("timers")
        if not isinstance(rows, list):
            raise ValueError("Bear Tracker lastDeathTimers response has no timers list")
        valid = []
        notices = []
        # First load all death baselines, so a row cannot use an outdated death.
        for row in rows:
            if not isinstance(row, dict):
                continue
            parsed = self._identity(row.get("worldName"), row.get("huntName"), row.get("huntKey"))
            if not parsed:
                continue
            identity, key, definition = parsed
            self.aliases[identity] = key
            death = _timestamp(row.get("lastDeathTime")) or 0.0
            if self._record_death(identity, death) and not initial:
                notices.append(self._death_notice(identity))
            valid.append((row, identity, key, definition))
        result = {}
        for row, identity, key, definition in valid:
            if initial:
                # Explicitly suppress S monsters that were already alive when
                # monitoring starts, even if they receive fresh duplicate relays.
                seen = _timestamp(row.get("lastSeen"))
                if seen and seen > self.deaths[identity]:
                    self.ignored_baseline[identity] = self.deaths[identity]
                continue
            sighting = self._sighting(identity, key, definition,
                                      _timestamp(row.get("lastSeen")),
                                      _position(row.get("position")), now)
            if sighting:
                result[identity] = sighting
        # Re-emit recently confirmed socket sightings so delivery failures can
        # be retried by the caller. The caller persists per-group success keys.
        for identity, sighting in list(self.active.items()):
            if (now - sighting["seen_at"] > self.max_age
                    or self.deaths.get(identity, 0) >= sighting["seen_at"]):
                self.active.pop(identity, None)
            elif not initial:
                result.setdefault(identity, sighting)
        return notices + list(result.values())

    def event(self, event_name, payload, *, now=None):
        if event_name not in ("FoundReport", "DeathReport") or not isinstance(payload, dict):
            return None
        if payload.get("DataCenter", self.world_centers.get(payload.get("World"))) != self.world_centers.get(payload.get("World")):
            return None
        parsed = self._identity(payload.get("World"), payload.get("Hunt"))
        if not parsed:
            return None
        identity, key, definition = parsed
        report_at = _timestamp(payload.get("Time"))
        now = time.time() if now is None else now
        if event_name == "DeathReport":
            if report_at and report_at <= now + 60:
                if self._record_death(identity, report_at):
                    return self._death_notice(identity)
            return None
        # The website uses Time=0 to invalidate relays. FATE progress events are
        # separate; rejecting explicit completed progress is an extra guard.
        if _number(payload.get("Time")) == 0:
            self.active.pop(identity, None)
            self.retracted_at[identity] = now
            return self._death_notice(identity, retracted=True)
        progress = _number(payload.get("Progress"))
        if progress is not None and progress >= 100:
            return None
        return self._sighting(identity, key, definition, report_at,
                              _position(payload.get("Position")), now)


def _snapshot(client, listen_scopes=None):
    """Use the existing cookie/CSRF client on one worker thread under its lock."""
    with client.lock:
        client.sync()
        resources = client.resources
        worlds, _ = resolve_scope(resources, listen_scopes)
        if not worlds:
            return resources, {"timers": []}
        payload = client.post("lastDeathTimers", {"QueryDeathTimers": list(worlds), "RankType": "sRank"})
        client.monitor_status = {**getattr(client, "monitor_status", {}), "snapshot_at": time.time()}
        return resources, payload


async def stream_sightings(client, poll_interval: float = 20, *, max_age: float = 300, listen_scopes=None) -> AsyncIterator[dict]:
    """Yield Rank-3 sightings and deaths for the selected centers and worlds.

    The first HTTP response only establishes a baseline. Socket reconnects keep
    that state and recover recent sightings from later HTTP snapshots when the
    server supplies lastSeen. Unknown/invalid coordinates yield position=None.
    Deaths/retractions yield type='death' so the caller can cancel pending sends.
    Cancelling or closing this async
    generator closes its WebSocket and ClientSession; no background tasks remain.
    Confirmed live sightings repeat on snapshots until TTL/death so the caller
    can retry deliveries and must deduplicate successful sends by spawn_key.
    """
    import aiohttp

    interval = max(5.0, float(poll_interval))
    started_at = time.time()
    resources, payload = await asyncio.to_thread(_snapshot, client, listen_scopes)
    client.monitor_status = {"connected": False, "snapshot_at": time.time()}
    state = SightingState(resources, started_at=started_at, max_age=max_age, listen_scopes=listen_scopes)
    if not state.world_centers:
        return
    state.snapshot(payload, initial=True)
    _log_info("Bear Tracker S monitor baseline loaded: %d timer rows; data center=%s",
             len(payload["timers"]), "、".join(state.centers))
    timeout = aiohttp.ClientTimeout(total=35, sock_connect=20)
    async with aiohttp.ClientSession(timeout=timeout, headers={
        "User-Agent": "AstrBot-BearTracker/2.0",
        "Origin": "https://tracker.beartoolkit.com",
    }) as session:
        while True:
            try:
                async with session.ws_connect(SOCKET_URL, heartbeat=None,
                                              max_msg_size=1_000_000) as socket:
                    joined = False
                    last_frame_at = asyncio.get_running_loop().time()
                    next_poll = asyncio.get_running_loop().time() + interval
                    while True:
                        remaining = next_poll - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            try:
                                resources, payload = await asyncio.to_thread(_snapshot, client, listen_scopes)
                                state.resources = resources
                                state.world_centers, state.centers = resolve_scope(resources, listen_scopes)
                                for sighting in state.snapshot(payload):
                                    yield sighting
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                LOG.warning("Bear Tracker sighting snapshot refresh failed", exc_info=True)
                            next_poll = asyncio.get_running_loop().time() + interval
                            remaining = interval
                        try:
                            message = await asyncio.wait_for(socket.receive(), timeout=min(remaining, 45.0))
                        except asyncio.TimeoutError:
                            if asyncio.get_running_loop().time() - last_frame_at > 70:
                                raise ConnectionError("Bear Tracker Engine.IO heartbeat timed out")
                            continue
                        if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE,
                                            aiohttp.WSMsgType.ERROR):
                            raise ConnectionError("Bear Tracker sighting socket disconnected")
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        last_frame_at = asyncio.get_running_loop().time()
                        frame = message.data
                        if frame.startswith("0"):
                            handshake = json.loads(frame[1:])
                            if not isinstance(handshake, dict) or "sid" not in handshake:
                                raise ValueError("Unexpected Engine.IO handshake")
                            await socket.send_str("40" + NAMESPACE + ",")
                        elif frame.startswith("2"):
                            await socket.send_str("3" + frame[1:])
                        elif frame.startswith("40" + NAMESPACE) and not joined:
                            await socket.send_str("42" + NAMESPACE + "," + json.dumps(
                                ["Change Room Request", state.centers], ensure_ascii=False))
                            joined = True
                            client.monitor_status.update(connected=True, connected_at=time.time(),
                                                         rooms=list(state.centers), worlds=list(state.world_centers))
                            _log_info("Bear Tracker S monitor connected; subscribed to %s", "、".join(state.centers))
                            # Covers the brief initial HTTP-to-socket gap and
                            # reconnect outages without replaying the baseline.
                            next_poll = 0
                        elif frame == "1" or frame.startswith(("41" + NAMESPACE, "44" + NAMESPACE)):
                            raise ConnectionError("Bear Tracker hunt namespace disconnected or rejected")
                        else:
                            event = decode_event(frame)
                            if event:
                                sighting = state.event(*event)
                                if sighting:
                                    yield sighting
            except asyncio.CancelledError:
                raise
            except Exception:
                client.monitor_status["connected"] = False
                LOG.warning("Bear Tracker sighting feed unavailable; reconnecting", exc_info=True)
                # HTTP is only a recovery source if it explicitly reports a
                # recent sighting with coordinates. Timer windows never qualify.
                try:
                    resources, payload = await asyncio.to_thread(_snapshot, client, listen_scopes)
                    state.resources = resources
                    state.world_centers, state.centers = resolve_scope(resources, listen_scopes)
                    for sighting in state.snapshot(payload):
                        yield sighting
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.warning("Bear Tracker recovery snapshot failed", exc_info=True)
                await asyncio.sleep(interval)
