"""Bounded, atomic configuration snapshots; no changes to stored settings."""
import copy
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def differences(before, after, prefix=''):
    rows = []
    for key in sorted(set(before) | set(after)):
        path = (prefix + '.' if prefix else '') + key
        a, b = before.get(key), after.get(key)
        if key in before and key in after and isinstance(a, dict) and isinstance(b, dict):
            rows.extend(differences(a, b, path))
        elif a != b or (key in before) != (key in after):
            secret = bool(re.search(r'token|secret|password|credential|api_key', path, re.I))
            show = lambda v: '（敏感字段已隐藏）' if secret else json.dumps(v, ensure_ascii=False)[:1600]
            rows.append({'path': path, 'before': show(a), 'after': show(b)})
    return rows


class ConfigHistory:
    def __init__(self, config, folder):
        self.config = config
        self.folder = Path(folder) / 'config_history'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.original_save = getattr(config, 'save_config', None)
        self.original_async = getattr(config,'save_config_async',None)
        self.snapshot('启动配置')
        if self.original_save:
            self.hook = self.tracked_save
            object.__setattr__(config, 'save_config', self.hook)
        if self.original_async:
            self.async_hook=self.tracked_async
            object.__setattr__(config,'save_config_async',self.async_hook)

    def read(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
            raise ValueError('配置历史编号无效')
        path = self.folder / (identity + '.json')
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('配置历史不存在或过大')
        row = json.loads(path.read_text('utf-8'))
        if not isinstance(row.get('config'), dict) or fingerprint(row['config']) != row.get('fingerprint'):
            raise ValueError('历史配置校验失败')
        return row

    def entries(self):
        rows = []
        for path in self.folder.glob('*.json'):
            try:
                row = self.read(path.stem)
                rows.append({k: row[k] for k in ('id', 'at', 'reason', 'fingerprint')})
            except (ValueError, OSError, KeyError):
                continue
        return sorted(rows, key=lambda r: r['at'], reverse=True)

    def snapshot(self, reason, config=None):
        with self.lock:
            value = copy.deepcopy(dict(self.config) if config is None else config)
            digest = fingerprint(value)
            entries = self.entries()
            if entries and entries[0]['fingerprint'] == digest:
                return entries[0]['id']
            identity = uuid.uuid4().hex
            row = {'id': identity, 'at': time.time(), 'reason': reason, 'fingerprint': digest, 'config': value}
            target = self.folder / (identity + '.json')
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(row, ensure_ascii=False, indent=2, allow_nan=False), 'utf-8')
            temporary.replace(target)
            for item in entries[49:]:
                (self.folder / (item['id'] + '.json')).unlink(missing_ok=True)
            return identity

    def tracked_save(self, values=None, **kwargs):
        with self.lock:
            self.snapshot('保存前自动备份')
            result = self.original_save(values, **kwargs)
            self.snapshot('配置已保存')
            return result

    async def tracked_async(self,values=None,**kwargs):
        self.snapshot('异步保存前自动备份')
        result=await self.original_async(values,**kwargs)
        self.snapshot('配置已保存')
        return result

    def diff(self, identity):
        row = self.read(identity)
        return {'id': identity, 'at': row['at'], 'current': fingerprint(dict(self.config)),
                'changes': differences(dict(self.config), row['config'])}

    def restore(self, identity, expected):
        with self.lock:
            if fingerprint(dict(self.config)) != expected:
                raise ValueError('配置已被其他页面修改，请重新查看差异后恢复')
            previous = copy.deepcopy(dict(self.config))
            target = self.read(identity)['config']
            self.snapshot('恢复前自动备份', previous)
            try:
                self.config.clear()
                self.config.update(copy.deepcopy(target))
                if self.original_save:
                    self.original_save()
            except BaseException:
                self.config.clear()
                self.config.update(previous)
                raise
            self.snapshot('已恢复历史配置')

    def close(self):
        if self.original_async and getattr(self.config,'save_config_async',None)==self.async_hook:
            object.__setattr__(self.config,'save_config_async',self.original_async)
        if self.original_save and getattr(self.config, 'save_config', None) == self.hook:
            object.__setattr__(self.config, 'save_config', self.original_save)
