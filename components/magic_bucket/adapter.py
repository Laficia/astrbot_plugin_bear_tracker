"""Small OneBot v11 boundary; the engine has no framework dependency."""
import asyncio
import base64
import copy
import json
from pathlib import Path
from .runtime_paths import output_path


def merge(base, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def load_config(settings, data_dir):
    root = Path(__file__).parent
    config = json.loads((root/'profile.json').read_text('utf8'))
    overrides = json.loads(settings.get('overrides_json') or '{}')
    if not isinstance(overrides, dict):
        raise ValueError('overrides_json 必须是 JSON 对象')
    merge(config, overrides)
    if settings.get('rules_json', '').strip():
        config['rules'] = json.loads(settings['rules_json'])
    if not isinstance(config['rules'], list):
        raise ValueError('rules_json 必须是 JSON 数组')
    for rule in config['rules']:
        rule.pop('__template_key', None)
        for key in ('source_groups', 'target_groups'):
            rule[key] = [int(group) for group in rule[key]]
    config['send_interval_ms'] = settings.get('send_interval_ms', 500)
    for section, key, base in [('map_images','dir',root), ('mango_watch','map_image',root),
                                ('mango_watch','work_dir',data_dir), ('spawn_watch','path',data_dir),
                                ('hunt_alerts','state',data_dir)]:
        path = Path(config[section][key])
        config[section][key] = str(path if path.is_absolute() else Path(base)/path)
    for section,key,fallback in [('mango_watch','work_dir','mango'),('spawn_watch','path','bot_relay.json'),('hunt_alerts','state','hunt-alerts-state.json')]:
        config[section][key]=output_path(data_dir,config[section][key],fallback)
    alerts = config['hunt_alerts']
    alerts['output_dir']=str(Path(data_dir)/'shared')
    if alerts.get('enabled'):
        shared = Path(alerts.get('root') or '')
        if not alerts.get('root') or not (shared/'bot_relay.json').is_file():
            raise ValueError('超时提醒需要 hunt_alerts.root 指向有效看板数据目录（含 bot_relay.json）')
    return config


class OneBotAPI:
    def __init__(self, bot, self_id=None):
        self.bot = bot
        self.self_id = self_id
        self.qq = self.messaging = self

    async def call(self, action, **kwargs):
        if self.self_id:
            kwargs['self_id'] = self.self_id
        result = await self.bot.api.call_action(action, **kwargs)
        if isinstance(result, dict):
            if result.get('status') == 'failed' or result.get('retcode', 0) not in (0, None):
                raise RuntimeError(f'OneBot {action} failed')
            return result.get('data', result)
        return result

    @staticmethod
    def prepare(segments):
        segments = copy.deepcopy(segments)
        for segment in segments:
            if segment.get('type') != 'image':
                continue
            data = segment.setdefault('data', {})
            value = str(data.get('file', ''))
            if value and not value.startswith(('http://','https://','base64://','file://')):
                path = Path(value)
                if path.is_file():
                    data['file'] = 'base64://' + base64.b64encode(path.read_bytes()).decode('ascii')
        return segments

    async def send_group_msg(self, group_id, segments):
        prepared = await asyncio.to_thread(self.prepare, segments)
        return await self.call('send_group_msg', group_id=int(group_id), message=prepared)

    async def send_group_forward_msg_by_id(self, group_id, ids):
        original = await self.call('get_msg', message_id=int(ids[0]))
        if not isinstance(original, dict) or not isinstance(original.get('message'), list):
            raise ValueError('get_msg 必须返回消息段数组，请检查 OneBot 配置')
        return await self.send_group_msg(group_id, original['message'])

    async def delete_msg(self, message_id):
        return await self.call('delete_msg', message_id=int(message_id))


class Segment:
    def __init__(self, value):
        self.value = value
        self.text = value.get('data', {}).get('text', '')

    def to_dict(self):
        return self.value


class ImageSegment(Segment):
    _type = 'image'


class Message(list):
    def filter_text(self):
        return [s for s in self if s.value.get('type') == 'text']


class Event:
    def __init__(self, event, api):
        self.astr = event
        self.api = api
        raw = event.message_obj.raw_message
        self.group_id = event.get_group_id()
        self.user_id = event.get_sender_id()
        self.self_id = raw.get('self_id') or event.message_obj.self_id
        self.message_id = raw.get('message_id') or event.message_obj.message_id
        self.time = raw.get('time')
        self.message = Message((ImageSegment(s) if s.get('type') == 'image' else Segment(s)) for s in raw['message'])

    async def reply(self, text, at_sender=False):
        return await self.api.send_group_msg(self.group_id, [
            {'type':'reply','data':{'id':str(self.message_id)}},
            {'type':'text','data':{'text':text}}])
