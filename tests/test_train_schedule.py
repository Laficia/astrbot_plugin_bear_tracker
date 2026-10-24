import asyncio
import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api import AstrBotConfig
from astrbot.api.message_components import Plain
from astrbot_plugin_bear_tracker import train_schedule as train
from astrbot_plugin_bear_tracker.train_webui import TrainWebUI

NOW=datetime(2026,10,24,12,0,0,tzinfo=train.TZ).timestamp()


def schedule(**changes):
    return {'id':'one','regionId':'bean','serverId':'yixiujiade','version':'7.0',
            'times':{'morning':'09:00','noon':'12:00','evening':'20:00','dawn':'无'},
            'pausedSlots':{},'slotNotices':{'evening':['周末限定','以招募为准']},
            'startStation':'奥阔帕恰山','startCrystal':'瓦丘思佩洛','notice':'临时调整见招募',
            'routeText':'山 → 湿 → 树',**changes}


class ScheduleTests(unittest.TestCase):
    def test_next_departure_sorted_with_china_timezone_pauses_and_midnight(self):
        rows=train.flatten_schedules({'schedules':[schedule(pausedSlots={'noon':True}),
                    schedule(id='two',times={'dawn':'00:30','morning':'25:00'},serverId='shuijingta')]},NOW)
        self.assertEqual([(r['日期'],r['时间']) for r in rows],[('今天','20:00'),('明天','00:30'),('明天','09:00')])
        self.assertEqual(rows[0]['班次备注'],'周末限定；以招募为准')
        at_noon=train.flatten_schedules({'schedules':[schedule()]},NOW,hours=1)
        self.assertEqual(len(at_noon),1)
        self.assertEqual(at_noon[0]['时间'],'12:00')
        self.assertEqual(train.flatten_schedules({'schedules':[schedule()]},NOW+1,hours=1),[])

    def test_unknown_server_uses_source_id_and_malformed_source_fails(self):
        rows=train.flatten_schedules({'schedules':[schedule(serverId='new-server')]},NOW)
        self.assertEqual(rows[0]['服务器'],'new-server')
        for value in [{}, {'schedules':[{}]}, {'schedules':[schedule(regionId='invented')]}]:
            with self.subTest(value=value),self.assertRaises(ValueError):train.flatten_schedules(value,NOW)

    def test_command_arguments_aliases_boundaries_and_disabled_rules(self):
        for text in ['/发车','/车表 狗','/车表（豆豆柴）','/车表(狗区)']:
            with self.subTest(text=text):self.assertEqual(train.match_command(text,train.DEFAULT_CONFIG)[1],'bean')
        for alias,expected in [('鸟','chocobo'),('猫小胖','cat'),('猪区','moogle'),('全部','all')]:
            self.assertEqual(train.match_command('/车表 '+alias,train.DEFAULT_CONFIG)[1],expected)
        for text in ['车表 狗','/车表扩展 狗','我想/车表 狗','/无关指令']:
            self.assertIsNone(train.match_command(text,train.DEFAULT_CONFIG))
        config=copy.deepcopy(train.DEFAULT_CONFIG);config['enabled']=False
        self.assertIsNone(train.match_command('/车表',config))
        with self.assertRaises(ValueError):train.match_command('/车表 不存在的大区',train.DEFAULT_CONFIG)

    def test_validation_rejects_duplicate_commands_reserved_names_and_template_access(self):
        for commands in [['车表','/车表'],['S播报'],['熊资料'],['a b'],[]]:
            config=copy.deepcopy(train.DEFAULT_CONFIG);config['rules'][0]['commands']=commands
            with self.subTest(commands=commands),self.assertRaises(ValueError):train.validate_config(config)
        for text in ['{未知}','{车表.__class__}','{车表[0]}','{数量:999999}','{数量!r}','{']:
            config=copy.deepcopy(train.DEFAULT_CONFIG);config['rules'][0]['reply_template']=text
            with self.subTest(text=text),self.assertRaises(ValueError):train.validate_config(config)
        config=copy.deepcopy(train.DEFAULT_CONFIG);config['rules'][0]['commands']=['/开车']
        self.assertEqual(train.validate_config(config)['rules'][0]['commands'],['开车'])
        original=copy.deepcopy(train.DEFAULT_CONFIG)
        train.validate_config(original)
        self.assertEqual(original,train.DEFAULT_CONFIG)

    def test_long_reply_chunks_preserve_content(self):
        text='A'*2000+'\n'+'第二行\n'*600
        chunks=train.split_reply(text)
        self.assertTrue(all(len(chunk)<=1800 for chunk in chunks))
        self.assertEqual(''.join(chunks),text)


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config={'enabled':True,'message_template':'原播报模板','train_commands':copy.deepcopy(train.DEFAULT_CONFIG)}
        self.plugin=SimpleNamespace(config=self.config)
        self.feature=train.TrainFeature(self.plugin);self.plugin.trains=self.feature
        self.feature.client.fetch=AsyncMock(return_value={'schedules':[schedule()]})
        self.clock=patch.object(train.time,'time',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)

    async def test_region_filtered_sorted_text_and_all_regions_grouped(self):
        self.feature.client.fetch.return_value={'schedules':[schedule(),schedule(id='two',regionId='cat',serverId='yanyu')]}
        result=await self.feature.query('/车表 狗')
        self.assertIn('[狗-伊修加德]',result['text']);self.assertNotIn('【猫小胖】',result['text'])
        result=await self.feature.query('/车表 全部')
        self.assertNotIn('【豆豆柴】',result['text']);self.assertNotIn('【猫小胖】',result['text'])
        self.assertIn('[狗-伊修加德]',result['text']);self.assertIn('[猫-',result['text'])
        self.assertIn('周末限定',result['text'])
        self.assertLess(result['text'].index('12:00'),result['text'].index('20:00'))

    async def test_custom_alias_and_fixed_reply_never_fetch_website(self):
        rule=self.config['train_commands']['rules'][0]
        rule.update(commands=['上车说明'],mode='text',reply_template='库啵，请看招募。{{固定文字}} {来源}')
        result=await self.feature.query('/上车说明')
        self.assertIn('{固定文字}',result['text']);self.assertIn(train.SOURCE,result['text'])
        self.feature.client.fetch.assert_not_awaited()
        self.assertIsNone(await self.feature.query('/车表'))

    async def test_network_failure_does_not_invent_or_serve_unlabelled_old_schedule(self):
        self.feature.client.fetch.side_effect=ValueError('网站不可用')
        with self.assertRaises(ValueError):await self.feature.query('/车表 狗')

    def fake_event(self,text,raw=None,wake=False):
        return SimpleNamespace(get_message_str=lambda:text,message_obj=SimpleNamespace(raw_message=raw),
                               is_at_or_wake_command=wake,stop_event=Mock(),send=AsyncMock())

    async def test_actual_event_recovers_stripped_slash_and_sends_plain_text_without_llm(self):
        raw={'message':[{'type':'text','data':{'text':'/车表 狗'}}]}
        event=self.fake_event('车表 狗',raw)
        await self.feature.on_event(event)
        event.stop_event.assert_called_once();event.send.assert_awaited()
        chain=event.send.await_args.args[0]
        self.assertTrue(all(isinstance(component,Plain) for component in chain.chain))
        self.assertIn('[狗-伊修加德]',chain.chain[0].text)
        self.assertEqual(self.config['message_template'],'原播报模板')

    async def test_unrelated_plain_text_invalid_area_and_disabled_feature(self):
        unrelated=self.fake_event('车表 狗')
        await self.feature.on_event(unrelated);unrelated.send.assert_not_awaited();unrelated.stop_event.assert_not_called()
        bad=self.fake_event('/车表 月球')
        await self.feature.on_event(bad);bad.send.assert_awaited_once();self.feature.client.fetch.assert_not_awaited()
        self.config['train_commands']['enabled']=False
        disabled=self.fake_event('/车表');await self.feature.on_event(disabled)
        disabled.send.assert_not_awaited()

    async def test_own_bot_messages_cannot_trigger_another_reply(self):
        event=self.fake_event('/车表 狗')
        event.get_sender_id=lambda:'10001';event.get_self_id=lambda:10001
        await self.feature.on_event(event)
        event.send.assert_not_awaited();event.stop_event.assert_not_called()
        self.feature.client.fetch.assert_not_awaited()

    async def test_webui_save_is_atomic_and_keeps_other_plugin_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            config=AstrBotConfig(str(Path(folder)/'config.json'),default_config=self.config)
            self.plugin.config=config;self.plugin.context=SimpleNamespace(register_web_api=Mock())
            ui=TrainWebUI(self.plugin)
            new=copy.deepcopy(train.DEFAULT_CONFIG);new['rules'][0]['commands']=['新车表']
            request=SimpleNamespace(json=AsyncMock(return_value={'config':new}))
            with patch('astrbot_plugin_bear_tracker.train_webui.request',request):
                self.assertEqual((await ui.settings())['status'],'ok')
                before=Path(folder,'config.json').read_bytes()
                request.json.return_value={'config':{'enabled':'false'}}
                self.assertEqual((await ui.settings())['status'],'error')
                self.assertEqual(Path(folder,'config.json').read_bytes(),before)
            self.assertEqual(config['message_template'],'原播报模板')
            self.assertEqual(config['train_commands']['rules'][0]['commands'],['新车表'])

    async def test_unload_cancels_pending_query_and_prevents_sends(self):
        started=asyncio.Event()
        async def blocked(*args):started.set();await asyncio.sleep(100)
        self.feature.client.fetch.side_effect=blocked
        event=self.fake_event('/车表')
        task=asyncio.create_task(self.feature.on_event(event));await started.wait()
        await self.feature.close()
        self.assertTrue(task.cancelled());event.send.assert_not_awaited()
