"""Per-feature switches and opt-in group restrictions. Empty scopes inherit routes."""
import copy
import re
from .quiet_hours import QuietHours

FEATURES = {
    'sightings': '网站 S 怪播报', 'forwarding': '群消息转发', 'text': '文本与地图',
    'collection': '查漏与采集', 'mango': '芒果广播', 'hunt': '狩猎提醒',
    'trains': '车表与指令', 'manual': '手动群发', 'preview': '效果预览',
}


class FeatureDisabled(QuietHours):
    pass


def control(config, feature):
    value = config.get('feature_controls', {}).get(feature, {})
    return {'enabled': value.get('enabled', True), 'groups': list(value.get('groups', []))}


def allows(config, feature, group=None):
    value = control(config, feature)
    if value['enabled'] is not True:
        return False
    return not value['groups'] or (group is not None and str(group) in value['groups'])


def require_feature(config, feature, group=None):
    if not allows(config, feature, group):
        raise FeatureDisabled(FEATURES[feature] + '已关闭，或该群不在本功能的生效范围内')


def enabled(config, feature):
    return control(config, feature)['enabled'] is True


def validate_control(value):
    if not isinstance(value, dict) or set(value) != {'enabled', 'groups'} or type(value['enabled']) is not bool:
        raise ValueError('功能配置需要启用开关和群号列表')
    groups = value['groups']
    if not isinstance(groups, list) or len(groups) > 100 or any(
            not isinstance(g, str) or not re.fullmatch(r'[A-Za-z0-9]{1,128}', g) for g in groups):
        raise ValueError('每行只填写一个群号，最多 100 个，不需要英文前缀')
    return {'enabled': value['enabled'], 'groups': list(dict.fromkeys(groups))}


def updated_controls(config, feature, value):
    if feature not in FEATURES:
        raise ValueError('未知功能')
    result = copy.deepcopy(config.get('feature_controls', {}))
    result[feature] = validate_control(value)
    return result


def magic_profile(settings, profile):
    """Disable builders without modifying the user's stored sub-switches/tables."""
    parent = getattr(settings, 'parent', None)
    if parent is None:
        return profile
    if not control(parent, 'text')['enabled']:
        for section in ('map_names', 'map_images', 'decorations', 'completions'):
            profile[section]['enabled'] = False
        profile['map_names']['note_emoji'] = ''
        profile['completions']['aliases'] = {}
    for feature, sections in {'collection': ('leak_fill', 'spawn_watch'),
                              'mango': ('mango_watch',), 'hunt': ('hunt_alerts',)}.items():
        if not control(parent, feature)['enabled']:
            for section in sections:
                profile[section]['enabled'] = False
    return profile
