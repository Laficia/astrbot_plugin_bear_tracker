"""Validated trigger cadence and per-hunt elapsed real hours since gate opening."""
import math

DEFAULT_INTERVAL = 60
MIN_INTERVAL, MAX_INTERVAL = 20, 3600
MAX_DELAY_HOURS = 168


def validate_interval(value):
    if type(value) is not int or not MIN_INTERVAL <= value <= MAX_INTERVAL:
        raise ValueError('可触发检查间隔须为 20～3600 秒的整数')
    return value


def validate_delays(value, allowed):
    if not isinstance(value, dict) or any(key not in allowed for key in value):
        raise ValueError('进闸等待时间需要按支持提醒的 S 怪设置')
    result = {}
    for key, hours in value.items():
        if type(hours) not in (int, float) or not math.isfinite(hours) or not 0 <= hours <= MAX_DELAY_HOURS:
            raise ValueError('每只 S 怪的进闸等待时间须为 0～168 小时，可填写小数')
        result[key] = float(hours)
    return result


def delays(config, allowed):
    stored = validate_delays(config.get('trigger_delay_hours', {}), allowed)
    return {key: stored.get(key, 0.0) for key in allowed}


def delay_ready(event, hours, now):
    if hours == 0:
        return True
    opened = event.get('gate_open_at')
    return type(opened) in (int, float) and math.isfinite(opened) and now >= opened + hours * 3600
