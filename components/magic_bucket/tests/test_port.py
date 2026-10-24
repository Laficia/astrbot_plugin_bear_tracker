import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_magic_bucket.adapter import Event, OneBotAPI, load_config
from astrbot_plugin_magic_bucket.engine import ForwardBotPlugin, MissingForwardMessageID
from astrbot_plugin_magic_bucket.hunt_alerts import HuntAlerts, write_json
from astrbot_plugin_magic_bucket.spawn_watch import SpawnWatcher


class Bot:
    def __init__(self):
        self.api = self
        self.calls = []
        self.original = [{'type':'text','data':{'text':'重要'}}]
        self.missing = False
        self.delete_failure = None

    async def call_action(self, action, **args):
        self.calls.append((action, copy.deepcopy(args)))
        if action == 'get_msg':
            return {'message':copy.deepcopy(self.original)}
        if action == 'delete_msg' and str(args['message_id']) == self.delete_failure:
            raise ValueError('denied')
        return {} if self.missing else {'message_id':1000+len(self.calls)}


def astr_event(bot, text, group=245422536, images=()):
    raw = {'self_id':3923833280, 'user_id':123, 'group_id':group, 'message_id':42,
           'message':[{'type':'text','data':{'text':text}}] + list(images)}
    return SimpleNamespace(bot=bot, get_group_id=lambda:str(group), get_sender_id=lambda:'123',
        message_obj=SimpleNamespace(raw_message=raw, self_id='3923833280', message_id='42'))


class PortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bot = Bot()
        self.api = OneBotAPI(self.bot)
        self.cfg = load_config({'send_interval_ms':0}, Path(self.tmp.name))
        self.cfg['leak_fill']['enabled'] = False
        self.cfg['spawn_watch']['enabled'] = False
        self.engine = ForwardBotPlugin(self.cfg, self.api)
        await self.engine.on_load()
        self.addAsyncCleanup(self.engine.on_close)

    async def send(self, text, group=245422536, images=()):
        event = Event(astr_event(self.bot, text, group, images), self.api)
        await self.engine.onGroupMessageReceived(event)
        return event

    def sent(self, group=136293623):
        return [args['message'] for action,args in self.bot.calls if action=='send_group_msg' and args['group_id']==group]

    async def test_profile_and_maps(self):
        self.assertEqual(len(self.engine.manager.rules), 4)
        self.assertEqual(len(self.engine._map_images.index), 18)
        self.assertIsNotNone(self.engine._mango)

    async def test_ss_image_and_names(self):
        await self.send('重要 水塔 树海SS前置')
        message = self.sent()[0]
        self.assertIn('【水晶塔】', message[0]['data']['text'])
        self.assertIn('亚克特尔树海', message[0]['data']['text'])
        self.assertTrue(message[-1]['data']['file'].startswith('base64://'))

    async def test_s_completion_no_ss_map(self):
        await self.send('重要 遗产之地S')
        message = self.sent()[0]
        self.assertIn('先驱勇士 阿提卡斯', message[0]['data']['text'])
        self.assertFalse(any(s['type']=='image' for s in message))

    async def test_monster_completes_map(self):
        await self.send('重要 狭缝')
        self.assertIn('天外天垓', self.sent()[0][0]['data']['text'])

    async def test_car_order_not_completed(self):
        await self.send('剩余车序 遗产之地')
        self.assertNotIn('先驱勇士 阿提卡斯', self.sent()[0][0]['data']['text'])

    async def test_ignore_self_slash_unmatched(self):
        await self.send('/重要')
        await self.send('今天好')
        ev = Event(astr_event(self.bot,'重要'),self.api)
        ev.user_id=ev.self_id
        await self.engine.onGroupMessageReceived(ev)
        self.assertFalse(self.bot.calls)

    async def test_recall_scope_and_partial_retry(self):
        code = self.engine._cache_forwarded('245422536',['1','2'])
        self.bot.delete_failure='2'
        await self.send('撤回'+code,group=136293623)
        self.assertFalse(any(a=='delete_msg' for a,_ in self.bot.calls))
        await self.send('撤回'+code)
        self.assertEqual(self.engine._recall_cache[code].message_ids,['2'])
        self.bot.delete_failure=None
        await self.send('撤回'+code)
        self.assertNotIn(code,self.engine._recall_cache)

    async def test_no_duplicate_on_missing_receipt(self):
        self.bot.missing=True
        with self.assertRaises(MissingForwardMessageID):
            await self.engine.safe_send_combined(136293623,'text',None,['https://example.com/a.png'],'test')
        self.assertEqual(len(self.sent()),1)

    async def test_array_forward_preserves_segments(self):
        self.bot.original += [{'type':'at','data':{'qq':'123'}}]
        await self.engine.safe_forward_message(136293623,'42','test')
        self.assertEqual(self.sent()[0], self.bot.original)

    async def test_map_only_for_enabled_rule(self):
        self.engine.manager.rules[0].attach_map_image=False
        await self.send('重要 树海SS前置')
        self.assertFalse(any(s['type']=='image' for s in self.sent()[0]))

    async def test_multi_green_requires_pick_then_recall(self):
        mango=self.engine._mango
        points={str(i):{'state':i not in (1,4)} for i in range(1,11)}
        with patch.object(mango,'fetch_points',return_value=(points,{})):
            ev=await self.send('银泪湖 天外天垓 需要小异亚')
        self.assertTrue(ev.consumed)
        self.assertFalse(self.sent())
        await self.send('(17.6,30.3)')
        self.assertEqual(len(self.sent()),1)
        text=self.sent()[0][0]['data']['text']
        self.assertIn('17.6',text)
        self.assertNotIn('14.9',text)
        code=next(iter(self.engine._recall_cache))
        await self.send('撤回'+code,group=136293623)
        self.assertNotIn(code,self.engine._recall_cache)

    async def test_leak_fill_and_shutdown(self):
        self.cfg['leak_fill'].update(enabled=True,groups=[10,20],notify_group=30,window_seconds=.01)
        other=ForwardBotPlugin(self.cfg,self.api)
        await other.on_load()
        try:
            ev=Event(astr_event(self.bot,'[Sonar] test',group=10),self.api)
            await other.onGroupMessageReceived(ev)
            await asyncio.sleep(.05)
            self.assertEqual(len(self.sent(group=20)),1)
            self.assertTrue(other._recall_cache)
            other._spawn_leak_task(asyncio.sleep(50))
        finally:
            await other.on_close()
        self.assertFalse(other._leak_tasks)
        self.assertFalse(other._recall_cache)

    async def test_spawn_parser_and_file(self):
        path=Path(self.tmp.name)/'relay.json'
        watcher=SpawnWatcher(path,servers=['水晶塔'])
        self.assertTrue(watcher.observe('[Sonar] 60S: 刚德瑞瓦\n[狗-水晶塔] 翻云雾海 (16.8, 23.3)'))
        watcher.flush()
        self.assertIn('Gandarewa',json.loads(path.read_text('utf8'))['relay']['水晶塔'])

    async def test_alert_history_controls_reminder(self):
        root=Path(self.tmp.name)
        catalog=json.loads((Path(__file__).parents[1]/'hunt_alert_catalog.json').read_text('utf8'))
        hunt=catalog['hunts'][0]['k']; world=catalog['overdueWorlds'][0]
        write_json(root/'bot_relay.json',{'relay':{world:{hunt:[1000,1,1]}}})
        write_json(root/'manual-reports.json',[])
        messages=[]
        async def send(text): messages.append(text); return '1'
        alerts=HuntAlerts(catalog,root=root,state=root/'state.json',clock=lambda:3000,history_fetcher=lambda *_:[])
        await alerts.tick(send)
        self.assertEqual(len(messages),1)
        await alerts.tick(send)
        self.assertEqual(len(messages),1)
        alerts.state={'ss':{},'alerts':{},'rounds':{}}
        alerts.history_fetcher=lambda *_:[{'Time':1200}]
        await alerts.tick(send)
        self.assertEqual(len(messages),1)

    async def test_onebot_failed_status(self):
        async def fail(*args,**kwargs): return {'status':'failed','retcode':100}
        with patch.object(self.bot,'call_action',fail):
            with self.assertRaises(RuntimeError):
                await self.api.send_group_msg(1,[])

    async def test_onebot_routes_to_origin_account(self):
        api=OneBotAPI(self.bot,'3923833280')
        await api.send_group_msg(123,[])
        self.assertEqual(self.bot.calls[-1][1]['self_id'],'3923833280')


if __name__=='__main__':
    unittest.main()
