import asyncio
import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch
from PIL import Image
from astrbot.api.message_components import Plain,Image as MessageImage
from astrbot_plugin_bear_tracker.train_schedule import TrainFeature,DEFAULT_CONFIG as COMMANDS,TZ,flatten_schedules
from astrbot_plugin_bear_tracker.train_hunt import HuntTrainFeature,DEFAULT_CONFIG,DEFAULT_TEMPLATE,validate_config,calendar_allowed
from astrbot_plugin_bear_tracker.train_cards import route_sections,render_recent,render_route
from astrbot_plugin_bear_tracker.train_webui import TrainWebUI

NOW=datetime(2026,10,24,22,30,tzinfo=TZ).timestamp()

def schedule(**changes):
    return {'id':'test-train','regionId':'moogle','serverId':'shenchuanmo','version':'7.0',
            'times':{'evening':'22:40'},'pausedSlots':{},'slotNotices':{},'startStation':'奥山瓦丘',
            'startCrystal':'','routeText':'','notice':'','driver':'湿宝',**changes}


class ConfigTests(unittest.TestCase):
    def test_template_and_command_validation_no_manual_leader_setting(self):
        config=validate_config({},['发车','车表'])
        self.assertEqual(config['minutes'],10);self.assertNotIn('leaders',config)
        for update in [{'image_commands':['车表']},{'route_commands':['车图']},{'groups':['bot:GroupMessage:1']},{'minutes':0},{'template':'{服务器.__class__}'},{'leaders':{'x':'湿宝'}}]:
            with self.subTest(update=update),self.assertRaises(ValueError):validate_config({**DEFAULT_CONFIG,**update},['发车','车表'])

    def test_weekend_restrictions_apply_to_departure_date(self):
        row={'departure_at':NOW,'备注':'周六周日限定','班次备注':''}
        self.assertTrue(calendar_allowed(row));row['departure_at']=datetime(2026,10,26,22,40,tzinfo=TZ).timestamp()
        self.assertFalse(calendar_allowed(row));row['备注']='仅工作日';self.assertTrue(calendar_allowed(row))


class HuntTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config={'train_commands':copy.deepcopy(COMMANDS),'automation_platform_id':'bot',
                     'train_hunt':{**copy.deepcopy(DEFAULT_CONFIG),'centers':['moogle'],'groups':['200']}}
        self.plugin=SimpleNamespace(config=self.config,alerts=SimpleNamespace(data_dir=Path(self.temp.name)),
            context=SimpleNamespace(send_message=AsyncMock(return_value=True),register_web_api=Mock()))
        self.plugin.trains=TrainFeature(self.plugin);self.hunt=HuntTrainFeature(self.plugin);self.plugin.train_hunt=self.hunt
        self.plugin.trains.client.fetch=AsyncMock(return_value={'schedules':[schedule()]})
        self.clock=patch('astrbot_plugin_bear_tracker.train_hunt.time.time',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)
        self.addAsyncCleanup(self.hunt.close)

    async def test_reminder_is_dynamic_plain_text_exact_format_and_once_per_group(self):
        await self.hunt.tick();await self.hunt.tick()
        self.plugin.context.send_message.assert_awaited_once()
        origin,chain=self.plugin.context.send_message.await_args.args
        self.assertEqual(origin,'bot:GroupMessage:200');self.assertTrue(all(isinstance(c,Plain) for c in chain.chain))
        self.assertEqual(chain.chain[0].text,'【狩猎车提醒】\n服务器：神拳痕\n版本：7A\n时间：22:40\n始发地：奥山瓦丘\n车头：湿宝\n距离发车还有 10 分钟！')
        restored=HuntTrainFeature(self.plugin);await restored.tick();self.assertEqual(self.plugin.context.send_message.await_count,1)

    async def test_missing_source_driver_is_not_taken_from_editor_or_hardcoded(self):
        self.plugin.trains.client.fetch.return_value={'schedules':[schedule(driver=None,updatedBy='站长')]}
        row=(await self.hunt.rows())[0];self.assertEqual(row['车头'],'待公布')
        self.assertIn('车头：待公布',self.hunt.message(row))

    async def test_paused_expired_filtered_disabled_and_unconfigured_targets_never_send(self):
        for change in [{'groups':[]},{'enabled':False},{'centers':['bean']},{'servers':['水晶塔']}]:
            with self.subTest(change=change):
                self.config['train_hunt']={**copy.deepcopy(DEFAULT_CONFIG),'groups':['200'],'centers':['moogle'],**change}
                await self.hunt.tick()
        self.config['train_hunt']={**DEFAULT_CONFIG,'groups':['200'],'centers':['moogle']}
        self.plugin.trains.client.fetch.return_value={'schedules':[schedule(pausedSlots={'evening':True})]};await self.hunt.tick()
        self.plugin.trains.client.fetch.return_value={'schedules':[schedule(times={'evening':'22:20'})]};await self.hunt.tick()
        self.plugin.context.send_message.assert_not_awaited()

    async def test_quiet_period_suppresses_reminder_without_replaying_on_resume(self):
        self.config['broadcast_window']={'start':'08:00','end':'22:00'};await self.hunt.tick()
        self.config['broadcast_window']={'start':'00:00','end':'00:00'};await self.hunt.tick()
        self.plugin.context.send_message.assert_not_awaited()

    async def test_feature_group_scope_and_native_master_stop_auto_reminders(self):
        self.config['feature_controls']={'trains':{'enabled':True,'groups':['999']}}
        await self.hunt.tick();self.plugin.context.send_message.assert_not_awaited()
        self.config['train_commands']['enabled']=False;await self.hunt.tick();self.plugin.context.send_message.assert_not_awaited()

    async def test_live_config_changed_during_fetch_is_rechecked_before_send(self):
        async def fetch(*args,**kwargs):
            self.config['train_hunt']['groups']=[]
            return {'schedules':[schedule()]}
        self.plugin.trains.client.fetch.side_effect=fetch;await self.hunt.tick();self.plugin.context.send_message.assert_not_awaited()

    async def test_failed_or_unknown_delivery_is_not_blindly_replayed(self):
        self.plugin.context.send_message.side_effect=RuntimeError('protocol offline')
        await self.hunt.tick();await self.hunt.tick()
        self.assertEqual(self.plugin.context.send_message.await_count,1);self.assertIn('核对群消息',self.hunt.last_error or '') if self.hunt.last_error else None

    async def test_source_failure_never_sends_invented_reminders(self):
        self.plugin.trains.client.fetch.side_effect=ValueError('unavailable')
        with self.assertRaises(ValueError):await self.hunt.tick()
        self.plugin.context.send_message.assert_not_awaited()

    async def test_shared_route_json_is_read_completely_across_network_chunks(self):
        data=json.dumps({'shared':{'points':{},'lines':{},'route':[]}},ensure_ascii=False).encode()
        async def chunks(size):
            for offset in range(0,len(data),7):yield data[offset:offset+7]
        response=SimpleNamespace(raise_for_status=Mock(),content=SimpleNamespace(iter_chunked=chunks))
        request=Mock();request.__aenter__=AsyncMock(return_value=response);request.__aexit__=AsyncMock(return_value=False)
        session=Mock();session.get.return_value=request
        manager=Mock();manager.__aenter__=AsyncMock(return_value=session);manager.__aexit__=AsyncMock(return_value=False)
        with patch('astrbot_plugin_bear_tracker.train_hunt.aiohttp.ClientSession',return_value=manager):
            self.assertEqual((await self.hunt.route_snapshot())['shared']['points'],{})

    async def test_stable_numbers_survive_source_reordering_and_restart(self):
        first=await self.hunt.rows();number=first[0]['编号']
        self.plugin.trains.client.fetch.return_value={'schedules':[schedule(id='other'),schedule()]}
        restored=HuntTrainFeature(self.plugin);rows=await restored.rows()
        self.assertEqual(next(r for r in rows if r['source_id']=='test-train')['编号'],number)

    async def test_image_commands_send_one_image_in_same_conversation_and_text_queries_stay_plain(self):
        self.config['train_hunt']['default_center']='moogle'
        event=SimpleNamespace(get_group_id=lambda:'200',get_sender_id=lambda:'1',get_self_id=lambda:'2',get_message_str=lambda:'/车图 猪',
              message_obj=SimpleNamespace(raw_message={}),stop_event=Mock(),send=AsyncMock())
        await self.plugin.trains.on_event(event);event.stop_event.assert_called_once();event.send.assert_awaited_once()
        self.assertTrue(all(isinstance(c,MessageImage) for c in event.send.await_args.args[0].chain))
        event.send.reset_mock();event.get_message_str=lambda:'/车表 猪';await self.plugin.trains.on_event(event)
        self.assertTrue(all(isinstance(c,Plain) for c in event.send.await_args.args[0].chain))
        self.plugin.context.send_message.assert_not_awaited()

    async def test_image_preparation_rechecks_quiet_time_before_sending(self):
        event=SimpleNamespace(get_group_id=lambda:'200',stop_event=Mock(),send=AsyncMock())
        async def render(*args):self.config['broadcast_window']={'start':'08:00','end':'22:00'};return Path(self.temp.name)/'x.png'
        self.hunt.render=render;await self.hunt.handle(event,'/车图 猪');event.send.assert_not_awaited()

    async def test_hunt_settings_validation_is_atomic_and_preserves_existing_templates(self):
        ui=TrainWebUI(self.plugin);before=copy.deepcopy(self.config)
        body={'config':{**DEFAULT_CONFIG,'image_commands':['车表']}}
        with patch('astrbot_plugin_bear_tracker.train_webui.request',SimpleNamespace(json=AsyncMock(return_value=body))):result=await ui.hunt_settings()
        self.assertEqual(result['status'],'error');self.assertEqual(self.config,before)
        body['config']={**DEFAULT_CONFIG,'groups':['300']}
        with patch('astrbot_plugin_bear_tracker.train_webui.request',SimpleNamespace(json=AsyncMock(return_value=body))):result=await ui.hunt_settings()
        self.assertEqual(result['status'],'ok');self.assertEqual(self.config['train_commands'],before['train_commands'])


