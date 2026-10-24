"""Read-only hunt train schedules and configurable plain-text query commands."""
from __future__ import annotations
import asyncio
from .quiet_hours import delivery_allowed
import copy
from datetime import datetime, timedelta, timezone
import html
import json
from pathlib import Path
import re
from string import Formatter
import time

import aiohttp
from astrbot.api.event import MessageChain

SOURCE = 'https://ffxivhuntcn.com/ShiJianShunXu'
API = 'https://ffxivhuntcn.com/api/v1/trains/schedules'
TZ = timezone(timedelta(hours=8))
CENTERS = {'bean':'豆豆柴','chocobo':'陆行鸟','cat':'猫小胖','moogle':'莫古力'}
SHORT = {'bean':'狗','chocobo':'鸟','cat':'猫','moogle':'猪'}
SLOTS = {'morning':'早班','noon':'午班','evening':'晚班','dawn':'凌晨'}
SERVERS = {r['id']:r for r in json.loads(Path(__file__).with_name('train_catalog.json').read_text('utf8'))['servers']}
SUMMARY_FIELDS = ['大区','小时','车表','数量','查询时间','来源','提示']
ITEM_FIELDS = ['序号','日期','时间','大区','大区简称','服务器','版本','班次','始发站','水晶','路线','备注','班次备注','车头','编号','始发地']
DEFAULT_RULE = {'id':'train-default','name':'狩猎车表','enabled':True,'commands':['发车','车表'],
                'mode':'schedule','default_center':'bean','hours':24,'limit':30,
                'reply_template':'库啵，{大区}未来{小时}小时的发车安排：\n{车表}\n\n{提示}\n{来源}',
                'item_template':'{序号}. {日期} {时间} [{大区简称}-{服务器}] {版本}A · {班次}\n集合：{始发站} {水晶}\n{班次备注}'}
DEFAULT_CONFIG = {'enabled':True,'rules':[DEFAULT_RULE]}
PROTECTED_COMMANDS = {'s播报','熊计时','熊资料','撤回','help','帮助','plugin','plugins'}


def clean(value):
    if not isinstance(value,str):return ''
    return html.unescape(re.sub(r'<br\s*/?>','\n',value,flags=re.I)).strip()


def validate_format(text, fields, maximum=2000):
    if not isinstance(text,str) or not text.strip() or len(text)>maximum:
        raise ValueError(f'模板不能为空，且最多 {maximum} 字')
    try:
        for _,key,spec,conversion in Formatter().parse(text):
            if key is not None and (key not in fields or spec or conversion is not None):
                raise ValueError('不支持的模板变量或表达式：{'+key+'}')
    except ValueError as exc:
        raise ValueError('模板格式无效：'+str(exc)) from exc
    return text.replace('\r\n','\n').replace('\r','\n')


