"""Scope the preserved engine per event, without sharing mutable event state."""
from contextvars import ContextVar
import re
import time
from .delivery_journal import delivery_scope, SCOPE
from .components.magic_bucket.engine import MissingForwardMessageID

class MergedDelivery(MissingForwardMessageID):pass
from .feature_controls import allows, enabled
from .components.magic_bucket.s_hunt_names import ForwardBotPlugin as BaseEngine

_source_group = ContextVar('bear_magic_source_group', default=None)
_SCOPED = {'_map_names': 'text', '_map_images': 'text', '_decorator': 'text',
           '_completer': 'text', '_note_pattern': 'text', '_leak': 'collection',
           '_spawn': 'collection', '_mango': 'mango', '_hunt_alerts': 'hunt'}


class ScopedRules:
    def __init__(self, original, config):
        self.original, self.config = original, config

    def __getattr__(self, name):
        return getattr(self.original, name)

    def find_matching_rules(self, message, source_group):
        if not allows(self.config, 'forwarding', source_group):
            return []
        return self.original.find_matching_rules(message, source_group)


class ScopedMagicEngine(BaseEngine):
    def __init__(self, config, api, owner_config):
        self.owner_config = owner_config
        super().__init__(config, api)

    async def on_load(self):
        await super().on_load()
        self.manager = ScopedRules(self.manager, self.owner_config)

    def __getattribute__(self, name):
        # The original pipeline uses None for disabled processors. ContextVar
        # keeps simultaneous events and their inherited background tasks isolated.
        feature = _SCOPED.get(name)
        if feature:
            config = object.__getattribute__(self, 'owner_config')
            group = _source_group.get()
            if not enabled(config, feature) or (group is not None and not allows(config, feature, group)):
                return None
        return super().__getattribute__(name)

    async def onGroupMessageReceived(self, event):
        token = _source_group.set(str(event.group_id))
        try:
            with delivery_scope('forwarding',str(getattr(event,'message_id','')) or None,source_group=str(event.group_id),source_text=''.join(s.text for s in event.message.filter_text()) if hasattr(event,'message') else '',expires_at=time.time()+300):
                return await super().onGroupMessageReceived(event)
        finally:
            _source_group.reset(token)

    @staticmethod
    def _extract_message_id(result):
        if isinstance(result,dict):
            if result.get('_bear_merged'):raise MergedDelivery('相同报告已合并')
            if result.get('_bear_unknown'):raise MissingForwardMessageID('发送结果待确认，不重复发送')
        return BaseEngine._extract_message_id(result)

    async def _send_segments(self,target_group,segments,rule_name,label,max_retries):
        feature='collection' if rule_name=='leak_fill' else 'mango' if 'mango' in rule_name else 'hunt' if rule_name=='hunt_overdue' else (SCOPE.get() or {}).get('feature','forwarding')
        with delivery_scope(feature,dedup=label not in ('补发通知','补发撤回码')):
            try:return await super()._send_segments(target_group,segments,rule_name,label,max_retries)
            except MergedDelivery:return None

    async def safe_forward_message(self,target_group,message_id,rule_name,max_retries=2):
        # A following separate map must not be lost when the text already exists.
        rules=getattr(self,'config',{}).get('rules',[])
        attach=any(r.get('name')==rule_name and r.get('attach_map_image') for r in rules)
        with delivery_scope('forwarding',dedup=not attach):
            try:return await super().safe_forward_message(target_group,message_id,rule_name,max_retries)
            except MergedDelivery:return None

    async def _reply(self,event,text):
        stats=(SCOPE.get() or {}).get('stats',{})
        merged=stats.get('merged',0)
        if merged and text.startswith(('已转发','本次没有可缓存')):
            if not stats.get('sent'):return
            text=re.sub(r'\n(\d+) 条发送失败。',lambda m:'\n'+str(max(0,int(m[1])-merged))+' 条发送失败。' if int(m[1])>merged else '',text)
            text+='\n相同的 S 怪报告已按群合并。'
        with delivery_scope('commands',dedup=False):
            return await super()._reply(event,text)