class CardTests(unittest.TestCase):
    def row(self):
        rows=flatten_schedules({'schedules':[schedule()]},NOW)
        return {**rows[0],'编号':'8','车头':'湿宝','版本显示':'7A','始发地':'奥山瓦丘'}

    def test_route_uses_only_lit_points_for_correct_server_map_and_line(self):
        key=json.dumps(['神拳痕','m-70-1',1],ensure_ascii=False,separators=(',',':'))
        snapshot={'shared':{'points':{key:[{'monsterId':'a-m-70-1-1','lit':True,'x':15.9,'y':24.0},
                                          {'monsterId':'a-m-70-1-2','lit':False,'x':7.7,'y':25.1}]}}}
        sections=route_sections(self.row(),snapshot)
        self.assertEqual(sections[0]['name'],'奥阔帕恰山');self.assertEqual(sections[0]['monsters'][0]['position'],(15.9,24.0))
        self.assertIsNone(sections[0]['monsters'][1]['position']);self.assertEqual(sections[0]['monsters'][1]['status'],'待确认')
        row={**self.row(),'服务器':'伊修加德'};self.assertTrue(all(m['position'] is None for s in route_sections(row,snapshot) for m in s['monsters']))

    def test_invalid_coordinate_is_never_drawn_and_multiline_is_separated(self):
        key=lambda n:json.dumps(['神拳痕','m-70-1',n],ensure_ascii=False,separators=(',',':'))
        snap={'shared':{'lines':{key(0):2},'points':{key(1):[{'monsterId':'a-m-70-1-1','lit':True,'x':61.0,'y':2.0}]}}}
        sections=route_sections(self.row(),snap);self.assertEqual(sections[0]['name'],'奥阔帕恰山 · 1线');self.assertIn('2线',sections[1]['name'])
        self.assertIsNone(sections[0]['monsters'][0]['position'])

    def test_both_cards_are_valid_dark_images_and_route_does_not_invent_missing_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            row=self.row();recent=render_recent([row],directory,'莫古力',DEFAULT_CONFIG)
            sections=route_sections(row,{'shared':{'points':{}}});route=render_route(row,sections,directory)
            for file in (recent,route):
                with Image.open(file) as image:
                    self.assertGreaterEqual(image.width,860);self.assertGreater(image.height,500);self.assertEqual(image.getpixel((0,0)),(11,16,22))
            self.assertTrue(all(m['position'] is None for s in sections for m in s['monsters']))
