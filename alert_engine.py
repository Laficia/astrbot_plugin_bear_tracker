"""AstrBot delivery lifecycle, durable per-group deduplication and retries."""
from __future__ import annotations

import asyncio
from .quiet_hours import delivery_allowed,guarded_send,QuietHours
from .feature_controls import allows as feature_allows,enabled as feature_enabled
import math
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image

from .alert_state import AlertState
from .map_renderer import BearMapRenderer, MapRenderError
from .relay_source import stream_sightings
from .service import SOURCE, stamp
from .listening_scope import DEFAULT_SCOPES, resolve_scope
from .message_template import DEFAULT_TEMPLATE, render_template


class AlertEngine:
    def __init__(self, context, client, config, data_dir):
        self.context, self.client, self.config = context, client, config
        self.data_dir = Path(data_dir)
        self.state = AlertState(self.data_dir / "s_alert_state.json")
        self.maps = BearMapRenderer(self.data_dir / "maps")
        self.tasks = []
        self.pending = {}
        self.changed = asyncio.Event()
        self.wake_delivery = asyncio.Event()
        self.started_at = time.time()
        self.last_report = None
        self.last_error = ""
        self.recent = {}
        self.hidden_recent = {}
        self.silenced_spawns = {}

    @property
    def max_age(self):
        return max(30, min(600, int(self.config.get("max_report_age_seconds", 300))))

    def destinations(self):
        targets = dict(self.state.subscriptions)
        for umo in self.config.get("target_sessions", []):
            if isinstance(umo, str) and ":GroupMessage:" in umo and umo.rsplit(":", 1)[-1]:
                targets.setdefault(umo, {"since": self.started_at, "group": umo.split(":", 2)[-1]})
        return targets

    def accepts(self, sighting):
        worlds, _ = resolve_scope(self.client.resources or {}, self.config.get("listen_scopes", DEFAULT_SCOPES))
        return sighting.get("world") in worlds

    def start(self):
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._listen(), name="bear-s-source"),
                          asyncio.create_task(self._deliver(), name="bear-s-delivery")]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    def subscribe(self, umo, group):
        self.state.subscribe(umo, group)
        self.changed.set()

    def unsubscribe(self, umo):
        self.state.unsubscribe(umo)
        self.changed.set()

    async def _listen(self):
        while True:
            if not self.config.get("enabled", True) or not feature_enabled(self.config,'sightings') or not self.destinations() or not self.config.get("listen_scopes", DEFAULT_SCOPES):
                self.changed.clear()
                await self.changed.wait()
                continue
            interval = max(10, min(120, float(self.config.get("poll_interval_seconds", 30))))
            consume = None
            change = None
            try:
                self.changed.clear()
                consume = asyncio.create_task(self._consume(interval))
                change = asyncio.create_task(self.changed.wait())
                done, _ = await asyncio.wait((consume, change), return_when=asyncio.FIRST_COMPLETED)
                if consume in done:
                    await consume
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("[BearTracker S] 数据连接失败，将自动重试: %s", self.last_error)
                await asyncio.sleep(10)
            finally:
                for task in (consume, change):
                    if task:
                        task.cancel()
                await asyncio.gather(*(task for task in (consume, change) if task), return_exceptions=True)

    async def _consume(self, interval):
        source = stream_sightings(self.client, poll_interval=interval, max_age=self.max_age,
                                  listen_scopes=self.config.get("listen_scopes", DEFAULT_SCOPES))
        try:
            async for sighting in source:
                self.enqueue(sighting)
        finally:
            await source.aclose()
            self.client.monitor_status = {**getattr(self.client, "monitor_status", {}), "connected": False}

    def clear_recent(self):
        now=time.time()
        self.hidden_recent={key:expiry for key,expiry in self.hidden_recent.items() if expiry>now}
        self.hidden_recent.update({key:now+self.max_age+30 for key in self.recent})
        count=len(self.recent)
        self.recent.clear()
        return count

    def silence_spawn(self,key):
        now=time.time()
        self.silenced_spawns={k:expiry for k,expiry in getattr(self,'silenced_spawns',{}).items() if expiry>now}
        self.silenced_spawns[key]=now+630

    def silence_pending(self):
        for spawn_key,_ in self.pending:self.silence_spawn(spawn_key)
        self.pending.clear()

    def enqueue(self, sighting):
        if sighting.get("type") in ("death", "retract"):
            for key, item in list(self.pending.items()):
                old = item["sighting"]
                if old["world"] == sighting["world"] and old["hunt_name"] == sighting["hunt_name"]:
                    # Do not cancel a newer spawn when an older report is late.
                    if sighting.get("type") == "retract" or sighting.get("retracted") or float(sighting.get("death_at", 0)) >= old["seen_at"]:
                        self.pending.pop(key, None)
            return
        now = time.time()
        if not self.accepts(sighting):
            return
        seen = float(sighting["seen_at"])
        if not math.isfinite(seen) or seen < now - self.max_age or seen > now + 30:
            return
        self.last_report = now
        definition = self.client.resources.get("DatabaseHunt", {}).get(sighting["hunt_key"], {})
        if self.hidden_recent.get(sighting['spawn_key'],0)<=now:
            self.recent[sighting["spawn_key"]] = {**sighting, "display_name": self.client.info(sighting["hunt_key"], definition)["名称"]}
        if len(self.recent) > 50:
            self.recent.pop(next(iter(self.recent)))
        if not delivery_allowed(self.config) or not feature_enabled(self.config,'sightings'):
            self.silence_spawn(sighting['spawn_key'])
            return
        if getattr(self,'silenced_spawns',{}).get(sighting['spawn_key'],0)>now:return
        for umo, sub in self.destinations().items():
            if not feature_allows(self.config,'sightings',umo.split(':',2)[-1]):continue
            if seen < float(sub["since"]) - 2:
                continue
            old = self.state.delivery(sighting["spawn_key"], umo)
            if old and old["has_map"]:
                continue
            if old and not self.position(sighting):
                continue
            key = (sighting["spawn_key"], umo)
            current = self.pending.get(key)
            self.pending[key] = {"sighting": sighting, "next": current["next"] if current else 0,
                                 "attempt": current["attempt"] if current else 0}
        self.wake_delivery.set()

    @staticmethod
    def position(sighting):
        p = sighting.get("position")
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            return None
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            return None
        return (x, y) if math.isfinite(x) and math.isfinite(y) and x >= 1 and y >= 1 else None

    def template_values(self, sighting):
        key = sighting["hunt_key"]
        value = self.client.resources.get("DatabaseHunt", {}).get(key, {})
        name = self.client.info(key, value)["名称"]
        region = value.get("Region", "")
        map_name = self.client.translate("regions", region) if region else "网站未提供地图名称"
        instance = sighting.get("instance")
        world = sighting["world"]
        centers = self.client.resources.get("DataCenters", {})
        data_center = next((dc for dc, details in centers.items() if world in details.get("Names", [])),
                           sighting.get("data_center") or "未知大区")
        dc_label = {"豆豆柴": "狗", "陆行鸟": "鸟", "猫小胖": "猫", "莫古力": "猪"}.get(data_center, data_center)
        map_name += f"（{instance}线）" if instance else ""
        p = self.position(sighting)
        location = f"({p[0]:.1f}, {p[1]:.1f})" if p else "(坐标待更新)"
        level = value.get("Level")
        if not isinstance(level, int) or isinstance(level, bool) or level <= 0:
            # The current website supplies the expansion, rather than a level
            # on FoundReport. Resolve the S rank's level from that hunt's data.
            level = {"ARR": 50, "HW": 60, "SB": 70, "ShB": 80, "EW": 90, "DT": 100}.get(value.get("Patch"), "")
        values = {"等级": str(level), "怪名": name, "大区": data_center, "大区简称": dc_label,
                  "服务器": world, "地图": map_name, "分线": str(instance) if instance else "",
                  "X": f"{p[0]:.1f}" if p else "待更新", "Y": f"{p[1]:.1f}" if p else "待更新",
                  "坐标": location[1:-1]}
        return values

    def describe(self, sighting, *, has_map=False, supplement=False):
        values = self.template_values(sighting)
        p = self.position(sighting)
        try:
            text = render_template(self.config.get("message_template", DEFAULT_TEMPLATE), values)
        except ValueError:
            # Manual edits outside the validated WebUI must not stop delivery.
            logger.warning("[BearTracker S] 播报模板无效，本次使用默认模板")
            text = render_template(DEFAULT_TEMPLATE, values)
        if supplement:
            text = "【S怪坐标补充】\n" + text
        if not has_map:
            text += "\n地图暂不可用。" if p else "\n等待网站更新坐标。"
        return text

    async def _deliver(self):
        while True:
            try:
                await self._delivery_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"播报任务错误: {type(exc).__name__}: {exc}"
                logger.exception("[BearTracker S] 播报任务异常，将继续运行")
                await asyncio.sleep(5)

    async def _delivery_loop(self):
        while True:
            self.wake_delivery.clear()
            if not delivery_allowed(self.config):self.silence_pending()
            for key, item in list(self.pending.items()):
                spawn_key, umo = key
                sighting = item["sighting"]
                if umo not in self.destinations() or not self.config.get("enabled", True) or not self.accepts(sighting) or not feature_allows(self.config,'sightings',umo.split(':',2)[-1]):
                    self.pending.pop(key, None)
                    continue
                if time.time() - float(sighting["seen_at"]) > self.max_age:
                    self.pending.pop(key, None)
                    continue
                if item["next"] > time.time():
                    continue
                old = self.state.delivery(spawn_key, umo)
                if old and old["has_map"]:
                    self.pending.pop(key, None)
                    continue
                path = None
                p = self.position(sighting)
                if p:
                    try:
                        definition = self.client.resources.get("DatabaseHunt", {}).get(sighting["hunt_key"], {})
                        name = self.client.info(sighting["hunt_key"], definition)["名称"]
                        path = await asyncio.to_thread(self.maps.render, self.client.resources,
                                                       sighting["hunt_key"], *p,
                                                       server=sighting["world"] + (f"（{sighting['instance']}线）" if sighting.get("instance") else ""),
                                                       hunt_name=name,
                                                       region_name=self.client.translate("regions", definition.get("Region", "")))
                    except (MapRenderError, OSError, ValueError) as exc:
                        logger.warning("[BearTracker S] 地图生成失败: %s", exc)
                if self.pending.get(key) is not item:
                    continue  # A death/retraction or newer position arrived while the map loaded.
                if not delivery_allowed(self.config) or not feature_allows(self.config,'sightings',umo.split(':',2)[-1]):
                    self.silence_spawn(spawn_key);self.pending.pop(key,None);continue
                if (umo not in self.destinations() or not self.config.get("enabled", True) or not self.accepts(sighting)
                        or time.time() - float(sighting["seen_at"]) > self.max_age):
                    self.pending.pop(key, None)
                    continue
                # Allow a short retry for a transient image download failure before text fallback.
                if p and not path and item["attempt"] < 2:
                    item["attempt"] += 1
                    item["next"] = time.time() + 10
                    continue
                if old and not path:
                    self.pending.pop(key, None)
                    continue
                chain = MessageChain().message(self.describe(sighting, has_map=bool(path), supplement=bool(old)))
                chain.use_t2i(False).use_markdown(False)
                if path:
                    chain.chain.append(Image.fromFileSystem(str(path)))
                try:
                    # Some official QQ adapters silently skip proactive sends
                    # until this group has spoken since the adapter started.
                    get_platform = getattr(self.context, "get_platform_inst", None)
                    platform = get_platform(umo.split(":", 1)[0]) if get_platform else None
                    scenes = getattr(platform, "_session_scene", None)
                    if isinstance(scenes, dict) and umo.split(":", 2)[-1] not in scenes:
                        raise RuntimeError("请在目标群发言一次，以恢复QQ主动消息场景")
                    sent = await asyncio.wait_for(guarded_send(self.context,self.config,umo,chain,event_key=str(spawn_key)+(':map' if path else ':text'),report={**sighting,'position':p},expires_at=float(sighting['seen_at'])+self.max_age), timeout=30)
                    if sent is False:
                        raise RuntimeError("目标平台未连接")
                    self.state.mark_sent(spawn_key, umo, bool(path))
                    self.pending.pop(key, None)
                    self.last_error = ""
                except asyncio.CancelledError:
                    raise
                except QuietHours:
                    self.silence_spawn(spawn_key);self.pending.pop(key,None)
                except Exception as exc:
                    item["attempt"] += 1
                    item["next"] = time.time() + min(60, 5 * 2 ** min(item["attempt"], 4))
                    self.last_error = f"发送失败: {type(exc).__name__}: {exc}"
                    logger.warning("[BearTracker S] 群播报失败，将重试: %s", self.last_error)
            try:
                await asyncio.wait_for(self.wake_delivery.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    def status(self, umo):
        active = umo in self.destinations()
        scopes = self.config.get("listen_scopes", DEFAULT_SCOPES)
        scope_text = "；".join(s["data_center"] + "：" + ("、".join(s["servers"]) if s["servers"] else "全部服务器") for s in scopes) or "未选择"
        return (f"S播报：{'已开启' if active else '未开启'}\n"
                f"监听范围：{scope_text}\n"
                f"订阅群数量：{len(self.destinations())}\n"
                f"服务开关：{'开启' if self.config.get('enabled', True) else '关闭（请在插件配置启用）'}\n"
                f"最近接收发现报告：{stamp(self.last_report) or '尚未收到'}\n"
                f"待发送：{len(self.pending)}\n"
                f"最近发送/任务错误：{self.last_error or '无'}\n"
                "来源依赖玩家上报；刷新窗口不会触发通知。")
