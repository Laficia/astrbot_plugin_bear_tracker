"""Read the Srank floating window's public inputs; never submit or run page code.

The audited weather/ET rules are ported from the reference page. A changed
calculation block fails closed until it can be audited again. This source is
separate from actual S sightings: a trigger opportunity is never a FoundReport.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

BASE = "https://119.29.119.179"
SOURCE = BASE + "/Srank/"
RULE_DATA = json.loads(Path(__file__).with_name("trigger_rules.json").read_text("utf-8"))
RULES, WEATHER = RULE_DATA["rules"], RULE_DATA["weather"]
DEFAULT_TRIGGER_HUNTS = list(RULES)
ET_HOUR, ET_DAY, WEATHER_PERIOD = 175, 4200, 1400
# The reference site's full Tracker snapshot is cached for about 15 minutes;
# weather/ET transitions are still evaluated every poll against the current clock.
MAX_SOURCE_AGE = 20 * 60


def validate_trigger_hunts(hunts):
    if not isinstance(hunts, list) or len(hunts) > len(RULES):
        raise ValueError("提醒S怪必须为列表，最多13项")
    if any(not isinstance(key, str) or key not in RULES for key in hunts):
        raise ValueError("包含不支持提醒的S怪，请从列表中勾选")
    return list(dict.fromkeys(hunts))


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def forecast_seed(timestamp):
    unix = math.floor(timestamp)
    bell = unix // ET_HOUR
    increment = (bell + 8 - bell % 8) % 24
    base = ((unix // ET_DAY) * 100 + increment) & 0xFFFFFFFF
    step = ((base << 11) ^ base) & 0xFFFFFFFF
    return ((step >> 8) ^ step) % 100


def weather_at(zone, timestamp):
    seed = forecast_seed(timestamp)
    return next(name for bound, name in WEATHER[zone] if seed < bound)


def merged(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def condition_window(key, now):
    """Return the current continuous condition interval, matching SRK.analyze.

    ET lookback is deliberately the page's one day, including its moon rule
    semantics. We do not silently substitute a different site's calendar.
    """
    spec = RULES[key]
    windows = []
    if spec["kind"] in ("weather", "weather_hold"):
        base = math.floor(now / WEATHER_PERIOD) * WEATHER_PERIOD
        run = None
        # The page uses floor(700000 / 1400) + 8 periods in each direction.
        for i in range(-508, 509):
            start = base + i * WEATHER_PERIOD
            weather = weather_at(spec["zone"], start)
            ok = weather in spec["include"] if "include" in spec else weather not in spec["exclude"]
            if ok and run is None:
                run = start
            if not ok and run is not None:
                windows.append([run + spec.get("hold", 0), start])
                run = None
        if run is not None:
            windows.append([run + spec.get("hold", 0), base + 509 * WEATHER_PERIOD])
    else:
        day = math.floor(now / ET_DAY)
        for d in range(day - 1, day + 2):
            dom = d % 32 + 1
            if "days" in spec and dom not in spec["days"]:
                continue
            hours = spec["first_day_hours"] if dom == 1 and "first_day_hours" in spec else spec["hours"]
            for h1, h2 in hours:
                start, end = d * ET_DAY + h1 * ET_HOUR, d * ET_DAY + h2 * ET_HOUR
                if "weather" not in spec:
                    windows.append([start, end])
                    continue
                period = math.floor(start / WEATHER_PERIOD) * WEATHER_PERIOD
                while period < end:
                    if weather_at(spec["zone"], period) in spec["weather"]:
                        windows.append([max(start, period), min(end, period + WEATHER_PERIOD)])
                    period += WEATHER_PERIOD
    return next((interval for interval in merged(windows) if interval[0] <= now < interval[1]), None)


def decode_page(page):
    try:
        block = page[page.index("const ET_SEC_HOUR ="):page.index("// ---------------------------------------------------------------- 对外主入口")]
    except ValueError as exc:
        raise ValueError("参考站触发规则结构已变化，暂停触发提醒") from exc
    digest = hashlib.sha256(re.sub(r"\s+", "", block).encode()).hexdigest()
    if digest != RULE_DATA["source_rules_sha256"] or "const realOpen = cur && ct && ct.ok.length > 0;" not in page:
        raise ValueError("参考站时间、天气或触发期规则已变化，需重新校准")
    group_block = page.split("const GROUPS =", 1)[-1].split("const GROUP_ORDER", 1)[0]
    groups = re.findall(r"hunts:\s*(\[[^\]]+\])", group_block)
    keys = [key for group in groups for key in json.loads(group.replace("'", '"'))]
    if set(keys) != set(RULES) or len(keys) != len(RULES):
        raise ValueError("参考站悬浮窗的S怪范围已变化，需重新校准")
    decoder = json.JSONDecoder()
    centers = {}
    for marker in re.finditer(r"const DATA\s*=\s*", page):
        try:
            data, _ = decoder.raw_decode(page[marker.end():])
        except ValueError:
            continue
        if "generated" in data and data.get("dc") and data.get("hunts"):
            centers[data["dc"]] = {h["k"]: h for h in data["hunts"] if h.get("k") in RULES}
    if not centers or any(set(rows) != set(RULES) for rows in centers.values()):
        raise ValueError("参考站缺少完整触发期计时参数")
    return centers


def validate_live(data, center, now):
    if (not isinstance(data, dict) or data.get("source") != "tracker" or data.get("dc") != center
            or not isinstance(data.get("live"), dict)):
        raise ValueError("参考站实时数据来源或大区不匹配")
    status = data.get("trackerStatus", {})
    if status.get("state") != "ok":
        raise ValueError("参考站上游未完整同步，暂缓触发提醒")
    for timestamp in (data.get("generated"), status.get("lastSuccessAt")):
        if not number(timestamp) or not -60 <= now - timestamp <= MAX_SOURCE_AGE:
            raise ValueError("参考站计时数据已过期，暂缓触发提醒")


def timer_lines(raw, key, world, center, definition, manual, maintenance, now):
    """Apply the page's shared kill/maintenance overlays, preserving instance IDs."""
    lines = {r[0]: [r[0], r[1], len(r) > 2 and r[2] is True] for r in raw
             if isinstance(r, list) and len(r) >= 2 and type(r[0]) is int and r[0] >= 0
             and number(r[1]) and 0 < r[1] <= now}
    count = definition.get("ln", 1)
    if type(count) is not int or not 1 <= count <= 20:
        raise ValueError("参考站分线数量无效")
    latest = {}
    for report in manual:
        if (report.get("withdrawnAt") or report.get("type") != "kill" or report.get("dc") != center
                or report.get("world") != world or report.get("hunt") != key):
            continue
        line, ts = report.get("line", 0), report.get("killAt")
        if type(line) is int and line >= 0 and number(ts) and 0 < ts <= now:
            latest[line] = max(latest.get(line, 0), ts)
    for line, ts in latest.items():
        line = 1 if count == 1 and (line > 0 or len(lines) <= 1) else line
        if line == 0:
            if ts > max((r[1] for r in lines.values()), default=0):
                lines[0] = [0, ts, False]
        elif line not in lines or ts > lines[line][1]:
            lines[line] = [line, ts, False]
    end = max((r["endAt"] for r in maintenance if not r.get("withdrawnAt") and r.get("dc") == center and world in r.get("worlds", [])
               and number(r.get("endAt")) and 0 < r["endAt"] <= now), default=0)
    if end:
        lines = {line: [line, end, True] if ts < end else [line, ts, maint]
                 for line, ts, maint in lines.values() if line != 0 or ts >= end}
        for line in range(1, count + 1):
            lines.setdefault(line, [line, end, True])
    return list(lines.values())


