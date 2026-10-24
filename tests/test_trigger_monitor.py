"""Trigger reminders: reference conditions, fresh inputs, lifecycle and delivery."""
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_bear_tracker import trigger_monitor, trigger_source
from astrbot_plugin_bear_tracker.trigger_monitor import TriggerMonitor
from astrbot_plugin_bear_tracker.trigger_source import condition_window, eligible_events, timer_lines, validate_trigger_hunts, DEFAULT_TRIGGER_HUNTS
from astrbot_plugin_bear_tracker.message_template import DEFAULT_TRIGGER_TEMPLATE, TRIGGER_SAMPLE_VALUES
from astrbot.api.message_components import Plain

DAY = 320000 + 10
NOW = DAY * 4200 + 9 * 175
WORLD, DC = "伊修加德", "豆豆柴"
GROUP = "莫古环:GroupMessage:736805976"
DEFINITION = {"Bonnacon": {"ln": 1, "rt": [65, 10], "mt": [39, 6]}}


def snapshot(now=NOW, death=None):
    return {"source": "tracker", "dc": DC, "generated": now,
            "trackerStatus": {"state": "ok", "lastSuccessAt": now},
            "live": {WORLD: {"Bonnacon": [[1, now - 66 * 3600 if death is None else death, False]]}}}


def event(now=NOW):
    return eligible_events(snapshot(now), DC, [WORLD], DEFINITION, [], [], now)[0]


