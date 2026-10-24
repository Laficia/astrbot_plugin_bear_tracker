"""Minimal lifecycle/config/API hosting for the preserved component code."""
from collections.abc import MutableMapping
import copy
from astrbot.api import logger
from pathlib import Path

from astrbot.api.star import StarTools
from .quiet_hours import delivery_allowed,require_delivery,GuardedBot,QuietHours
from .feature_controls import enabled,require_feature

NAME = 'astrbot_plugin_bear_tracker'


class Component:
    """No Star subclass: only the owning plugin registers AstrBot handlers."""
    def __init__(self, context):
        self.context = context
        self.logger = logger

class ComponentDataTools:
    def __init__(self, key):
        self.key = key

    def get_data_dir(self, _legacy_name=None):
        path = Path(StarTools.get_data_dir(NAME)) / 'components' / self.key
        path.mkdir(parents=True, exist_ok=True)
        return path


class ComponentConfig(MutableMapping):
    """Partial writes affect one namespace and preserve sibling configuration."""
    def __init__(self, parent, key, defaults):
        self.parent, self.key, self.defaults = parent, key, defaults

    def values_dict(self):
        return self.parent.get(self.key, self.defaults)

    def __getitem__(self, key):
        return self.values_dict()[key]

    def __iter__(self):
        return iter(self.values_dict())

    def __len__(self):
        return len(self.values_dict())

    def __setitem__(self, key, value):
        values = copy.deepcopy(self.values_dict())
        values[key] = value
        self.parent[self.key] = values

    def __delitem__(self, key):
        values = copy.deepcopy(self.values_dict())
        del values[key]
        self.parent[self.key] = values

    def save_config(self, values):
        changed = copy.deepcopy(self.values_dict())
        changed.update(copy.deepcopy(values))
        if hasattr(self.parent, 'save_config'):
            self.parent.save_config({self.key: changed})
        else:
            self.parent[self.key] = changed


class ComponentContext:
    def __init__(self, context, key, owner=None):
        self.context, self.key, self.owner = context, key, owner

    def guard_bot(self,bot):
        return GuardedBot(bot,self.owner.config,getattr(getattr(self.owner,'operations',None),'delivery',None),shared_platform(self.owner.config)) if self.owner else bot

    def __getattr__(self, name):
        return getattr(self.context, name)

    def register_web_api(self, route, handler, methods, desc):
        # Route under the owner plugin for native plugin-view authentication.
        suffix = route.split('/webui/', 1)[1]
        if self.key == 'hunhuan_forward' and suffix in ('settings', 'match'):
            handler = self.automatic_forwarding_disabled
        if self.key=='hunhuan_forward' and suffix=='send' and self.owner:
            original=handler
            async def guarded():
                try:
                    require_delivery(self.owner.config)
                    if not enabled(self.owner.config,'manual'):
                        raise QuietHours('手动群发已关闭，请先在手动群发页启用')
                except QuietHours as exc:return {'status':'error','message':str(exc)}
                return await original()
            handler=guarded
        self.context.register_web_api(
            f'/{NAME}/webui/{self.key}/{suffix}', handler, methods, desc)

    @staticmethod
    async def automatic_forwarding_disabled():
        return {'status': 'error', 'message': '自动转发已统一由魔法水桶管理，请在魔法水桶板块设置规则。'}


def shared_platform(config):
    selected=config.get('automation_platform_id','')
    if selected:return selected
    platforms={origin.split(':',1)[0] for origin in config.get('target_sessions',[]) if ':GroupMessage:' in origin}
    return next(iter(platforms)) if len(platforms)==1 else ''


class SharedMagicConfig(ComponentConfig):
    """The receiver page owns automatic enablement and platform selection."""
    @property
    def platform_id(self):return shared_platform(self.parent)

    def values_dict(self):
        values=copy.deepcopy(super().values_dict())
        values['enabled']=bool(self.parent.get('enabled',True) and shared_platform(self.parent))
        values['bot_uin']=''
        return values

    def save_config(self,values):
        changed=copy.deepcopy(self.parent.get(self.key,self.defaults))
        changed.update({k:copy.deepcopy(v) for k,v in values.items() if k not in ('enabled','bot_uin','send_interval_ms')})
        if hasattr(self.parent,'save_config'):self.parent.save_config({self.key:changed})
        else:self.parent[self.key]=changed


class MergedComponents:
    def __init__(self, plugin):
        from .components.magic_bucket.main import MagicBucket
        from .components.hunhuan_forward.main import HunHuanForwardPlugin
        self.plugin=plugin
        self.magic = MagicBucket(ComponentContext(plugin.context, 'magic_bucket',plugin),
                                 SharedMagicConfig(plugin.config, 'magic_bucket', {
                                     'enabled': False, 'bot_uin': '', 'send_interval_ms': 500,
                                     'rules_json': '', 'overrides_json': '{}'}))
        self.manual = HunHuanForwardPlugin(ComponentContext(plugin.context, 'hunhuan_forward',plugin),
                                          ComponentConfig(plugin.config, 'hunhuan_forward', {
                                              'send_interval_ms': 500, 'rules': []}))
        original_limited=self.manual.engine.limited
        async def limited(action,*args,**kwargs):
            async def checked(*call_args,**call_kwargs):
                if (call_args and isinstance(call_args[0],str) and call_args[0].startswith('send_')) or getattr(action,'__name__','')=='reply':
                    require_delivery(plugin.config)
                    require_feature(plugin.config,'manual',call_kwargs.get('group_id'))
                journal=getattr(getattr(plugin,'operations',None),'delivery',None)
                if journal and call_args and call_args[0]=='send_group_msg':
                    transport=getattr(action,'__self__',None)
                    platform=next((p.meta().id for p in plugin.context.platform_manager.platform_insts if p.get_client() is getattr(transport,'bot',None)), '')
                    from .delivery_journal import delivery_scope
                    with delivery_scope('manual'):
                        return await journal.send(platform,str(call_kwargs.get('group_id','')),call_kwargs.get('message'),lambda:action(*call_args,**call_kwargs),'manual',api=True)
                return await action(*call_args,**call_kwargs)
            return await original_limited(checked,*args,**kwargs)
        self.manual.engine.limited=limited

    async def on_group(self, event):
        # User decision: Magic is the only automatic forwarding/recall handler.
        platform=shared_platform(self.plugin.config)
        if not self.plugin.config.get('enabled',True) or not platform or not delivery_allowed(self.plugin.config):return
        if getattr(event,'get_platform_id',lambda:'')()!=platform:return
        await self.magic.on_group(event)

    async def close(self):
        try:
            await self.manual.terminate()
        finally:
            await self.magic.terminate()
