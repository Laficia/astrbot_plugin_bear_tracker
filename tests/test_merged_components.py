"""Merged-host boundaries; outbound calls are in-memory replacements."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_bear_tracker.component_host import ComponentConfig, MergedComponents
from astrbot_plugin_bear_tracker.components.magic_bucket import webui as magic_web
from astrbot_plugin_bear_tracker.components.hunhuan_forward import webui as manual_web

ROOT = Path(__file__).parents[1]


class ConfigTests(unittest.TestCase):
    def test_partial_saves_preserve_other_modules_and_bear_settings(self):
        root = {'enabled': True, 'magic_bucket': {'enabled': False, 'x': 1},
                'hunhuan_forward': {'rules': [], 'send_interval_ms': 500}}
        view = ComponentConfig(root, 'magic_bucket', {})
        view.save_config({'enabled': True})
        self.assertEqual(root, {'enabled': True, 'magic_bucket': {'enabled': True, 'x': 1},
                               'hunhuan_forward': {'rules': [], 'send_interval_ms': 500}})
        self.assertEqual(dict(view), {'enabled': True, 'x': 1})

    def test_failed_disk_save_cannot_change_component_config(self):
        class Failing(dict):
            def save_config(self, values):
                raise OSError('full')
        root = Failing(magic_bucket={'enabled': False})
        with self.assertRaises(OSError):
            ComponentConfig(root,'magic_bucket',{}).save_config({'enabled': True})
        self.assertFalse(root['magic_bucket']['enabled'])

    def test_all_engine_modules_and_assets_are_exact_copies(self):
        manifest = json.loads((ROOT/'components/source_manifest.json').read_text('utf8'))
        for component, spec in manifest.items():
            for name, digest in spec['sha256'].items():
                if name in ('profile.json','README.md'):continue
                path = ROOT/'components'/component/name
                if name in ('main.py','webui.py'):
                    path = path.with_name('original_'+path.name+'.txt')
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, name)

    def test_views_keep_owner_auth_and_share_one_document(self):
        panels = (ROOT/'views/console/panels.js').read_text('utf8')
        for module in ['magic_bucket','hunhuan_forward']:
            self.assertIn('webui/'+module+'/',panels)
        root = (ROOT/'views/console/index.html').read_text('utf8')
        self.assertNotIn('<iframe',root)
        self.assertNotIn('href="magic.html"',root)
        self.assertIn('href="#magic-rules"',root)
        self.assertIn('href="#manual"',root)
        for page in ['magic','manual']:
            self.assertIn('location.replace("index.html#', (ROOT/'views/console'/(page+'.html')).read_text('utf8'))


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.routes = {}
        self.config = {'enabled':True, 'target_sessions':['bot:GroupMessage:200'], 'magic_bucket':{'enabled':False},
                       'hunhuan_forward': {'send_interval_ms':0,'rules':[]}}
        self.api_call = AsyncMock(return_value=[{'group_id':200,'group_name':'模拟群'}])
        bot = SimpleNamespace(api=SimpleNamespace(call_action=self.api_call))
        platform = SimpleNamespace(meta=lambda:SimpleNamespace(name='aiocqhttp',id='bot'),get_client=lambda:bot)
        self.context = SimpleNamespace(
            register_web_api=lambda route, handler, methods, desc:self.routes.update({route:handler}),
            platform_manager=SimpleNamespace(platform_insts=[platform]))
        self.plugin = SimpleNamespace(config=self.config,context=self.context)
        self.merged = MergedComponents(self.plugin)
        self.addAsyncCleanup(self.merged.close)

    async def test_only_magic_receives_group_events_and_hunhuan_never_automatically_forwards(self):
        self.merged.magic.on_group = AsyncMock()
        self.merged.manual.on_group = AsyncMock()
        event = SimpleNamespace(get_platform_id=lambda:'bot')
        await self.merged.on_group(event)
        self.merged.magic.on_group.assert_awaited_once_with(event)
        self.merged.manual.on_group.assert_not_awaited()

    async def test_api_routes_are_under_owner_without_legacy_route_collisions(self):
        self.assertEqual(len(self.routes),9)
        self.assertTrue(all(r.startswith('/astrbot_plugin_bear_tracker/webui/') for r in self.routes))
        result = await self.routes['/astrbot_plugin_bear_tracker/webui/hunhuan_forward/settings']()
        self.assertEqual(result['status'],'error')
        self.assertEqual(self.config['hunhuan_forward']['rules'],[])

    async def test_magic_save_keeps_sighting_settings_and_manual_settings(self):
        profile = json.loads((ROOT/'tests/fixture_profile.json').read_text('utf8'))
        body = {'profile':profile,'enabled':False,'bot_uin':''}
        before = copy.deepcopy(self.config)
        with patch.object(magic_web,'request',SimpleNamespace(json=AsyncMock(return_value=body))):
            result = await self.merged.magic.webui.settings()
        self.assertEqual(result['status'],'ok')
        self.assertEqual(self.config['hunhuan_forward'],before['hunhuan_forward'])
        self.assertTrue(self.config['enabled'])
        self.api_call.assert_not_awaited()

    async def test_manual_text_image_send_idempotency_and_recall_with_mock_transport(self):
        async def call(action, **kwargs):
            if action=='get_group_list':
                return [{'group_id':200,'group_name':'模拟群'}]
            return {'message_id':42}
        self.api_call.side_effect = call
        image='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6koAAAAASUVORK5CYII='
        body={'request_id':'merged-test-12345678','platform_id':'bot','group_ids':[200],'text':'模拟消息','images':[image]}
        with patch.object(manual_web,'request',SimpleNamespace(json=AsyncMock(return_value=body))):
            result=await self.merged.manual.webui.send()
            repeated=await self.merged.manual.webui.send()
        self.assertEqual(result,repeated)
        self.assertEqual(result['data']['results'][0]['state'],'sent')
        sends=[args for args in self.api_call.call_args_list if args.args[0]=='send_group_msg']
        self.assertEqual(len(sends),1)
        self.assertEqual([s['type'] for s in sends[0].kwargs['message']],['text','image'])
        with patch.object(manual_web,'request',SimpleNamespace(json=AsyncMock(return_value={'id':body['request_id']}))):
            recalled=await self.merged.manual.webui.recall()
        self.assertEqual(recalled['data']['results'][0]['state'],'recalled')

    async def test_component_imports_do_not_register_independent_stars_or_event_handlers(self):
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import star_handlers_registry
        self.assertFalse(any('.components.' in key for key in star_map))
        self.assertFalse(any('.components.' in h.handler_module_path for h in star_handlers_registry))

    async def test_unload_cancels_background_work_and_closes_both_components(self):
        task=asyncio.create_task(asyncio.sleep(100))
        self.merged.manual.tasks.add(task)
        await self.merged.close()
        self.assertTrue(task.cancelled())
        self.assertTrue(self.merged.manual.engine.closed)
        self.assertTrue(self.merged.magic.closed)