def validate_config(value):
    if not isinstance(value,dict) or type(value.get('enabled')) is not bool:
        raise ValueError('请检查车表指令开关')
    rules=value.get('rules')
    if not isinstance(rules,list) or len(rules)>20:raise ValueError('最多设置 20 条指令规则')
    result=[];seen=set();ids=set()
    for index,r in enumerate(rules):
        if not isinstance(r,dict):raise ValueError('指令规则必须是对象')
        item=copy.deepcopy(r)
        if not isinstance(item.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',item['id']) or item['id'] in ids:
            raise ValueError('规则标识无效或重复，请刷新后重试')
        ids.add(item['id'])
        if not isinstance(item.get('name'),str) or not 1<=len(item['name'].strip())<=50:raise ValueError('规则名称须为 1～50 字')
        if type(item.get('enabled')) is not bool:raise ValueError('规则开关无效')
        commands=item.get('commands')
        if not isinstance(commands,list) or not 1<=len(commands)<=10:raise ValueError('每条规则填写 1～10 个指令')
        normalized=[]
        for command in commands:
            if not isinstance(command,str):raise ValueError('指令应为文字')
            command=command.strip().removeprefix('/')
            if not re.fullmatch(r'[^\s/()（）]{1,24}',command):raise ValueError('指令最多 24 字，不能包含空格、括号或斜杠')
            key=command.casefold()
            if key in PROTECTED_COMMANDS:raise ValueError('指令 /'+command+' 已由现有功能使用')
            if key in seen:raise ValueError('指令不能重复：/'+command)
            seen.add(key);normalized.append(command)
        item['commands']=normalized
        if item.get('mode') not in ('schedule','text'):raise ValueError('请选择动态车表或固定文字')
        if item.get('default_center') not in (*CENTERS,'all'):raise ValueError('默认大区无效')
        if type(item.get('hours')) is not int or not 1<=item['hours']<=24:raise ValueError('查询时长须为 1～24 小时')
        if type(item.get('limit')) is not int or not 1<=item['limit']<=100:raise ValueError('最多显示条数须为 1～100')
        item['reply_template']=validate_format(item.get('reply_template'),SUMMARY_FIELDS)
        item['item_template']=validate_format(item.get('item_template'),ITEM_FIELDS)
        result.append(item)
    return {'enabled':value['enabled'],'rules':result}


def resolve_center(text):
    value=text.strip()
    if value in ('全部','全区','所有','all'):return 'all'
    for key,name in CENTERS.items():
        if value in (key,name,name+'区',SHORT[key],SHORT[key]+'区'):return key
    raise ValueError('请填写大区：豆豆柴（狗）、陆行鸟（鸟）、猫小胖（猫）、莫古力（猪）或全部。')


def match_command(text,config):
    if not config['enabled'] or not isinstance(text,str):return None
    found=re.fullmatch(r'\s*/([^\s/()（）]+)(?:\s*(?:\(([^()]*)\)|（([^（）]*)）|\s+(.+)))?\s*',text)
    if not found:return None
    command=found[1].casefold();argument=next((v.strip() for v in found.groups()[1:] if v is not None),'')
    for rule in config['rules']:
        if rule['enabled'] and command in [c.casefold() for c in rule['commands']]:
            return rule,resolve_center(argument) if argument else rule['default_center']
    return None


def flatten_schedules(data,now,hours=24,center='all'):
    if not isinstance(data,dict) or not isinstance(data.get('schedules'),list) or len(data['schedules'])>5000:
        raise ValueError('网站车表数据格式异常')
    current=datetime.fromtimestamp(now,TZ);end=now+hours*3600
    entries=[];identities=set()
    for row in data['schedules']:
        if not isinstance(row,dict) or not isinstance(row.get('times'),dict):raise ValueError('网站排班数据格式异常')
        region=row.get('regionId');server=row.get('serverId');version=row.get('version')
        if region not in CENTERS or not isinstance(server,str) or not isinstance(version,str):raise ValueError('网站大区或服务器数据无法识别')
        if center!='all' and center!=region:continue
        for slot,label in SLOTS.items():
            clock=row['times'].get(slot,'无')
            if (row.get('pausedSlots') or {}).get(slot):continue
            if not isinstance(clock,str) or not re.fullmatch(r'\d{1,2}:\d{2}',clock):continue
            hour,minute=map(int,clock.split(':'))
            if hour>23 or minute>59:continue
            departure=current.replace(hour=hour,minute=minute,second=0,microsecond=0)
            if departure.timestamp()<now:departure+=timedelta(days=1)
            if departure.timestamp()>end:continue
            identity=(row.get('id'),slot)
            if identity in identities:continue
            identities.add(identity)
            notices=(row.get('slotNotices') or {}).get(slot,[])
            if not isinstance(notices,list):raise ValueError('网站班次备注数据异常')
            entries.append({'id':str(row.get('id',''))+'-'+slot,'source_id':str(row.get('id','')),'slot':slot,
                            'route_map_ids':row.get('routeMapIds',[]) if isinstance(row.get('routeMapIds',[]),list) else [],
                            '车头':'待公布','编号':'','始发地':' '.join(filter(None,[clean(row.get('startStation')),clean(row.get('startCrystal'))])),
                            'region_id':region,'大区':CENTERS[region],
                            '大区简称':SHORT[region],'服务器':SERVERS.get(server,{}).get('name',server),
                            '版本':version,'班次':label,'日期':'今天' if departure.date()==current.date() else '明天',
                            '时间':departure.strftime('%H:%M'),'始发站':clean(row.get('startStation')) or '待公布',
                            '水晶':clean(row.get('startCrystal')),'路线':clean(row.get('routeText')),
                            '备注':clean(row.get('notice')),'班次备注':'；'.join(clean(v) for v in notices if clean(v)),
                            'departure_at':departure.timestamp()})
    return sorted(entries,key=lambda r:(r['departure_at'],r['时间'],tuple(-ord(c) for c in r['版本']),r['服务器']))


class TrainClient:
    def __init__(self):
        self.data=None;self.fetched_at=0;self.last_error='';self.lock=asyncio.Lock()

    async def fetch(self,force=False):
        async with self.lock:
            if not force and not self.last_error and self.data is not None and time.time()-self.fetched_at<60:return self.data
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                    async with s.get(API,headers={'User-Agent':'AstrBot-BearTracker/2.6','Cache-Control':'no-cache'}) as r:
                        r.raise_for_status();raw=bytearray()
                        async for chunk in r.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw)>8*1024*1024:raise ValueError('网站车表数据过大')
                        value=json.loads(raw)
                flatten_schedules(value,time.time())
                self.data=value;self.fetched_at=time.time();self.last_error=''
                return value
            except Exception as exc:
                self.last_error='车表暂时读取失败：'+type(exc).__name__
                raise ValueError(self.last_error+'，请稍后重试。') from exc


