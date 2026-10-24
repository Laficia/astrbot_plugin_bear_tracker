"""Authenticated operational tools: history, delivery records and recovery."""
import asyncio
import copy
import json
import time

from astrbot.api.web import request
from .config_history import ConfigHistory, fingerprint
from .connection_supervisor import ConnectionSupervisor, DEFAULT as WATCHDOG_DEFAULT, validate as validate_watchdog
from .delivery_journal import DeliveryJournal, FEATURE_LABELS, delivery_scope, SafeDeliveryFailure
from .component_host import shared_platform
from .feature_controls import require_feature
from .quiet_hours import require_delivery

DEFAULT = {'dedup': {'enabled': True, 'window_seconds': 180}, 'watchdog': WATCHDOG_DEFAULT}


def settings(value):
    if not isinstance(value,dict) or set(value)!=set(DEFAULT):
        raise ValueError('运行设置需要去重与连接守护两个板块')
    dedup=value['dedup']
    if not isinstance(dedup,dict) or set(dedup)!={'enabled','window_seconds'} or type(dedup['enabled']) is not bool or type(dedup['window_seconds']) is not int or not 10<=dedup['window_seconds']<=600:
        raise ValueError('合并开关或时间范围无效（10～600秒）')
    return {'dedup':copy.deepcopy(dedup),'watchdog':validate_watchdog(value['watchdog'])}


