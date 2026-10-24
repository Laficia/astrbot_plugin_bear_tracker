import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_bear_tracker.reference_catalog import ReferenceCatalog, decode_reference, REFERENCE_URL
from astrbot_plugin_bear_tracker.webui import BearWebUI, validate_settings


class ReferenceTests(unittest.TestCase):
    def test_catalog_has_47_matched_regions_points_and_conditions(self):
        catalog = ReferenceCatalog()
        self.assertEqual(len(catalog.data["hunts"]), 47)
        self.assertEqual(sum(len(h["points"]) for h in catalog.data["hunts"].values()), 938)
        maps = json.loads(Path(__file__).resolve().parents[1].joinpath("map_metadata.json").read_text("utf-8"))["maps"]
        for hunt in catalog.data["hunts"].values():
            self.assertIn(hunt["region"], maps)
            self.assertTrue(hunt["method"])
            self.assertEqual(len(hunt["points"]), len(hunt["point_ids"]))

    def test_parser_never_executes_embedded_scripts_and_rejects_missing_data(self):
        with self.assertRaises(ValueError):
            decode_reference('<script>throw Error("must never run")</script>', {})

    def test_failed_refresh_preserves_verified_cache(self):
        catalog = ReferenceCatalog()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reference.json"
            catalog.load_cache(path)
            old = catalog.data
            with patch("astrbot_plugin_bear_tracker.reference_catalog.download_reference", side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    catalog.refresh()
            self.assertIs(catalog.data, old)
            self.assertFalse(path.exists())

    def test_audit_detects_wrong_map_and_wrong_point(self):
        catalog = ReferenceCatalog()
        catalog.data = {"hunts": {"S": {"region": "Zone", "point_ids": [1], "points": [[12, 13]]}}}
        resources = {"DatabaseHunt": {"S": {"Region": "Other"}}}
        self.assertEqual(catalog.audit(resources)["checked"], 0)
        resources["DatabaseHunt"]["S"]["Region"] = "Zone"
        resources["SpawnPoint"] = {"S": {"SpawnPoint01": [12, 14]}}
        self.assertEqual(len(catalog.audit(resources)["issues"]), 1)
        resources["SpawnPoint"]["S"]["SpawnPoint01"] = [12, 13]
        self.assertEqual(catalog.audit(resources)["points_checked"], 1)


class SettingsTests(unittest.TestCase):
    def test_no_private_targets_or_bad_timing_can_be_saved(self):
        for body in [{"enabled": "false"}, {"poll_interval_seconds": 1}, {"max_report_age_seconds": True},
                     {"target_sessions": ["bot:FriendMessage:person"]}, {"target_sessions": ["12345"]}]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_settings(body)
        self.assertEqual(validate_settings({"target_sessions": ["bot:GroupMessage:group"]}),
                         {"target_sessions": ["bot:GroupMessage:group"]})


class WebUITests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_uses_requested_hunt_map_and_coordinates_without_sending(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "map.png"
            path.write_bytes(b"PNG")
            reference = SimpleNamespace(data={"hunts": {"S": {"region": "Zone", "region_name": "地图"}}})
            client = SimpleNamespace(reference=reference, resources={"DatabaseHunt": {"S": {"Region": "Zone"}}})
            maps = SimpleNamespace(render=Mock(return_value=path))
            context = SimpleNamespace(register_web_api=Mock(), send_message=AsyncMock())
            ui = BearWebUI(SimpleNamespace(context=context, client=client, alerts=SimpleNamespace(maps=maps)))
            ui.sync = AsyncMock()
            req = SimpleNamespace(json=AsyncMock(return_value={"hunt_key": "S", "x": 18.4, "y": 15.1}))
            with patch("astrbot_plugin_bear_tracker.webui.request", req):
                data = await ui.preview()
            self.assertEqual(data["status"], "ok")
            maps.render.assert_called_once_with(client.resources, "S", 18.4, 15.1)
            context.send_message.assert_not_awaited()
            client.resources["DatabaseHunt"]["S"]["Region"] = "Wrong"
            maps.render.reset_mock()
            with patch("astrbot_plugin_bear_tracker.webui.request", req):
                data = await ui.preview()
            self.assertEqual(data["status"], "error")
            maps.render.assert_not_called()
