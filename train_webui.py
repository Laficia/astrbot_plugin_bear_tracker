"""Authenticated train schedule configuration and non-sending previews."""
import copy
import time
import base64
from astrbot.api.web import request
from .train_schedule import (CENTERS, DEFAULT_CONFIG, DEFAULT_RULE, SUMMARY_FIELDS,
                             ITEM_FIELDS, SOURCE, flatten_schedules, validate_config)
from .train_hunt import validate_config as validate_hunt,DEFAULT_CONFIG as HUNT_DEFAULTS,FIELDS as HUNT_FIELDS


class TrainWebUI:
    def __init__(self,plugin):
        self.plugin=plugin
        for name,method in [('state','GET'),('settings','POST'),('query','POST'),('table','POST'),('hunt-settings','POST'),('hunt-preview','POST')]:
            plugin.context.register_web_api('/astrbot_plugin_bear_tracker/webui/train/'+name,
                                            getattr(self,name.replace('-','_')),[method],'车表与指令')

    async def state(self):
        client=self.plugin.trains.client
        return {'status':'ok','data':{'config':copy.deepcopy(self.plugin.trains.config()),
                'default_rule':copy.deepcopy(DEFAULT_RULE),'centers':CENTERS,
                'summary_variables':SUMMARY_FIELDS,'item_variables':ITEM_FIELDS,
                'source':SOURCE,'fetched_at':client.fetched_at,'last_error':client.last_error,
                'hunt_config':copy.deepcopy(self.plugin.train_hunt.config()) if hasattr(self.plugin,'train_hunt') else copy.deepcopy(HUNT_DEFAULTS),
                'hunt_variables':HUNT_FIELDS,'hunt_default_template':HUNT_DEFAULTS['template'],
                'hunt_last_error':getattr(getattr(self.plugin,'train_hunt',None),'last_error','')}}

    async def settings(self):
        try:
            body=await request.json(default={});config=validate_config(body.get('config'))
            if hasattr(self.plugin,'train_hunt'):
                validate_hunt(self.plugin.config.get('train_hunt',{}),[c for r in config['rules'] for c in r['commands']])
            if 'trains' in self.plugin.config.get('feature_controls',{}):
                config['enabled']=self.plugin.config.get('train_commands',{}).get('enabled',True)
            if hasattr(self.plugin.config,'save_config'):self.plugin.config.save_config({'train_commands':config})
            else:self.plugin.config['train_commands']=config
            return {'status':'ok','data':{'message':'车表指令已保存并立即生效'}}
        except (ValueError,TypeError,OSError) as exc:return {'status':'error','message':str(exc)}

    async def query(self):
        try:
            body=await request.json(default={});text=body.get('command','')
            if not isinstance(text,str) or len(text)>200:raise ValueError('测试指令最多 200 字')
            result=await self.plugin.trains.query(text,body.get('config'))
            if result is None:raise ValueError('未命中已启用的指令。请检查总开关、规则开关及指令名称。')
            return {'status':'ok','data':result}
        except (ValueError,TypeError) as exc:return {'status':'error','message':str(exc)}

    async def table(self):
        try:
            body=await request.json(default={});center=body.get('center','bean')
            if center not in (*CENTERS,'all'):raise ValueError('请选择大区')
            force=body.get('force',False)
            if type(force) is not bool:raise ValueError('刷新参数无效')
            data=await self.plugin.trains.client.fetch(force)
            rows=flatten_schedules(data,time.time(),24,center)
            if hasattr(self.plugin,'train_hunt'):rows=self.plugin.train_hunt.enrich(data,rows)
            return {'status':'ok','data':{'rows':rows,'source':SOURCE,'fetched_at':self.plugin.trains.client.fetched_at}}
        except (ValueError,TypeError) as exc:return {'status':'error','message':str(exc)}

    async def hunt_settings(self):
        try:
            body=await request.json(default={})
            commands=[c for r in self.plugin.trains.config()['rules'] for c in r['commands']]
            config=validate_hunt(body.get('config'),commands)
            if hasattr(self.plugin.config,'save_config'):self.plugin.config.save_config({'train_hunt':config})
            else:self.plugin.config['train_hunt']=config
            return {'status':'ok','data':{'message':'狩猎车提醒与图片指令已保存'}}
        except (ValueError,TypeError,OSError) as exc:return {'status':'error','message':str(exc)}

    async def hunt_preview(self):
        try:
            body=await request.json(default={});kind=body.get('kind','text');argument=body.get('argument','')
            if not isinstance(argument,str) or len(argument)>200:raise ValueError('预览参数过长')
            config=validate_hunt(body.get('config'),[c for r in self.plugin.trains.config()['rules'] for c in r['commands']])
            if kind=='text':
                rows=await self.plugin.train_hunt.rows(config=config)
                row=next((r for r in rows if r['编号']==argument),rows[0] if rows else None)
                if row is None:raise ValueError('网站暂无可预览的班次')
                text=self.plugin.train_hunt.message(row,row['departure_at']-config['minutes']*60,config)
                return {'status':'ok','data':{'text':text}}
            path=await self.plugin.train_hunt.render(kind,argument,config)
            return {'status':'ok','data':{'image':'data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()}}
        except Exception as exc:return {'status':'error','message':str(exc)}
