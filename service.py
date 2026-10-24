"""Read-only client for Bear Tracker's public website endpoints."""
from __future__ import annotations

import http.cookiejar
import json
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
if __package__:
    from .reference_catalog import ReferenceCatalog
else:
    from reference_catalog import ReferenceCatalog

BASE = "https://tracker.beartoolkit.com"
SOURCE = BASE + "/timer"
TZ = timezone(timedelta(hours=8))


def stamp(value):
    return datetime.fromtimestamp(float(value), TZ).isoformat(timespec="seconds") if value else None


def norm(value):
    return re.sub(r"[\s\-_：:·——]+", "", str(value)).casefold()


def format_result(data):
    if isinstance(data, str):
        return data
    if "本次显示" in data:
        rows = data["本次显示"]
        lines = ["熊工具箱 · 实时计时", "查询时间：" + data["查询时间_北京时间"]]
        for r in rows:
            line = f"{r['服务器']} · {r['名称']}"
            if r.get("分线") is not None:
                line += f" · 分线{r['分线']}"
            lines.extend([line, r["状态"]])
            if r.get("窗口开始"):
                lines.append(f"刷新窗口：{r['窗口开始']} ～ {r['窗口结束']}")
            lines.append(f"{r['基准类型']}：{r['基准时间']}")
        if data.get("提示"):
            lines.append(data["提示"])
        lines.append("刷新窗口不等于已出现；网站记录可能漏报。")
    elif "目标资料" in data:
        lines = ["熊工具箱 · 狩猎资料"]
        for r in data["目标资料"]:
            lines.extend([f"{r['名称']}（{r['等级']}）", "地图：" + "、".join(r["地图"]), "触发条件：" + r["触发条件"]])
            if r.get("触发条件来源") and r["触发条件来源"] != SOURCE:
                lines.append("触发资料来源：" + r["触发条件来源"])
            p = r.get("普通刷新参数_小时")
            if p:
                lines.append(f"击杀后刷新窗口：{p[0]:g}～{sum(p):g}小时")
        if not data["目标资料"]:
            lines.append("用法：/熊资料 目标名称；/熊计时 服务器名 目标名称 S或A或FATE")
        if data.get("提示"):
            lines.append(data["提示"])
    else:
        return data.get("提示") or data.get("错误") or str(data)
    lines.append("来源：" + SOURCE)
    return "\n".join(lines)


