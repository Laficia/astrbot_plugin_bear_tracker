import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot.api import AstrBotConfig
from astrbot_plugin_bear_tracker.message_template import DEFAULT_TEMPLATE, SAMPLE_VALUES, TRIGGER_SAMPLE_VALUES, render_template, validate_template
from astrbot_plugin_bear_tracker.webui import BearWebUI, validate_settings
from astrbot_plugin_bear_tracker.trigger_source import DEFAULT_TRIGGER_HUNTS


class TemplateTests(unittest.TestCase):
    def test_default_and_multiline_templates_render_values_once(self):
        self.assertEqual(render_template(DEFAULT_TEMPLATE, SAMPLE_VALUES),
                         "库啵，发现一只50S: 虚无探索者出现在[狗-伊修加德] 西萨纳兰 (18.4, 15.1)")
        values = {**SAMPLE_VALUES, "怪名": "带{花括号}的名称"}
        self.assertEqual(render_template("{{S怪}}\r\n{怪名} [{大区}] {X}/{Y}", values),
                         "{S怪}\n带{花括号}的名称 [豆豆柴] 18.4/15.1")

    def test_mistyped_variables_or_expressions_cannot_be_saved(self):
        for template in [None, "", " \n ", "x" * 2001, "{未知}", "{", "{}", "{怪名.__class__}",
                         "{怪名[0]}", "{等级!r}", "{等级:1000000000}", "{等级:{X}}"]:
            with self.subTest(template=str(template)[:40]), self.assertRaises(ValueError):
                validate_settings({"message_template": template})
        self.assertEqual(validate_template("自定义纯文字"), "自定义纯文字")

    def test_trigger_template_has_an_independent_variable_set(self):
        self.assertEqual(render_template("{怪名}可以触发啦", TRIGGER_SAMPLE_VALUES, "trigger"), "虚无探索者可以触发啦")
        with self.assertRaises(ValueError):
            validate_settings({"trigger_message_template": "{坐标}"})
        with self.assertRaises(ValueError):
            validate_settings({"trigger_alerts_enabled": "true"})


class TemplateWebUITests(unittest.IsolatedAsyncioTestCase):
    async def test_hunt_selection_saves_independently_and_invalid_input_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = {"trigger_hunts": DEFAULT_TRIGGER_HUNTS, "message_template": "已有出现模板",
                        "trigger_message_template": "已有提醒模板", "listen_scopes": [{"data_center": "豆豆柴", "servers": []}]}
            config = AstrBotConfig(str(path), default_config=original)
            changed = asyncio.Event()
            trigger = SimpleNamespace(apply_hunt_selection=Mock())
            plugin = SimpleNamespace(config=config, context=SimpleNamespace(register_web_api=Mock()),
                                     alerts=SimpleNamespace(changed=changed), triggers=trigger)
            ui = BearWebUI(plugin)
            req = SimpleNamespace(json=AsyncMock(return_value={"trigger_hunts": []}))
            with patch("astrbot_plugin_bear_tracker.webui.request", req):
                self.assertEqual((await ui.settings())["status"], "ok")
            self.assertEqual(json.loads(path.read_text("utf-8-sig")), {**original, "trigger_hunts": []})
            trigger.apply_hunt_selection.assert_called_once()
            self.assertFalse(changed.is_set())
            before = path.read_bytes()
            req.json.return_value = {"trigger_hunts": ["未知S怪"], "trigger_message_template": "不应保存"}
            with patch("astrbot_plugin_bear_tracker.webui.request", req):
                self.assertEqual((await ui.settings())["status"], "error")
            self.assertEqual(path.read_bytes(), before)

    async def test_template_only_save_persists_without_touching_routes_or_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = {"target_sessions": ["莫古环:GroupMessage:100000001"],
                        "listen_scopes": [{"data_center": "豆豆柴", "servers": []}], "enabled": True}
            path.write_text(json.dumps(original), "utf-8")
            config = AstrBotConfig(str(path), default_config={**original, "message_template": DEFAULT_TEMPLATE})
            context = SimpleNamespace(register_web_api=Mock(), send_message=AsyncMock())
            plugin = SimpleNamespace(context=context, config=config, alerts=SimpleNamespace(changed=asyncio.Event()))
            ui = BearWebUI(plugin)
            ui.sync = AsyncMock()
            template = "{等级}S {怪名}\n[{大区简称}-{服务器}] {地图} ({坐标})"
            req = SimpleNamespace(json=AsyncMock(return_value={"message_template": template}))
            with patch("astrbot_plugin_bear_tracker.webui.request", req):
                self.assertEqual((await ui.settings())["status"], "ok")
            self.assertEqual(json.loads(path.read_text("utf-8-sig")), {**original, "message_template": template})
            self.assertEqual(config["message_template"], template)
            self.assertFalse(plugin.alerts.changed.is_set())
            before = path.read_bytes()
            req.json.return_value = {"message_template": "{错误变量}", "enabled": False}
            with patch("astrbot_plugin_bear_tracker.webui.request", req):
                self.assertEqual((await ui.settings())["status"], "error")
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(config["enabled"])
            ui.sync.assert_not_awaited()

    async def test_preview_does_not_save_or_send_and_handles_invalid_input(self):
        context = SimpleNamespace(register_web_api=Mock(), send_message=AsyncMock())
        plugin = SimpleNamespace(context=context, config={})
        ui = BearWebUI(plugin)
        req = SimpleNamespace(json=AsyncMock(return_value={"message_template": "{怪名} ({坐标})"}))
        with patch("astrbot_plugin_bear_tracker.webui.request", req):
            result = await ui.template_preview()
            self.assertEqual(result["data"]["text"], "虚无探索者 (18.4, 15.1)")
            req.json.return_value = {"message_template": "{错误变量}"}
            self.assertEqual((await ui.template_preview())["status"], "error")
            req.json.return_value = {"kind": "trigger", "message_template": "{怪名}可以触发啦"}
            self.assertEqual((await ui.template_preview())["data"]["text"], "虚无探索者可以触发啦")
        self.assertEqual(plugin.config, {})
        context.send_message.assert_not_awaited()
