"""Authenticated AstrBot plugin-view endpoints and non-sending previews."""
import asyncio
import base64
import copy
import json
import math
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.web import request
from ._host import StarTools
from .adapter import Message, Segment, OneBotAPI
from .s_hunt_names import ForwardBotPlugin, load_config, canonical_hunts, exact_aliases, validate_aliases, common_alias_catalog
from .rules import ForwardRule
from ...quiet_hours import delivery_allowed

PREFIX = '/astrbot_plugin_magic_bucket/webui/'
ROOT = Path(__file__).parent


def effective(settings):
    return load_config(settings, StarTools.get_data_dir('astrbot_plugin_magic_bucket'))


def validate(body):
    if not isinstance(body, dict) or not isinstance(body.get('profile'), dict):
        raise ValueError('配置必须包含 profile 对象')
    enabled = body.get('enabled', False)
    account = str(body.get('bot_uin', '')).strip()
    if not isinstance(enabled, bool) or (account and (not account.isdecimal() or len(account)>20)):
        raise ValueError('请检查启用开关和 QQ 号')
    profile = copy.deepcopy(body['profile'])
    defaults = json.loads((ROOT/'profile.json').read_text('utf8'))
    if set(profile) != set(defaults):
        raise ValueError('配置段不完整，请刷新页面后重试')
    interval = profile.get('send_interval_ms')
    if type(interval) is not int or not 0 <= interval <= 60000:
        raise ValueError('发送间隔应在 0～60000 毫秒之间')
    rules = profile.get('rules')
    if not isinstance(rules, list) or len(rules)>100:
        raise ValueError('转发规则最多 100 条')
    for index, rule in enumerate(rules):
        try:
            if not isinstance(rule, dict):
                raise ValueError('规则应为对象')
            for key in ('source_groups','target_groups'):
                if not isinstance(rule.get(key), list) or not 1<=len(rule[key])<=100:
                    raise ValueError('源群和目标群必须各有 1～100 项')
                if any(isinstance(v,bool) or not str(v).isdecimal() or int(v)<=0 or len(str(v))>20 for v in rule[key]):
                    raise ValueError('群号应为正整数')
                rule[key]=list(dict.fromkeys(int(v) for v in rule[key]))
            if not isinstance(rule.get('keywords'),list) or not rule['keywords'] or any(not isinstance(w,str) or not w.strip() for w in rule['keywords']):
                raise ValueError('关键词不能为空')
            ForwardRule.from_dict(rule)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f'第 {index+1} 条规则：{exc}') from exc
    for section in ('map_names','map_images','decorations','completions','leak_fill','spawn_watch','mango_watch','hunt_alerts'):
        if not isinstance(profile.get(section),dict) or type(profile[section].get('enabled')) is not bool:
            raise ValueError(section+' 缺少有效开关')
    for section,key in [('map_names','aliases'),('completions','pairs')]:
        value=profile[section].get(key)
        if not isinstance(value,dict) or any(not isinstance(k,str) or not isinstance(v,str) or not k.strip() or not v.strip() for k,v in value.items()):
            raise ValueError(section+'.'+key+' 应为非空文本的键值表')
    validate_aliases(profile)
    for section,keys in [('leak_fill',('window_seconds','backlook_seconds')),('spawn_watch',('dedupe_seconds','keep_seconds')),('mango_watch',('cooldown_seconds','timeout_seconds','pending_ttl_seconds'))]:
        for key in keys:
            value=profile[section].get(key)
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<value<=604800:
                raise ValueError(section+'.'+key+' 应为有效正数')
    for section,keys in [('leak_fill',('groups',)),('mango_watch',('source_groups','target_groups'))]:
        for key in keys:
            values=profile[section].get(key)
            if not isinstance(values,list) or len(values)>100 or any(isinstance(v,bool) or not str(v).isdecimal() or int(v)<=0 for v in values):
                raise ValueError(section+'.'+key+' 应为群号列表')
            profile[section][key]=list(dict.fromkeys(int(v) for v in values))
    for key in ('source_group','notify_group'):
        if not str(profile['hunt_alerts'].get(key,'')).isdecimal():
            raise ValueError('hunt_alerts.'+key+' 应为群号')
    values={'enabled':enabled,'bot_uin':account,'send_interval_ms':interval,
            'rules_json':json.dumps(rules,ensure_ascii=False),
            'overrides_json':json.dumps(profile,ensure_ascii=False)}
    # Also verifies configured paths for enabled reminders before persistence.
    effective(values)
    return values


class PreviewAPI:
    def __init__(self, text):
        self.qq=self.messaging=self
        self.text=text
        self.sent=[]

    async def send_group_msg(self, group, segments):
        self.sent.append({'group':str(group),'segments':segments})
        return {'message_id':len(self.sent)}

    async def send_group_forward_msg_by_id(self, group, ids):
        return await self.send_group_msg(group,[{'type':'text','data':{'text':self.text}}])


