"""Feature isolation, source scopes and transport gates; no real group sends."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from astrbot_plugin_bear_tracker.feature_controls import allows,updated_controls,magic_profile,FeatureDisabled
from astrbot_plugin_bear_tracker.scoped_magic import ScopedMagicEngine,BaseEngine
from astrbot_plugin_bear_tracker.component_host import SharedMagicConfig,MergedComponents
from astrbot_plugin_bear_tracker.components.magic_bucket.webui import PreviewAPI
from astrbot_plugin_bear_tracker.components.magic_bucket.adapter import Message,Segment
from astrbot_plugin_bear_tracker.webui import BearWebUI
from astrbot_plugin_bear_tracker.train_schedule import TrainFeature,DEFAULT_CONFIG
from astrbot_plugin_bear_tracker.quiet_hours import guarded_send

ROOT=Path(__file__).parents[1]
PROFILE=json.loads((ROOT/'tests/fixture_profile.json').read_text('utf8'))


class ControlTests(unittest.TestCase):
    def test_defaults_preserve_behavior_and_empty_groups_do_not_create_routes(self):
        self.assertTrue(allows({},'manual','123'))
        config={'feature_controls':{'manual':{'enabled':True,'groups':['100']}}}
        self.assertTrue(allows(config,'manual','100'));self.assertFalse(allows(config,'manual','200'))
        config['feature_controls']['manual']['enabled']=False
        self.assertFalse(allows(config,'manual','100'))

    def test_atomic_update_preserves_siblings_and_rejects_invalid_groups(self):
        config={'feature_controls':{'text':{'enabled':False,'groups':['100']}}};before=copy.deepcopy(config)
        result=updated_controls(config,'manual',{'enabled':True,'groups':['200','200']})
        self.assertEqual(result['manual']['groups'],['200']);self.assertEqual(config,before)
        self.assertEqual(result['text'],before['feature_controls']['text'])
        for value in [{'enabled':1,'groups':[]},{'enabled':True,'groups':['bot:GroupMessage:123']},{'enabled':True,'groups':'123'}]:
            with self.assertRaises(ValueError):updated_controls(config,'text',value)
        with self.assertRaises(ValueError):updated_controls(config,'unknown',{'enabled':True,'groups':[]})

    def test_disabling_text_preserves_all_saved_subsettings_and_aliases(self):
        config={'feature_controls':{'text':{'enabled':False,'groups':[]}},'magic_bucket':{'overrides_json':json.dumps(PROFILE)}}
        before=copy.deepcopy(config);profile=magic_profile(SharedMagicConfig(config,'magic_bucket',{}),copy.deepcopy(PROFILE))
        self.assertFalse(profile['map_names']['enabled']);self.assertEqual(profile['completions']['aliases'],{})
        self.assertEqual(config,before)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def run_engine(self,controls):
        profile=copy.deepcopy(PROFILE)
        for section in ('hunt_alerts','leak_fill','spawn_watch','mango_watch'):profile[section]['enabled']=False
        profile['send_interval_ms']=0;profile['completions']['aliases']={'空探':'虚无探索者'}
        text='重要 空探 S';api=PreviewAPI(text);engine=ScopedMagicEngine(profile,api,{'feature_controls':controls})
        await engine.on_load();self.addAsyncCleanup(engine.on_close)
        event=SimpleNamespace(group_id=int(profile['rules'][0]['source_groups'][0]),user_id=1,self_id=2,message_id='0',time=None,
                              message=Message([Segment({'type':'text','data':{'text':text}})]),reply=AsyncMock())
        await engine.onGroupMessageReceived(event)
        return api.sent

    async def test_forward_switch_and_source_scope_stop_only_forwarding(self):
        self.assertEqual(await self.run_engine({'forwarding':{'enabled':False,'groups':[]}}),[])
        self.assertEqual(await self.run_engine({'forwarding':{'enabled':True,'groups':['999']}}),[])
        self.assertTrue(await self.run_engine({}))

    async def test_text_scope_skips_formatting_but_keeps_original_forwarding(self):
        formatted=await self.run_engine({})
        raw=await self.run_engine({'text':{'enabled':True,'groups':['999']}})
        text=lambda rows:'\n'.join(seg['data'].get('text','') for row in rows for seg in row['segments'] if seg['type']=='text')
        self.assertIn('虚无探索者',text(formatted));self.assertIn('西萨纳兰',text(formatted))
        self.assertTrue(raw);self.assertIn('空探',text(raw));self.assertNotIn('虚无探索者',text(raw))

    async def test_concurrent_event_scopes_do_not_leak_into_one_another(self):
        engine=ScopedMagicEngine({},None,{'feature_controls':{'text':{'enabled':True,'groups':['100']}}})
        marker=object();engine._decorator=marker
        seen=[];ready=asyncio.Event()
        async def observe(_engine,event):
            before=_engine._decorator
            if event.group_id==100:ready.set();await asyncio.sleep(.01)
            else:await ready.wait()
            seen.append((event.group_id,before,_engine._decorator))
        with patch.object(BaseEngine,'onGroupMessageReceived',observe):
            await asyncio.gather(engine.onGroupMessageReceived(SimpleNamespace(group_id=100)),engine.onGroupMessageReceived(SimpleNamespace(group_id=200)))
        self.assertIn((100,marker,marker),seen);self.assertIn((200,None,None),seen)
        self.assertIs(engine._decorator,marker)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_sighting_transport_rechecks_feature_and_group_after_map_wait(self):
        config={'feature_controls':{'sightings':{'enabled':True,'groups':['100']}}};ctx=SimpleNamespace(send_message=AsyncMock())
        with self.assertRaises(FeatureDisabled):await guarded_send(ctx,config,'bot:GroupMessage:200',object())
        ctx.send_message.assert_not_awaited()
        await guarded_send(ctx,config,'bot:GroupMessage:100',object())
        config['feature_controls']['sightings']['enabled']=False
        with self.assertRaises(FeatureDisabled):await guarded_send(ctx,config,'bot:GroupMessage:100',object())
        self.assertEqual(ctx.send_message.await_count,1)

    async def test_manual_target_scope_checked_inside_rate_limiter_and_recall_still_allowed(self):
        config={'feature_controls':{'manual':{'enabled':True,'groups':['100']}}}
        plugin=SimpleNamespace(config=config,context=SimpleNamespace(register_web_api=Mock()))
        host=MergedComponents(plugin);self.addAsyncCleanup(host.close);host.manual.engine.interval=0
        action=AsyncMock()
        with self.assertRaises(FeatureDisabled):await host.manual.engine.limited(action,'send_group_msg',group_id=200)
        action.assert_not_awaited()
        await host.manual.engine.limited(action,'send_group_msg',group_id=100)
        config['feature_controls']['manual']['enabled']=False
        with self.assertRaises(FeatureDisabled):await host.manual.engine.limited(action,'send_group_msg',group_id=100)
        await host.manual.engine.limited(action,'delete_msg',message_id=1)
        self.assertEqual(action.await_count,2)

    async def test_train_scope_blocks_queries_before_network(self):
        cfg={'train_commands':copy.deepcopy(DEFAULT_CONFIG),'feature_controls':{'trains':{'enabled':True,'groups':['100']}}}
        feature=TrainFeature(SimpleNamespace(config=cfg));feature.query=AsyncMock(return_value={'parts':['测试']})
        event=SimpleNamespace(get_group_id=lambda:'200',get_sender_id=lambda:'1',get_self_id=lambda:'2',get_message_str=lambda:'/车表',
                              message_obj=SimpleNamespace(raw_message={}),stop_event=Mock(),send=AsyncMock())
        await feature.on_event(event);feature.query.assert_not_awaited();event.send.assert_not_awaited()
        event.get_group_id=lambda:'100';await feature.on_event(event);feature.query.assert_awaited_once();event.send.assert_awaited_once()

    async def test_feature_save_preserves_other_configuration_and_is_atomic_on_validation(self):
        config={'magic_bucket':{'overrides_json':json.dumps(PROFILE),'custom':'preserve'},'message_template':'我的模板'}
        plugin=SimpleNamespace(config=config,context=SimpleNamespace(register_web_api=Mock()),components=SimpleNamespace(magic=SimpleNamespace(reset_engines=AsyncMock())))
        ui=BearWebUI(plugin);ui.feature_status=AsyncMock(return_value={'status':'ok'})
        before=copy.deepcopy(config)
        request=SimpleNamespace(json=AsyncMock(return_value={'feature':'text','control':{'enabled':False,'groups':['100']}}))
        with patch('astrbot_plugin_bear_tracker.webui.request',request):self.assertEqual((await ui.feature_settings())['status'],'ok')
        self.assertEqual(config['magic_bucket'],before['magic_bucket']);self.assertEqual(config['message_template'],'我的模板')
        before=copy.deepcopy(config);request.json.return_value['control']['groups']=['bad:prefix']
        with patch('astrbot_plugin_bear_tracker.webui.request',request):self.assertEqual((await ui.feature_settings())['status'],'error')
        self.assertEqual(config,before)

    async def test_single_feature_header_controls_native_switch_without_losing_any_other_profile_fields(self):
        config={'magic_bucket':{'overrides_json':json.dumps(PROFILE),'rules_json':'','send_interval_ms':500},
                'train_commands':copy.deepcopy(DEFAULT_CONFIG)}
        plugin=SimpleNamespace(config=config,context=SimpleNamespace(register_web_api=Mock()),components=SimpleNamespace(magic=SimpleNamespace(reset_engines=AsyncMock())))
        ui=BearWebUI(plugin);ui.feature_status=AsyncMock(return_value={'status':'ok'})
        for feature,section in [('mango','mango_watch'),('hunt','hunt_alerts'),('trains',None)]:
            before=json.loads(config['magic_bucket']['overrides_json'])
            body={'feature':feature,'control':{'enabled':False,'groups':['100']}}
            with patch('astrbot_plugin_bear_tracker.webui.request',SimpleNamespace(json=AsyncMock(return_value=body))):
                result=await ui.feature_settings()
            self.assertEqual(result['status'],'ok',result)
            after=json.loads(config['magic_bucket']['overrides_json'])
            if section:
                expected=copy.deepcopy(before);expected[section]['enabled']=False;self.assertEqual(after,expected)
            else:self.assertEqual(after,before);self.assertFalse(config['train_commands']['enabled'])
            self.assertFalse(config['feature_controls'][feature]['enabled'])

    async def test_disabled_processors_do_not_block_other_independent_features(self):
        profile=copy.deepcopy(PROFILE)
        for section in ('hunt_alerts','leak_fill','spawn_watch','mango_watch'):profile[section]['enabled']=False
        profile['send_interval_ms']=0
        config={'feature_controls':{'forwarding':{'enabled':False,'groups':[]},'collection':{'enabled':True,'groups':['100']},'hunt':{'enabled':True,'groups':['200']}}}
        engine=ScopedMagicEngine(profile,PreviewAPI('消息'),config);await engine.on_load();self.addAsyncCleanup(engine.on_close)
        spawn=Mock();spawn.observe.return_value=True;hunt=Mock();engine._spawn=spawn;engine._hunt_alerts=hunt
        event=lambda group:SimpleNamespace(group_id=group,user_id=1,self_id=2,message_id='0',time=None,
            message=Message([Segment({'type':'text','data':{'text':'消息'}})]),reply=AsyncMock())
        await engine.onGroupMessageReceived(event(100))
        spawn.observe.assert_called_once_with('消息');spawn.flush.assert_called_once();hunt.observe_ss.assert_not_called()
        await engine.onGroupMessageReceived(event(200))
        self.assertEqual(spawn.observe.call_count,1);hunt.observe_ss.assert_called_once()
        self.assertEqual(engine.api.sent,[])
