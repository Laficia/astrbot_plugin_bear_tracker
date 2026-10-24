import copy
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_bear_tracker.listening_scope import resolve_scope, validate_scopes
from astrbot_plugin_bear_tracker.relay_source import SightingState, _snapshot
from astrbot_plugin_bear_tracker.webui import normalize_group_ids, resolve_group_receivers

NOW = 2_000_000_000
RESOURCES = {"DataCenters": {"豆豆柴": {"Names": ["水晶塔", "伊修加德"]},
                            "陆行鸟": {"Names": ["红玉海", "萌芽池"]},
                            "猫小胖": {"Names": ["静语庄园"]}},
             "DatabaseHunt": {"Laideronnette": {"Rank": 3, "Region": "Central Shroud"}}}
SCOPES = [{"data_center": "陆行鸟", "servers": ["红玉海"]}, {"data_center": "猫小胖", "servers": []}]


class ScopeTests(unittest.TestCase):
    def test_explicit_server_and_whole_center_resolve_without_other_servers(self):
        worlds, rooms = resolve_scope(RESOURCES, SCOPES)
        self.assertEqual(worlds, {"红玉海": "陆行鸟", "静语庄园": "猫小胖"})
        self.assertEqual(rooms, ["陆行鸟", "猫小胖"])
        self.assertNotIn("萌芽池", worlds)
        self.assertEqual(resolve_scope(RESOURCES, []), ({}, []))
        self.assertEqual(set(resolve_scope(RESOURCES)[0]), {"水晶塔", "伊修加德"})

    def test_wrong_center_server_pair_is_rejected_and_duplicates_merge(self):
        with self.assertRaises(ValueError):
            validate_scopes([{"data_center": "陆行鸟", "servers": ["水晶塔"]}], RESOURCES)
        with self.assertRaises(ValueError):
            validate_scopes([{"data_center": "不存在", "servers": []}], RESOURCES)
        merged = validate_scopes(SCOPES + [{"data_center": "陆行鸟", "servers": ["萌芽池"]}], RESOURCES)
        self.assertEqual(merged[0]["servers"], ["红玉海", "萌芽池"])
        merged = validate_scopes(merged + [{"data_center": "陆行鸟", "servers": []}], RESOURCES)
        self.assertEqual(merged[0]["servers"], [])

    def test_cross_center_found_reports_are_filtered_by_world_and_center(self):
        state = SightingState(RESOURCES, started_at=NOW, listen_scopes=SCOPES)
        base = {"Hunt": "Laideronnette", "Time": NOW + 1, "Position": [20, 20]}
        first = state.event("FoundReport", {**base, "World": "红玉海", "DataCenter": "陆行鸟"}, now=NOW + 2)
        self.assertEqual(first["data_center"], "陆行鸟")
        second = state.event("FoundReport", {**base, "World": "静语庄园", "DataCenter": "猫小胖"}, now=NOW + 2)
        self.assertEqual(second["data_center"], "猫小胖")
        for world, dc in [("萌芽池", "陆行鸟"), ("水晶塔", "豆豆柴"), ("红玉海", "猫小胖")]:
            self.assertIsNone(state.event("FoundReport", {**base, "World": world, "DataCenter": dc}, now=NOW + 2))

    def test_snapshot_queries_only_selected_worlds(self):
        calls = []
        client = SimpleNamespace(lock=threading.Lock(), resources=RESOURCES, sync=lambda: None,
                                 post=lambda path, payload: calls.append(payload) or {"timers": []})
        _snapshot(client, SCOPES)
        self.assertEqual(calls[0]["QueryDeathTimers"], ["红玉海", "静语庄园"])
        calls.clear()
        _snapshot(client, [])
        self.assertEqual(calls, [])


class GroupNumberTests(unittest.TestCase):
    def test_plain_group_numbers_are_routed_without_user_prefix(self):
        self.assertEqual(normalize_group_ids(["100000001", "123456789", "100000001"]), ["100000001", "123456789"])
        self.assertEqual(resolve_group_receivers(["莫古环:GroupMessage:old", "阿咬:GroupMessage:keep"],
                         {"莫古环": ["100000001"]}, {"莫古环", "阿咬"}),
                         ["阿咬:GroupMessage:keep", "莫古环:GroupMessage:100000001"])

    def test_removing_groups_and_invalid_inputs(self):
        self.assertEqual(resolve_group_receivers(["莫古环:GroupMessage:100000001"], {"莫古环": []}, {"莫古环"}), [])
        for value in [["莫古环:GroupMessage:100000001"], ["123 456"], [123456], "123456"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_group_ids(value)
        with self.assertRaises(ValueError):
            resolve_group_receivers([], {"未知机器人": ["123456"]}, {"莫古环"})
