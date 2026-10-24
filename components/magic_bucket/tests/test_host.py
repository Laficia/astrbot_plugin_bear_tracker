"""Optional actual AstrBot SDK contract checks; no QQ traffic."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace
import json

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from test_port import astr_event, Bot


@unittest.skipUnless(importlib.util.find_spec('astrbot'), 'Actual AstrBot SDK not available')
class HostTests(unittest.IsolatedAsyncioTestCase):
    async def test_webui_validation_and_no_send_preview(self):
        from astrbot_plugin_magic_bucket.main import MagicBucket
        from astrbot_plugin_magic_bucket.webui import MagicWebUI, validate
        root=Path(__file__).parents[1]
        profile=json.loads((root/'profile.json').read_text('utf8'))
        body={'profile':profile,'enabled':False,'bot_uin':'3923833280','text':'重要 水塔 树海SS前置','group':'245422536'}
        routes=[]
        context=SimpleNamespace(register_web_api=lambda *args:routes.append(args))
        plugin=MagicBucket(context,{'enabled':False})
        self.assertEqual(len(routes),3)
        with patch('astrbot_plugin_magic_bucket.webui.request',SimpleNamespace(json=AsyncMock(return_value=body))):
            result=await plugin.webui.preview()
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['data']['matched'],1)
        self.assertTrue(result['data']['messages'][0]['image'].startswith('data:image/png;base64,'))
        self.assertIn('【水晶塔】',result['data']['messages'][0]['text'])
        self.assertFalse(plugin.engines)
        self.assertEqual(plugin.received,0)
        profile['rules'][0]['keywords']=['']
        with self.assertRaises(ValueError): validate(body)
        await plugin.terminate()

    async def test_webui_save_and_reject_invalid(self):
        from astrbot_plugin_magic_bucket.main import MagicBucket
        root=Path(__file__).parents[1]
        body={'profile':json.loads((root/'profile.json').read_text('utf8')),'enabled':False,'bot_uin':'3923833280'}
        plugin=MagicBucket(SimpleNamespace(register_web_api=lambda *args:None),{'enabled':False})
        with patch('astrbot_plugin_magic_bucket.webui.request',SimpleNamespace(json=AsyncMock(return_value=body))):
            result=await plugin.webui.settings()
        self.assertEqual(result['status'],'ok')
        self.assertFalse(plugin.settings['enabled'])
        self.assertEqual(len(json.loads(plugin.settings['rules_json'])),4)
        before=dict(plugin.settings)
        body['profile']['leak_fill']['window_seconds']=-1
        with patch('astrbot_plugin_magic_bucket.webui.request',SimpleNamespace(json=AsyncMock(return_value=body))):
            result=await plugin.webui.settings()
        self.assertEqual(result['status'],'error')
        self.assertEqual(plugin.settings,before)
        await plugin.terminate()

    async def test_default_does_not_send(self):
        from astrbot_plugin_magic_bucket.main import MagicBucket
        plugin=MagicBucket(None,{})
        bot=Bot()
        await plugin.on_group(astr_event(bot,'重要'))
        self.assertFalse(bot.calls)
        await plugin.terminate()

    async def test_actual_sdk_entry_and_account_isolation(self):
        from astrbot_plugin_magic_bucket.main import MagicBucket
        plugin=MagicBucket(None,{'enabled':True,'send_interval_ms':0,'bot_uin':'',
            'overrides_json':'{"leak_fill":{"enabled":false},"spawn_watch":{"enabled":false}}'})
        bot=Bot()
        event=astr_event(bot,'重要')
        event.get_platform_name=lambda:'aiocqhttp'
        event.get_platform_id=lambda:'test_platform'
        event.stop_event=lambda:None
        with tempfile.TemporaryDirectory() as data:
            with patch('astrbot_plugin_magic_bucket.main.StarTools.get_data_dir',return_value=Path(data)):
                await plugin.on_group(event)
                self.assertEqual(len(plugin.engines),1)
                event.message_obj.raw_message['self_id']=3923833281
                await plugin.on_group(event)
                self.assertEqual(len(plugin.engines),2)
                a,b=plugin.engines.values()
                self.assertIsNot(a._recall_cache,b._recall_cache)
                self.assertNotEqual(a.config['spawn_watch']['path'],b.config['spawn_watch']['path'])
                await plugin.terminate()
                self.assertFalse(plugin.engines)

    async def test_account_filter(self):
        from astrbot_plugin_magic_bucket.main import MagicBucket
        plugin=MagicBucket(None,{'enabled':True,'bot_uin':'999'})
        bot=Bot(); event=astr_event(bot,'重要')
        event.get_platform_name=lambda:'aiocqhttp'
        await plugin.on_group(event)
        self.assertFalse(plugin.engines)
        self.assertFalse(bot.calls)
        await plugin.terminate()
