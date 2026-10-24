"""Operational contracts, using in-memory transports only; never sends to QQ."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from astrbot_plugin_bear_tracker.config_history import ConfigHistory, fingerprint
from astrbot_plugin_bear_tracker.delivery_journal import DeliveryJournal, DeliveryContext, DeliveryUncertain, SafeDeliveryFailure, delivery_scope, recorded_chain
from astrbot_plugin_bear_tracker.connection_supervisor import ConnectionSupervisor, DEFAULT, validate
from astrbot_plugin_bear_tracker.operations import Operations, settings
from astrbot_plugin_bear_tracker.quiet_hours import GuardedBot
from astrbot_plugin_bear_tracker.scoped_magic import ScopedMagicEngine
from astrbot_plugin_bear_tracker.components.magic_bucket.engine import MissingForwardMessageID
from astrbot.api.event import MessageChain

class Saved(dict):
    def save_config(self, values=None, **kw):
        self.update(copy.deepcopy(values or {}))
    async def save_config_async(self, values=None, **kw):
        self.save_config(values)
        return True

class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_saves_async_history_diff_and_rollback_preserve_modules(self):
        with tempfile.TemporaryDirectory() as root:
            cfg=Saved(enabled=True,magic_bucket={'rules':'unchanged'},message_template='old')
            audit=ConfigHistory(cfg,root);old=audit.entries()[0]['id']
            cfg.save_config({'message_template':'new'})
            await cfg.save_config_async({'enabled':False})
            self.assertEqual(len(audit.entries()),3)
            diff=audit.diff(old);self.assertEqual({c['path'] for c in diff['changes']},{'enabled','message_template'})
            audit.restore(old,diff['current']);self.assertEqual(cfg['message_template'],'old');self.assertTrue(cfg['enabled']);self.assertEqual(cfg['magic_bucket'],{'rules':'unchanged'})
            self.assertGreaterEqual(len(audit.entries()),4);audit.close()
    async def test_stale_restore_and_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            cfg=Saved(x=1);a=ConfigHistory(cfg,root);row=a.entries()[0]['id'];cfg.save_config({'x':2})
            with self.assertRaises(ValueError):a.restore(row,fingerprint({'x':1}))
            with self.assertRaises(ValueError):a.read('../config')
            a.close()
    async def test_bounded_history_and_redacted_diff(self):
        with tempfile.TemporaryDirectory() as root:
            cfg=Saved(token='private-old');a=ConfigHistory(cfg,root);old=a.entries()[0]['id'];cfg.save_config({'token':'private-new'})
            self.assertNotIn('private',json.dumps(a.diff(old)))
            for n in range(60):cfg.save_config({'number':n})
            self.assertEqual(len(a.entries()),50);a.close()

class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.bot=NS(_wsr_api_clients={'account':object()},api=NS(call_action=AsyncMock(return_value={'message_id':88})))
        self.platform=NS(meta=lambda:NS(id='bot',name='aiocqhttp'),get_client=lambda:self.bot)
        self.context=NS(platform_manager=NS(platform_insts=[self.platform]),send_message=AsyncMock(return_value=True))
        self.owner=NS(config={},client=NS(resources={'DatabaseHunt':{'test':{'Rank':3}},'DataCenters':{}},info=lambda k,d:{'名称':'虚无探索者'}))
        self.j=DeliveryJournal(self.owner,self.temp.name,self.context);self.addCleanup(self.j.close)
        self.text=[{'type':'text','data':{'text':'库啵，虚无探索者出现在[狗-伊修加德] 西萨纳兰 (18.4, 15.1)'}}]
        self.report={'hunt_key':'test','world':'伊修加德','instance':1,'position':[18.4,15.1]}
        self.action=AsyncMock(return_value={'message_id':88})
    async def send(self,feature='forwarding',key='message',segments=None,action=None,report=None):
        return await self.j.send('bot','10001',segments if segments is not None else self.text,action or self.action,feature,event_key=key,report=report,api=True)
    async def test_offline_is_definite_failure_before_transport(self):
        self.bot._wsr_api_clients={}
        with self.assertRaises(SafeDeliveryFailure):await self.send()
        self.action.assert_not_awaited();self.assertEqual(self.j.records()[0]['state'],'failed');self.assertTrue(self.j.records()[0]['can_retry'])
    async def test_unknown_result_never_replays_even_when_transport_recovers(self):
        result=await self.send(action=AsyncMock(side_effect=TimeoutError('uncertain')))
        self.assertTrue(result['_bear_unknown']);await self.send();self.action.assert_not_awaited()
        self.assertFalse(self.j.records()[0]['can_retry'])
    async def test_no_message_id_is_unknown_not_success(self):
        result=await self.send(action=AsyncMock(return_value={}))
        self.assertTrue(result['_bear_unknown']);self.assertEqual(self.j.records()[0]['state'],'unknown')
    async def test_definite_error_retries_at_most_three_times(self):
        action=AsyncMock(return_value={'status':'failed','retcode':1})
        for _ in range(5):
            with self.assertRaises(SafeDeliveryFailure):await self.send(action=action)
        self.assertEqual(action.await_count,3)
    async def test_same_report_from_different_feature_is_merged(self):
        await self.send('sightings','site',report=self.report)
        result=await self.send('collection','fill')
        self.assertTrue(result['_bear_merged']);self.assertEqual(self.action.await_count,1)
    async def test_new_coordinate_map_supplement_and_new_line_are_kept(self):
        await self.send('sightings','site',report=self.report)
        await self.send('collection','map',segments=self.text+[{'type':'image','data':{'file':'https://example.invalid/map.png'}}])
        await self.send('forwarding','updated',segments=[{'type':'text','data':{'text':self.text[0]['data']['text'].replace('18.4','18.5')}}])
        await self.send('forwarding','line',segments=[{'type':'text','data':{'text':self.text[0]['data']['text']+' 2线'}}])
        self.assertEqual(self.action.await_count,4)
    async def test_disable_dedup_keeps_cross_feature_reports(self):
        self.owner.config={'operations':{'dedup':{'enabled':False,'window_seconds':180}}}
        await self.send('sightings','site',report=self.report);await self.send('forwarding','source');self.assertEqual(self.action.await_count,2)
    async def test_ambiguous_identity_ss_and_multiple_coordinates_not_deduplicated(self):
        for text in ['SS 虚无探索者 伊修加德 (18.4, 15.1)','虚无探索者 伊修加德 水晶塔 (18.4, 15.1)','虚无探索者 伊修加德 (18.4, 15.1) (20, 20)']:
            self.assertEqual(self.j.semantic(text),'')
    async def test_same_identity_concurrent_sources_only_send_once(self):
        await asyncio.gather(self.send('sightings','a',report=self.report),self.send('forwarding','b'))
        self.assertEqual(self.action.await_count,1)
    async def test_scope_and_native_chain_recording(self):
        proxy=DeliveryContext(self.context,self.j)
        await recorded_chain(proxy,'bot:GroupMessage:10001',MessageChain().message('train'),feature='trains',event_key='train',expires_at=time.time()+30)
        row=self.j.records()[0];self.assertEqual(row['feature'],'trains');self.assertEqual(row['state'],'accepted')
    async def test_images_snapshotted_and_rows_survive_reload(self):
        image=Path(self.temp.name)/'map.png';image.write_bytes(b'map-image')
        await self.send(segments=self.text+[{'type':'image','data':{'file':str(image)}}]);image.unlink()
        saved=json.loads(self.j.query('SELECT payload FROM deliveries')[0]['payload']);self.assertEqual(Path(saved[1]['data']['file']).read_bytes(),b'map-image')
        self.j.execute("UPDATE deliveries SET state='sending'")
        other=DeliveryJournal(self.owner,self.temp.name,self.context)
        try:self.assertEqual(other.records()[0]['state'],'unknown')
        finally:other.close()
    async def test_invalid_image_snapshot_does_not_stop_normal_send(self):
        await self.send(segments=[{'type':'image','data':{'file':'base64://INVALID'}}])
        self.action.assert_awaited_once();self.assertFalse(self.j.records()[0]['can_retry'])
    async def test_guarded_legacy_api_unknown_stops_core_retry_loop(self):
        wrapped=GuardedBot(self.bot,{},self.j,'bot');self.bot.api.call_action=AsyncMock(side_effect=TimeoutError('unknown'))
        from astrbot_plugin_bear_tracker.components.magic_bucket.adapter import OneBotAPI
        engine=ScopedMagicEngine({},OneBotAPI(wrapped),{});engine._last_action_time=None;engine._action_lock=asyncio.Lock()
        with delivery_scope('forwarding','one'):
            with self.assertRaises(MissingForwardMessageID):await engine._send_segments(10001,self.text,'rule','text',2)
        self.bot.api.call_action.assert_awaited_once()
    async def test_retry_guard_honors_quiet_window_and_feature_source_group(self):
        operations=object.__new__(Operations);operations.owner=NS(config={'enabled':True,'automation_platform_id':'bot','feature_controls':{'forwarding':{'enabled':True,'groups':['20001']}},'magic_bucket':{'rules_json':json.dumps([{'name':'x','type':'keyword','keywords':['重要'],'source_groups':[20001],'target_groups':[10001]}])}})
        row={'feature':'forwarding','platform':'bot','target':'10001'};meta={'expires_at':time.time()+30,'source_group':'20001','source_text':'重要 S'}
        operations.retry_guard(row,meta)
        operations.owner.config['broadcast_window']={'start':'00:00','end':'00:01'}
        with self.assertRaises(ValueError):operations.retry_guard(row,meta)
    async def test_unknown_record_retry_is_rejected_without_transport(self):
        operations=object.__new__(Operations);operations.delivery=self.j
        await self.send(action=AsyncMock(side_effect=TimeoutError()))
        rid=self.j.records()[0]['id']
        with patch('astrbot_plugin_bear_tracker.operations.request',NS(json=AsyncMock(return_value={'id':rid}))):
            result=await operations.retry()
        self.assertEqual(result['status'],'error');self.action.assert_not_awaited()

    async def test_definite_failure_retry_gets_new_receipt_and_blocks_old_queue(self):
        self.bot._wsr_api_clients={}
        with delivery_scope('manual','request'):
            with self.assertRaises(SafeDeliveryFailure):await self.send('manual')
        self.bot._wsr_api_clients={'account':object()}
        row=self.j.records()[0]
        ops=object.__new__(Operations);ops.delivery=self.j;ops.jobs=set()
        ops.owner=NS(config={},components=NS(manual=NS(webui=NS(group_list=AsyncMock(return_value=[{'id':'10001'}])))))
        with patch('astrbot_plugin_bear_tracker.operations.request',NS(json=AsyncMock(return_value={'id':row['id']}))):
            result=await ops.retry()
        self.assertEqual(result['status'],'ok')
        self.bot.api.call_action.assert_awaited_once()
        rows=self.j.records();self.assertIn('retried',{r['state'] for r in rows});self.assertIn('sent',{r['state'] for r in rows})
        # Original queued event must not send again after the successful retry.
        with delivery_scope('manual','request'):await self.send('manual')
        self.action.assert_not_awaited()

    async def test_manual_retry_rechecks_group_membership_before_transport(self):
        self.bot._wsr_api_clients={}
        with self.assertRaises(SafeDeliveryFailure):await self.send('manual')
        self.bot._wsr_api_clients={'account':object()}
        ops=object.__new__(Operations);ops.delivery=self.j;ops.jobs=set();ops.owner=NS(config={},components=NS(manual=NS(webui=NS(group_list=AsyncMock(return_value=[])))))
        with patch('astrbot_plugin_bear_tracker.operations.request',NS(json=AsyncMock(return_value={'id':self.j.records()[0]['id']}))):result=await ops.retry()
        self.assertEqual(result['status'],'error');self.bot.api.call_action.assert_not_awaited()

class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_cooldown_process_limit_and_reload_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            cfg={**DEFAULT,'enabled':True,'launcher':'C:/NapCatWinBootMain.exe','account':'10001'}
            owner=NS(config={'operations':{'watchdog':cfg}},alerts=NS(data_dir=root),webui=NS(bot_connection=lambda:{'connected':False,'platform_id':'bot'}))
            watchdog=ConnectionSupervisor(owner);watchdog.port_open=AsyncMock(return_value=False)
            with patch('astrbot_plugin_bear_tracker.connection_supervisor.validate',lambda c:c),patch('astrbot_plugin_bear_tracker.connection_supervisor.subprocess.Popen',return_value=NS(poll=lambda:0)) as popen,patch('astrbot_plugin_bear_tracker.connection_supervisor.subprocess.CREATE_NO_WINDOW',0,create=True):
                now=time.time();await watchdog.tick(now);await watchdog.tick(now+15);popen.assert_not_called();await watchdog.tick(now+30);self.assertEqual(popen.call_count,1)
                await watchdog.tick(now+45);self.assertEqual(popen.call_count,1)
                recovered=ConnectionSupervisor(owner);self.assertEqual(len(recovered.attempts),1)
                watchdog.port_open=AsyncMock(return_value=True);await watchdog.tick(now+500);self.assertEqual(popen.call_count,1)
    async def test_healthy_connection_and_disabled_guard_never_start(self):
        with tempfile.TemporaryDirectory() as root:
            owner=NS(config={},alerts=NS(data_dir=root),webui=NS(bot_connection=lambda:{'connected':True}))
            guard=ConnectionSupervisor(owner)
            with patch('astrbot_plugin_bear_tracker.connection_supervisor.subprocess.Popen') as popen:
                for _ in range(5):await guard.tick()
                popen.assert_not_called()
    async def test_arbitrary_launchers_and_invalid_settings_rejected(self):
        with self.assertRaises(ValueError):validate({**DEFAULT,'enabled':True,'launcher':'C:/Windows/System32/cmd.exe','account':'10001'})
        with self.assertRaises(ValueError):settings({'dedup':{'enabled':True,'window_seconds':0},'watchdog':DEFAULT})

class FrameworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_config_history_preserves_real_save_contract(self):
        from astrbot.api import AstrBotConfig
        with tempfile.TemporaryDirectory() as root:
            cfg=AstrBotConfig(str(Path(root)/'config.json'),default_config={'enabled':True,'custom':{'keep':1}})
            audit=ConfigHistory(cfg,root);old=audit.entries()[0]['id']
            cfg.save_config({'enabled':False});await cfg.save_config_async({'extra':2})
            audit.restore(old,fingerprint(dict(cfg)));audit.close()
            self.assertEqual(json.loads((Path(root)/'config.json').read_text('utf-8-sig')),{'enabled':True,'custom':{'keep':1}})

class TimerCompressionTests(unittest.TestCase):
    def test_minifier_renaming_is_accepted_without_loosening_logic_or_properties(self):
        from astrbot_plugin_bear_tracker.static_literals import minified_timer_digest as digest
        a='(function(t,n){const r=n();return {line:t,best:r}})(window,function(){return 1})'
        b='(function(e,o){const i=o();return {line:e,best:i}})(window,function(){return 1})'
        self.assertEqual(digest(a),digest(b))
        for changed in [b.replace('return 1','return 2'),b.replace('line:e','lines:e'),b.replace('best:i','best:e'),b.replace('window','globalThis')]:self.assertNotEqual(digest(a),digest(changed))

class OutputMigrationTests(unittest.TestCase):
    def test_external_output_is_copied_into_plugin_data_without_changing_original(self):
        from astrbot_plugin_bear_tracker.components.magic_bucket.runtime_paths import output_path
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);data=root/'plugin-data';data.mkdir();external=root/'old-state.json';external.write_text('{"keep":1}','utf-8')
            safe=Path(output_path(data,str(external),'state.json'))
            self.assertTrue(safe.is_relative_to(data));self.assertEqual(safe.read_bytes(),external.read_bytes())
            safe.write_text('{"keep":2}','utf-8')
            self.assertEqual(json.loads(external.read_text('utf-8')),{'keep':1})
            self.assertEqual(Path(output_path(data,str(external),'state.json')).read_text('utf-8'),'{"keep":2}')
