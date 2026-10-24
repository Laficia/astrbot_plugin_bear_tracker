"""Authenticated AstrBot plugin-view API. No additional public server."""
from __future__ import annotations
import asyncio
import base64
import time
import re
import json
import copy

from astrbot.api.web import request
from .listening_scope import DEFAULT_SCOPES, center_worlds, validate_scopes, resolve_scope
from .message_template import (DEFAULT_TEMPLATE, DEFAULT_TRIGGER_TEMPLATE, SAMPLE_VALUES, VARIABLES,
                               TRIGGER_SAMPLE_VALUES, TRIGGER_VARIABLES, render_template, validate_template)
from .trigger_source import DEFAULT_TRIGGER_HUNTS, validate_trigger_hunts
from .component_host import shared_platform
from .quiet_hours import DEFAULT_WINDOW,validate_window,delivery_allowed
from .feature_controls import FEATURES,control,updated_controls
from .group_directory import GroupDirectory

PREFIX = "/astrbot_plugin_bear_tracker/webui"


class BearWebUI:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()
        self.group_directory = GroupDirectory(plugin.context, getattr(getattr(plugin, 'alerts', None), 'data_dir', None))
        for route, handler, method in [("status", self.status, "GET"), ("options", self.options, "GET"), ("catalog", self.catalog, "GET"),
                                       ("settings", self.settings, "POST"), ("template-preview", self.template_preview, "POST"), ("preview", self.preview, "POST"),
                                       ("calibrate", self.calibrate, "POST"), ("clear-recent", self.clear_recent, "POST"),
                                       ("feature-status",self.feature_status,"GET"),("feature-settings",self.feature_settings,"POST"),
                                       ("group-names",self.group_names,"POST")]:
            plugin.context.register_web_api(PREFIX + "/" + route, handler, [method], "S怪播报控制台")

    async def group_names(self):
        try:
            body = await request.json(default={})
            if not isinstance(body, dict) or type(body.get('refresh', False)) is not bool:
                raise ValueError('群名称查询参数无效')
            pid = body.get('platform_id') or shared_platform(self.plugin.config)
            if not isinstance(pid, str) or not pid:
                raise ValueError('请先选择发送机器人')
            return {'status': 'ok', 'data': await self.group_directory.query(pid, body.get('refresh', False))}
        except (ValueError, TypeError) as exc:
            return {'status': 'error', 'message': str(exc)}

    async def feature_status(self):
        config=self.plugin.config
        from .components.magic_bucket.webui import effective
        profile=effective(config.get('magic_bucket',{}))
        routes={
            'sightings':{'targets':[u.split(':',2)[-1] for u in self.plugin.alerts.destinations()]},
            'forwarding':{'sources':list(dict.fromkeys(str(g) for r in profile['rules'] for g in r['source_groups'])),
                          'targets':list(dict.fromkeys(str(g) for r in profile['rules'] for g in r['target_groups']))},
            'collection':{'sources':list(map(str,profile['leak_fill']['groups'])),
                          'targets':list(map(str,profile['leak_fill']['groups']))+[str(profile['leak_fill'].get('notify_group') or '')]},
            'mango':{'sources':list(map(str,profile['mango_watch']['source_groups'])),
                     'targets':list(map(str,profile['mango_watch']['target_groups']))},
            'hunt':{'sources':[str(profile['hunt_alerts'].get('source_group') or '')],
                    'targets':[str(profile['hunt_alerts'].get('notify_group') or '')]},
        }
        routes['text']=routes['forwarding'];routes['preview']=routes['forwarding']
        routes['trains']={'targets':config.get('train_hunt',{}).get('groups',[])}
        controls={key:control(config,key) for key in FEATURES}
        for key,sub in [('mango',profile['mango_watch']['enabled']),('hunt',profile['hunt_alerts']['enabled']),
                         ('trains',config.get('train_commands',{}).get('enabled',True))]:
            controls[key]['enabled']=controls[key]['enabled'] and bool(sub)
        return {'status':'ok','data':{'controls':controls,
                'routes':routes,'master_enabled':config.get('enabled',True),
                'platform_id':shared_platform(config),'within_broadcast_window':delivery_allowed(config),
                'sub_enabled':{'trains':config.get('train_commands',{}).get('enabled',True),
                    'collection':bool(profile['leak_fill']['enabled'] or profile['spawn_watch']['enabled']),
                    'mango':bool(profile['mango_watch']['enabled']),'hunt':bool(profile['hunt_alerts']['enabled'])}}}

    async def feature_settings(self):
        try:
            body=await request.json(default={})
            if not isinstance(body,dict):raise ValueError('功能配置必须为对象')
            async with self.lock:
                values=updated_controls(self.plugin.config,body.get('feature'),body.get('control'))
                config=self.plugin.config
                update={'feature_controls':values}
                feature=body['feature']
                if feature in ('mango','hunt'):
                    bucket=copy.deepcopy(config.get('magic_bucket',{}))
                    overrides=json.loads(bucket.get('overrides_json') or '{}')
                    section='mango_watch' if feature=='mango' else 'hunt_alerts'
                    overrides.setdefault(section,{})['enabled']=values[feature]['enabled']
                    # Use the existing validation, including shared directory checks.
                    from .components.magic_bucket.webui import validate,effective
                    candidate={**bucket,'overrides_json':json.dumps(overrides,ensure_ascii=False)}
                    validate({'profile':effective(candidate),'enabled':True,'bot_uin':''})
                    bucket['overrides_json']=candidate['overrides_json'];update['magic_bucket']=bucket
                if feature=='trains':
                    from .train_schedule import DEFAULT_CONFIG
                    update['train_commands']=copy.deepcopy(config.get('train_commands',DEFAULT_CONFIG))
                    update['train_commands']['enabled']=values[feature]['enabled']
                if hasattr(config,'save_config'):config.save_config(update)
                else:config.update(update)
                if body['feature'] in ('forwarding','text','collection','mango','hunt'):
                    await self.plugin.components.magic.reset_engines()
                if body['feature']=='sightings':
                    self.plugin.alerts.silence_pending()
                    self.plugin.triggers.silence_pending()
                    self.plugin.alerts.changed.set()
            return await self.feature_status()
        except (ValueError,TypeError,KeyError,OSError) as exc:
            return {'status':'error','message':str(exc)}

    async def clear_recent(self):
        # Display history is separate from delivery receipts and pending alerts.
        count = self.plugin.alerts.clear_recent()
        return {"status": "ok", "data": {"cleared": count}}

    async def sync(self):
        def work():
            with self.plugin.client.lock:
                self.plugin.client.sync()
        await asyncio.to_thread(work)

    async def status(self):
        e = self.plugin.alerts
        scopes = self.plugin.config.get("listen_scopes", DEFAULT_SCOPES)
        groups = {}
        for origin in e.destinations():
            platform, _, group = origin.split(":", 2)
            groups.setdefault(platform, []).append(group)
        worlds, centers = resolve_scope(e.client.resources or {}, scopes)
        return {"status": "ok", "data": {"enabled": self.plugin.config.get("enabled", True),
                "bot_connection": self.bot_connection(),
                "targets": list(e.destinations()), "automation_platform_id":shared_platform(self.plugin.config), "pending": len(e.pending), "last_report": e.last_report,
                "last_error": e.last_error, "monitor": getattr(e.client, "monitor_status", {}),
                "poll_interval_seconds": self.plugin.config.get("poll_interval_seconds", 30),
                "max_report_age_seconds": e.max_age, "recent": list(e.recent.values())[::-1],
                "forward_interval_ms": self.plugin.config.get('magic_bucket',{}).get('send_interval_ms',500),
                "broadcast_window": self.plugin.config.get('broadcast_window',DEFAULT_WINDOW),
                "within_broadcast_window": delivery_allowed(self.plugin.config),
                "reference_at": e.client.reference.data["fetched_at"],
                "reference_source": e.client.reference.data["source"],
                "reference_count": len(e.client.reference.data["hunts"]),
                "scope": "、".join(centers), "groups_by_platform": groups, "listen_scopes": scopes,
                "world_count": len(worlds), "center_count": len(centers),
                "message_template": self.plugin.config.get("message_template", DEFAULT_TEMPLATE),
                "trigger_message_template": self.plugin.config.get("trigger_message_template", DEFAULT_TRIGGER_TEMPLATE),
                "trigger_alerts_enabled": self.plugin.config.get("trigger_alerts_enabled", True),
                "trigger_hunts": [key for key in DEFAULT_TRIGGER_HUNTS if key in self.plugin.triggers.selected_hunts()],
                "trigger_monitor": self.plugin.triggers.status(),
                "aliases": {"豆豆柴": "狗", "陆行鸟": "鸟", "猫小胖": "猫", "莫古力": "猪"}}}

    def bot_connection(self):
        pid = shared_platform(self.plugin.config)
        manager = getattr(self.plugin.context, 'platform_manager', None)
        for platform in getattr(manager, 'platform_insts', []):
            meta = platform.meta()
            if meta.id != pid:
                continue
            if meta.name != 'aiocqhttp':
                return {'platform_id': pid, 'connected': None, 'message': '平台状态请在 AstrBot 连接页查看'}
            try:
                client = platform.get_client()
                accounts = getattr(client, '_wsr_api_clients', {})
                connected = bool(accounts)
            except Exception:
                connected = False
            return {'platform_id': pid, 'connected': connected,
                    'message': '机器人已连接' if connected else '机器人协议端未连接，请检查 QQ／NapCat 是否运行并登录'}
        return {'platform_id': pid, 'connected': False, 'message': '尚未选择已接入的发送机器人'}

    def platforms(self):
        manager = getattr(self.plugin.context, "platform_manager", None)
        result = []
        for platform in getattr(manager, "platform_insts", []):
            meta = platform.meta()
            if meta.name in ("aiocqhttp", "qq_official", "qq_official_webhook"):
                result.append({"id": meta.id, "name": meta.id})
        return result

    async def options(self):
        try:
            await self.sync()
            return {"status": "ok", "data": {"data_centers": center_worlds(self.plugin.client.resources),
                    "platforms": self.platforms(), "default_message_template": DEFAULT_TEMPLATE,
                    "template_variables": VARIABLES, "default_trigger_message_template": DEFAULT_TRIGGER_TEMPLATE,
                    "trigger_template_variables": TRIGGER_VARIABLES,
                    "trigger_hunts": [{"key": key, "name": self.plugin.client.info(key, self.plugin.client.resources.get("DatabaseHunt", {}).get(key, {}))["名称"]}
                                      for key in DEFAULT_TRIGGER_HUNTS]}}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def catalog(self):
        try:
            await self.sync()
            reference = self.plugin.client.reference
            return {"status": "ok", "data": {"hunts": list(reference.data["hunts"].values()),
                    "source": reference.data["source"], "fetched_at": reference.data["fetched_at"],
                    "audit": reference.audit(self.plugin.client.resources)}}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def settings(self):
        body = await request.json(default={})
        try:
            values = validate_settings(body)
            if 'automation_platform_id' in values and values['automation_platform_id'] not in {p['id'] for p in self.platforms()}:
                raise ValueError('请选择已接入的发送机器人')
            if "listen_scopes" in values:
                await self.sync()
                values["listen_scopes"] = validate_scopes(values["listen_scopes"], self.plugin.client.resources)
            if "group_ids" in body:
                platform_id = body.get("platform_id")
                if platform_id not in {p["id"] for p in self.platforms()}:
                    raise ValueError("请选择已连接的发送机器人")
                groups = normalize_group_ids(body["group_ids"])
                others = [umo for umo in self.plugin.alerts.destinations() if umo.split(":", 1)[0] != platform_id]
                values["target_sessions"] = others + [f"{platform_id}:GroupMessage:{group}" for group in groups]
            if "group_receivers" in body:
                values["target_sessions"] = resolve_group_receivers(
                    self.plugin.alerts.destinations(), body["group_receivers"], {p["id"] for p in self.platforms()})
            config = self.plugin.config
            if 'forward_interval_ms' in values:
                interval=values.pop('forward_interval_ms')
                values['magic_bucket']={**config.get('magic_bucket',{}),'send_interval_ms':interval}
            reset_magic=('enabled' in values and values['enabled']!=config.get('enabled',True)) or (
                'automation_platform_id' in values and values['automation_platform_id']!=shared_platform(config)) or (
                'broadcast_window' in values and values['broadcast_window']!=config.get('broadcast_window',DEFAULT_WINDOW)) or (
                'magic_bucket' in values and values['magic_bucket']!=config.get('magic_bucket',{}))
            defaults = {"enabled": True, "target_sessions": [], "listen_scopes": DEFAULT_SCOPES,
                        "poll_interval_seconds": 30, "max_report_age_seconds": 300}
            reconnect = any(key in values and values[key] != config.get(key, default)
                            for key, default in defaults.items())
            # Persist first; an invalid/disk-failed save must not alter runtime subscriptions.
            if hasattr(config, "save_config"):
                config.save_config(values)
            else:
                config.update(values)
            if reset_magic and getattr(self.plugin,'components',None):
                await self.plugin.components.magic.reset_engines()
            if not delivery_allowed(config):
                if hasattr(self.plugin.alerts,'silence_pending'):self.plugin.alerts.silence_pending()
                triggers=getattr(self.plugin,'triggers',None)
                if triggers and hasattr(triggers,'silence_pending'):triggers.silence_pending()
            if "trigger_hunts" in values:
                self.plugin.triggers.apply_hunt_selection()
            if "target_sessions" in values:
                state = self.plugin.alerts.state
                state.subscriptions = {umo: row for umo, row in state.subscriptions.items() if umo in values["target_sessions"]}
                state.save()
            if reconnect:
                self.plugin.alerts.changed.set()
            return {"status": "ok", "data": {"message": "配置已保存并生效"}}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def template_preview(self):
        body = await request.json(default={})
        try:
            if not isinstance(body, dict):
                raise ValueError("预览内容必须为对象")
            kind = body.get("kind", "sighting")
            text = render_template(body.get("message_template"), TRIGGER_SAMPLE_VALUES if kind == "trigger" else SAMPLE_VALUES, kind)
            return {"status": "ok", "data": {"text": text}}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    async def preview(self):
        body = await request.json(default={})
        try:
            async with self.lock:
                await self.sync()
                key = body.get("hunt_key")
                if key not in self.plugin.client.reference.data["hunts"]:
                    raise ValueError("请选择已校准的S怪")
                x, y = body.get("x"), body.get("y")
                ref = self.plugin.client.reference.data["hunts"][key]
                definition = self.plugin.client.resources["DatabaseHunt"].get(key, {})
                if definition.get("Region") != ref["region"]:
                    raise ValueError("参考地图与播报源地图归属不一致，停止绘制")
                path = await asyncio.to_thread(self.plugin.alerts.maps.render,
                                               self.plugin.client.resources, key, x, y)
                encoded = await asyncio.to_thread(lambda: base64.b64encode(path.read_bytes()).decode())
                return {"status": "ok", "data": {"image": "data:image/png;base64," + encoded,
                        "region": ref["region_name"], "x": float(x), "y": float(y)}}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def calibrate(self):
        try:
            async with self.lock:
                await self.sync()
                reference = self.plugin.client.reference
                await asyncio.to_thread(reference.refresh)
                audit = reference.audit(self.plugin.client.resources)
                return {"status": "ok", "data": {"message": "参考资料已更新", "audit": audit,
                        "fetched_at": reference.data["fetched_at"]}}
        except Exception as exc:
            return {"status": "error", "message": "校准失败，保留已有资料：" + str(exc)}


