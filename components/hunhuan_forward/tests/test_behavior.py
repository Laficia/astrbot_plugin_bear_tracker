import asyncio
import copy
import importlib
import logging
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_hunhuan_forward.core import ForwardEngine, OneBotTransport, Rule


def config(**updates):
    value = {
        "send_interval_ms": 0,
        "rules": [
            {
                "name": "通知",
                "type": "keyword",
                "source_groups": [100],
                "target_groups": [100, 200, 300],
                "keywords": ["重要", "撤回"],
                "forward_prefix": "不应出现",
            }
        ],
    }
    value.update(updates)
    return value


class Transport:
    def __init__(self):
        self.sent, self.replies, self.deleted, self.times = [], [], [], []
        self.fail_groups, self.fail_deletes, self.no_ids = set(), set(), False
        self.fail_reply = False
        self.attempts = []

    async def forward(self, group, mid):
        self.times.append(time.monotonic())
        self.attempts.append(group)
        if group in self.fail_groups:
            raise RuntimeError("test offline")
        self.sent.append((group, mid))
        return None if self.no_ids else str(1000 + len(self.sent))

    async def reply(self, text):
        self.times.append(time.monotonic())
        if self.fail_reply:
            raise RuntimeError("test receipt unavailable")
        self.replies.append(text)

    async def delete(self, mid):
        self.times.append(time.monotonic())
        if mid in self.fail_deletes:
            raise RuntimeError("test delete denied")
        self.deleted.append(mid)


class BehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.tx = ForwardEngine(config()), Transport()
        self.engine.retry_delay = 0
        self.scope = ("napcat", "999", "100")
        self.addCleanup(self.engine.close)

    async def send(self, text="重要通知", scope=None, group=100):
        return await self.engine.process(
            self.tx, scope or self.scope, group, text, "42"
        )

    def token(self):
        self.assertEqual(len(self.engine.cache), 1)
        return next(iter(self.engine.cache))

    async def test_forward_and_recall(self):
        await self.send()
        self.assertEqual(self.tx.sent, [(200, "42"), (300, "42")])
        token = self.token()
        self.assertRegex(token, "^[a-f0-9]{8}$")
        self.assertEqual(
            self.tx.replies[-1],
            f"已转发 2 条消息。\n2 分钟内在本群发送「撤回{token}」可撤回。",
        )
        self.assertTrue(await self.send("撤回 " + token.upper()))
        self.assertEqual(self.tx.deleted, ["1001", "1002"])
        self.assertFalse(self.engine.cache)
        self.assertEqual(len(self.tx.sent), 2)

    async def test_wrong_group_platform_account_and_unknown(self):
        await self.send()
        token = self.token()
        for scope in [
            ("napcat", "999", "101"),
            ("other", "999", "100"),
            ("napcat", "888", "100"),
        ]:
            await self.send("撤回" + token, scope)
        await self.send("撤回ffffffff")
        self.assertFalse(self.tx.deleted)
        self.assertIn(token, self.engine.cache)

    async def test_expiry_and_idle_cleanup(self):
        await self.send()
        token = self.token()
        self.engine.cache[token].deadline = time.monotonic() - 1
        await self.send("撤回" + token)
        self.assertFalse(self.tx.deleted)
        self.engine.ttl = 0.01
        self.engine.remember(self.scope, ["1"])
        await asyncio.sleep(0.03)
        self.assertFalse(self.engine.cache)

    async def test_partial_failures_retry_only_failed_ids(self):
        self.tx.fail_groups = {200}
        await self.send()
        self.assertEqual(self.tx.attempts, [200, 200, 200, 300])
        token = self.token()
        batch = self.engine.cache[token]
        batch.ids.append("extra")
        deadline = batch.deadline
        self.tx.fail_deletes = {"extra"}
        await self.send("撤回" + token)
        self.assertEqual(batch.ids, ["extra"])
        self.assertEqual(batch.deadline, deadline)
        self.tx.fail_deletes.clear()
        await self.send("撤回" + token)
        self.assertEqual(self.tx.deleted, ["1001", "extra"])

    async def test_missing_ids_never_retry(self):
        self.tx.no_ids = True
        await self.send()
        self.assertEqual(self.tx.attempts, [200, 300])
        self.assertFalse(self.engine.cache)
        self.assertIn("2 次转发未返回消息 ID", self.tx.replies[-1])

    async def test_failed_receipt_never_resends(self):
        self.tx.fail_reply = True
        await self.send()
        self.assertEqual(len(self.tx.sent), 2)
        self.token()

    async def test_simultaneous_recall(self):
        await self.send()
        token = self.token()
        started, release = asyncio.Event(), asyncio.Event()
        delete = self.tx.delete

        async def slow(mid):
            started.set()
            await release.wait()
            await delete(mid)

        self.tx.delete = slow
        first = asyncio.create_task(self.send("撤回" + token))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(self.send("撤回" + token))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual(self.tx.deleted, ["1001", "1002"])
        self.assertTrue(any("正在撤回" in r for r in self.tx.replies))

    async def test_accepted_recall_finishes_after_expiry(self):
        self.engine.ttl, self.engine.interval = 0.01, 0.02
        token = self.engine.remember(self.scope, ["1", "2", "3"])
        await self.send("撤回" + token)
        self.assertEqual(self.tx.deleted, ["1", "2", "3"])
        self.assertFalse(self.engine.cache)

    async def test_shared_rate_limit_across_events(self):
        self.engine.interval = 0.015
        await asyncio.gather(self.send(), self.send())
        for before, after in zip(self.tx.times, self.tx.times[1:]):
            self.assertGreaterEqual(after - before, 0.013)

    async def test_ignored_messages_and_duplicate_rules(self):
        for text in ["普通消息", " /重要", "", "重要"]:
            await self.send(text, group=101)
        await self.send(" /重要")
        self.assertFalse(self.tx.sent)
        self.engine.rules *= 2
        await self.send()
        self.assertEqual([g for g, _ in self.tx.sent], [200, 300, 200, 300])

    async def test_collision_and_close(self):
        with patch(
            "astrbot_plugin_hunhuan_forward.core.secrets.token_hex",
            side_effect=["1234abcd", "1234abcd", "5678abcd"],
        ):
            first = self.engine.remember(self.scope, ["1"])
            second = self.engine.remember(self.scope, ["2"])
        self.assertNotEqual(first, second)
        timers = [b.timer for b in self.engine.cache.values()]
        self.engine.close()
        self.assertTrue(all(t.cancelled() for t in timers))
        self.assertFalse(self.engine.cache)
        await self.send()
        self.assertFalse(self.tx.sent)

    async def test_rule_validation_and_prefix(self):
        rule = config()["rules"][0]
        rule.update(type="prefix", source_groups=["100"], target_groups=["200"])
        r = Rule.parse(rule)
        self.assertEqual(r.destinations(100, " 重要通知 "), (200,))
        self.assertEqual(r.destinations(100, "很重要"), ())
        rule["type"] = "unknown"
        engine = ForwardEngine({"rules": [rule]})
        self.assertEqual(len(engine.errors), 1)
        self.assertFalse(engine.rules)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_segments_and_numeric_negative_ids(self):
        segments = [
            {"type": "text", "data": {"text": " 重要 "}},
            {"type": "image", "data": {"file": "id", "url": "https://example.com/img"}},
            {"type": "at", "data": {"qq": "all"}},
            {"type": "record", "data": {"file": "voice"}},
            {"type": "video", "data": {"file": "video"}},
            {"type": "reply", "data": {"id": "40"}},
        ]
        original = copy.deepcopy(segments)
        call = AsyncMock(
            side_effect=[{"message": segments}, {"message_id": -987}, {}, {}]
        )
        tx = OneBotTransport(
            types.SimpleNamespace(api=types.SimpleNamespace(call_action=call)),
            100,
            "42",
            "999",
        )
        self.assertEqual(await tx.forward(200, "42"), -987)
        call.assert_awaited_with(
            "send_group_msg", group_id=200, message=original, self_id=999
        )
        await tx.delete("-987")
        call.assert_awaited_with("delete_msg", message_id=-987, self_id=999)
        await tx.reply("完成")
        message = call.call_args.kwargs["message"]
        self.assertEqual(
            message,
            [
                {"type": "reply", "data": {"id": "42"}},
                {"type": "text", "data": {"text": "完成"}},
            ],
        )
        self.assertEqual(segments, original)

    async def test_onebot_envelope_failure_and_missing(self):
        call = AsyncMock(
            side_effect=[
                {"status": "ok", "retcode": 0, "data": {"message": []}},
                {"status": "ok", "retcode": 0, "data": {}},
                {"status": "failed", "retcode": 100, "data": None},
            ]
        )
        tx = OneBotTransport(
            types.SimpleNamespace(api=types.SimpleNamespace(call_action=call)), 100, 42
        )
        self.assertIsNone(await tx.forward(200, 42))
        with self.assertRaises(RuntimeError):
            await tx.delete(9)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_astrbot_entry_contract(self):
        # Lightweight API stand-in; this does not claim a real AstrBot/QQ session.
        api, events, stars = (
            types.ModuleType(n)
            for n in ("astrbot.api", "astrbot.api.event", "astrbot.api.star")
        )
        api.AstrBotConfig, api.logger = dict, logging.getLogger("bridge")
        events.AstrMessageEvent = object
        events.filter = types.SimpleNamespace(
            EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1),
            event_message_type=lambda *a, **k: lambda f: f,
        )
        stars.Context = object
        stars.Star = type(
            "Star",
            (),
            {"__init__": lambda self, context: setattr(self, "context", context)},
        )
        stars.register = lambda *args: lambda cls: cls
        with patch.dict(
            sys.modules,
            {
                "astrbot": types.ModuleType("astrbot"),
                "astrbot.api": api,
                "astrbot.api.event": events,
                "astrbot.api.star": stars,
                "astrbot.api.web": types.SimpleNamespace(request=None),
            },
        ):
            module = importlib.import_module("astrbot_plugin_hunhuan_forward.main")
            plugin = module.HunHuanForwardPlugin(
                types.SimpleNamespace(register_web_api=lambda *a: None), config()
            )
        self.addAsyncCleanup(plugin.terminate)
        call = AsyncMock(
            side_effect=[
                {"message": [{"type": "text", "data": {"text": "重要"}}]},
                {"message_id": 101},
                {"message": [{"type": "text", "data": {"text": "重要"}}]},
                {"message_id": 102},
                {},
            ]
        )
        raw = {
            "post_type": "message",
            "self_id": 999,
            "message": [{"type": "text", "data": {"text": "重要"}}],
        }
        event = types.SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp",
            get_platform_id=lambda: "napcat",
            get_group_id=lambda: "100",
            get_sender_id=lambda: "123",
            message_obj=types.SimpleNamespace(
                raw_message=raw, self_id="999", message_id="42"
            ),
            bot=types.SimpleNamespace(api=types.SimpleNamespace(call_action=call)),
            stop_event=lambda: None,
        )
        await plugin.on_group(event)
        self.assertEqual(len(plugin.engine.cache), 1)
        before = call.await_count
        event.get_sender_id = lambda: "999"
        await plugin.on_group(event)
        event.get_sender_id = lambda: "123"
        event.get_platform_name = lambda: "qq_official"
        await plugin.on_group(event)
        event.get_platform_name = lambda: "aiocqhttp"
        event.get_group_id = lambda: ""
        await plugin.on_group(event)
        self.assertEqual(call.await_count, before)


if __name__ == "__main__":
    unittest.main()