def eligible_events(data, center, worlds, definitions, manual, maintenance, now):
    validate_live(data, center, now)
    windows = {key: condition_window(key, now) for key in RULES}
    events = []
    for world in worlds:
        if world not in data["live"]:
            raise ValueError("参考站缺少服务器计时：" + world)
        for key, definition in definitions.items():
            condition = windows[key]
            if not condition:
                continue
            rows = timer_lines(data["live"][world].get(key, []), key, world, center,
                               definition, manual, maintenance, now)
            opened = []
            for line, death, maint in rows:
                # Floating-window rows are concrete numbered instances. A
                # shared kill with no instance must never invent another line.
                if not 1 <= line <= definition.get("ln", 1):
                    continue
                params = definition.get("mt" if maint and definition.get("mt") else "rt")
                if (not isinstance(params, list) or len(params) != 2 or not all(number(n) for n in params)
                        or params[0] <= 0 or params[1] < 0):
                    continue
                start, end = death + params[0] * 3600, death + sum(params) * 3600
                # This is exactly the green '可触发服' phase, not the grey forced phase.
                if start <= now < end:
                    opened.append((end, line, start))
            if not opened:
                continue
            first = min(opened)
            events.append({"hunt_key": key, "world": world, "data_center": center,
                           "instance": first[1] if definition.get("ln", 1) > 1 else None,
                           "position": None, "window_start": condition[0],
                           "source_at": data["generated"],
                           "window_end": min(condition[1], max(r[0] for r in opened)),
                           "event_key": json.dumps([center, world, key, condition[0]], ensure_ascii=False, separators=(",", ":"))})
    return events


class ReferenceTriggerSource:
    def __init__(self):
        self.metadata = {}
        self.checked_at = 0

    @staticmethod
    def read(path, *, text=False):
        req = urllib.request.Request(BASE + path, headers={"User-Agent": "AstrBot-BearTracker/2.4", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as response:
            content = response.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            raise ValueError("参考站响应过大")
        return content.decode("utf-8") if text else json.loads(content)

    def fetch(self, world_centers, now=None):
        if not self.metadata or time.time() - self.checked_at >= 6 * 3600:
            self.metadata = decode_page(self.read("/Srank/", text=True))
            self.checked_at = time.time()
        manual, maintenance = self.read("/api/manual-reports"), self.read("/api/maintenance-reports")
        if (manual.get("ok") is not True or maintenance.get("ok") is not True
                or not isinstance(manual.get("reports"), list) or not isinstance(maintenance.get("reports"), list)):
            raise ValueError("参考站共享击杀或维护记录暂不可用")
        events, valid_worlds, errors = [], set(), []
        for center in dict.fromkeys(world_centers.values()):
            if center not in self.metadata:
                errors.append(center + "：参考站尚未提供该大区的触发期数据")
                continue
            worlds = [w for w, dc in world_centers.items() if dc == center]
            try:
                data = self.read("/api/live?dc=" + urllib.parse.quote(center))
                observed = time.time() if now is None else now
                events.extend(eligible_events(data, center, worlds, self.metadata[center],
                                              manual["reports"], maintenance["reports"], observed))
                valid_worlds.update(worlds)
            except Exception as exc:
                errors.append(center + "：" + str(exc))
        return events, valid_worlds, errors
