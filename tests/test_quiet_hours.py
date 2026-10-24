"""Shared window boundaries and final-send checks; no real messages or network."""
import asyncio,copy,time
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch
from astrbot_plugin_bear_tracker.quiet_hours import BEIJING,delivery_allowed,validate_window,QuietHours,GuardedBot
from astrbot_plugin_bear_tracker.component_host import MergedComponents
from astrbot_plugin_bear_tracker.webui import BearWebUI,validate_settings
from astrbot_plugin_bear_tracker.train_schedule import TrainFeature,DEFAULT_CONFIG
import test_alert_engine as alerts_fixture
import test_trigger_monitor as trigger_fixture


def stamp(hour,minute=0,second=0):return datetime(2026,10,24,hour,minute,second,tzinfo=BEIJING).timestamp()
def quiet_config(now=None):
    current=datetime.fromtimestamp(time.time() if now is None else now,BEIJING)
    minutes=current.hour*60+current.minute
    def text(value):return f'{value//60:02}:{value%60:02}'
    return {'start':text((minutes+120)%1440),'end':text((minutes+180)%1440)}


class WindowTests(unittest.TestCase):
    def test_normal_window_is_start_inclusive_end_exclusive_in_beijing_time(self):
        config={'broadcast_window':{'start':'08:30','end':'22:00'}}
        for now,allowed in [(stamp(8,29,59),False),(stamp(8,30),True),(stamp(21,59,59),True),(stamp(22),False)]:
            self.assertEqual(delivery_allowed(config,now),allowed)

    def test_cross_midnight_and_equal_times(self):
        config={'broadcast_window':{'start':'22:00','end':'06:30'}}
        for hour,minute,allowed in [(21,59,False),(22,0,True),(0,0,True),(6,29,True),(6,30,False)]:
            self.assertEqual(delivery_allowed(config,stamp(hour,minute)),allowed)
        for hour in range(24):
            self.assertTrue(delivery_allowed({},stamp(hour)))
            self.assertTrue(delivery_allowed({'broadcast_window':{'start':'08:30','end':'08:30'}},stamp(hour)))

    def test_invalid_window_fails_closed_and_cannot_be_saved(self):
        for value in [None,'08:00-22:00',{}, {'start':'24:00','end':'08:00'}, {'start':'8:00','end':'22:00'}, {'start':'08:00','end':'22:60'}]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):validate_window(value)
                self.assertFalse(delivery_allowed({'broadcast_window':value}))
        for value in [-1,True,60001,'500']:
            with self.assertRaises(ValueError):validate_settings({'forward_interval_ms':value})


class SendGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_magic_transport_blocks_all_send_actions_but_allows_reads_and_recall(self):
        config={'broadcast_window':quiet_config()};call=AsyncMock(return_value={'message_id':1})
        bot=GuardedBot(SimpleNamespace(api=SimpleNamespace(call_action=call)),config)
        for action in ('send_group_msg','send_private_msg','send_group_forward_msg'):
            with self.assertRaises(QuietHours):await bot.api.call_action(action)
        call.assert_not_awaited()
        await bot.api.call_action('get_group_list');await bot.api.call_action('delete_msg',message_id=1)
        self.assertEqual(call.await_count,2)

    def merged(self,config):
        routes={}
        context=SimpleNamespace(register_web_api=lambda route,handler,*args:routes.update({route:handler}))
        plugin=SimpleNamespace(config=config,context=context)
        merged=MergedComponents(plugin);self.addAsyncCleanup(merged.close)
        return plugin,merged,routes

    async def test_manual_send_endpoint_and_automatic_forwarding_stay_silent(self):
        config={'enabled':True,'automation_platform_id':'bot','broadcast_window':quiet_config()}
        plugin,merged,routes=self.merged(config)
        merged.magic.on_group=AsyncMock()
        await merged.on_group(SimpleNamespace(get_platform_id=lambda:'bot'))
        merged.magic.on_group.assert_not_awaited()
        result=await routes['/astrbot_plugin_bear_tracker/webui/hunhuan_forward/send']()
        self.assertEqual(result['status'],'error');self.assertIn('静默',result['message'])
        self.assertEqual(merged.manual.webui.jobs,{})

    async def test_queued_manual_send_rechecks_after_its_rate_limit_lock(self):
        config={};plugin,merged,_=self.merged(config)
        lock=merged.manual.engine.lock;await lock.acquire()
        action=AsyncMock()
        task=asyncio.create_task(merged.manual.engine.limited(action,'send_group_msg',group_id=1))
        await asyncio.sleep(0)
        config['broadcast_window']=quiet_config();lock.release()
        with self.assertRaises(QuietHours):await task
        action.assert_not_awaited()
        await merged.manual.engine.limited(action,'delete_msg',message_id=1)
        action.assert_awaited_once()

    async def test_s_reports_received_while_quiet_are_not_replayed_later(self):
        fixture=alerts_fixture.AlertEngineTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.config['broadcast_window']=quiet_config(alerts_fixture.NOW)
        report=alerts_fixture.sighting();fixture.engine.enqueue(report)
        self.assertTrue(fixture.engine.recent);self.assertFalse(fixture.engine.pending)
        fixture.config.pop('broadcast_window');fixture.engine.enqueue(report);await fixture.run_pass()
        fixture.context.send_message.assert_not_awaited()
        fixture.engine.enqueue(alerts_fixture.sighting(spawn_key='next-spawn'));await fixture.run_pass()
        fixture.context.send_message.assert_awaited_once()

    async def test_s_report_render_crossing_into_quiet_time_cannot_send(self):
        fixture=alerts_fixture.AlertEngineTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.engine.enqueue(alerts_fixture.sighting())
        def render(*args,**kwargs):
            fixture.config['broadcast_window']=quiet_config(alerts_fixture.NOW)
            return fixture.root/'pin.png'
        fixture.renderer.render.side_effect=render
        await fixture.run_pass();fixture.context.send_message.assert_not_awaited()
        self.assertFalse(fixture.engine.pending)

    async def test_trigger_opportunity_muted_in_window_does_not_burst_when_open(self):
        fixture=trigger_fixture.MonitorTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        now=trigger_fixture.NOW;world=trigger_fixture.WORLD;e=trigger_fixture.event()
        fixture.monitor.apply_snapshot([],{world},{world},now)
        fixture.monitor.apply_snapshot([e],{world},{world},now)
        fixture.config['broadcast_window']=quiet_config(now)
        await fixture.monitor.deliver({world},now)
        fixture.config.pop('broadcast_window')
        fixture.monitor.apply_snapshot([e],{world},{world},now);await fixture.monitor.deliver({world},now)
        fixture.context.send_message.assert_not_awaited()

    def event(self):
        return SimpleNamespace(get_message_str=lambda:'/车表 狗',message_obj=SimpleNamespace(raw_message={}),
                               stop_event=Mock(),send=AsyncMock())

    async def test_train_commands_are_silent_but_webui_preview_remains_available(self):
        config={'broadcast_window':quiet_config(),'train_commands':copy.deepcopy(DEFAULT_CONFIG)}
        config['train_commands']['rules'][0].update(mode='text',reply_template='测试预览')
        feature=TrainFeature(SimpleNamespace(config=config));feature.client.fetch=AsyncMock()
        event=self.event();await feature.on_event(event)
        event.stop_event.assert_called_once();event.send.assert_not_awaited();feature.client.fetch.assert_not_awaited()
        self.assertEqual((await feature.query('/车表'))['text'],'测试预览')

    async def test_multi_part_train_reply_stops_if_window_closes_between_parts(self):
        config={};feature=TrainFeature(SimpleNamespace(config=config))
        feature.query=AsyncMock(return_value={'parts':['first','second']})
        event=self.event()
        async def first(chain):config['broadcast_window']=quiet_config()
        event.send.side_effect=first
        await feature.on_event(event);event.send.assert_awaited_once()

    async def test_shared_interval_and_window_save_atomically_without_overwriting_rules(self):
        plugin,merged,_=self.merged({'enabled':True,'magic_bucket':{'send_interval_ms':500,'rules_json':'preserved'}})
        plugin.alerts=SimpleNamespace(silence_pending=Mock(),changed=SimpleNamespace(set=Mock()))
        merged.magic.reset_engines=AsyncMock();plugin.components=merged
        ui=BearWebUI(plugin)
        body={'forward_interval_ms':750,'broadcast_window':quiet_config()}
        with patch('astrbot_plugin_bear_tracker.webui.request',SimpleNamespace(json=AsyncMock(return_value=body))):result=await ui.settings()
        self.assertEqual(result['status'],'ok',result)
        self.assertEqual(plugin.config['magic_bucket'],{'send_interval_ms':750,'rules_json':'preserved'})
        merged.magic.reset_engines.assert_awaited_once();plugin.alerts.silence_pending.assert_called_once()
