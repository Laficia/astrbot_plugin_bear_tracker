import json
import math
from pathlib import Path
import sys
import asyncio
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay_source import SightingState, decode_event, stream_sightings


NOW = 2_000_000_000
WORLD = "水晶塔"
RESOURCES = {"DatabaseHunt": {
    "Laideronnette": {"Rank": 3, "Region": "Central Shroud"},
    "Forneus": {"Rank": 2, "Region": "Central Shroud"},
    "Ker Labyrinthos": {"Rank": 5, "Region": "Labyrinthos"},
}}


def found(**updates):
    event = {"Hunt": "Laideronnette 1", "World": WORLD, "DataCenter": "豆豆柴",
             "Time": NOW + 10, "Position": [22.3, 25.4], "Progress": None}
    event.update(updates)
    return event


def row(**updates):
    value = {"huntName": "Laideronnette 1", "huntKey": "Laideronnette",
             "worldName": WORLD, "lastDeathTime": NOW - 1000}
    value.update(updates)
    return value


class SightingTests(unittest.TestCase):
    def setUp(self):
        self.state = SightingState(RESOURCES, started_at=NOW, max_age=300)
        self.state.snapshot({"timers": [row()]}, initial=True, now=NOW)

    def test_only_actual_rank_three_found_report_qualifies(self):
        self.assertEqual(self.state.snapshot({"timers": [row()]}, now=NOW), [])
        self.assertEqual(self.state.event("DeathReport", found(), now=NOW + 20)["type"], "death")
        self.assertIsNone(self.state.event("水晶塔_Laideronnette 1", found(), now=NOW + 20))
        for invalid in (found(World="萌芽池"), found(Hunt="Forneus"),
                        found(Hunt="Ker Labyrinthos"), found(DataCenter="陆行鸟"),
                        found(Time=NOW * 1000),
                        found(Time=NOW - 1), found(Time=NOW + 1000),
                        found(Progress=100)):
            self.assertIsNone(self.state.event("FoundReport", invalid, now=NOW + 20), invalid)

    def test_same_spawn_key_repeats_and_new_death_creates_new_key(self):
        first = self.state.event("FoundReport", found(), now=NOW + 20)
        second = self.state.event("FoundReport", found(Time=NOW + 15), now=NOW + 20)
        self.assertEqual(first["spawn_key"], second["spawn_key"])
        self.assertEqual(first["instance"], 1)
        self.assertEqual(first["hunt_key"], "Laideronnette")
        self.assertEqual(first["position"], [22.3, 25.4])
        self.assertEqual(first["region_key"], "Central Shroud")
        self.assertEqual(json.loads(first["spawn_key"]), [WORLD, "Laideronnette 1", NOW - 1000])
        self.state.event("DeathReport", found(Time=NOW + 18), now=NOW + 20)
        self.assertIsNone(self.state.event("FoundReport", found(Time=NOW + 17), now=NOW + 20))
        third = self.state.event("FoundReport", found(Time=NOW + 19), now=NOW + 20)
        self.assertNotEqual(first["spawn_key"], third["spawn_key"])
        self.assertEqual(third["death_at"], NOW + 18)

    def test_initial_live_baseline_is_not_replayed(self):
        self.state.snapshot({"timers": [row(lastSeen=NOW + 1, position=[20, 20])]},
                            initial=True, now=NOW + 2)
        self.assertIsNone(self.state.event("FoundReport", found(), now=NOW + 20))
        self.assertEqual(self.state.snapshot({"timers": [row(lastSeen=NOW + 10, position=[20, 20])]},
                                             now=NOW + 20), [])
        self.state.event("DeathReport", found(Time=NOW + 15), now=NOW + 20)
        self.assertIsNotNone(self.state.event("FoundReport", found(Time=NOW + 16), now=NOW + 20))

    def test_reconnect_snapshot_recovers_only_located_live_sightings(self):
        unknown = self.state.snapshot({"timers": [row(lastSeen=NOW + 1)]}, now=NOW + 20)
        self.assertIsNone(unknown[0]["position"])
        result = self.state.snapshot({"timers": [row(lastSeen=NOW + 1, position=[20, 20])]}, now=NOW + 20)
        self.assertEqual(len(result), 1)
        deceased = self.state.snapshot({"timers": [row(lastDeathTime=NOW + 5,
                            lastSeen=NOW + 1, position=[20, 20])]}, now=NOW + 20)
        self.assertEqual([item["type"] for item in deceased], ["death"])

    def test_confirmed_socket_sighting_retries_until_death_or_ttl(self):
        original = self.state.event("FoundReport", found(), now=NOW + 20)
        self.assertEqual(self.state.snapshot({"timers": [row()]}, now=NOW + 30), [original])
        self.assertEqual(self.state.snapshot({"timers": [row()]}, now=NOW + 400), [])
        self.state.event("FoundReport", found(Time=NOW + 410), now=NOW + 420)
        self.state.event("DeathReport", found(Time=NOW + 415), now=NOW + 420)
        self.assertEqual(self.state.snapshot({"timers": [row()]}, now=NOW + 425), [])

    def test_instances_have_independent_deaths_and_spawn_keys(self):
        self.state.snapshot({"timers": [row(huntName="Laideronnette 2")]}, now=NOW)
        one = self.state.event("FoundReport", found(), now=NOW + 20)
        two = self.state.event("FoundReport", found(Hunt="Laideronnette 2"), now=NOW + 20)
        self.assertEqual(two["instance"], 2)
        self.assertNotEqual(one["spawn_key"], two["spawn_key"])

    def test_delayed_preceding_death_does_not_cancel_or_rename_living_spawn(self):
        first = self.state.event("FoundReport", found(), now=NOW + 20)
        self.assertIsNone(self.state.event("DeathReport", found(Time=NOW + 5), now=NOW + 20))
        second = self.state.event("FoundReport", found(Time=NOW + 12), now=NOW + 20)
        self.assertEqual(first["spawn_key"], second["spawn_key"])
        recovered = self.state.snapshot({"timers": [row(lastDeathTime=NOW + 6)]}, now=NOW + 20)
        self.assertEqual(recovered, [second])

    def test_frames_are_namespace_specific_and_strict(self):
        frame = '42/HuntUpdate,' + json.dumps(["FoundReport", found()])
        self.assertEqual(decode_event(frame), ("FoundReport", found()))
        for invalid in ('2', '42/Other,["FoundReport",{}]', '42/HuntUpdate,broken',
                        '42/HuntUpdate,["FoundReport"]', '42/HuntUpdate,["FoundReport",[]]'):
            self.assertIsNone(decode_event(invalid))

    def test_unknown_or_invalid_coordinates_never_guess_location(self):
        for position in (None, [math.nan, 20], [20, math.inf], [False, 20], [-1, 20]):
            sighting = self.state.event("FoundReport", found(Position=position), now=NOW + 20)
            self.assertIsNone(sighting["position"])
        located = self.state.event("FoundReport", found(), now=NOW + 20)
        missing = self.state.event("FoundReport", found(Position=None), now=NOW + 20)
        self.assertEqual(missing["position"], located["position"])

    def test_retraction_cancels_retries_and_stale_snapshot_cannot_restore(self):
        self.state.event("FoundReport", found(), now=NOW + 20)
        revoked = self.state.event("FoundReport", found(Time=0), now=NOW + 21)
        self.assertEqual(revoked["type"], "death")
        self.assertTrue(revoked["retracted"])
        self.assertEqual(self.state.snapshot({"timers": [row(lastSeen=NOW + 10, position=[20, 20])]},
                                             now=NOW + 22), [])
        self.assertIsNotNone(self.state.event("FoundReport", found(Time=NOW + 23), now=NOW + 25))


class StreamProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_heartbeat_namespace_room_and_cleanup(self):
        timestamp = time.time()
        event = found(Time=timestamp)
        dead = found(Time=timestamp + 0.001)
        messages = ['0{"sid":"transport-id","pingInterval":25000,"pingTimeout":20000}',
                    '40/HuntUpdate,{"sid":"namespace-id"}', '2',
                    '42/HuntUpdate,' + json.dumps(["FoundReport", event]),
                    '42/HuntUpdate,' + json.dumps(["DeathReport", dead])]
        sent = []
        cleanup = []

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                cleanup.append("socket")

            async def send_str(self, value):
                sent.append(value)

            async def receive(self):
                if messages:
                    return SimpleNamespace(type=1, data=messages.pop(0))
                await asyncio.Event().wait()

        class Session:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                cleanup.append("session")

            def ws_connect(self, url, **kwargs):
                self.url = url
                return Socket()

        class Client:
            lock = threading.Lock()
            resources = RESOURCES

            def sync(self):
                self.assert_locked()

            def assert_locked(self):
                if not self.lock.locked():
                    raise AssertionError("Resource client must be locked")

            def post(self, endpoint, payload):
                self.assert_locked()
                return {"timers": [row(lastDeathTime=timestamp - 1000)]}

        fake = SimpleNamespace(ClientSession=Session, ClientTimeout=lambda **_: None,
                               WSMsgType=SimpleNamespace(TEXT=1, CLOSED=2, CLOSE=3, ERROR=4))
        with patch.dict(sys.modules, {"aiohttp": fake}):
            stream = stream_sightings(Client())
            # Set the sighting after startup; no historic event replay is needed.
            messages[-2] = '42/HuntUpdate,' + json.dumps(["FoundReport", found(Time=timestamp + 1)])
            messages[-1] = '42/HuntUpdate,' + json.dumps(["DeathReport", found(Time=timestamp + 2)])
            sighting = await anext(stream)
            self.assertEqual(sighting["hunt_key"], "Laideronnette")
            death = await anext(stream)
            self.assertEqual(death["type"], "death")
            await stream.aclose()
        self.assertEqual(sent[0], "40/HuntUpdate,")
        self.assertEqual(json.loads(sent[1].split(",", 1)[1]),
                         ["Change Room Request", ["豆豆柴"]])
        self.assertEqual(sent[2], "3")
        self.assertEqual(cleanup, ["socket", "session"])


if __name__ == "__main__":
    unittest.main()