class Operations:
    def __init__(self, owner, folder):
        self.owner=owner
        self.history=ConfigHistory(owner.config,folder)
        self.delivery=DeliveryJournal(owner,folder,owner.context)
        self.watchdog=ConnectionSupervisor(owner)
        self.jobs=set()
        self.lock=asyncio.Lock()
        prefix='/astrbot_plugin_bear_tracker/webui/operations/'
        for name,method,handler in [('state','GET',self.state),('settings','POST',self.save_settings),
                ('records','POST',self.records),('retry','POST',self.retry),('history','GET',self.history_list),
                ('diff','POST',self.diff),('restore','POST',self.restore),('export','GET',self.export)]:
            owner.context.register_web_api(prefix+name,handler,[method],'运行与维护')

    def config(self):
        current=self.owner.config.get('operations',{})
        return {key:{**value,**current.get(key,{})} for key,value in DEFAULT.items()}

    async def state(self):
        return {'status':'ok','data':{'config':self.config(),'bot':self.owner.webui.bot_connection(),
            'watchdog':self.watchdog.status(),'counts':self.delivery.query('SELECT state,COUNT(*) AS count FROM deliveries GROUP BY state'),
            'features':FEATURE_LABELS}}

    async def save_settings(self):
        try:
            body=await request.json(default={})
            value=settings(body.get('config'))
            async with self.owner.webui.lock:
                cfg=self.owner.config
                if hasattr(cfg,'save_config'):cfg.save_config({'operations':value})
                else:cfg['operations']=value;self.history.snapshot('运行设置已保存')
            return await self.state()
        except (ValueError,TypeError,OSError) as exc:return {'status':'error','message':str(exc)}

    async def records(self):
        try:
            body=await request.json(default={})
            if not isinstance(body,dict):raise ValueError('查询条件无效')
            limit=body.get('limit',100)
            if type(limit) is not int:raise ValueError('条数无效')
            return {'status':'ok','data':{'records':self.delivery.records(str(body.get('state') or ''),str(body.get('feature') or ''),str(body.get('target') or ''),limit)}}
        except (ValueError,TypeError) as exc:return {'status':'error','message':str(exc)}

    async def history_list(self):
        return {'status':'ok','data':{'current':fingerprint(dict(self.owner.config)),'entries':self.history.entries()}}

    async def diff(self):
        try:
            body=await request.json(default={})
            return {'status':'ok','data':self.history.diff(body.get('id'))}
        except (ValueError,OSError,TypeError) as exc:return {'status':'error','message':str(exc)}

    async def export(self):
        return {'status':'ok','data':{'plugin':'astrbot_plugin_bear_tracker','config':copy.deepcopy(dict(self.owner.config))}}

    async def restore(self):
        try:
            body=await request.json(default={})
            async with self.owner.webui.lock:
                self.history.restore(body.get('id'),body.get('current'))
                self.owner.alerts.silence_pending()
                self.owner.triggers.silence_pending()
                self.owner.triggers.apply_hunt_selection()
                if hasattr(self.owner.triggers,"timing_changed"):
                    from .trigger_source import DEFAULT_TRIGGER_HUNTS
                    self.owner.triggers.timing_changed(DEFAULT_TRIGGER_HUNTS)
                await self.owner.components.magic.reset_engines()
                self.owner.alerts.changed.set()
            return {'status':'ok','data':{'message':'历史配置已恢复；恢复前配置也已自动备份'}}
        except (ValueError,OSError,TypeError) as exc:return {'status':'error','message':str(exc)}

    def retry_guard(self,row,meta):
        config=self.owner.config
        require_delivery(config)
        feature=row['feature']
        if feature!='commands':
            source=meta.get('source_group') if feature in ('forwarding','collection','mango','hunt') else row['target']
            require_feature(config, 'sightings' if feature=='trigger' else feature, source)
        if time.time()>=meta.get('expires_at',0):raise ValueError('原消息已过有效时间，不能重试')
        if feature not in ('manual','sightings','trigger','forwarding','collection','mango','hunt','trains'):
            raise ValueError('指令回复请重新输入原指令')
        if feature!='manual' and (not config.get('enabled',True) or row['platform']!=shared_platform(config)):
            raise ValueError('自动发送已关闭或发送机器人已更改')
        if feature in ('sightings','trigger'):
            origin=f"{row['platform']}:GroupMessage:{row['target']}"
            if origin not in self.owner.alerts.destinations():raise ValueError('目标群已移出网站播报配置')
            if feature=='sightings' and not self.owner.alerts.accepts(meta.get('report',{})):raise ValueError('报告已失效或移出监听范围')
            if feature=='trigger':raise ValueError('可触发提醒由实时窗口队列安全重试，不能手工重播旧窗口')
        elif feature=='trains':
            if row['target'] not in self.owner.train_hunt.config()['groups'] or not self.owner.train_hunt.active(row['target']):raise ValueError('狩猎车提醒群或开关已更改')
        elif feature!='manual':
            from .components.magic_bucket.webui import effective
            profile=effective(config.get('magic_bucket',{}))
            source=str(meta.get('source_group',''))
            if feature=='forwarding':
                text=meta.get('source_text','')
                match=any(source in map(str,r['source_groups']) and row['target'] in map(str,r['target_groups']) and any(text.startswith(k) if r['type']=='prefix' else k in text for k in r['keywords']) for r in profile['rules'])
            else:
                section=profile[{'collection':'leak_fill','mango':'mango_watch','hunt':'hunt_alerts'}[feature]]
                targets=section.get('groups',[]) if feature=='collection' else section.get('target_groups',[section.get('notify_group')])
                match=section.get('enabled') and row['target'] in map(str,targets)
            if not match:raise ValueError('当前规则已不允许发送到此群')
            if source:require_feature(config,feature,source)

    async def retry(self):
        identity=None
        try:
            body=await request.json(default={})
            rows=self.delivery.query('SELECT * FROM deliveries WHERE id=?',(body.get('id'),))
            if not rows:raise ValueError('发送记录不存在')
            row=rows[0];identity=row['id'];meta=json.loads(row['metadata']);segments=json.loads(row['payload'] or 'null')
            if row['state']!='failed' or not segments:raise ValueError('只有明确失败且内容完整的消息可重试；结果待确认的消息不会重发')
            self.retry_guard(row,meta)
            platform=self.delivery.connected(row['platform'])
            if platform.meta().name!='aiocqhttp':raise ValueError('该平台目前不支持记录重试')
            if row['feature']=='manual':
                groups=await asyncio.wait_for(self.owner.components.manual.webui.group_list(platform),8)
                if row['target'] not in {g['id'] for g in groups}:raise ValueError('机器人已不在目标群中')
            for segment in segments:
                file=segment.get('data',{}).get('file','')
                if segment.get('type')=='image' and file and not file.startswith(('http://','https://','base64://')):
                    from pathlib import Path
                    if not Path(file).is_file() or not Path(file).resolve().is_relative_to(self.delivery.assets.resolve()):raise ValueError('重试图片已过期')
            if not self.delivery.execute("UPDATE deliveries SET state='retrying' WHERE id=? AND state='failed'",(identity,)):
                raise ValueError('该记录已在其他页面重试')
            task=asyncio.current_task();self.jobs.add(task)
            try:
                from .components.magic_bucket.adapter import OneBotAPI
                async def action():
                    self.retry_guard(row,meta)
                    return await OneBotAPI(platform.get_client()).send_group_msg(row['target'],segments)
                with delivery_scope(row['feature'],'retry:'+identity+':'+str(time.time_ns()),**{k:v for k,v in meta.items() if k not in ('feature','event_key','stats')}):
                    result=await self.delivery.send(row['platform'],row['target'],segments,action,row['feature'],api=True,parent=identity,report=meta.get('report'))
                state='unknown' if result.get('_bear_unknown') else 'retried'
                self.delivery.execute('UPDATE deliveries SET state=?,updated=? WHERE id=?',(state,time.time(),identity))
                return {'status':'ok','data':{'message':'重试结果待确认，请核对发送记录' if state=='unknown' else '重试已完成，可查看新发送记录'}}
            except SafeDeliveryFailure:
                self.delivery.execute("UPDATE deliveries SET state='failed',detail='重试明确失败，请查看子记录',updated=? WHERE id=?",(time.time(),identity))
                raise
            except BaseException:
                self.delivery.execute("UPDATE deliveries SET state='unknown',detail='重试被中断或失败，请查看子记录',updated=? WHERE id=?",(time.time(),identity))
                raise
            finally:self.jobs.discard(task)
        except Exception as exc:return {'status':'error','message':str(exc) or type(exc).__name__}

    def start(self):self.watchdog.start()

    async def close(self):
        await self.watchdog.close()
        for task in list(self.jobs):task.cancel()
        await asyncio.gather(*self.jobs,return_exceptions=True)
        self.history.close()
        self.delivery.close()
