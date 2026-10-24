# -*- coding: utf-8 -*-
"""S 怪触发上报采集：把群里 Sonar 等机器人的触发播报写成看板数据文件。

观测到的播报格式（狗区扩散群）：
    [Sonar] 60S: 刚德瑞瓦
    [狗-水晶塔] 翻云雾海 (16.8, 23.3)

第一行「NS: 怪名」的 N 是距触发已过去的秒数；第二行是服务器与坐标。
同一条播报会出现在多个扩散群，按 (服务器, S怪) + 触发时间戳去重。

输出 JSON 与看板 RELAY 结构一致（站点按 max(时间戳) 合并）：
    {"generated": 123, "relay": {"水晶塔": {"Gandarewa": [ts, x, y], ...}}}
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("ForwardBotPlugin")

# 47 只 S 怪：中文名 -> 看板 RELAY 用的 key（与站点内嵌 hunts 数据一致）
HUNT_CN2KEY = {
    "阿格拉俄珀": "Aglaope",
    "阿格里帕": "Agrippa the Mighty",
    "阿姆斯特朗": "Armstrong",
    "先驱勇士 阿提卡斯": "Atticus the Primogenitor",
    "极乐鸟": "Bird of Paradise",
    "爬骨怪龙": "Bone Crawler",
    "火愤牛": "Bonnacon",
    "布隆特斯": "Brontes",
    "布弗鲁": "Burfurlur the Canny",
    "牛头黑神": "Chernobog",
    "咕尔呱洛斯": "Croakadile",
    "护土精灵": "Croque-Mitaine",
    "得到宽恕的炫学": "Forgiven Pedantry",
    "伽马": "Gamma",
    "刚德瑞瓦": "Gandarewa",
    "顾尼图": "Gunitt",
    "伊努索奇": "Ihnuxokiy",
    "伊休妲": "Ixtab",
    "凯撒贝希摩斯": "Kaiser Behemoth",
    "厌忌之人 奇里格": "Kirlirger the Abhorrent",
    "雷德罗巨蛇": "Laideronnette",
    "巴拉乌尔": "Lampalagua",
    "卢克洛塔": "Leucrotta",
    "夺心魔": "Mindflayer",
    "蚓螈巨虫": "Minhocao",
    "南迪": "Nandi",
    "狭缝": "Narrow-rift",
    "内尤佐缇": "Neyoozoteel",
    "努纽努维": "Nunyunuwi",
    "巨大鳐": "Okina",
    "俄菲翁尼厄斯": "Ophioneus",
    "兀鲁忽乃朝鲁": "Orghana",
    "沉思之物": "Ruminator",
    "萨法特": "Safat",
    "盐和光": "Salt and Light",
    "山谢亚": "Sansheya",
    "神穆尔鸟": "Senmurv",
    "颇胝迦": "Sphatika",
    "多智兽": "Tarchia",
    "天气预报机器人": "The Forecaster",
    "伽洛克": "The Garlok",
    "苍白骑士": "The Pale Rider",
    "千竿口花希达": "Thousand-cast Theda",
    "戾虫": "Tyger",
    "优昙婆罗花": "Udumbara",
    "乌尔伽鲁": "Wulgaru",
    "虚无探索者": "Zona Seeker"
}

# 默认认可的服务器（播报第二行 [狗-服务器] 里的服务器名）
DEFAULT_SERVERS = ("水晶塔", "银泪湖", "太阳海岸", "伊修加德", "红茶川")

# 播报前缀快筛（与 leak_fill.prefixes 保持一致）
DEFAULT_PREFIXES = ("[Sonar", "[银山雀儿")

# [Sonar] 60S: 刚德瑞瓦 \n [狗-水晶塔] 翻云雾海 (16.8, 23.3)
SPAWN_RE = re.compile(
    r"\[[^\]\n]*\]\s*(\d+)\s*[sS秒]?\s*[:：]?\s*([^\n\[\]]+?)\s*\n"
    r"\s*\[\s*[^\]\n\-]*\s*-\s*([^\]\s]+)\s*\]"
    r"[^\n(]*\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)"
)


class SpawnWatcher:
    """把触发播报写进看板数据文件；重启后从文件恢复，重启不丢记录。"""

    def __init__(
        self,
        path: str,
        servers=DEFAULT_SERVERS,
        prefixes=DEFAULT_PREFIXES,
        dedupe_seconds: float = 600.0,
        keep_seconds: float = 86400.0,
    ):
        self.path = Path(path)
        self.servers = {str(s) for s in servers}
        self.prefixes = tuple(str(p) for p in prefixes if str(p))
        self.dedupe = float(dedupe_seconds)
        self.keep = float(keep_seconds)
        # relay: {服务器: {huntKey: [触发时间戳, x, y]}}
        self.relay: dict[str, dict[str, list]] = {}
        self.generated = 0
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        relay = raw.get("relay")
        if not isinstance(relay, dict):
            return
        now = time.time()
        for srv, hunts in relay.items():
            if not isinstance(hunts, dict):
                continue
            for key, e in hunts.items():
                if (
                    isinstance(e, list)
                    and len(e) >= 3
                    and isinstance(e[0], (int, float))
                    and now - float(e[0]) < self.keep
                ):
                    self.relay.setdefault(srv, {})[key] = [float(e[0]), float(e[1]), float(e[2])]
        self.generated = int(raw.get("generated") or 0)

    def flush(self) -> None:
        """原子写盘（临时文件 + rename），失败只记日志不影响转发。"""
        now = int(time.time())
        if now <= self.generated:
            now = self.generated + 1
        self.generated = now
        payload = {"generated": self.generated, "relay": self.relay}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("触发上报写盘失败: %s", self.path)
            try:
                tmp.unlink()
            except OSError:
                pass

    # ---- 采集 ----

    def observe(self, text: str):
        """解析一条群消息；是新的触发播报就记录并返回 (服务器, key)，否则 None。"""
        t = (text or "").strip()
        if not t or not t.startswith(self.prefixes):
            return None
        m = SPAWN_RE.search(t)
        if not m:
            return None
        secs = int(m.group(1))
        name = m.group(2).strip()
        srv = m.group(3).strip()
        try:
            x, y = float(m.group(4)), float(m.group(5))
        except ValueError:
            return None
        key = HUNT_CN2KEY.get(name)
        if not key or srv not in self.servers:
            return None
        ts = max(0.0, time.time() - secs)
        cur = self.relay.get(srv, {}).get(key)
        # 同一事件会在多个群重复出现：触发时间接近视为同一条，跳过。
        if cur is not None and abs(cur[0] - ts) < self.dedupe:
            return None
        self.relay.setdefault(srv, {})[key] = [ts, x, y]
        self._prune()
        return (srv, key)

    def _prune(self) -> None:
        cutoff = time.time() - self.keep
        for srv in list(self.relay):
            hunts = self.relay[srv]
            for key in list(hunts):
                if hunts[key][0] < cutoff:
                    del hunts[key]
            if not hunts:
                del self.relay[srv]
