import asyncio
import base64
import copy
import importlib
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_hunhuan_forward.core import ForwardEngine, Rule

with patch.dict(
    sys.modules,
    {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.SimpleNamespace(logger=logging.getLogger("webui")),
        "astrbot.api.web": types.SimpleNamespace(request=None),
    },
):
    web = importlib.import_module("astrbot_plugin_hunhuan_forward.webui")

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6koAAAAASUVORK5CYII="


def rule(**changes):
    r = {
        "name": "重要通知",
        "type": "keyword",
        "source_groups": [100],
        "target_groups": [100, 200],
        "keywords": ["重要"],
        "platform_id": "bot",
        "enabled": True,
    }
    return r | changes


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = {"rules": [], "send_interval_ms": 0}
        self.calls, self.groups = (
            [],
            [
                {"group_id": 200, "group_name": "测试群 A"},
                {"group_id": 300, "group_name": "测试群 B"},
            ],
        )

        async def call(action, **params):
            self.calls.append((action, copy.deepcopy(params)))
            if action == "get_group_list":
                return self.groups
            if action == "send_group_msg":
                return {"message_id": -len(self.calls)}
            return {}

        self.call = call
        self.bot = types.SimpleNamespace(api=types.SimpleNamespace(call_action=call))
        self.platform = types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(name="aiocqhttp", id="bot"),
            get_client=lambda: self.bot,
        )
        self.engine = ForwardEngine(self.config)
        self.addCleanup(self.engine.close)
        self.plugin = types.SimpleNamespace(
            engine=self.engine,
            tasks=set(),
            context=types.SimpleNamespace(
                register_web_api=lambda *a: None,
                platform_manager=types.SimpleNamespace(platform_insts=[self.platform]),
            ),
        )
        self.ui = web.ForwardWebUI(self.plugin, self.config)

    async def request(self, method, body):
        with patch.object(
            web, "request", types.SimpleNamespace(json=AsyncMock(return_value=body))
        ):
            return await getattr(self.ui, method)()

    def payload(self, **updates):
        return {
            "request_id": "test-request-123456789",
            "platform_id": "bot",
            "group_ids": [200, 300],
            "text": "通知",
            "images": [PNG],
        } | updates

    async def test_save_add_edit_delete_and_stale_revision(self):
        token = self.engine.remember(("bot", "1", "100"), ["8"])
        result = await self.request(
            "settings",
            {
                "rules": [rule()],
                "revision": web.revision(self.config),
                "send_interval_ms": 100,
            },
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.engine.rules[0].words, ("重要",))
        self.assertIn(token, self.engine.cache)
        revision = result["data"]["revision"]
        result = await self.request(
            "settings",
            {
                "rules": [rule(keywords=["活动", "集合"])],
                "revision": revision,
                "send_interval_ms": 100,
            },
        )
        self.assertEqual(self.engine.rules[0].words, ("活动", "集合"))
        stale = await self.request("settings", {"rules": [], "revision": revision})
        self.assertEqual(stale["status"], "error")
        result = await self.request(
            "settings", {"rules": [], "revision": result["data"]["revision"]}
        )
        self.assertEqual(result["status"], "ok")
        self.assertFalse(self.engine.rules)

    async def test_failed_disk_write_keeps_config_and_runtime(self):
        class Config(dict):
            def save_config(self, values):
                self.update(values)
                raise OSError("test disk full")

        self.ui.config = Config(self.config)
        before = dict(self.ui.config)
        result = await self.request(
            "settings", {"rules": [rule()], "revision": web.revision(self.ui.config)}
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.ui.config, before)
        self.assertFalse(self.engine.rules)

    async def test_rule_matches_platform_toggle_and_command_guards(self):
        r = Rule.parse(rule())
        self.assertEqual(r.destinations(100, "重要通知", "bot"), (200,))
        self.assertFalse(r.destinations(100, "重要通知", "other"))
        self.assertFalse(
            Rule.parse(rule(enabled=False)).destinations(100, "重要", "bot")
        )
        for text, expected in [("重要通知", True), ("普通", False), ("/重要", False)]:
            result = await self.request(
                "match",
                {"rule": rule(), "text": text, "group_id": 100, "platform_id": "bot"},
            )
            self.assertEqual(result["data"]["matched"], expected)
        self.assertFalse(self.calls)

    async def test_text_images_send_and_idempotency(self):
        payload = self.payload(group_ids=[200, 200, 300])
        first = await self.request("send", payload)
        self.assertEqual(
            [r["state"] for r in first["data"]["results"]], ["sent", "sent"]
        )
        sends = [params for action, params in self.calls if action == "send_group_msg"]
        self.assertEqual(len(sends), 2)
        self.assertEqual(
            sends[0]["message"][0], {"type": "text", "data": {"text": "通知"}}
        )
        self.assertTrue(sends[0]["message"][1]["data"]["file"].startswith("base64://"))
        await self.request("send", payload)
        self.assertEqual(len([a for a, _ in self.calls if a == "send_group_msg"]), 2)
        collision = await self.request("send", payload | {"text": "不同内容"})
        self.assertEqual(collision["status"], "error")
        self.assertNotIn("fingerprint", first["data"])

    async def test_inflight_duplicate_does_not_send_twice(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(action, **params):
            if action == "get_group_list":
                started.set()
                await release.wait()
            return await self.call(action, **params)

        self.bot.api.call_action = slow
        with patch.object(
            web,
            "request",
            types.SimpleNamespace(json=AsyncMock(return_value=self.payload())),
        ):
            first = asyncio.create_task(self.ui.send())
            await asyncio.wait_for(started.wait(), 1)
            second = await self.ui.send()
            self.assertEqual(second["data"]["status"], "sending")
            release.set()
            await asyncio.wait_for(first, 1)
        self.assertEqual(len([a for a, _ in self.calls if a == "send_group_msg"]), 2)

    async def test_unjoined_group_rejected_before_any_send(self):
        result = await self.request("send", self.payload(group_ids=[200, 999]))
        self.assertEqual(result["data"]["status"], "failed")
        self.assertFalse(any(a == "send_group_msg" for a, _ in self.calls))

    async def test_missing_reply_and_exception_never_retry(self):
        async def uncertain(action, **params):
            if action == "send_group_msg":
                self.calls.append((action, params))
                if params["group_id"] == 200:
                    return {}
                raise TimeoutError("test timeout")
            return await self.call(action, **params)

        self.bot.api.call_action = uncertain
        result = await self.request("send", self.payload())
        self.assertEqual(
            [r["state"] for r in result["data"]["results"]], ["unknown", "unknown"]
        )
        self.assertEqual(len([a for a, _ in self.calls if a == "send_group_msg"]), 2)

    async def test_manual_recall_partial_failure_then_retry(self):
        result = await self.request("send", self.payload())
        job = result["data"]
        failed_id = job["results"][1]["message_id"]

        async def fail_one(action, **params):
            if action == "delete_msg" and str(params["message_id"]) == failed_id:
                raise RuntimeError("test recall denied")
            return await self.call(action, **params)

        self.bot.api.call_action = fail_one
        result = await self.request("recall", {"id": job["id"]})
        self.assertEqual(
            [r["state"] for r in result["data"]["results"]], ["recalled", "sent"]
        )
        self.bot.api.call_action = self.call
        result = await self.request("recall", {"id": job["id"]})
        self.assertTrue(
            all(r["state"] == "recalled" for r in result["data"]["results"])
        )
        self.assertEqual(len([a for a, _ in self.calls if a == "delete_msg"]), 2)
        self.ui.jobs[job["id"]]["expires_at"] = 0
        self.assertEqual(
            (await self.request("recall", {"id": job["id"]}))["status"], "error"
        )

    async def test_image_validation_and_empty_content(self):
        self.assertEqual(web.validate_content({"images": [PNG]})[0]["type"], "image")
        for body in [
            {},
            {"images": ["file:///private.png"]},
            {"images": [PNG] * 7},
            {
                "images": [
                    "data:image/png;base64,"
                    + base64.b64encode(b"not an image").decode()
                ]
            },
            {"text": "a" * 5001},
            {"images": ["data:image/svg+xml;base64,AAAA"]},
        ]:
            with self.assertRaises(ValueError):
                web.validate_content(body)

    async def test_invalid_keywords_cannot_save(self):
        for bad in [
            rule(keywords=[]),
            rule(keywords=[""]),
            rule(name=""),
            rule(type="regex"),
        ]:
            result = await self.request(
                "settings", {"rules": [bad], "revision": web.revision(self.config)}
            )
            self.assertEqual(result["status"], "error")
        self.assertFalse(self.engine.rules)


if __name__ == "__main__":
    unittest.main()
