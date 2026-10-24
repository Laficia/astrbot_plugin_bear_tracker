"""Read-only, per-platform group names for the authenticated console."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path


class GroupDirectory:
    ttl = 300
    retry_after = 30
    timeout = 4

    def __init__(self, context, data_dir=None):
        self.context = context
        self.path = Path(data_dir) / 'group_names.json' if data_dir else None
        self.cache = {}
        self.checked = {}
        self.locks = {}
        if self.path and self.path.is_file():
            try:
                if self.path.stat().st_size > 2 * 1024 * 1024:
                    return
                data = json.loads(self.path.read_text('utf-8'))
                for pid, row in data.items():
                    if isinstance(pid, str) and isinstance(row, dict):
                        self.cache[pid] = {'groups': self.normalize(row.get('groups')),
                                           'synced_at': float(row.get('synced_at') or 0)}
            except (OSError, ValueError, TypeError, AttributeError):
                self.cache = {}

    @staticmethod
    def normalize(groups):
        if not isinstance(groups, list):
            raise ValueError('群列表格式无效')
        result = {}
        for group in groups[:4000]:
            if not isinstance(group, dict):
                continue
            gid = str(group.get('group_id', group.get('id', '')))
            name = group.get('group_name', group.get('name', ''))
            if gid and len(gid) <= 128 and isinstance(name, str) and name.strip():
                result[gid] = {'id': gid, 'name': name.strip()[:256]}
        return list(result.values())

    def platform(self, pid):
        manager = getattr(self.context, 'platform_manager', None)
        for platform in getattr(manager, 'platform_insts', []):
            meta = platform.meta()
            if meta.id == pid:
                return platform
        raise ValueError('请选择已接入的机器人')

    def save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.cache, ensure_ascii=False), 'utf-8')
            temporary.replace(self.path)
        except OSError:
            # Display metadata must never interrupt message routing or saves.
            pass

    async def query(self, pid, force=False):
        platform = self.platform(pid)
        if platform.meta().name != 'aiocqhttp':
            return {'platform_id': pid, 'groups': [], 'synced_at': 0,
                    'cached': False, 'stale': True,
                    'message': '该平台暂不提供群名称查询，仍可使用原群号配置。'}
        async with self.locks.setdefault(pid, asyncio.Lock()):
            now = time.monotonic()
            checked, previous = self.checked.get(pid, (0, None))
            if previous and now - checked < (2 if force else self.retry_after if previous['stale'] else self.ttl):
                return {**previous, 'cached': True}
            row = self.cache.get(pid, {'groups': [], 'synced_at': 0})
            try:
                client = platform.get_client()
                result = await asyncio.wait_for(client.api.call_action('get_group_list'), self.timeout)
                if isinstance(result, dict):
                    if result.get('retcode', 0) != 0 or result.get('status', 'ok') != 'ok':
                        raise ValueError('群列表读取失败')
                    result = result.get('data')
                row = {'groups': self.normalize(result), 'synced_at': time.time()}
                self.cache[pid] = row
                self.save()
                response = {**row, 'platform_id': pid, 'cached': False, 'stale': False, 'message': ''}
            except Exception:
                response = {**row, 'platform_id': pid, 'cached': bool(row['synced_at']), 'stale': True,
                            'message': '暂时无法读取机器人群列表；已获取的名称使用缓存，其他群显示“群名未获取”。'}
            self.checked[pid] = (time.monotonic(), response)
            return response
