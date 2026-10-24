"""Persist SS notices and identify unreported S kills; no network on import."""
import hashlib
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import math
from pathlib import Path
import re
import time
import unicodedata
from .runtime_paths import data_directory


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.new')
    temp.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    temp.chmod(mode)
    os.replace(temp, path)


class HuntAlerts:
    def __init__(self, catalog, aliases=None, root=None, state=None, clock=time.time, history_fetcher=None, output_dir=None):
        self.root, self.state_path, self.clock = Path(root) if root else data_directory()/'shared', Path(state) if state else data_directory()/'hunt-alerts-state.json', clock
        self.output_dir=Path(output_dir) if output_dir else self.root
        self.history_fetcher = history_fetcher or self.fetch_history
        self.catalog = {h['k']: h for h in catalog['hunts']}
        self.worlds = set(catalog['worlds'])
        self.overdue_worlds = set(catalog.get('overdueWorlds', catalog['worlds']))
        if not self.overdue_worlds <= self.worlds:
            raise ValueError('Unknown overdue reminder world')
        self.maps = {h['regionCn']: h['regionCn'] for h in catalog['hunts']}
        for canonical, names in (aliases or {}).items():
            if isinstance(names, str) and names in self.maps:
                self.maps[canonical] = names
                continue
            if canonical in self.maps:
                for name in names if isinstance(names, list) else [names]:
                    if isinstance(name, str) and len(name) >= 2:
                        self.maps[name] = canonical
        try:
            self.state = read_json(self.state_path)
        except FileNotFoundError:
            self.state = {'ss': {}, 'alerts': {}}
        if not isinstance(self.state.get('ss'), dict) or not isinstance(self.state.get('alerts'), dict):
            raise ValueError('Invalid hunt alert state')
        self.state.setdefault('rounds', {})
        if not isinstance(self.state['rounds'], dict):
            raise ValueError('Invalid hunt alert rounds')
        # Retain the old reminder receipts and their cooldown across migration.
        for identity, row in self.state['alerts'].items():
            if row.get('world') and row.get('hunt') and 'at' in row:
                self.state['rounds'].setdefault(identity, dict(row, nextCheckAt=row.get('attemptedAt', 0)+1800, status='active'))

    @staticmethod
    def fetch_history(world, hunt):
        payload = json.dumps({'HuntName': hunt, 'WorldName': world}, ensure_ascii=False).encode()
        from urllib.request import Request, urlopen
        request = Request('https://tracker.beartoolkit.com/api/getHuntInfo',
                          data=payload, headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=12) as response:
            return json.loads(response.read(4_000_000))

    @staticmethod
    def matching_death(history, first_at, checked_at):
        if not isinstance(history, list):
            raise ValueError('Invalid Tracker kill history')
        matches = []
        for row in history:
            if not isinstance(row, dict):
                raise ValueError('Invalid Tracker kill history row')
            at = row.get('Time')
            if type(at) not in (int, float) or not math.isfinite(at):
                raise ValueError('Invalid Tracker death time')
            if row.get('Canceled') or row.get('Reporter') == 'historyMaint':
                continue
            # Time is the reported death time; ReportTime is submission time.
            # Reporter home world never changes the queried hunt/world scope.
            if first_at-300 <= at <= checked_at+30:
                matches.append(at)
        return max(matches) if matches else None

    def save(self):
        write_json(self.state_path, self.state)

    def publish(self):
        now = self.clock()
        events = [v for v in self.state['ss'].values() if 0 <= now-v['at'] < 3600]
        write_json(self.output_dir/'ss_events.json', {'generated': int(now), 'events': sorted(events, key=lambda v: -v['at'])}, 0o644)

    def observe_ss(self, text, group, message_id='', at=None):
        if str(group) != getattr(self, 'source_group', '1034151994'):
            return False
        text = unicodedata.normalize('NFKC', text)
        if not re.search(r'(?i)SS\s*前置', text) or re.search(r'未出|没出|没有|测试|取消|撤回|假消息|预告|[?？]', text):
            return False
        now = self.clock()
        at = now if at is None else float(at)
        if not now-3600 <= at <= now+30:
            return False
        maps = {canonical for name, canonical in self.maps.items() if name in text}
        worlds = [world for world in self.worlds if world in text]
        if not maps or len(worlds) > 1:
            return False
        world = worlds[0] if worlds else ''
        changed = False
        for region in sorted(maps):
            key = world + '|' + region
            old = self.state['ss'].get(key)
            if old and at-old['at'] < 600:
                continue
            self.state['ss'][key] = {'world': world, 'map': region, 'at': at, 'sourceGroup': getattr(self, 'source_group', '1034151994'), 'kind': 'ss_precondition'}
            changed = True
        if changed:
            self.save()
            self.publish()
        return changed

    def due(self):
        """Find rounds due for a fresh history check; cached timers are not deaths."""
        now = self.clock()
        kills, spawns = {}, {}
        relay = read_json(self.root/'bot_relay.json').get('relay', {})
        for world, hunts in relay.items():
            for key, entry in hunts.items():
                spawns[(world, key)] = max(spawns.get((world, key), 0), float(entry[0]))
        presence_path = self.root/'tracker-presence.json'
        if presence_path.exists():
            presence = read_json(presence_path)
            for world, hunts in presence.get('relay', {}).items():
                for key, entry in hunts.items():
                    at = float(entry[0])
                    current = spawns.get((world, key), 0)
                    death = presence.get('kills', {}).get(world, {}).get(key, 0)
                    # Use Tracker's first report for this still-live round;
                    # later QQ sightings must not restart its 30-minute clock.
                    spawns[(world, key)] = at if at > death and (not current or abs(current-at) < 72000) else max(current, at)
            for world, hunts in presence.get('kills', {}).items():
                for key, at in hunts.items():
                    kills[(world, key)] = max(kills.get((world, key), 0), float(at))
        # Alternate snapshots can contain report/observation times. Those are
        # not explicit death events and must never cancel an overdue alert.
        reports = read_json(self.root/'manual-reports.json')
        for row in reports:
            if row.get('type') == 'kill' and not row.get('withdrawnAt'):
                scope = (row['world'], row['hunt'])
                kills[scope] = max(kills.get(scope, 0), float(row['killAt']))
        maintenance_path = self.root/'maintenance-reports.json'
        maintenance = read_json(maintenance_path) if maintenance_path.exists() else []
        due = []
        for (world, key), at in sorted(spawns.items()):
            if world not in self.overdue_worlds or key not in self.catalog or not 1800 <= now-at < 72000:
                continue
            if kills.get((world, key), 0) >= at:
                continue
            if any(not r.get('withdrawnAt') and world in r.get('worlds', []) and r.get('endAt', 0) >= at for r in maintenance):
                continue
            identity = hashlib.sha256(f'{world}|{key}|{at:.3f}'.encode()).hexdigest()[:24]
            old = next(((i, r) for i, r in self.state['rounds'].items() if r.get('world') == world and r.get('hunt') == key and abs(r.get('at', 0)-at) < 600), None)
            if old:
                identity, row = old
                if row.get('status') == 'handled' or now < row.get('nextCheckAt', 0):
                    continue
                at = row['at']
            due.append({'id': identity, 'world': world, 'hunt': key, 'name': self.catalog[key]['cn'], 'map': self.catalog[key]['regionCn'], 'at': at})
        return due

    async def tick(self, send):
        self.publish()
        due = self.due()[:10]
        if not due:
            return
        pending = []
        for row in due:
            now = self.clock()
            try:
                history = await asyncio.to_thread(self.history_fetcher, row['world'], row['hunt'])
                now = self.clock()
                matched = self.matching_death(history, row['at'], now)
            except Exception as exc:
                self.state['rounds'][row['id']] = dict(row, status='history_unavailable', lastCheckAt=now, nextCheckAt=now+60, error=type(exc).__name__)
                self.save()
                continue
            self.state['rounds'][row['id']] = dict(row, status='handled' if matched is not None else 'active', lastCheckAt=now, nextCheckAt=now+1800, matchedDeathAt=matched)
            self.save()
            if matched is None:
                pending.append(dict(row, checkedAt=now))
        due = pending
        if not due:
            return
        # Reserve this check before sending; ambiguous replies never spam.
        for row in due:
            self.state['alerts'][row['id']] = dict(row, status='sending', attemptedAt=row['checkedAt'])
        self.save()
        def stamp(at):
            return datetime.fromtimestamp(at, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        text = '⏰ S怪击杀待确认（北京时间）\n' + '\n\n'.join(f"{r['world']} · {r['name']} · {r['map']}\n首次报告触发时间：{stamp(r['at'])}\n本次检查时间：{stamp(r['checkedAt'])}\n触发已过 {int((r['checkedAt']-r['at'])//60)} 分钟，熊站击杀历史中未查到本轮相近记录。" for r in due) + '\n如已击杀，请补报击杀时间。'
        receipt = None
        try:
            receipt = await send(text)
        finally:
            for row in due:
                self.state['alerts'][row['id']].update(status='sent' if receipt is not None else 'uncertain', messageId=str(receipt) if receipt is not None else None)
            self.save()