def validate_settings(body):
    if not isinstance(body, dict):
        raise ValueError("配置必须为对象")
    result = {}
    if 'broadcast_window' in body:result['broadcast_window']=validate_window(body['broadcast_window'])
    if 'forward_interval_ms' in body:
        interval=body['forward_interval_ms']
        if type(interval) is not int or not 0<=interval<=60000:raise ValueError('群转发发送间隔须为 0～60000 毫秒')
        result['forward_interval_ms']=interval
    if 'automation_platform_id' in body:
        platform=body['automation_platform_id']
        if not isinstance(platform,str) or not platform.strip() or ':' in platform or len(platform)>100:
            raise ValueError('发送机器人无效')
        result['automation_platform_id']=platform
    if "trigger_hunts" in body:
        result["trigger_hunts"] = validate_trigger_hunts(body["trigger_hunts"])
    if "message_template" in body:
        result["message_template"] = validate_template(body["message_template"])
    if "trigger_message_template" in body:
        result["trigger_message_template"] = validate_template(body["trigger_message_template"], "trigger")
    if "listen_scopes" in body:
        result["listen_scopes"] = validate_scopes(body["listen_scopes"])
    for key in ("enabled", "trigger_alerts_enabled"):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError(key + "必须为布尔值")
            result[key] = body[key]
    for key, lo, hi in [("poll_interval_seconds", 10, 120), ("max_report_age_seconds", 30, 600)]:
        if key in body:
            value = body[key]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{key}范围为{lo}～{hi}")
            result[key] = value
    if "target_sessions" in body:
        targets = body["target_sessions"]
        if not isinstance(targets, list) or len(targets) > 100:
            raise ValueError("接收会话必须为列表且不超过100项")
        for value in targets:
            if not isinstance(value, str) or len(value) > 300 or len(value.split(":", 2)) != 3:
                raise ValueError("会话格式必须为 平台实例ID:GroupMessage:群ID")
            platform, kind, group = value.split(":", 2)
            if kind != "GroupMessage" or not platform.strip() or not group.strip() or any(c.isspace() for c in value):
                raise ValueError("只支持完整的群会话ID")
        result["target_sessions"] = list(dict.fromkeys(targets))
    return result


def normalize_group_ids(groups):
    if not isinstance(groups, list) or len(groups) > 100:
        raise ValueError("群号必须为列表，最多100项")
    if not all(isinstance(group, str) and re.fullmatch(r"[A-Za-z0-9]{1,128}", group) for group in groups):
        raise ValueError("每行只填写一个群号，不需要机器人名称或其他前缀")
    return list(dict.fromkeys(groups))


def resolve_group_receivers(existing, receivers, allowed_platforms):
    if not isinstance(receivers, dict) or not receivers or any(p not in allowed_platforms for p in receivers):
        raise ValueError("请选择已连接的发送机器人")
    result = [umo for umo in existing if umo.split(":", 1)[0] not in receivers]
    for platform, groups in receivers.items():
        result.extend(f"{platform}:GroupMessage:{group}" for group in normalize_group_ids(groups))
    return result