def split_reply(text,limit=1800):
    # Preserve every entry, including long user templates, as plain text chunks.
    chunks=[];part=''
    for line in text.splitlines(keepends=True):
        while line:
            space=limit-len(part);part+=line[:space];line=line[space:]
            if len(part)==limit:chunks.append(part);part=''
    if part:chunks.append(part)
    return chunks or ['（暂无内容）']


from .feature_controls import allows as feature_allows


class TrainFeature:
    def __init__(self,plugin):
        self.plugin=plugin;self.client=TrainClient();self.closed=False;self.tasks=set()

    def config(self):return validate_config(self.plugin.config.get('train_commands',copy.deepcopy(DEFAULT_CONFIG)))

    async def query(self,text,config=None):
        config=validate_config(config) if config is not None else self.config()
        match=match_command(text,config)
        if not match:return None
        rule,center=match;now=time.time()
        values={'大区':CENTERS.get(center,'全部大区'),'小时':str(rule['hours']),'车表':'','数量':'0',
                '查询时间':datetime.fromtimestamp(now,TZ).strftime('%Y-%m-%d %H:%M'),
                '来源':SOURCE,'提示':'排班仅供参考，临时调整、连车和限定日期以班次备注及招募为准。'}
        rows=[];total=0
        if rule['mode']=='schedule':
            data=await self.client.fetch();now=time.time()
            values['查询时间']=datetime.fromtimestamp(now,TZ).strftime('%Y-%m-%d %H:%M')
            rows=flatten_schedules(data,now,rule['hours'],center);total=len(rows)
            extras=getattr(self.plugin,'train_hunt',None)
            if extras:rows=extras.enrich(data,rows)
            rows=rows[:rule['limit']];values['数量']=str(len(rows));blocks=[]
            for region,name in (('all','全部大区'),) if center=='all' else CENTERS.items():
                selected=rows if region=='all' else [r for r in rows if r['region_id']==region]
                if not selected:continue
                rendered=[rule['item_template'].format_map({**r,'序号':str(i+1)}).strip() for i,r in enumerate(selected)]
                blocks.append(('' if region=='all' else '【'+name+'】\n')+'\n'.join(rendered))
            values['车表']='\n\n'.join(blocks) if blocks else '查询时间范围内暂无排班。'
            if total>len(rows):values['提示']+=f'\n共 {total} 班，显示最近 {len(rows)} 班；可在控制台调整显示条数。'
        reply=rule['reply_template'].format_map(values)
        return {'matched':rule['name'],'text':reply,'parts':split_reply(reply),'rows':rows,'total':total,
                'fetched_at':self.client.fetched_at,'source':SOURCE}

    async def on_event(self,event):
        group=getattr(event,'get_group_id',lambda:'')()
        if not feature_allows(self.plugin.config,'trains',group):return
        sender=getattr(event,'get_sender_id',lambda:'')()
        account=getattr(event,'get_self_id',lambda:'')()
        if sender and account and str(sender)==str(account):return
        text=event.get_message_str().strip()
        raw=getattr(event.message_obj,'raw_message',None)
        if isinstance(raw,dict) and isinstance(raw.get('message'),list):
            original=''.join(s.get('data',{}).get('text','') for s in raw['message'] if s.get('type')=='text').strip()
            if original.startswith('/'):text=original
        if not text.startswith('/'):
            if not getattr(event,'is_at_or_wake_command',False):return
            text='/'+text
        extras=getattr(self.plugin,'train_hunt',None)
        if extras and await extras.handle(event,text):return
        try:config=self.config()
        except ValueError:return
        try:
            match=match_command(text,config)
        except ValueError as exc:
            # Only recognized commands get a usage reply for an invalid area.
            command=re.split(r'[\s(（]',text.lstrip('/'),maxsplit=1)[0].casefold()
            if not config['enabled'] or command not in {c.casefold() for r in config['rules'] if r['enabled'] for c in r['commands']}:return
            event.stop_event()
            if delivery_allowed(self.plugin.config) and feature_allows(self.plugin.config,'trains',group):await event.send(MessageChain().message(str(exc)).use_t2i(False).use_markdown(False))
            return
        if not match or self.closed:return
        if not delivery_allowed(self.plugin.config):event.stop_event();return
        event.stop_event();task=asyncio.current_task();self.tasks.add(task)
        try:
            try:result=await self.query(text)
            except ValueError as exc:result={'parts':[str(exc)]}
            if self.closed or not result:return
            for part in result['parts']:
                if self.closed or not delivery_allowed(self.plugin.config) or not feature_allows(self.plugin.config,'trains',group):return
                await event.send(MessageChain().message(part).use_t2i(False).use_markdown(False))
        finally:self.tasks.discard(task)

    async def close(self):
        self.closed=True
        pending=[t for t in self.tasks if t is not asyncio.current_task()]
        for t in pending:t.cancel()
        if pending:await asyncio.gather(*pending,return_exceptions=True)
