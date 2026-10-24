"""Delivery contract tests; no network, live bot or real group is contacted.

Run with AstrBot and its dependencies on PYTHONPATH. MessageChain/Image are the
real AstrBot types; only the map renderer and the outbound context are doubles.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.api.message_components import Image, Plain
from astrbot_plugin_bear_tracker import alert_engine
from astrbot_plugin_bear_tracker.alert_engine import AlertEngine
from astrbot_plugin_bear_tracker.alert_state import AlertState
from astrbot_plugin_bear_tracker.map_renderer import MapRenderError


NOW = 2_000_000_000
GROUP_A = "bot-instance:GroupMessage:group-a"
GROUP_B = "bot-instance:GroupMessage:group-b"


def sighting(**updates):
    value = {
        "spawn_key": '["水晶塔", "Laideronnette 1", 1999999000]',
        "hunt_key": "Laideronnette",
        "hunt_name": "Laideronnette 1",
        "world": "水晶塔",
        "data_center": "豆豆柴",
        "instance": 1,
        "seen_at": NOW,
        "position": [22.3, 25.4],
    }
    value.update(updates)
    return value


class _EndDeliveryPass(BaseException):
    """Stop the infinite worker at its idle wait after one complete pass."""


class AlertEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Mock(return_value=NOW)
        self.addCleanup(patch.stopall)
        patch.object(alert_engine.time, "time", self.clock).start()
        self.renderer = SimpleNamespace(render=Mock(return_value=self.root / "pin.png"))
        patch.object(alert_engine, "BearMapRenderer", return_value=self.renderer).start()
        self.client = SimpleNamespace(
            reference=SimpleNamespace(load_cache=Mock()),
            resources={"DatabaseHunt": {
                "Laideronnette": {"Rank": 3, "Region": "Central Shroud", "Patch": "ARR"}
            }},
            info=Mock(return_value={"名称": "雷德罗巨蛇"}),
            translate=Mock(return_value="黑衣森林中央林区"),
        )
        self.context = SimpleNamespace(send_message=AsyncMock(return_value=True), register_web_api=Mock())
        self.config = {"enabled": True, "max_report_age_seconds": 300}
        self.engine = self.make_engine()
        self.engine.subscribe(GROUP_A, "group-a")

    def make_engine(self):
        return AlertEngine(self.context, self.client, self.config, self.root)

    async def run_pass(self, engine=None):
        """Execute one worker pass without waiting for its five-second timer."""
        engine = engine or self.engine
        with patch.object(engine.wake_delivery, "wait",
                          new=AsyncMock(side_effect=_EndDeliveryPass)):
            with self.assertRaises(_EndDeliveryPass):
                await engine._deliver()

    def text(self, call_index=0):
        chain = self.context.send_message.await_args_list[call_index].args[1]
        return "".join(component.text for component in chain.chain if isinstance(component, Plain))

    async def test_duplicate_reports_send_one_image_and_text_per_group(self):
        report = sighting()
        self.engine.enqueue(report)
        self.engine.enqueue(dict(report))
        self.assertEqual(len(self.engine.pending), 1)
        await self.run_pass()
        self.context.send_message.assert_awaited_once()
        umo, chain = self.context.send_message.await_args.args
        self.assertEqual(umo, GROUP_A)
        self.assertEqual(sum(isinstance(component, Image) for component in chain.chain), 1)
        self.assertIn("[狗-水晶塔]", self.text())
        self.assertIn("黑衣森林中央林区", self.text())
        self.assertIn("雷德罗巨蛇", self.text())
        self.assertIn("(22.3, 25.4)", self.text())
        self.assertTrue(self.engine.state.delivery(report["spawn_key"], GROUP_A)["has_map"])
        self.engine.enqueue(report)
        await self.run_pass()
        self.assertEqual(self.context.send_message.await_count, 1)
        self.assertEqual(self.engine.pending, {})

    async def test_requested_single_line_message_format(self):
        self.client.resources["DatabaseHunt"]["Zona Seeker"] = {"Patch": "ARR", "Region": "Western Thanalan"}
        self.client.info.return_value = {"名称": "虚无探索者"}
        self.client.translate.return_value = "西萨纳兰"
        result = self.engine.describe(sighting(hunt_key="Zona Seeker", world="伊修加德", instance=None,
                                               position=[18.4, 15.1]), has_map=True)
        self.assertEqual(result, "库啵，发现一只50S: 虚无探索者出现在[狗-伊修加德] 西萨纳兰 (18.4, 15.1)")

    async def test_removing_a_scope_cancels_pending_send_and_rejects_new_reports(self):
        report = sighting()
        self.engine.enqueue(report)
        self.config["listen_scopes"] = []
        await self.run_pass()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.engine.pending, {})
        self.engine.enqueue(report)
        self.assertEqual(self.engine.pending, {})

    async def test_custom_template_is_used_by_delivery_with_live_fields(self):
        self.config["message_template"] = "{等级}S｜{怪名}\n[{大区简称}-{服务器}] {地图} ({坐标})｜{大区} {分线}线"
        self.engine.enqueue(sighting())
        await self.run_pass()
        self.assertEqual(self.text(), "50S｜雷德罗巨蛇\n[狗-水晶塔] 黑衣森林中央林区（1线） (22.3, 25.4)｜豆豆柴 1线")
        chain = self.context.send_message.await_args.args[1]
        self.assertEqual(sum(isinstance(c, Image) for c in chain.chain), 1)

    async def test_invalid_manual_config_uses_default_and_missing_coordinates_stay_explicit(self):
        self.config["message_template"] = "{怪名.bad}"
        result = self.engine.describe(sighting(position=None), has_map=False)
        self.assertIn("库啵，发现一只50S:", result)
        self.assertIn("(坐标待更新)", result)
        self.assertIn("等待网站更新坐标", result)

    async def test_all_four_data_center_aliases(self):
        for dc, alias in [("豆豆柴", "狗"), ("陆行鸟", "鸟"), ("猫小胖", "猫"), ("莫古力", "猪")]:
            with self.subTest(dc=dc):
                self.client.resources["DataCenters"] = {dc: {"Names": ["对应服务器"]}}
                self.assertIn(f"[{alias}-对应服务器]", self.engine.describe(sighting(world="对应服务器"), has_map=True))

    async def test_report_fields_and_hunt_definition_change_message(self):
        self.client.resources["DatabaseHunt"]["Ruminator"] = {"Patch": "EW", "Region": "Mare Lamentorum"}
        self.client.resources["DataCenters"] = {"莫古力": {"Names": ["白银乡"]}}
        self.client.info.return_value = {"名称": "沉思之物"}
        self.client.translate.return_value = "叹息海"
        report = sighting(hunt_key="Ruminator", world="白银乡", instance=None,
                          data_center="莫古力", position=[29.2, 18.7])
        self.assertEqual(self.engine.describe(report, has_map=True),
                         "库啵，发现一只90S: 沉思之物出现在[猪-白银乡] 叹息海 (29.2, 18.7)")
        self.client.resources["DatabaseHunt"]["Ruminator"]["Level"] = 91
        self.assertIn("一只91S:", self.engine.describe(report, has_map=True))

    async def test_exception_and_false_send_results_remain_pending_until_success(self):
        report = sighting()
        self.context.send_message.side_effect = [OSError("offline"), False, True]
        self.engine.enqueue(report)
        key = (report["spawn_key"], GROUP_A)
        for attempt in (1, 2):
            await self.run_pass()
            self.assertIsNone(self.engine.state.delivery(report["spawn_key"], GROUP_A))
            self.assertIn(key, self.engine.pending)
            self.assertEqual(self.engine.pending[key]["attempt"], attempt)
            self.assertGreater(self.engine.pending[key]["next"], NOW)
            # Reports repeating during backoff must not reset the retry timer.
            retry_at = self.engine.pending[key]["next"]
            self.engine.enqueue(dict(report))
            self.assertEqual(self.engine.pending[key]["next"], retry_at)
            self.engine.pending[key]["next"] = 0
        await self.run_pass()
        self.assertIsNotNone(self.engine.state.delivery(report["spawn_key"], GROUP_A))
        self.assertNotIn(key, self.engine.pending)
        self.assertEqual(self.context.send_message.await_count, 3)

    async def test_failed_group_does_not_block_or_repeat_successful_group(self):
        self.engine.subscribe(GROUP_B, "group-b")
        self.context.send_message.side_effect = [OSError("group A offline"), True, True]
        report = sighting()
        self.engine.enqueue(report)
        await self.run_pass()
        self.assertIsNone(self.engine.state.delivery(report["spawn_key"], GROUP_A))
        self.assertTrue(self.engine.state.delivery(report["spawn_key"], GROUP_B)["has_map"])
        self.assertEqual(len(self.engine.pending), 1)
        self.engine.pending[(report["spawn_key"], GROUP_A)]["next"] = 0
        self.engine.enqueue(report)
        await self.run_pass()
        destinations = [call.args[0] for call in self.context.send_message.await_args_list]
        self.assertEqual(destinations, [GROUP_A, GROUP_B, GROUP_A])

    async def test_durable_state_survives_engine_restart_and_avoids_duplicate(self):
        report = sighting()
        self.engine.enqueue(report)
        await self.run_pass()
        state_on_disk = json.loads((self.root / "s_alert_state.json").read_text("utf-8"))
        self.assertIn(GROUP_A, state_on_disk["subscriptions"])
        self.assertEqual(len(state_on_disk["sent"]), 1)
        self.clock.return_value = NOW + 10
        restarted = self.make_engine()
        self.assertIn(GROUP_A, restarted.destinations())
        restarted.enqueue(report)
        await self.run_pass(restarted)
        self.assertEqual(self.context.send_message.await_count, 1)
        # A genuinely new spawn must still be delivered after restart.
        restarted.enqueue(sighting(spawn_key="next-spawn", seen_at=NOW + 10))
        await self.run_pass(restarted)
        self.assertEqual(self.context.send_message.await_count, 2)

    async def test_missing_coordinates_send_text_then_one_durable_map_supplement(self):
        report = sighting(position=None)
        self.engine.enqueue(report)
        await self.run_pass()
        self.renderer.render.assert_not_called()
        self.assertIn("等待网站更新坐标", self.text())
        self.assertFalse(self.engine.state.delivery(report["spawn_key"], GROUP_A)["has_map"])
        self.engine.enqueue(report)
        await self.run_pass()
        self.assertEqual(self.context.send_message.await_count, 1)
        # Coordinate supplementation also works after a plugin restart.
        restarted = self.make_engine()
        located = sighting()
        restarted.enqueue(located)
        await self.run_pass(restarted)
        self.assertIn("S怪坐标补充", self.text(1))
        self.assertTrue(restarted.state.delivery(report["spawn_key"], GROUP_A)["has_map"])
        restarted.enqueue(located)
        await self.run_pass(restarted)
        self.assertEqual(self.context.send_message.await_count, 2)

    async def test_map_download_failure_retries_before_truthful_text_fallback(self):
        self.renderer.render.side_effect = MapRenderError("download unavailable")
        report = sighting()
        self.engine.enqueue(report)
        key = (report["spawn_key"], GROUP_A)
        for _ in range(2):
            await self.run_pass()
            self.context.send_message.assert_not_awaited()
            self.assertIsNone(self.engine.state.delivery(report["spawn_key"], GROUP_A))
            self.engine.pending[key]["next"] = 0
        await self.run_pass()
        self.context.send_message.assert_awaited_once()
        self.assertIn("地图暂不可用", self.text())
        self.assertIn("(22.3, 25.4)", self.text())
        chain = self.context.send_message.await_args.args[1]
        self.assertFalse(any(isinstance(component, Image) for component in chain.chain))
        self.assertFalse(self.engine.state.delivery(report["spawn_key"], GROUP_A)["has_map"])

    async def test_expired_future_and_pre_subscription_reports_are_rejected(self):
        for seen_at in (NOW - 301, NOW + 31, float("nan"), float("inf"), NOW - 3):
            self.engine.enqueue(sighting(seen_at=seen_at))
        self.assertEqual(self.engine.pending, {})
        self.context.send_message.assert_not_awaited()
        self.engine.enqueue(sighting())
        self.clock.return_value = NOW + 301
        await self.run_pass()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.engine.pending, {})

    async def test_unsubscribe_before_delivery_and_disabled_switch_stop_sending(self):
        report = sighting()
        self.engine.enqueue(report)
        self.engine.unsubscribe(GROUP_A)
        await self.run_pass()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.engine.pending, {})
        self.assertNotIn(GROUP_A, self.make_engine().destinations())
        self.engine.subscribe(GROUP_A, "group-a")
        self.engine.enqueue(report)
        self.config["enabled"] = False
        await self.run_pass()
        self.context.send_message.assert_not_awaited()

    async def test_qq_scene_missing_does_not_claim_delivery_and_recovers_after_group_message(self):
        platform = SimpleNamespace(_session_scene={})
        self.context.get_platform_inst = Mock(return_value=platform)
        report = sighting()
        self.engine.enqueue(report)
        await self.run_pass()
        self.context.send_message.assert_not_awaited()
        self.assertIsNone(self.engine.state.delivery(report["spawn_key"], GROUP_A))
        self.assertIn("目标群发言", self.engine.last_error)
        platform._session_scene["group-a"] = "group"
        self.engine.pending[(report["spawn_key"], GROUP_A)]["next"] = 0
        await self.run_pass()
        self.context.send_message.assert_awaited_once()
        self.assertIsNotNone(self.engine.state.delivery(report["spawn_key"], GROUP_A))

    async def test_death_arriving_during_map_render_cancels_pending_send(self):
        report = sighting()
        self.engine.enqueue(report)

        async def render_then_receive_death(*_args, **_kwargs):
            self.engine.enqueue({"type": "death", "world": report["world"],
                                 "hunt_name": report["hunt_name"], "death_at": NOW + 1})
            return self.root / "pin.png"

        with patch.object(alert_engine.asyncio, "to_thread", side_effect=render_then_receive_death):
            await self.run_pass()
        self.context.send_message.assert_not_awaited()
        self.assertIsNone(self.engine.state.delivery(report["spawn_key"], GROUP_A))

    async def test_render_await_rechecks_subscription_enabled_and_age(self):
        for change in ("unsubscribe", "disable", "expire"):
            with self.subTest(change=change):
                self.clock.return_value = NOW
                self.config["enabled"] = True
                self.engine.subscribe(GROUP_A, "group-a")
                self.engine.pending.clear()
                self.context.send_message.reset_mock()
                report = sighting(spawn_key="render-" + change)
                self.engine.enqueue(report)

                async def render_while_state_changes(*_args, **_kwargs):
                    if change == "unsubscribe":
                        self.engine.unsubscribe(GROUP_A)
                    elif change == "disable":
                        self.config["enabled"] = False
                    else:
                        self.clock.return_value = NOW + 301
                    return self.root / "pin.png"

                with patch.object(alert_engine.asyncio, "to_thread", side_effect=render_while_state_changes):
                    await self.run_pass()
                self.context.send_message.assert_not_awaited()
                self.assertIsNone(self.engine.state.delivery(report["spawn_key"], GROUP_A))

    async def test_group_command_enforces_admin_for_changes_and_allows_member_status(self):
        from astrbot.api.event import MessageChain
        from astrbot_plugin_bear_tracker import main as plugin_main

        with patch.object(plugin_main, "BearClient", return_value=self.client), \
                patch.object(plugin_main.StarTools, "get_data_dir", return_value=self.root / "commands"):
            plugin = plugin_main.BearTrackerPlugin(self.context, self.config)
            self.addAsyncCleanup(plugin.operations.close)
        event = SimpleNamespace(
            get_group_id=Mock(return_value="group-a"),
            get_platform_id=Mock(return_value="bot-instance"),
            get_platform_name=Mock(return_value="qq_official"),
            unified_msg_origin="bot-instance:GroupMessage:person_group-a",
            is_admin=Mock(return_value=False),
            plain_result=lambda text: MessageChain().message(text),
        )

        async def command(action):
            return "\n".join([result.get_plain_text() async for result in plugin.alert_command(event, action)])

        self.assertIn("管理员权限", await command("开启"))
        self.assertNotIn(GROUP_A, plugin.alerts.destinations())
        self.assertIn("未开启", await command("状态"))
        event.is_admin.return_value = True
        self.assertIn("已订阅", await command("开启"))
        self.assertIn(GROUP_A, plugin.alerts.destinations())
        self.assertNotIn(event.unified_msg_origin, plugin.alerts.destinations())
        event.is_admin.return_value = False
        self.assertIn("管理员权限", await command("关闭"))
        self.assertIn(GROUP_A, plugin.alerts.destinations())
        event.is_admin.return_value = True
        self.assertIn("已关闭", await command("关闭"))
        self.assertNotIn(GROUP_A, plugin.alerts.destinations())
        event.get_group_id.return_value = ""
        self.assertIn("请在", await command("开启"))
        self.assertEqual(plugin.alerts.destinations(), {})

    def test_group_origin_keeps_platform_instance_and_non_qq_thread(self):
        from astrbot_plugin_bear_tracker.main import BearTrackerPlugin

        for platform in ("qq_official", "aiocqhttp"):
            event = SimpleNamespace(
                get_platform_name=lambda: platform,
                get_platform_id=lambda: "second-bot",
                get_group_id=lambda: "group-a",
                unified_msg_origin="second-bot:GroupMessage:member_group-a",
            )
            self.assertEqual(BearTrackerPlugin.group_origin(event), "second-bot:GroupMessage:group-a")
        event = SimpleNamespace(get_platform_name=lambda: "telegram",
                                unified_msg_origin="tg:GroupMessage:group/topic-42")
        self.assertEqual(BearTrackerPlugin.group_origin(event), event.unified_msg_origin)

    async def test_plugin_initialize_and_terminate_manage_real_worker_tasks(self):
        from astrbot_plugin_bear_tracker import main as plugin_main

        with patch.object(plugin_main, "BearClient", return_value=self.client), \
                patch.object(plugin_main.StarTools, "get_data_dir", return_value=self.root / "plugin"):
            plugin = plugin_main.BearTrackerPlugin(self.context, self.config)
        await plugin.initialize()
        tasks = list(plugin.alerts.tasks)
        self.assertEqual(len(tasks), 2)
        tasks.append(plugin.triggers.task)
        await asyncio.sleep(0)
        await plugin.terminate()
        self.assertTrue(all(task.done() and task.cancelled() for task in tasks))
        self.assertEqual(plugin.alerts.tasks, [])
        self.assertIsNone(plugin.triggers.task)

    async def test_start_is_idempotent_and_close_cancels_both_workers(self):
        listen_started, delivery_started = asyncio.Event(), asyncio.Event()

        async def listen():
            listen_started.set()
            await asyncio.Event().wait()

        async def deliver():
            delivery_started.set()
            await asyncio.Event().wait()

        self.engine._listen, self.engine._deliver = listen, deliver
        self.engine.start()
        first_tasks = list(self.engine.tasks)
        self.engine.start()
        self.assertEqual(self.engine.tasks, first_tasks)
        await asyncio.wait_for(asyncio.gather(listen_started.wait(), delivery_started.wait()), timeout=1)
        await self.engine.close()
        self.assertTrue(all(task.done() and task.cancelled() for task in first_tasks))
        self.assertEqual(self.engine.tasks, [])
        await self.engine.close()


class AlertStateTests(unittest.TestCase):
    def test_subscriptions_and_per_destination_receipts_persist_independently(self):
        with tempfile.TemporaryDirectory() as folder, patch("time.time", return_value=NOW):
            path = Path(folder) / "state.json"
            state = AlertState(path)
            state.subscribe(GROUP_A, "group-a")
            state.subscribe(GROUP_B, "group-b")
            state.mark_sent("spawn-1", GROUP_A, False)
            state.mark_sent("spawn-1", GROUP_B, True)
            loaded = AlertState(path)
            self.assertFalse(loaded.delivery("spawn-1", GROUP_A)["has_map"])
            self.assertTrue(loaded.delivery("spawn-1", GROUP_B)["has_map"])
            self.assertIsNone(loaded.delivery("spawn-2", GROUP_A))
            loaded.unsubscribe(GROUP_A)
            again = AlertState(path)
            self.assertNotIn(GROUP_A, again.subscriptions)
            self.assertIn(GROUP_B, again.subscriptions)
            self.assertTrue(again.delivery("spawn-1", GROUP_B)["has_map"])


if __name__ == "__main__":
    unittest.main()
