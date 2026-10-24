"""Hunt train reminders and explicit image commands, sharing existing train data."""
import asyncio
import copy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import time

import aiohttp
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image
from .train_schedule import CENTERS,TZ,flatten_schedules,resolve_center,validate_format,PROTECTED_COMMANDS
from .feature_controls import allows
from .component_host import shared_platform
from .delivery_journal import recorded_chain, recorded_event
from .quiet_hours import delivery_allowed

ROUTE_SOURCE='https://119.29.119.179/Arank/'
ROUTE_API='http://119.29.119.179/Arank/api/state'
FIELDS=['服务器','大区','大区简称','版本','时间','日期','始发地','车头','分钟','编号','班次','备注']
DEFAULT_TEMPLATE='【狩猎车提醒】\n服务器：{服务器}\n版本：{版本}\n时间：{时间}\n始发地：{始发地}\n车头：{车头}\n距离发车还有 {分钟} 分钟！'
DEFAULT_CONFIG={'enabled':True,'groups':[],'centers':['bean'],'servers':[],'minutes':10,
                'template':DEFAULT_TEMPLATE,'image_commands':['车图','近期发车'],
                'route_commands':['车线','狩猎路线'],'default_center':'bean'}


def validate_config(value,existing_commands=()):
    if not isinstance(value,dict):raise ValueError('狩猎车提醒配置必须为对象')
    config=copy.deepcopy(DEFAULT_CONFIG);config.update(copy.deepcopy(value))
    if set(config)!=set(DEFAULT_CONFIG) or type(config['enabled']) is not bool:raise ValueError('狩猎车配置项或开关无效')
    if type(config['minutes']) is not int or not 1<=config['minutes']<=120:raise ValueError('提前提醒时间须为 1～120 分钟')
    for key in ('groups','servers'):
        values=config[key]
        if not isinstance(values,list) or len(values)>100 or any(not isinstance(v,str) or not v.strip() or len(v)>128 for v in values):raise ValueError('请检查群号或服务器列表')
        config[key]=list(dict.fromkeys(v.strip() for v in values))
    if any(not re.fullmatch(r'[A-Za-z0-9]{1,128}',g) for g in config['groups']):raise ValueError('每行只填写一个群号，不需要前缀')
    if not isinstance(config['centers'],list) or any(c not in CENTERS for c in config['centers']):raise ValueError('请选择提醒大区')
    config['centers']=list(dict.fromkeys(config['centers']))
    if config['default_center'] not in (*CENTERS,'all'):raise ValueError('图片指令默认大区无效')
    seen={c.casefold() for c in existing_commands}|PROTECTED_COMMANDS
    for key in ('image_commands','route_commands'):
        if not isinstance(config[key],list) or not 1<=len(config[key])<=10:raise ValueError('图片指令须填写 1～10 项')
        commands=[]
        for name in config[key]:
            if not isinstance(name,str):raise ValueError('图片指令应为文字')
            name=name.strip().removeprefix('/')
            if not re.fullmatch(r'[^\s/()（）]{1,24}',name) or name.casefold() in seen:raise ValueError('图片指令重复、无效或与现有指令冲突：'+name)
            seen.add(name.casefold());commands.append(name)
        config[key]=commands
    config['template']=validate_format(config['template'],FIELDS)
    return config


def version_label(value):
    return re.sub(r'\.0$','',str(value)).removesuffix('A')+'A'


def starting_place(row):
    station=row['始发站'];crystal=row['水晶']
    # Some legacy timetable entries put their entire route in startCrystal.
    # Only extract an explicitly stated assembly place, never guess one.
    if len(crystal)>48 or len(re.findall(r'\s[-→]\s',crystal))>1:
        match=re.search(r'[（(]([^()（）]{1,40})\s*集合\s*[)）]',crystal)
        return match[1].strip() if match else station+'（发车地点见招募）'
    return ' '.join(v for v in (station,crystal) if v and v!='无')


def calendar_allowed(row):
    text=row.get('备注','')+'；'+row.get('班次备注','')
    day=datetime.fromtimestamp(row['departure_at'],TZ).weekday()
    if any(w in text for w in ('周末限定','仅周末','周六周日','周六、周日')):return day in (5,6)
    if any(w in text for w in ('工作日限定','仅工作日','周一至周五','周一到周五','周一-周五','周一~周五')):return day<5
    if re.search(r'(?:仅|限定|固定|每周).*?周[一二三四五六日天]|周[一二三四五六日天].*?(?:限定|发车)',text):
        days={('一二三四五六日'.index(w) if w!='天' else 6) for w in re.findall(r'周([一二三四五六日天])',text)}
        if days:return day in days
    return True


