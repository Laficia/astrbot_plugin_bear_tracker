"""Consolidated controls, isolated history clearing, aliases and train order."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import time
from unittest.mock import Mock,AsyncMock,patch
from datetime import datetime,timezone,timedelta

from astrbot_plugin_bear_tracker.component_host import SharedMagicConfig,MergedComponents
from astrbot_plugin_bear_tracker.webui import BearWebUI
from astrbot_plugin_bear_tracker.alert_engine import AlertEngine
from astrbot_plugin_bear_tracker.components.magic_bucket import webui as magic_web
from astrbot_plugin_bear_tracker.components.magic_bucket.s_hunt_names import AliasDecorator,load_config,validate_aliases,canonical_hunts
from astrbot_plugin_bear_tracker.train_schedule import TrainFeature,DEFAULT_CONFIG

ROOT=Path(__file__).parents[1]
PROFILE=json.loads((ROOT/'tests/fixture_profile.json').read_text('utf-8'))


class AliasTests(unittest.TestCase):
    def test_multiple_aliases_expand_without_touching_existing_full_names_or_cascading(self):
        decorate=AliasDecorator(None,{'空探':'虚无探索者','探':'虚无探索者','蛇':'雷德罗巨蛇'},canonical_hunts(PROFILE))
        text,hits=decorate.decorate('空探、探、雷德罗巨蛇、蛇')
        self.assertEqual(text,'虚无探索者、虚无探索者、雷德罗巨蛇、雷德罗巨蛇')
        self.assertEqual(hits,['虚无探索者','雷德罗巨蛇'])

    def test_alias_removal_does_not_reintroduce_bundled_defaults(self):
        profile=copy.deepcopy(PROFILE);profile['completions']['aliases']={}
        cfg=load_config({'overrides_json':json.dumps(profile),'send_interval_ms':0},ROOT)
        self.assertEqual(cfg['completions']['aliases'],{})
        profile['completions']['aliases']={'空探':'虚无探索者','虚探':'虚无探索者'}
        validate_aliases(profile)
        for aliases in ({'虚无探索者':'伽洛克'},{'abc':'不存在的S怪'},{'a,b':'伽洛克'}):
            profile['completions']['aliases']=aliases
            with self.subTest(aliases=aliases),self.assertRaises(ValueError):validate_aliases(profile)

    def test_shared_controls_cannot_be_overridden_by_stale_water_bucket_forms(self):
        root={'enabled':True,'target_sessions':['bot:GroupMessage:200'],'magic_bucket':{'enabled':False,'bot_uin':'old','send_interval_ms':500}}
        config=SharedMagicConfig(root,'magic_bucket',{})
        self.assertTrue(config['enabled']);self.assertEqual(config['bot_uin'],'');self.assertEqual(config.platform_id,'bot')
        config.save_config({'enabled':False,'bot_uin':'123','send_interval_ms':100})
        self.assertTrue(config['enabled']);self.assertEqual(root['magic_bucket']['send_interval_ms'],500)
        self.assertEqual(root['magic_bucket']['bot_uin'],'old')
        root['enabled']=False;self.assertFalse(config['enabled'])
        root['automation_platform_id']='new';self.assertEqual(config.platform_id,'new')


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_history_leaves_pending_receipts_and_subscriptions_untouched(self):
        engine=AlertEngine.__new__(AlertEngine)
        engine.recent={'one':{'hunt_key':'S'}};engine.hidden_recent={};engine.pending={'pending':'alert'};engine.state=object();engine.last_report=123
        engine.config={'max_report_age_seconds':300}
        ui=BearWebUI(SimpleNamespace(alerts=engine,context=SimpleNamespace(register_web_api=Mock())))
        before=(engine.pending,engine.state,engine.last_report)
        self.assertEqual((await ui.clear_recent())['data']['cleared'],1)
        self.assertEqual(engine.recent,{})
        self.assertEqual((engine.pending,engine.state,engine.last_report),before)
        self.assertEqual((await ui.clear_recent())['data']['cleared'],0)
        engine.accepts=Mock(return_value=True);engine.destinations=Mock(return_value={})
        engine.client=SimpleNamespace(resources={},info=lambda *args:{'名称':'S怪'})
        engine.wake_delivery=SimpleNamespace(set=Mock())
        report={'spawn_key':'one','seen_at':time.time(),'hunt_key':'S'}
        engine.enqueue(report);self.assertEqual(engine.recent,{})
        engine.enqueue({**report,'spawn_key':'new'});self.assertEqual(list(engine.recent),['new'])

    async def test_only_selected_platform_handles_automatic_messages_and_master_switch_stops_both(self):
        config={'enabled':True,'automation_platform_id':'chosen','magic_bucket':{'enabled':False},'hunhuan_forward':{}}
        context=SimpleNamespace(register_web_api=Mock())
        plugin=SimpleNamespace(config=config,context=context)
        merged=MergedComponents(plugin);self.addAsyncCleanup(merged.close)
        merged.magic.on_group=AsyncMock()
        await merged.on_group(SimpleNamespace(get_platform_id=lambda:'other'))
        merged.magic.on_group.assert_not_awaited()
        event=SimpleNamespace(get_platform_id=lambda:'chosen');await merged.on_group(event)
        merged.magic.on_group.assert_awaited_once_with(event)
        merged.magic.on_group.reset_mock();config['enabled']=False
        await merged.on_group(event);merged.magic.on_group.assert_not_awaited()

    async def test_change_shared_platform_resets_old_background_forwarding_engines(self):
        config={'enabled':True,'automation_platform_id':'old'}
        changed=Mock();reset=AsyncMock()
        platform=SimpleNamespace(meta=lambda:SimpleNamespace(name='aiocqhttp',id='new'))
        plugin=SimpleNamespace(config=config,alerts=SimpleNamespace(changed=SimpleNamespace(set=changed)),
            context=SimpleNamespace(register_web_api=Mock(),platform_manager=SimpleNamespace(platform_insts=[platform])),
            components=SimpleNamespace(magic=SimpleNamespace(reset_engines=reset)))
        ui=BearWebUI(plugin)
        req=SimpleNamespace(json=AsyncMock(return_value={'enabled':False,'automation_platform_id':'new'}))
        with patch('astrbot_plugin_bear_tracker.webui.request',req):result=await ui.settings()
        self.assertEqual(result['status'],'ok');reset.assert_awaited_once()
        self.assertFalse(config['enabled']);self.assertEqual(config['automation_platform_id'],'new')

    async def test_real_forwarding_preview_normalizes_alias_and_completes_map_without_sending(self):
        profile=copy.deepcopy(PROFILE);profile['completions']['aliases']={'空探':'虚无探索者','虚探':'虚无探索者'}
        component=SimpleNamespace()
        ui=magic_web.MagicWebUI(SimpleNamespace(context=SimpleNamespace(register_web_api=Mock())))
        body={'enabled':False,'bot_uin':'','profile':profile,'text':'重要 空探 S',
              'group':profile['rules'][0]['source_groups'][0]}
        with patch.object(magic_web,'request',SimpleNamespace(json=AsyncMock(return_value=body))):result=await ui.preview()
        self.assertEqual(result['status'],'ok',result)
        self.assertTrue(result['data']['messages'])
        output='\n'.join(m['text'] for m in result['data']['messages'])
        self.assertIn('虚无探索者',output);self.assertIn('西萨纳兰',output);self.assertNotIn('空探',output)

    async def test_all_regions_replies_are_globally_sorted_and_single_regions_stay_filtered(self):
        config=copy.deepcopy(DEFAULT_CONFIG);config['rules'][0].update(reply_template='{车表}',item_template='{时间} [{大区简称}-{服务器}]')
        feature=TrainFeature(SimpleNamespace(config={'train_commands':config}))
        def schedule(key,region,server,clock):
            return {'id':key,'version':'7.0','regionId':region,'serverId':server,'times':{'noon':clock}}
        feature.client.fetch=AsyncMock(return_value={'schedules':[schedule('a','bean','yixiujiade','10:00'),schedule('b','cat','yanyu','09:00'),schedule('c','bean','yixiujiade','11:00')]})
        now=datetime(2026,10,24,8,tzinfo=timezone(timedelta(hours=8))).timestamp()
        with patch('astrbot_plugin_bear_tracker.train_schedule.time.time',return_value=now):
            result=await feature.query('/车表 全部')
            self.assertEqual([r['时间'] for r in result['rows']],['09:00','10:00','11:00'])
            self.assertLess(result['text'].index('09:00'),result['text'].index('10:00'))
            self.assertNotIn('【',result['text'])
            single=await feature.query('/车表 狗');self.assertEqual(len(single['rows']),2)
            self.assertNotIn('09:00',single['text'])