class BearClient:
    def __init__(self):
        self.reference = ReferenceCatalog()
        self.monitor_status = {}
        self.translations = json.loads(Path(__file__).with_name("translations.json").read_text(encoding="utf-8"))
        self.resources = None
        self.resource_at = 0
        self.token = None
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [("User-Agent", "Mozilla/5.0 (compatible; AstrBot-BearTracker/1.0)")]
        self.lock = threading.Lock()
        self.cache = {}

    def natural_target(self, text):
        candidates = []
        for key, names in self.translations["hunts"].items():
            for name in (key, names.get("cn", "")):
                if name and norm(name) in norm(text):
                    candidates.append(name)
        return max(candidates, key=len) if candidates else None

    def direct_answer(self, text):
        text = text.strip()
        command = re.match(r"^/?熊(资料|计时)(?:\s+(.*))?$", text)
        if command:
            args = (command[2] or "").strip()
            if command[1] == "资料":
                return format_result(self.knowledge(args))
            parts = args.split()
            if not parts:
                return "用法：/熊计时 服务器名 目标名称 S或A或FATE"
            category = parts.pop() if len(parts) > 1 and parts[-1].casefold() in ("s", "a", "fate") else "S"
            return format_result(self.query(parts[0], " ".join(parts[1:]), category))
        target = self.natural_target(text)
        if not target:
            return None
        if re.search(r"怎么触发|如何触发|触发条件|刷新条件|怎么刷", text):
            return format_result(self.knowledge(target))
        if re.search(r"计时|刷新时间|什么时候刷|多久刷|何时刷新", text):
            with self.lock:
                self.sync()
                names = [n for dc, d in self.resources["DataCenters"].items() for n in [dc, *d["Names"]]]
            servers = [n for n in names if norm(n) in norm(text)]
            if not servers:
                return "请告诉我服务器名，例如：莫古环，查一下水晶塔雷德罗巨蛇的刷新时间。"
            category = "FATE" if "fate" in text.casefold() or target.startswith("FATE ") else "A" if re.search(r"A[级级怪狩猎]|\bA\b", text, re.I) else "S"
            return format_result(self.query(max(servers, key=len), target, category))
        return None

    def post(self, endpoint, data):
        req = urllib.request.Request(BASE + "/api/" + endpoint, data=json.dumps(data).encode("utf-8"), headers={
            "Content-Type": "application/json", "X-CSRFToken": self.token,
            "Referer": SOURCE, "Origin": BASE,
        })
        with self.opener.open(req, timeout=25) as response:
            return json.load(response)

    def sync(self):
        if self.resources is not None and time.monotonic() - self.resource_at < 3600:
            return
        with self.opener.open(SOURCE, timeout=25) as response:
            html = response.read().decode("utf-8")
        match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
        if not match:
            raise ValueError("网站未返回查询所需的 CSRF 标记，页面可能已改版")
        self.token = match[1]
        self.resources = self.post("syncSession", {})["resources"]
        self.resource_at = time.monotonic()

    def translate(self, group, key):
        entry = self.translations.get(group, {}).get(key, {})
        return entry.get("cn") or entry.get("en") or key

    def definitions(self, category):
        if category != "fate":
            return self.resources["DatabaseHunt"]
        result = {}
        for group in ("BossFATEs", "FATEChains"):
            for value in self.resources["DatabaseFate"].get(group, {}).values():
                key = value["DatabaseName"]
                if key not in result:
                    result[key] = dict(value)
                    result[key]["Regions"] = []
                result[key]["Regions"].append(value.get("Region", ""))
        return result

    def info(self, key, value):
        names = value.get("Name", {})
        name = names.get("ChineseSimplified", key) if isinstance(names, dict) and names else self.translate("hunts", key)
        regions = value.get("Regions", [value.get("Region", "")])
        reference = self.reference.data["hunts"].get(key, {})
        reference_matches = reference.get("region") in regions
        condition = "\n".join([reference.get("method", ""), *reference.get("notes", [])]) if reference_matches else ""
        return {
            "名称": name, "英文标识": key,
            "地图": list(dict.fromkeys(self.translate("regions", r) for r in regions if r)),
            "等级": {1: "B", 2: "A", 3: "S"}.get(value.get("Rank"), "FATE"),
            "版本": value.get("Patch"),
            "触发条件": condition or (self.translate("conditions", key) if key in self.translations["conditions"] else "网站未提供；请勿推测"),
            "触发条件来源": self.reference.data["source"] if condition else SOURCE,
            "普通刷新参数_小时": value.get("RespawnTimer"),
            "维护刷新参数_小时": value.get("MaintTimer"),
        }

    def matches(self, query, key, value):
        if not query:
            return True
        info = self.info(key, value)
        fields = [key, info["名称"], *info["地图"]]
        fields.extend(self.translations["hunts"].get(key, {}).values())
        fields.extend(value.get("Name", {}).values() if isinstance(value.get("Name"), dict) else [])
        return any(norm(query) in norm(field) for field in fields)

    def worlds(self, server):
        dcs = self.resources["DataCenters"]
        if not server.strip():
            return [], "请先询问用户服务器名或大区名，不能自行猜测。"
        for dc, data in dcs.items():
            if norm(server) == norm(dc):
                return data["Names"], None
            for world in data["Names"]:
                if norm(server) == norm(world):
                    return [world], None
        candidates = [w for d in dcs.values() for w in d["Names"] if norm(server) in norm(w)]
        return [], "未识别服务器，请使用完整服务器名或大区名。候选：" + "、".join(candidates[:15])

    def query(self, server, target="", category="S", limit=12):
        with self.lock:
            self.sync()
            rank = {"s": "sRank", "srank": "sRank", "a": "aRank", "arank": "aRank", "fate": "fate", "临危受命": "fate"}.get(category.casefold())
            if not rank:
                return {"错误": "类型仅支持 S、A、FATE"}
            worlds, error = self.worlds(server)
            if error:
                return {"提示": error, "大区": list(self.resources["DataCenters"])}
            cache_key = (tuple(worlds), rank)
            cached = self.cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 15:
                _, fetched, payload = cached
            else:
                payload = self.post("lastDeathTimers", {"QueryDeathTimers": worlds, "RankType": rank})
                if not isinstance(payload.get("timers"), list):
                    raise ValueError("计时接口数据格式发生变化")
                fetched = time.time()
                self.cache[cache_key] = (time.monotonic(), fetched, payload)
                if len(self.cache) > 32:
                    self.cache.pop(next(iter(self.cache)))
            definitions = self.definitions(rank)
            result = []
            now = time.time()
            for timer in payload["timers"]:
                key = timer.get("huntKey", timer.get("huntName", ""))
                value = definitions.get(key, definitions.get(key[:-2], {}))
                if not self.matches(target, key, value):
                    continue
                record = self.info(key, value)
                suffix = re.search(r" (\d+)$", timer.get("huntName", ""))
                instance = timer.get("instance")
                if instance is None and suffix:
                    instance = int(suffix[1])
                record.update({"服务器": timer.get("worldName"), "分线": instance,
                               "计时对象": timer.get("huntName"), "基准时间": stamp(timer.get("lastDeathTime")),
                               "基准类型": "维护重置" if timer.get("isMaint") else "上次击杀记录"})
                params = value.get("MaintTimer" if timer.get("isMaint") else "RespawnTimer")
                death = timer.get("lastDeathTime")
                if params and len(params) == 2 and death:
                    # Website displays [minimum, minimum + window length], not [minimum, maximum].
                    start = float(death) + float(params[0]) * 3600
                    end = start + float(params[1]) * 3600
                    record.update({"窗口开始": stamp(start), "窗口结束": stamp(end),
                                   "距窗口开始_秒": round(start - now),
                                   "状态": "尚未进入刷新窗口" if now < start else "已进入刷新窗口" if now <= end else "已超过理论窗口，可能漏报或条件尚未满足"})
                else:
                    record["状态"] = "缺少刷新参数或基准记录，不能计算"
                if timer.get("lastSeen"):
                    record["网站发现报告时间"] = stamp(timer["lastSeen"])
                result.append(record)
            result.sort(key=lambda r: r.get("距窗口开始_秒", float("inf")))
            limit = max(1, min(25, int(limit)))
            return {"来源": SOURCE, "查询时间_北京时间": stamp(now), "接口读取时间": stamp(fetched),
                    "说明": "实时读取网站记录，最多缓存15秒；记录由玩家上报。刷新窗口不等于已出现，触发条件仍需满足。参数为[最小间隔,窗口长度]。缺失或过旧记录不可当作实际存活证明。",
                    "匹配数量": len(result), "本次显示": result[:limit],
                    "提示": "结果已截断，请指定目标或服务器缩小查询" if len(result) > limit else "未找到记录，请检查名称、服务器或尝试A/S/FATE类型" if not result else ""}

    def knowledge(self, keyword):
        with self.lock:
            self.sync()
            results = []
            for category in ("sRank", "fate"):
                for key, value in self.definitions(category).items():
                    if keyword and self.matches(keyword, key, value):
                        results.append(self.info(key, value))
            guides = [{"条目": key, "内容": value.get("cn") or value.get("en")}
                      for key, value in self.translations["guide"].items()]
            return {"来源": SOURCE, "说明": "网站资料查询，不能替代实时计时；天气/月相的下一次实际触发时刻尚未自动计算。",
                    "网站用途": "FF14精英狩猎与FATE记录，展示玩家上报的击杀时间、刷新窗口与触发条件。",
                    "大区与服务器": self.resources["DataCenters"] if not keyword or any(s in keyword for s in ("服务器", "大区")) else None,
                    "资料总数": len(results), "目标资料": results[:15],
                    "使用说明": guides if not results else [],
                    "提示": "资料过多，请使用更具体的名称" if len(results) > 15 else ""}
