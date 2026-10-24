"""One Beijing-time sending window for every plugin feature."""
from datetime import datetime,timezone,timedelta
import re
import time

DEFAULT_WINDOW={'start':'00:00','end':'00:00'}
BEIJING=timezone(timedelta(hours=8))


class QuietHours(ValueError):pass


def validate_window(value):
    if not isinstance(value,dict) or set(value)!={'start','end'}:
        raise ValueError('播报区间需要开始和结束时间')
    result={}
    for key in ('start','end'):
        text=value[key]
        if not isinstance(text,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',text):
            raise ValueError('播报区间请使用 HH:MM，范围为 00:00～23:59')
        result[key]=text
    return result


def delivery_allowed(config,now=None):
    try:window=validate_window(config.get('broadcast_window',DEFAULT_WINDOW))
    except ValueError:return False
    start,end=window['start'],window['end']
    if start==end:return True
    current=datetime.fromtimestamp(time.time() if now is None else now,BEIJING).strftime('%H:%M')
    return start<=current<end if start<end else current>=start or current<end


def require_delivery(config):
    if not delivery_allowed(config):
        window=config.get('broadcast_window',DEFAULT_WINDOW)
        if not isinstance(window,dict):window={}
        raise QuietHours('当前为静默时段；播报区间为 '+str(window.get('start','?'))+'—'+str(window.get('end','?'))+'（北京时间）')


async def guarded_send(context,config,origin,chain):
    require_delivery(config)
    from .feature_controls import require_feature
    require_feature(config,'sightings',origin.split(':',2)[-1])
    return await context.send_message(origin,chain)


class GuardedAPI:
    def __init__(self,api,config):self.original,self.config=api,config
    def __getattr__(self,name):return getattr(self.original,name)
    async def call_action(self,action,**kwargs):
        if action.startswith('send_'):require_delivery(self.config)
        return await self.original.call_action(action,**kwargs)


class GuardedBot:
    def __init__(self,bot,config):self.original=bot;self.api=GuardedAPI(bot.api,config)
    def __getattr__(self,name):return getattr(self.original,name)
