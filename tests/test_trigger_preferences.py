"""Contracts for configurable cadence and delay measured from server gate time."""
import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from astrbot_plugin_bear_tracker.trigger_source import eligible_events, DEFAULT_TRIGGER_HUNTS
from astrbot_plugin_bear_tracker.trigger_preferences import validate_interval, validate_delays, delay_ready
from astrbot_plugin_bear_tracker.trigger_monitor import TriggerMonitor
from astrbot_plugin_bear_tracker.webui import BearWebUI, validate_settings
from astrbot_plugin_bear_tracker.tests.test_trigger_monitor import NOW, WORLD, DC, GROUP, DEFINITION, snapshot, event

class PreferencesTests(unittest.TestCase):
    def test_interval_bounds_and_finite_per_hunt_hours(self):
        for seconds in [20,60,300,3600]:self.assertEqual(validate_interval(seconds),seconds)
        for bad in [True,19,3601,60.5,'60',None]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):validate_interval(bad)
        self.assertEqual(validate_delays({'Bonnacon':0.5},DEFAULT_TRIGGER_HUNTS),{'Bonnacon':0.5})
        for bad in [{'unknown':1},{'Bonnacon':True},{'Bonnacon':-1},{'Bonnacon':169},{'Bonnacon':float('nan')},{'Bonnacon':float('inf')},[],None]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):validate_delays(bad,DEFAULT_TRIGGER_HUNTS)
        self.assertEqual(validate_settings({'trigger_check_interval_seconds':120,'trigger_delay_hours':{'Bonnacon':2}}),{'trigger_check_interval_seconds':120,'trigger_delay_hours':{'Bonnacon':2.0}})

    def test_delay_uses_gate_start_not_death_or_weather_start(self):
        opened=NOW-3600
        result=eligible_events(snapshot(),DC,[WORLD],DEFINITION,[],[],NOW,{'Bonnacon':1})
        self.assertEqual(result[0]['gate_open_at'],opened)
        self.assertTrue(delay_ready(result[0],1,NOW))
        self.assertFalse(delay_ready(result[0],1,NOW-0.01))
        self.assertEqual(eligible_events(snapshot(),DC,[WORLD],DEFINITION,[],[],NOW,{'Bonnacon':1.001}),[])
        self.assertEqual(eligible_events(snapshot(),DC,[WORLD],DEFINITION,[],[],NOW,{'Bonnacon':0}),eligible_events(snapshot(),DC,[WORLD],DEFINITION,[],[],NOW))

    def test_wait_completion_still_requires_weather_time_and_nonforced_cd(self):
        outside=NOW+2*175
        self.assertEqual(eligible_events(snapshot(outside,death=NOW-66*3600),DC,[WORLD],DEFINITION,[],[],outside,{'Bonnacon':0.5}),[])
        self.assertEqual(eligible_events(snapshot(death=NOW-75*3600),DC,[WORLD],DEFINITION,[],[],NOW,{'Bonnacon':1}),[])
        self.assertEqual(eligible_events(snapshot(),DC,[WORLD],DEFINITION,[],[],NOW,{'Bonnacon':168}),[])

    def test_maintenance_cd_and_multiline_gate_are_calculated_separately(self):
        data=snapshot();data['live'][WORLD]['Bonnacon']=[[1,NOW-40*3600,True]]
        result=eligible_events(data,DC,[WORLD],DEFINITION,[],[],NOW,{'Bonnacon':1})
        self.assertEqual(result[0]['gate_open_at'],NOW-3600)
        definition={'Bonnacon':{**DEFINITION['Bonnacon'],'ln':2}}
        data['live'][WORLD]['Bonnacon']=[[1,NOW-65.5*3600,False],[2,NOW-66.5*3600,False]]
        result=eligible_events(data,DC,[WORLD],definition,[],[],NOW,{'Bonnacon':1})
        self.assertEqual(result[0]['instance'],2)

class DelayedMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config={'enabled':True,'trigger_alerts_enabled':True,'trigger_delay_hours':{'Bonnacon':1.05}}
        self.context=NS(send_message=AsyncMock(return_value=True))
        self.alerts=NS(config=self.config,data_dir=Path(self.temp.name),context=self.context,destinations=lambda:{GROUP:{'since':NOW-10}},accepts=lambda e:True,max_age=300,template_values=lambda e:{'等级':'50','怪名':'火愤牛','大区':'豆豆柴','大区简称':'狗','服务器':e['world'],'地图':'','分线':''})
        self.monitor=TriggerMonitor(self.alerts)
        self.clock=patch('astrbot_plugin_bear_tracker.trigger_monitor.time.time',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)

    def delayed_events(self,now=NOW):
        return eligible_events(snapshot(now,death=NOW-66*3600),DC,[WORLD],DEFINITION,[],[],now,self.config['trigger_delay_hours'])

    async def test_reaches_delay_within_same_condition_window_and_sends_once(self):
        self.monitor.apply_snapshot(self.delayed_events(),{WORLD},{WORLD},NOW)
        await self.monitor.deliver({WORLD},NOW);self.context.send_message.assert_not_awaited()
        later=NOW+180
        self.clock.stop();self.clock=patch('astrbot_plugin_bear_tracker.trigger_monitor.time.time',return_value=later);self.clock.start();self.addCleanup(self.clock.stop)
        self.monitor.apply_snapshot(self.delayed_events(later),{WORLD},{WORLD},later)
        await self.monitor.deliver({WORLD},later);self.context.send_message.assert_awaited_once()
        restarted=TriggerMonitor(self.alerts);restarted.apply_snapshot(self.delayed_events(later),{WORLD},{WORLD},later)
        await restarted.deliver({WORLD},later);self.context.send_message.assert_awaited_once()

    async def test_pending_and_batched_server_list_rechecks_each_servers_delay(self):
        self.config['trigger_delay_hours']={'Bonnacon':1}
        first=event();second={**first,'world':'水晶塔','event_key':'second','gate_open_at':NOW-1800}
        worlds={WORLD,'水晶塔'}
        self.monitor.apply_snapshot([],worlds,worlds,NOW);self.monitor.apply_snapshot([first,second],worlds,worlds,NOW)
        await self.monitor.deliver(worlds,NOW)
        text=self.context.send_message.await_args.args[1].chain[0].text
        self.assertIn(WORLD,text);self.assertNotIn('水晶塔',text)

    async def test_changed_wait_is_baselined_without_replaying_current_window(self):
        self.config['trigger_delay_hours']={'Bonnacon':0}
        self.monitor.apply_snapshot([],{WORLD},{WORLD},NOW)
        self.monitor.apply_snapshot([event()],{WORLD},{WORLD},NOW)
        self.monitor.timing_changed(['Bonnacon'])
        self.monitor.apply_snapshot([event()],{WORLD},{WORLD},NOW)
        await self.monitor.deliver({WORLD},NOW);self.context.send_message.assert_not_awaited()
        newer={**event(),'event_key':'new-window','window_start':event()['window_start']+1}
        self.monitor.apply_snapshot([newer],{WORLD},{WORLD},NOW)
        await self.monitor.deliver({WORLD},NOW);self.context.send_message.assert_awaited_once()

    async def test_old_cached_event_without_gate_time_cannot_bypass_new_wait(self):
        old=event();old.pop('gate_open_at')
        self.monitor.apply_snapshot([],{WORLD},{WORLD},NOW);self.monitor.apply_snapshot([old],{WORLD},{WORLD},NOW)
        await self.monitor.deliver({WORLD},NOW);self.context.send_message.assert_not_awaited()

    async def test_configured_cadence_wakes_when_settings_change(self):
        self.config['trigger_check_interval_seconds']=3600
        self.assertEqual(self.monitor.interval(),3600)
        called=asyncio.Event();count=0
        async def tick():
            nonlocal count
            count+=1;called.set()
        self.monitor.tick=tick;self.monitor.start()
        try:
            await asyncio.wait_for(called.wait(),1);called.clear()
            self.config['trigger_check_interval_seconds']=120;self.monitor.timing_changed()
            await asyncio.wait_for(called.wait(),1)
            self.assertEqual(count,2);self.assertEqual(self.monitor.interval(),120)
        finally:await self.monitor.close()

class SavePreferencesTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_hunt_change_preserves_other_hunts_and_existing_settings(self):
        with tempfile.TemporaryDirectory() as root:
            config={'trigger_delay_hours':{'Bonnacon':2,'Gamma':3},'message_template':'keep','listen_scopes':[{'data_center':'豆豆柴','servers':[]}]}
            changed=Mock();plugin=NS(config=config,context=NS(register_web_api=Mock()),alerts=NS(data_dir=Path(root)),triggers=NS(timing_changed=changed))
            ui=BearWebUI(plugin)
            with patch('astrbot_plugin_bear_tracker.webui.request',NS(json=AsyncMock(return_value={'trigger_delay_hours':{'Bonnacon':0.5},'trigger_check_interval_seconds':120}))):result=await ui.settings()
            self.assertEqual(result['status'],'ok');self.assertEqual(config['trigger_delay_hours'],{'Bonnacon':0.5,'Gamma':3});self.assertEqual(config['message_template'],'keep');self.assertEqual(config['trigger_check_interval_seconds'],120)
            changed.assert_called_once_with(['Bonnacon'])

    async def test_invalid_timing_payload_is_atomic_and_does_not_wake(self):
        config={'trigger_delay_hours':{'Bonnacon':2}};before=copy.deepcopy(config);changed=Mock()
        plugin=NS(config=config,context=NS(register_web_api=Mock()),triggers=NS(timing_changed=changed))
        ui=BearWebUI(plugin)
        with patch('astrbot_plugin_bear_tracker.webui.request',NS(json=AsyncMock(return_value={'trigger_delay_hours':{'Bonnacon':-1},'trigger_check_interval_seconds':120}))):result=await ui.settings()
        self.assertEqual(result['status'],'error');self.assertEqual(config,before);changed.assert_not_called()

class CooldownDisplayTests(unittest.TestCase):
    def test_reference_duration_is_added_to_minimum_instead_of_displayed_as_maximum(self):
        from astrbot_plugin_bear_tracker.trigger_source import cooldown_details
        result=cooldown_details('Gandarewa')
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['normal'],[84,132])
        self.assertEqual(result[0]['maintenance'],[50.4,79.2])
        self.assertEqual(set(result[0]['data_centers']),{'豆豆柴','莫古力'})

    def test_live_center_variations_stay_separate_and_override_bundled_display(self):
        from astrbot_plugin_bear_tracker.trigger_source import cooldown_details
        metadata={'豆豆柴':{'Gamma':{'rt':[80,40],'mt':[48,24]}},'莫古力':{'Gamma':{'rt':[84,48],'mt':[50.4,28.8]}}}
        rows=cooldown_details('Gamma',metadata)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0],{'data_centers':['豆豆柴'],'normal':[80,120],'maintenance':[48,72]})
        self.assertEqual(rows[1]['normal'],[84,132])

    def test_missing_and_invalid_cd_is_not_invented(self):
        from astrbot_plugin_bear_tracker.trigger_source import cooldown_details, DEFAULT_TRIGGER_HUNTS
        self.assertTrue(all(cooldown_details(key) for key in DEFAULT_TRIGGER_HUNTS))
        self.assertEqual(cooldown_details('Gamma',{'豆豆柴':{'Gamma':{'rt':[84,-1],'mt':[float('nan'),1]}}}),[])
