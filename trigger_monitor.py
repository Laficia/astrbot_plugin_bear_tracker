"""Independent trigger-opportunity alerts, durable per-window/group deduplication."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import time
from .quiet_hours import delivery_allowed,guarded_send,QuietHours
from .trigger_preferences import DEFAULT_INTERVAL, validate_interval, delays, delay_ready
from .feature_controls import allows as feature_allows,enabled as feature_enabled

from astrbot.api import logger
from astrbot.api.event import MessageChain

from .listening_scope import DEFAULT_SCOPES, resolve_scope
from .message_template import DEFAULT_TRIGGER_TEMPLATE, render_template
from .trigger_source import ReferenceTriggerSource, SOURCE, MAX_SOURCE_AGE, DEFAULT_TRIGGER_HUNTS, validate_trigger_hunts


class TriggerMonitor:
    def __init__(self, alerts):
        self.alerts = alerts
        self.config = alerts.config
        self.source = ReferenceTriggerSource()
        self.path = Path(alerts.data_dir) / "s_trigger_state.json"
        if self.path.exists():
            data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(data.get("active"), dict) or not isinstance(data.get("sent"), dict):
                raise ValueError("触发提醒状态文件无效，请保留后检查")
            self.active, self.sent = data["active"], data["sent"]
        else:
            self.active, self.sent = {}, {}
        # Carry existing successful receipts into the new cross-server window
        # key before the first snapshot can remove an old server's active row.
        for event in self.active.values():
            for target in event.get("targets", []):
                receipt = self.sent_key(event["event_key"], target)
                if receipt in self.sent:
                    self.sent.setdefault(self.sent_key(self.batch_key(event), target), self.sent[receipt])
        self.primed_worlds = set()
        self.task = None
        self.last_sync = None
        self.last_error = ""
        self.connected = False
        self.wake = asyncio.Event()
        self.timing_baselines = set()

    def interval(self):
        try:return validate_interval(self.config.get("trigger_check_interval_seconds", DEFAULT_INTERVAL))
        except ValueError:return DEFAULT_INTERVAL

    def timing_changed(self, hunts=()):
        hunts=set(hunts)
        self.timing_baselines.update(hunts)
        self.active={key:e for key,e in self.active.items() if e["hunt_key"] not in hunts}
        if hunts:self.save()
        self.wake.set()

    def enabled(self):
        return self.config.get("enabled", True) and feature_enabled(self.config,'sightings') and self.config.get("trigger_alerts_enabled", True) and bool(self.selected_hunts())

    def selected_hunts(self):
        try:
            return set(validate_trigger_hunts(self.config.get("trigger_hunts", DEFAULT_TRIGGER_HUNTS)))
        except ValueError:
            return set()

    def apply_hunt_selection(self):
        selected = self.selected_hunts()
        changed = False
        for event in self.active.values():
            if event["hunt_key"] not in selected and (event.get("targets") or event.get("notify")):
                event.update(targets=[], notify=False, retry={})
                changed = True
        if not selected:
            changed = bool(self.active) or changed
            self.active = {}
            self.primed_worlds.clear()
            self.connected = False
        if changed:
            self.save()

    @staticmethod
    def identity(event):
        return json.dumps([event["data_center"], event["world"], event["hunt_key"]], ensure_ascii=False)

    @staticmethod
    def sent_key(event_key, target):
        return hashlib.sha256((event_key + "\0" + target).encode()).hexdigest()

    @staticmethod
    def batch_key(event):
        return json.dumps(["hunt-window", event["hunt_key"], event["window_start"]], ensure_ascii=False)

    def already_sent(self, event, target):
        return (self.sent_key(event["event_key"], target) in self.sent
                or self.sent_key(self.batch_key(event), target) in self.sent)

    def save(self):
        cutoff = time.time() - 14 * 86400
        self.sent = {key: ts for key, ts in self.sent.items() if ts >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"active": self.active, "sent": self.sent}, ensure_ascii=False), "utf-8")
        temp.replace(self.path)

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self.run(), name="bear-s-trigger-windows")

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def apply_snapshot(self, events, valid_worlds, selected_worlds, now):
        previous = self.active
        self.active = {key: item for key, item in previous.items()
                       if item["world"] in selected_worlds and item["world"] not in valid_worlds}
        self.primed_worlds.intersection_update(set(selected_worlds) & valid_worlds)
        destinations = self.alerts.destinations()
        for event in events:
            identity = self.identity(event)
            old = previous.get(identity)
            if event["hunt_key"] not in self.selected_hunts():
                self.active[identity] = {**event, "notify": False, "created_at": now, "targets": [], "retry": {}}
                continue
            if old and old["event_key"] == event["event_key"]:
                # Keep the original recipients on restart; newly added groups
                # must not receive a backlog from an already open window.
                self.active[identity] = {**old, **event, "targets": [t for t in old.get("targets", []) if t in destinations]}
            else:
                notify = event["world"] in self.primed_worlds and event["hunt_key"] not in self.timing_baselines
                targets = [umo for umo, sub in destinations.items() if now >= float(sub["since"]) - 2]
                self.active[identity] = {**event, "notify": notify, "created_at": now,
                                         "targets": targets if notify else [], "retry": {}}
        self.primed_worlds.update(valid_worlds)
        if valid_worlds:self.timing_baselines.clear()
        self.save()

    def describe(self, events):
        events = [events] if isinstance(events, dict) else events
        rows = [self.alerts.template_values(event) for event in events]
        values = dict(rows[0])
        aliases = {"豆豆柴": "狗", "陆行鸟": "鸟", "猫小胖": "猫", "莫古力": "猪"}
        values["服务器列表"] = "、".join(dict.fromkeys(
            f'[{aliases.get(event["data_center"], event["data_center"])}-{event["world"]}]' for event in events))
        for key in ("大区", "大区简称", "服务器", "地图", "分线"):
            values[key] = "、".join(dict.fromkeys(row[key] for row in rows if row[key]))
        try:
            template = self.config.get("trigger_message_template", DEFAULT_TRIGGER_TEMPLATE)
            if template == "库啵，[{大区简称}-{服务器}] 的{等级}S: {怪名}可以触发啦，冒险者大人们快去尝试呀！":
                template = DEFAULT_TRIGGER_TEMPLATE
            return render_template(template, values, "trigger")
        except ValueError:
            logger.warning("[BearTracker S] 触发提醒模板无效，本次使用默认模板")
            return render_template(DEFAULT_TRIGGER_TEMPLATE, values, "trigger")

    def silence_pending(self):
        changed=False
        for event in self.active.values():
            if event.get('targets'):
                event.update(targets=[],notify=False,retry={});changed=True
        if changed:self.save()

    async def deliver(self, valid_worlds, observed_at):
        if not delivery_allowed(self.config):self.silence_pending();return
        waiting=delays(self.config, DEFAULT_TRIGGER_HUNTS)
        batches = {}
        for event in list(self.active.values()):
            batches.setdefault(self.batch_key(event), []).append(event)
        for batch_key, events in batches.items():
            targets = list(dict.fromkeys(target for e in events for target in e.get("targets", [])))
            for target in targets:
                now = time.time()
                if not self.enabled() or not feature_allows(self.config,'sightings',target.split(':',2)[-1]) or target not in self.alerts.destinations() or now - observed_at > 90:
                    continue
                current = [e for e in events if e["world"] in valid_worlds
                           and e["hunt_key"] in self.selected_hunts() and self.alerts.accepts(e)
                           and delay_ready(e, waiting[e["hunt_key"]], now)
                           and now < e["window_end"] and now - e.get("source_at", observed_at) <= MAX_SOURCE_AGE]
                pending = [e for e in current if target in e.get("targets", [])
                           and now - e["created_at"] <= self.alerts.max_age]
                if not pending:
                    continue
                sent_key = self.sent_key(batch_key, target)
                # Existing per-world receipts also suppress this window after an
                # upgrade. Later eligible servers never produce separate alerts.
                if sent_key in self.sent or any(self.sent_key(e["event_key"], target) in self.sent for e in events):
                    if sent_key not in self.sent:
                        self.sent[sent_key] = now
                        self.save()
                    continue
                retries = [e.get("retry", {}).get(target, {}) for e in pending]
                if max((retry.get("next", 0) for retry in retries), default=0) > now:
                    continue
                try:
                    context = self.alerts.context
                    get_platform = getattr(context, "get_platform_inst", None)
                    platform = get_platform(target.split(":", 1)[0]) if get_platform else None
                    scenes = getattr(platform, "_session_scene", None)
                    if isinstance(scenes, dict) and target.split(":", 2)[-1] not in scenes:
                        raise RuntimeError("请在目标群发言一次，以恢复QQ主动消息场景")
                    chain = MessageChain().message(self.describe(current)).use_t2i(False).use_markdown(False)
                    result = await asyncio.wait_for(guarded_send(context,self.config,target,chain,feature='trigger',event_key=sent_key,expires_at=min(e['window_end'] for e in current)), timeout=30)
                    if result is False:
                        raise RuntimeError("目标平台未连接")
                    self.sent[sent_key] = time.time()
                    for event in current:
                        self.sent[self.sent_key(event["event_key"], target)] = self.sent[sent_key]
                        event["retry"].pop(target, None)
                    self.save()
                except asyncio.CancelledError:
                    raise
                except QuietHours:
                    self.silence_pending();return
                except Exception as exc:
                    attempt = max((retry.get("attempt", 0) for retry in retries), default=0) + 1
                    for event in pending:
                        event["retry"][target] = {"attempt": attempt, "next": time.time() + min(60, 10 * 2 ** min(attempt, 3))}
                    self.last_error = "触发提醒发送失败：" + str(exc)
                    self.save()
                    logger.warning("[BearTracker S] %s", self.last_error)

    async def tick(self):
        if not self.enabled() or not self.alerts.destinations() or not self.config.get("listen_scopes", DEFAULT_SCOPES):
            if self.active or self.primed_worlds:
                self.active = {}
                self.primed_worlds.clear()
                self.save()
            self.connected = False
            return
        def sync():
            with self.alerts.client.lock:
                self.alerts.client.sync()
        await asyncio.to_thread(sync)
        worlds, _ = resolve_scope(self.alerts.client.resources, self.config.get("listen_scopes", DEFAULT_SCOPES))
        waiting=delays(self.config, DEFAULT_TRIGGER_HUNTS)
        events, valid, errors = await asyncio.to_thread(self.source.fetch, worlds, delay_hours=waiting)
        current, _ = resolve_scope(self.alerts.client.resources, self.config.get("listen_scopes", DEFAULT_SCOPES))
        if worlds != current or not self.enabled() or waiting != delays(self.config, DEFAULT_TRIGGER_HUNTS):
            return
        now = time.time()
        self.apply_snapshot(events, valid, current, now)
        self.last_sync = now if valid else self.last_sync
        self.connected = bool(valid)
        self.last_error = "；".join(errors)
        await self.deliver(valid, now)

    async def run(self):
        while True:
            self.wake.clear()
            start = time.monotonic()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.primed_worlds.clear()
                error = "触发期数据读取失败：" + str(exc)
                if error != self.last_error:
                    logger.warning("[BearTracker S] %s", error)
                self.last_error = error
            try:
                await asyncio.wait_for(self.wake.wait(),max(1, self.interval() - (time.monotonic() - start)))
            except asyncio.TimeoutError:pass

    def status(self):
        destinations = self.alerts.destinations()
        pending = len({(self.batch_key(event), target) for event in self.active.values() for target in event.get("targets", [])
                       if event["hunt_key"] in self.selected_hunts() and target in destinations and not self.already_sent(event, target)
                       and time.time() < min(event["window_end"], event["created_at"] + self.alerts.max_age)})
        return {"enabled": bool(self.config.get("trigger_alerts_enabled", True)), "source": SOURCE,
                "connected": self.connected, "last_sync": self.last_sync, "last_error": self.last_error,
                "check_interval_seconds": self.interval(),
                "eligible_count": sum(e["hunt_key"] in self.selected_hunts() and e["world"] in self.primed_worlds and e["window_end"] > time.time() for e in self.active.values()),
                "pending": pending, "hunt_count": len(DEFAULT_TRIGGER_HUNTS), "selected_hunt_count": len(self.selected_hunts())}