class HuntTrainFeature:
    def __init__(self,plugin):
        self.plugin=plugin;self.path=Path(plugin.alerts.data_dir)/'train_hunt_state.json'
        self.images=self.path.parent/'train_images';self.task=None;self.closed=False
        self.lock=asyncio.Lock();self.image_lock=asyncio.Lock();self.last_error='';self.last_check=0;self.jobs=set()
        self.route_data=None;self.route_at=0;self.route_lock=asyncio.Lock()
        try:self.state=json.loads(self.path.read_text('utf-8'))
        except (OSError,ValueError):self.state={'numbers':{},'receipts':{}}
        self.state.setdefault('numbers',{});self.state.setdefault('receipts',{})

    def config(self):
        commands=[c for r in self.plugin.trains.config()['rules'] for c in r['commands']]
        return validate_config(self.plugin.config.get('train_hunt',{}),commands)

    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(self.state,ensure_ascii=False),'utf-8');temp.replace(self.path)

    def enrich(self,data,rows,config=None):
        config=config or self.config();numbers=self.state['numbers'];changed=False
        next_id=max(numbers.values(),default=0)+1
        for source in data['schedules']:
            identity=str(source.get('id',''))
            if identity not in numbers:numbers[identity]=next_id;next_id+=1;changed=True
        if changed:self.save()
        sources={str(r.get('id','')):r for r in data['schedules']}
        result=[]
        for item in rows:
            row=copy.deepcopy(item);source=sources.get(row['source_id'],{})
            leader=next((source.get(k) for k in ('leader','driver','conductor','trainLeader') if isinstance(source.get(k),str) and source[k].strip()),'')
            row.update(编号=str(numbers[row['source_id']]),车头=leader.strip() or '待公布',
                       始发地=starting_place(row),版本显示=version_label(row['版本']))
            result.append(row)
        return result

    async def rows(self,center='all',force=False,config=None):
        data=await self.plugin.trains.client.fetch(force)
        return self.enrich(data,flatten_schedules(data,time.time(),24,center),config)

    def active(self,group=None):
        return not self.closed and self.plugin.trains.config()['enabled'] and allows(self.plugin.config,'trains',group) and delivery_allowed(self.plugin.config)

    def start(self):
        if not self.task:self.task=asyncio.create_task(self.run(),name='bear-train-reminders')

    async def close(self):
        self.closed=True
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        pending=[job for job in self.jobs if job is not asyncio.current_task()]
        for job in pending:job.cancel()
        await asyncio.gather(*pending,return_exceptions=True)

    async def run(self):
        while True:
            try:await self.tick()
            except asyncio.CancelledError:raise
            except Exception as exc:self.last_error='狩猎车提醒：'+str(exc)
            await asyncio.sleep(20)

    def message(self,row,now=None,config=None):
        now=time.time() if now is None else now;config=config or self.config()
        values={**row,'版本':row['版本显示'],'分钟':str(max(0,math.ceil((row['departure_at']-now)/60)))}
        return config['template'].format_map(values)

    async def tick(self):
        async with self.lock:
            config=self.config();platform=shared_platform(self.plugin.config)
            if not config['enabled'] or not config['groups'] or not config['centers'] or not platform:return
            if not self.plugin.trains.config()['enabled']:return
            rows=await self.rows(force=True,config=config);now=time.time();self.last_check=now
            receipts=self.state['receipts']
            for key in [k for k,v in receipts.items() if now-v['at']>7*86400]:receipts.pop(key)
            for row in rows:
                due=row['departure_at']-config['minutes']*60
                if not 0<=now-due<90 or row['region_id'] not in config['centers'] or not calendar_allowed(row):continue
                if config['servers'] and row['服务器'] not in config['servers']:continue
                for group in config['groups']:
                    origin=f'{platform}:GroupMessage:{group}'
                    key=hashlib.sha256(f"{row['id']}|{row['departure_at']}|{origin}".encode()).hexdigest()
                    if key in receipts:continue
                    if not self.active(group):
                        receipts[key]={'at':now,'state':'silenced'};self.save();continue
                    # Reserve before the transport await: restarts never replay a
                    # message whose delivery result may be unknown.
                    receipts[key]={'at':now,'state':'sending'};self.save()
                    latest=self.config()
                    if (not latest['enabled'] or group not in latest['groups'] or platform!=shared_platform(self.plugin.config) or not self.active(group)
                        or row['region_id'] not in latest['centers'] or (latest['servers'] and row['服务器'] not in latest['servers'])
                        or not 0<=time.time()-(row['departure_at']-latest['minutes']*60)<90):
                        receipts[key]['state']='silenced';self.save();continue
                    try:
                        chain=MessageChain().message(self.message(row,config=latest)).use_t2i(False).use_markdown(False)
                        result=await asyncio.wait_for(recorded_chain(self.plugin.context,origin,chain,feature='trains',event_key=key,expires_at=row['departure_at']-latest['minutes']*60+90),30)
                        if result is False:raise RuntimeError('发送机器人未连接')
                        receipts[key]['state']='sent';self.last_error=''
                    except asyncio.CancelledError:raise
                    except Exception as exc:
                        receipts[key]['state']='unknown';self.last_error='提醒未取得成功回执，请核对群消息：'+str(exc)
                    self.save()

    async def route_snapshot(self,force=False):
        async with self.route_lock:
            if not force and self.route_data is not None and time.time()-self.route_at<30:return self.route_data
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(ROUTE_API,headers={'Cache-Control':'no-cache'}) as response:
                    response.raise_for_status();raw=bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw)>8*1024*1024:raise ValueError('线路点位数据过大')
                    data=json.loads(raw)
            if not isinstance(data,dict) or not isinstance(data.get('shared',{}).get('points'),dict):raise ValueError('共享点位格式异常')
            self.route_data=data;self.route_at=time.time();return data

    async def render(self,kind,argument='',config=None):
        from .train_cards import render_recent,render_route,route_sections
        config=validate_config(config,[c for r in self.plugin.trains.config()['rules'] for c in r['commands']]) if config is not None else self.config()
        if kind=='recent':
            center=resolve_center(argument) if argument else config['default_center']
            rows=await self.rows(center,config=config)
            if not rows:raise ValueError('未来 24 小时暂无可展示的班次')
            async with self.image_lock:return await asyncio.to_thread(render_recent,rows[:4],self.images,CENTERS.get(center,'全部大区'),config)
        if kind!='route':raise ValueError('图片类型无效')
        found=re.fullmatch(r'#?(\d+)(?:\s+(\d{1,2}:\d{2}))?',argument.strip())
        if not found:raise ValueError('请使用 /'+config['route_commands'][0]+' 班次编号，例如 /'+config['route_commands'][0]+' 8；编号见 /'+config['image_commands'][0]+'，可追加时间筛选')
        rows=await self.rows(config=config);row=next((r for r in rows if r['编号']==str(int(found[1])) and (not found[2] or r['时间']==found[2])),None)
        if row is None:raise ValueError('该编号在未来 24 小时内没有班次，请先查看最新发车图')
        try:snapshot=await self.route_snapshot();error=''
        except (aiohttp.ClientError,ValueError,asyncio.TimeoutError) as exc:snapshot={'shared':{'points':{},'lines':{},'route':[]}};error='共享点位暂不可用，坐标与状态不作推测'
        sections=route_sections(row,snapshot)
        async with self.image_lock:return await asyncio.to_thread(render_route,row,sections,self.images,error,snapshot.get('updatedAt',''))

    async def handle(self,event,text):
        try:config=self.config()
        except ValueError:return False
        match=re.fullmatch(r'/([^\s()（）]+)(?:\s*(?:\(([^()]*)\)|（([^（）]*)）|\s+(.+)))?\s*',text)
        if not match:return False
        command=match[1].casefold();kind='recent' if command in [s.casefold() for s in config['image_commands']] else 'route' if command in [s.casefold() for s in config['route_commands']] else None
        if not kind:return False
        event.stop_event();group=getattr(event,'get_group_id',lambda:'')()
        if not self.active(group):return True
        argument=next((v.strip() for v in match.groups()[1:] if v is not None),'')
        task=asyncio.current_task();self.jobs.add(task)
        try:
            path=await self.render(kind,argument)
            if self.active(group):await recorded_event(self.plugin,event,MessageChain().file_image(str(path)).use_t2i(False).use_markdown(False))
        except (ValueError,OSError,aiohttp.ClientError) as exc:
            if self.active(group):await recorded_event(self.plugin,event,MessageChain().message(str(exc)).use_t2i(False).use_markdown(False))
        finally:self.jobs.discard(task)
        return True