class MagicWebUI:
    def __init__(self, plugin):
        self.plugin=plugin
        self.lock=asyncio.Lock()
        for name,handler,method in [('status',self.status,'GET'),('settings',self.settings,'POST'),('preview',self.preview,'POST')]:
            plugin.context.register_web_api(PREFIX+name,handler,[method],'魔法水桶控制台')

    async def status(self):
        p=self.plugin
        platforms=[]
        manager=getattr(p.context,'platform_manager',None)
        for platform in getattr(manager,'platform_insts',[]):
            meta=platform.meta()
            if meta.name=='aiocqhttp':
                clients=getattr(platform.get_client(),'_wsr_api_clients',{})
                platforms.append({'id':meta.id,'accounts':[str(a) for a in clients] if isinstance(clients,dict) else []})
        raw=json.loads((ROOT/'profile.json').read_text('utf8'))
        from .adapter import merge
        overrides=json.loads(p.settings.get('overrides_json') or '{}')
        merge(raw,overrides)
        exact_aliases(raw,overrides)
        if p.settings.get('rules_json','').strip(): raw['rules']=json.loads(p.settings['rules_json'])
        raw['send_interval_ms']=p.settings.get('send_interval_ms',500)
        return {'status':'ok','data':{'enabled':p.settings.get('enabled',False),'bot_uin':p.settings.get('bot_uin',''),
            'automation_platform_id':getattr(p.settings,'platform_id',''),
            'within_broadcast_window':delivery_allowed(p.context.owner.config) if getattr(p.context,'owner',None) else True,
            'profile':raw,'s_hunt_names':canonical_hunts(raw),'s_hunt_alias_source':common_alias_catalog()['source'],
            'platforms':platforms,'version':'1.1.0','received':p.received,
            'last_received':p.last_received,'last_error':p.last_error,'engines':len(p.engines),
            'pending':sum(len(e._leak_tasks) for e in p.engines.values()),
            'recalls':sum(len(e._recall_cache) for e in p.engines.values()),
            'map_count':len(list(Path(profile['map_images']['dir']).glob('*.png')))}}

    async def settings(self):
        body=await request.json(default={})
        try:
            values=validate(body)
            async with self.lock:
                p=self.plugin
                parent=getattr(p.settings,'parent',{})
                # Single-feature enablement is owned by the new page header.
                overrides=json.loads(values['overrides_json'])
                existing=json.loads(p.settings.get('overrides_json') or '{}')
                for feature,section in [('mango','mango_watch'),('hunt','hunt_alerts')]:
                    if feature in parent.get('feature_controls',{}):
                        overrides[section]['enabled']=existing.get(section,{}).get('enabled',False)
                values['overrides_json']=json.dumps(overrides,ensure_ascii=False)
                changed=any(values[k]!=p.settings.get(k) for k in values)
                if hasattr(p.settings,'save_config'):
                    p.settings.save_config(values)
                else:
                    p.settings.update(values)
                if changed:
                    await p.reset_engines()
            return {'status':'ok','data':{'message':'配置已保存并生效'}}
        except (ValueError,TypeError,KeyError,OSError) as exc:
            return {'status':'error','message':str(exc)}

    async def preview(self):
        body=await request.json(default={})
        engine=None
        try:
            values=validate(body)
            text=body.get('text','')
            if not isinstance(text,str) or len(text)>4000:
                raise ValueError('预览文字最多 4000 字')
            group=int(body.get('group') or json.loads(values['rules_json'])[0]['source_groups'][0])
            from ...feature_controls import require_feature,magic_profile
            from ...scoped_magic import ScopedMagicEngine
            settings=getattr(self.plugin,'settings',{})
            parent=getattr(settings,'parent',{})
            require_feature(parent,'preview',group)
            cfg=effective(values)
            cfg=magic_profile(settings,cfg)
            # Preview executes the real formatting pipeline using an in-memory
            # transport. No QQ API, timers, persistent records or mango HTTP calls.
            for section in ('hunt_alerts','leak_fill','spawn_watch','mango_watch'):
                cfg[section]['enabled']=False
            cfg['send_interval_ms']=0
            api=PreviewAPI(text)
            engine=ScopedMagicEngine(cfg,api,parent)
            await engine.on_load()
            event=SimpleNamespace(group_id=group,user_id=1,self_id=2,message_id='0',time=None,
                                  message=Message([Segment({'type':'text','data':{'text':text}})]))
            async def reply(**kwargs): pass
            event.reply=reply
            await engine.onGroupMessageReceived(event)
            messages=[]
            # Only return one copy per unique group/text/map combination.
            for row in api.sent[:100]:
                message={'group':row['group'],'text':'','image':None}
                for seg in row['segments']:
                    if seg['type']=='text': message['text']+=seg['data']['text']
                    if seg['type']=='image':
                        path=Path(seg['data']['file'])
                        if (path.is_relative_to(ROOT) or path.is_relative_to(Path(cfg['map_images']['dir']))) and path.is_file():
                            message['image']='data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()
                messages.append(message)
            return {'status':'ok','data':{'messages':messages,'matched':len(engine.manager.find_matching_rules(text,group))}}
        except (ValueError,TypeError,KeyError,IndexError,OSError) as exc:
            return {'status':'error','message':str(exc)}
        finally:
            if engine is not None and hasattr(engine,'_recall_cache'): await engine.on_close()