class ConditionTests(unittest.TestCase):
    def test_hunt_selection_empty_and_invalid_values_are_unambiguous(self):
        self.assertEqual(validate_trigger_hunts([]), [])
        self.assertEqual(validate_trigger_hunts(["Bonnacon", "Bonnacon"]), ["Bonnacon"])
        self.assertEqual(len(validate_trigger_hunts(DEFAULT_TRIGGER_HUNTS)), 13)
        for value in [None, "Bonnacon", ["未知S怪"], [False]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_trigger_hunts(value)

    def test_et_windows_are_half_open_and_cross_midnight_correctly(self):
        start = DAY * 4200 + 19 * 175
        self.assertIsNone(condition_window("Croque-Mitaine", start - 1))
        self.assertEqual(condition_window("Croque-Mitaine", start), [start, DAY * 4200 + 22 * 175])
        self.assertIsNone(condition_window("Croque-Mitaine", DAY * 4200 + 22 * 175))
        midnight = (DAY + 1) * 4200
        self.assertIsNotNone(condition_window("Gamma", midnight))
        self.assertIsNone(condition_window("Gamma", midnight + 8 * 175))

    def test_moon_and_weather_conditions_are_required(self):
        new_moon = 320000 * 4200
        self.assertIsNotNone(condition_window("Mindflayer", new_moon))
        self.assertIsNone(condition_window("Mindflayer", new_moon + 3 * 175))
        self.assertIsNone(condition_window("Mindflayer", new_moon + 5 * 4200 + 18 * 175))
        full_moon = (320000 + 15) * 4200 + 17 * 175
        self.assertIsNotNone(condition_window("Croakadile", full_moon))
        self.assertIsNone(condition_window("Croakadile", full_moon - 175))
        with patch.object(trigger_source, "weather_at", return_value="Rain"):
            self.assertIsNone(condition_window("Burfurlur the Canny", NOW))
        with patch.object(trigger_source, "weather_at", return_value="Clear Skies"):
            self.assertIsNotNone(condition_window("Burfurlur the Canny", NOW))

    def test_continuous_rain_must_reach_30_minutes(self):
        start = DAY * 4200
        def weather(zone, timestamp):
            return "Rain" if start <= timestamp < start + 2800 else "Clear Skies"
        with patch.object(trigger_source, "weather_at", side_effect=weather):
            self.assertIsNone(condition_window("Laideronnette", start + 1799))
            self.assertEqual(condition_window("Laideronnette", start + 1800), [start + 1800, start + 2800])
            self.assertIsNone(condition_window("Laideronnette", start + 2800))

    def test_cooldown_and_condition_must_both_be_open_not_forced(self):
        self.assertEqual(len(eligible_events(snapshot(), DC, [WORLD], DEFINITION, [], [], NOW)), 1)
        for death in [NOW - 64 * 3600, NOW - 75 * 3600, 0]:
            self.assertEqual(eligible_events(snapshot(death=death), DC, [WORLD], DEFINITION, [], [], NOW), [])
        outside = DAY * 4200 + 11 * 175
        self.assertEqual(eligible_events(snapshot(outside), DC, [WORLD], DEFINITION, [], [], outside), [])

    def test_stale_partial_wrong_center_and_missing_world_fail_closed(self):
        for change in [{"generated": NOW - trigger_source.MAX_SOURCE_AGE - 1}, {"dc": "莫古力"},
                       {"trackerStatus": {"state": "partial", "lastSuccessAt": NOW}}, {"live": {}}]:
            data = {**snapshot(), **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                eligible_events(data, DC, [WORLD], DEFINITION, [], [], NOW)

    def test_shared_kills_maintenance_and_withdrawals_keep_instance_identity(self):
        raw = [[1, NOW - 1000, False], [2, NOW - 900, False]]
        definition = {"ln": 2}
        manual = [{"type": "kill", "dc": DC, "world": WORLD, "hunt": "S", "line": 2, "killAt": NOW - 400},
                  {"type": "kill", "dc": DC, "world": WORLD, "hunt": "S", "line": 1, "killAt": NOW, "withdrawnAt": NOW}]
        maintenance = [{"dc": DC, "worlds": [WORLD], "endAt": NOW - 600},
                       {"dc": DC, "worlds": [WORLD], "endAt": NOW, "withdrawnAt": NOW}]
        self.assertEqual(sorted(timer_lines(raw, "S", WORLD, DC, definition, manual, maintenance, NOW)),
                         [[1, NOW - 600, True], [2, NOW - 400, False]])

    def test_unknown_reference_rules_are_never_executed(self):
        with self.assertRaises(ValueError):
            trigger_source.decode_page('<script>throw Error("must not run")</script>')

    def test_unattributed_multiline_kill_does_not_invent_a_triggerable_instance(self):
        data = snapshot()
        data["live"][WORLD]["Bonnacon"] = [[1, NOW - 80 * 3600, False], [2, NOW - 80 * 3600, False]]
        manual = [{"type": "kill", "dc": DC, "world": WORLD, "hunt": "Bonnacon", "line": 0, "killAt": NOW - 66 * 3600}]
        definitions = {"Bonnacon": {**DEFINITION["Bonnacon"], "ln": 2}}
        self.assertEqual(eligible_events(data, DC, [WORLD], definitions, manual, [], NOW), [])


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"enabled": True, "trigger_alerts_enabled": True}
        self.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.targets = {GROUP: {"since": NOW - 10}}
        self.allowed = True
        self.alerts = SimpleNamespace(config=self.config, data_dir=Path(self.temp.name), context=self.context,
                                     destinations=lambda: self.targets, accepts=lambda event: self.allowed,
                                     max_age=300, template_values=lambda event: dict(TRIGGER_SAMPLE_VALUES))
        self.monitor = TriggerMonitor(self.alerts)
        self.clock = patch.object(trigger_monitor.time, "time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def test_startup_baseline_then_new_window_delivers_one_text_and_deduplicates_restart(self):
        e = event()
        self.monitor.apply_snapshot([e], {WORLD}, {WORLD}, NOW)
        await self.monitor.deliver({WORLD}, NOW)
        self.context.send_message.assert_not_awaited()
        self.monitor.apply_snapshot([], {WORLD}, {WORLD}, NOW)
        self.monitor.apply_snapshot([e], {WORLD}, {WORLD}, NOW)
        await self.monitor.deliver({WORLD}, NOW)
        self.context.send_message.assert_awaited_once()
        chain = self.context.send_message.await_args.args[1]
        self.assertEqual(len(chain.chain), 1)
        self.assertIsInstance(chain.chain[0], Plain)
        self.assertEqual(chain.chain[0].text, DEFAULT_TRIGGER_TEMPLATE.format_map(
            {**TRIGGER_SAMPLE_VALUES, "服务器列表": "[狗-伊修加德]"}))
        self.monitor.apply_snapshot([e], {WORLD}, {WORLD}, NOW)
        await self.monitor.deliver({WORLD}, NOW)
        restarted = TriggerMonitor(self.alerts)
        restarted.apply_snapshot([e], {WORLD}, {WORLD}, NOW)
        await restarted.deliver({WORLD}, NOW)
        self.context.send_message.assert_awaited_once()

    async def test_only_checked_hunts_are_delivered(self):
        self.config["trigger_hunts"] = ["Zona Seeker"]
        first = event()
        second = {**first, "hunt_key": "Zona Seeker", "event_key": "second-event"}
        self.monitor.apply_snapshot([], {WORLD}, {WORLD}, NOW)
        self.monitor.apply_snapshot([first, second], {WORLD}, {WORLD}, NOW)
        await self.monitor.deliver({WORLD}, NOW)
        self.context.send_message.assert_awaited_once()
        self.assertNotIn(self.monitor.sent_key(first["event_key"], GROUP), self.monitor.sent)
        self.assertIn(self.monitor.sent_key(second["event_key"], GROUP), self.monitor.sent)

    async def test_deselect_cancels_pending_and_reselect_does_not_replay_old_window(self):
        e = event()
        self.monitor.apply_snapshot([], {WORLD}, {WORLD}, NOW)
        self.monitor.apply_snapshot([e], {WORLD}, {WORLD}, NOW)
        self.assertEqual(self.monitor.status()["pending"], 1)
        self.config["trigger_hunts"] = ["Zona Seeker"]
        self.monitor.apply_hunt_selection()
        self.assertEqual(self.monitor.status()["pending"], 0)
        await self.monitor.deliver({WORLD}, NOW)
        self.config["trigger_hunts"] = ["Bonnacon"]
        self.monitor.apply_hunt_selection()
        self.monitor.apply_snapshot([e], {WORLD}, {WORLD}, NOW)
        await self.monitor.deliver({WORLD}, NOW)
        self.context.send_message.assert_not_awaited()
        self.monitor.apply_snapshot([{**e, "event_key": "next-condition-window", "window_start": e["window_start"]+1}], {WORLD}, {WORLD}, NOW)
        await self.monitor.deliver({WORLD}, NOW)
        self.context.send_message.assert_awaited_once()

    async def test_select_none_stops_monitoring_and_survives_restart(self):
        self.config["trigger_hunts"] = []
        self.monitor.apply_hunt_selection()
        self.assertFalse(self.monitor.enabled())
        await self.monitor.tick()  # No client/network is needed for an empty selection.
        self.assertEqual(self.monitor.status()["selected_hunt_count"], 0)
        self.assertFalse(TriggerMonitor(self.alerts).enabled())

    async def test_pending_delivery_retries_after_restart_and_only_for_original_groups(self):
        self.monitor.apply_snapshot([], {WORLD}, {WORLD}, NOW)
        self.monitor.apply_snapshot([event()], {WORLD}, {WORLD}, NOW)
        self.context.send_message.return_value = False
        await self.monitor.deliver({WORLD}, NOW)
        self.context.send_message.assert_awaited_once()
        self.targets["another:GroupMessage:new"] = {"since": NOW}
        restarted = TriggerMonitor(self.alerts)
        restarted.apply_snapshot([event()], {WORLD}, {WORLD}, NOW)
        for item in restarted.active.values():
            item["retry"][GROUP]["next"] = 0
        self.context.send_message.return_value = True
        await restarted.deliver({WORLD}, NOW)
        self.assertEqual(self.context.send_message.await_count, 2)
        self.assertTrue(all(call.args[0] == GROUP for call in self.context.send_message.await_args_list))

    async def test_scope_disable_expiry_and_source_failure_prevent_sends(self):
        self.monitor.apply_snapshot([], {WORLD}, {WORLD}, NOW)
        self.monitor.apply_snapshot([event()], {WORLD}, {WORLD}, NOW)
        self.allowed = False
        await self.monitor.deliver({WORLD}, NOW)
        self.allowed = True
        self.config["trigger_alerts_enabled"] = False
        await self.monitor.deliver({WORLD}, NOW)
        self.config["trigger_alerts_enabled"] = True
        await self.monitor.deliver(set(), NOW)
        await self.monitor.deliver({WORLD}, NOW - 91)
        for item in self.monitor.active.values():
            item["window_end"] = NOW
        await self.monitor.deliver({WORLD}, NOW)
        self.context.send_message.assert_not_awaited()

    async def test_multiple_worlds_and_centers_merge_one_message_per_hunt_and_group(self):
        first=event()
        second={**first,"world":"水晶塔","event_key":"second-world"}
        third={**first,"world":"白银乡","data_center":"莫古力","event_key":"third-world"}
        worlds={WORLD,"水晶塔","白银乡"}
        self.monitor.apply_snapshot([],worlds,worlds,NOW)
        self.monitor.apply_snapshot([first,second,third],worlds,worlds,NOW)
        self.assertEqual(self.monitor.status()["pending"],1)
        await self.monitor.deliver(worlds,NOW)
        self.context.send_message.assert_awaited_once()
        text=self.context.send_message.await_args.args[1].chain[0].text
        self.assertEqual(text,"库啵，50S: 虚无探索者可以触发啦，目前有概率出现在[狗-伊修加德]、[狗-水晶塔]、[猪-白银乡]等，冒险者大人们快去尝试呀！")
        self.assertEqual(self.monitor.status()["pending"],0)

    async def test_later_eligible_world_cannot_send_separate_reminder_in_same_window(self):
        first=event()
        second={**first,"world":"水晶塔","event_key":"second-world"}
        worlds={WORLD,"水晶塔"}
        self.monitor.apply_snapshot([],worlds,worlds,NOW)
        self.monitor.apply_snapshot([first],worlds,worlds,NOW)
        await self.monitor.deliver(worlds,NOW)
        restarted=TriggerMonitor(self.alerts)
        restarted.apply_snapshot([first,second],worlds,worlds,NOW)
        await restarted.deliver(worlds,NOW)
        self.context.send_message.assert_awaited_once()
        self.assertEqual(restarted.status()["pending"],0)

    async def test_hunts_groups_and_new_condition_windows_remain_independent(self):
        first=event()
        other={**first,"hunt_key":"Zona Seeker","event_key":"different-hunt"}
        another_group="莫古环:GroupMessage:local-mocked-only"
        self.targets[another_group]={"since":NOW-10}
        self.monitor.apply_snapshot([],{WORLD},{WORLD},NOW)
        self.monitor.apply_snapshot([first,other],{WORLD},{WORLD},NOW)
        await self.monitor.deliver({WORLD},NOW)
        self.assertEqual(self.context.send_message.await_count,4)
        fresh={**first,"event_key":"new-window","window_start":first["window_start"]+1}
        self.monitor.apply_snapshot([fresh,other],{WORLD},{WORLD},NOW)
        await self.monitor.deliver({WORLD},NOW)
        self.assertEqual(self.context.send_message.await_count,6)

    async def test_old_default_template_upgrades_to_batch_wording(self):
        self.config["trigger_message_template"]="库啵，[{大区简称}-{服务器}] 的{等级}S: {怪名}可以触发啦，冒险者大人们快去尝试呀！"
        self.assertEqual(self.monitor.describe(event()), "库啵，50S: 虚无探索者可以触发啦，目前有概率出现在[狗-伊修加德]等，冒险者大人们快去尝试呀！")

    async def test_batch_excludes_invalid_expired_unselected_and_out_of_scope_worlds(self):
        first=event()
        bad={**first,"world":"水晶塔","event_key":"invalid-source"}
        expired={**first,"world":"太阳海岸","event_key":"expired","window_end":NOW}
        excluded={**first,"world":"白银乡","event_key":"scope-excluded"}
        other={**first,"hunt_key":"Zona Seeker","event_key":"unchecked"}
        self.config["trigger_hunts"]=["Bonnacon"]
        self.alerts.accepts=lambda e:e["world"]!="白银乡"
        worlds={WORLD,"水晶塔","太阳海岸","白银乡"}
        self.monitor.apply_snapshot([],worlds,worlds,NOW)
        self.monitor.apply_snapshot([first,bad,expired,excluded,other],worlds,worlds,NOW)
        await self.monitor.deliver(worlds-{"水晶塔"},NOW)
        self.context.send_message.assert_awaited_once()
        text=self.context.send_message.await_args.args[1].chain[0].text
        self.assertIn("[狗-伊修加德]",text)
        for world in ["水晶塔","太阳海岸","白银乡"]:
            self.assertNotIn(world,text)

    async def test_batch_retry_persists_and_only_lists_servers_still_eligible(self):
        first=event()
        second={**first,"world":"水晶塔","event_key":"second-world"}
        worlds={WORLD,"水晶塔"}
        self.monitor.apply_snapshot([],worlds,worlds,NOW)
        self.monitor.apply_snapshot([first,second],worlds,worlds,NOW)
        self.context.send_message.return_value=False
        await self.monitor.deliver(worlds,NOW)
        restarted=TriggerMonitor(self.alerts)
        for e in restarted.active.values():e["retry"][GROUP]["next"]=0
        self.context.send_message.return_value=True
        restarted.apply_snapshot([first],worlds,worlds,NOW)
        await restarted.deliver(worlds,NOW)
        self.assertEqual(self.context.send_message.await_count,2)
        text=self.context.send_message.await_args.args[1].chain[0].text
        self.assertIn("伊修加德",text)
        self.assertNotIn("水晶塔",text)

    async def test_legacy_per_world_receipt_migrates_without_sending_again(self):
        first=event()
        second={**first,"world":"水晶塔","event_key":"second-world"}
        worlds={WORLD,"水晶塔"}
        self.monitor.apply_snapshot([],worlds,worlds,NOW)
        self.monitor.apply_snapshot([first],worlds,worlds,NOW)
        self.monitor.sent[self.monitor.sent_key(first["event_key"],GROUP)]=NOW
        self.monitor.save()
        restarted=TriggerMonitor(self.alerts)
        restarted.apply_snapshot([second],worlds,worlds,NOW)
        await restarted.deliver(worlds,NOW)
        self.context.send_message.assert_not_awaited()

    async def test_independent_template_and_recovery_baseline_do_not_change_sighting_template(self):
        self.config["message_template"] = "原出现模板"
        self.config["trigger_message_template"] = "[{大区简称}-{服务器}] {怪名} {等级}S"
        self.assertEqual(self.monitor.describe(event()), "[狗-伊修加德] 虚无探索者 50S")
        self.assertEqual(self.config["message_template"], "原出现模板")
        self.monitor.apply_snapshot([], {WORLD}, {WORLD}, NOW)
        self.monitor.apply_snapshot([], set(), {WORLD}, NOW)
        self.monitor.apply_snapshot([event()], {WORLD}, {WORLD}, NOW)
        await self.monitor.deliver({WORLD}, NOW)
        self.context.send_message.assert_not_awaited()
