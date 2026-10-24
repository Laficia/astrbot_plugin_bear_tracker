"""Scope the preserved engine per event, without sharing mutable event state."""
from contextvars import ContextVar
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
            return await super().onGroupMessageReceived(event)
        finally:
            _source_group.reset(token)
