"""踩芒果喊话广播（狭缝 S / 天外天垓 · 小异亚招募）。

链路：
  测试链源群有人喊「<服务器> 天外天垓 需要小异亚」
    → 从站点同源的 tracker-points 接口拉该服狭缝(Narrow-rift)实时点位
    → 绿点 = tracker 没记录踩过的固定点位（排除 B 怪点；数据版本不匹配视为未同步）
    → 只有 1 个绿点：直接广播该点（文案「对应的唯一绿点」）
    → 有多个绿点：先不广播，把全绿点高亮图 + 选点提示发回源群，
      等对方回复其中一个坐标（如 ( 14.9 , 36.1 )），才广播被选定的那一个点
      （文案「选定的绿点」）——广播出去的永远只有一个点位
    → 广播是「文字 + 图」一条消息，发到目标群

图片只有标记没有文字：绿环=还没踩过的触发点位，金菱形=大水晶（以太之光传送点）；
标记含义写在广播文案里，图上不放任何文字。

绿点语义与站点完全一致（site/index.html SRKPoints.apply/state）：
  state 为真 → 已踩（红=verified / 黄=未验证）；state 假或整条缺失 → 绿点；
  admin 只可能挂在 state 为真的点上，不影响绿点判定；
  bmob 点位（B 怪）永远不算绿点；接口版本与配置不一致时整体视为未同步，
  不拿旧数据误导大家去已经踩过的点。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:  # 服务器没装 pillow 时整体停用本功能，不影响插件其他部分加载
    from PIL import Image, ImageDraw, ImageFont

    HAS_PIL = True
except ImportError:  # pragma: no cover - 仅在缺依赖的环境命中
    Image = ImageDraw = None
    HAS_PIL = False

DEFAULT_TEMPLATE = (
    "号外号外，我们需要更多的小异亚来唤醒沉睡的大芒果！！\n"
    "请能帮忙的小伙伴们前往【{world}】{coords}\n"
    "（{note}；图上蓝点=本次标出的点，淡蓝晶体=大水晶传送点）"
)

# 多个绿点时请对方回复选点的提示；{list} 是逐行「编号 ( x , y )」
DEFAULT_PICK_PROMPT = (
    "【{world}】天外天垓当前有 {count} 个绿点：\n"
    "{list}\n"
    "请回复其中一个坐标（如 {example}）选定要广播的绿点。"
    "（图上蓝点=本次标出的点，淡蓝晶体=大水晶传送点）"
)

# 喊话里「天外天垓」的替身说法，写哪个都认
DEFAULT_REGION_WORDS = ("天外天垓", "狭缝", "踩芒果", "大芒果")

# 天外天垓的大水晶（以太之光），坐标与站点 aeth 表一致；图上用淡青晶体标出
DEFAULT_AETHERYTES = (
    ("异亚村落", 22.8, 8.6),
    ("半途终旅", 10.6, 27.1),
    ("奥密克戎基地", 31.3, 28.3),
)

# 坐标识别：全角/半角括号与逗号都可省略，如 ( 14.9 , 36.1 ) / 14.9,36.1
_COORD_RE = re.compile(
    r"[（(]?\s*(\d{1,2}(?:\.\d{1,2})?)\s*[，,]\s*(\d{1,2}(?:\.\d{1,2})?)\s*[)）]?"
)
# 坐标匹配容差（配置里的点位保留 1 位小数，允许对方四舍五入到 2 位）
_COORD_TOL = 0.05


def parse_coords(text: str) -> list[tuple[float, float]]:
    """从文本里抠出所有「x, y」坐标。"""
    out: list[tuple[float, float]] = []
    for m in _COORD_RE.finditer(text or ""):
        try:
            out.append((float(m.group(1)), float(m.group(2))))
        except ValueError:
            continue
    return out


@dataclass
class PendingPick:
    """一次多绿点喊话挂起的选点等待。"""

    dc: str
    world: str
    greens: list[tuple[int, float, float]]
    image_path: str | None = None
    expires_at: float = 0.0

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.expires_at

    def match(self, x: float, y: float):
        """坐标命中哪个绿点；不中返回 None。"""
        for green in self.greens:
            if abs(green[1] - x) <= _COORD_TOL and abs(green[2] - y) <= _COORD_TOL:
                return green
        return None

# 高亮配色（与站点绿点同族的亮绿，白描边保证在任何地图底色上都看得见）
_RING = (34, 197, 94)
_RING_SOFT = (34, 197, 94, 90)
_WHITE = (255, 255, 255)

# 大水晶（以太之光）：淡青白多面体，配色照搬灰机 wiki 狭缝页地图
_GEM_LINE = (26, 60, 62)
_GEM_DARK = (70, 130, 145)
_GEM_MID = (129, 175, 188)
_GEM_LIGHT = (182, 230, 234)
_GEM_HI = (214, 252, 255)


class MangoWatch:
    """踩芒果喊话的匹配、取数、绿点计算与图片渲染。"""

    def __init__(self, config: dict):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.source_groups = [int(g) for g in (cfg.get("source_groups") or [])]
        self.target_groups = [int(g) for g in (cfg.get("target_groups") or [])]
        self.hunt = str(cfg.get("hunt") or "Narrow-rift")
        self.line = int(cfg.get("line") or 1)
        self.version = int(cfg.get("version") or 0) or None
        self.map_size = float(cfg.get("map_size") or 41)
        self.points = [
            (float(p[0]), float(p[1])) for p in (cfg.get("points") or []) if len(p) >= 2
        ]
        self.bmob = {int(b) for b in (cfg.get("bmob") or [])}
        # 服务器名 -> 大区（两 DC 全量；喊话匹配只认表里的名字，避免误判）
        self.servers = {str(k): str(v) for k, v in (cfg.get("servers") or {}).items()}
        self.api = str(cfg.get("points_api") or "http://119.29.119.179/api/tracker-points")
        self.map_image = str(cfg.get("map_image") or "")
        self.work_dir = Path(str(cfg.get("work_dir") or "data/mango"))
        self.cooldown = float(cfg.get("cooldown_seconds") or 300)
        self.timeout = float(cfg.get("timeout_seconds") or 6)
        self.template = str(cfg.get("template") or DEFAULT_TEMPLATE)
        self.pick_prompt = str(cfg.get("pick_prompt") or DEFAULT_PICK_PROMPT)
        # 多绿点喊话挂起后，等对方回复坐标的有效时间
        self.pending_ttl = float(cfg.get("pending_ttl_seconds") or 600)
        self.region_words = tuple(
            str(w) for w in (cfg.get("region_words") or DEFAULT_REGION_WORDS) if str(w)
        )
        # 大水晶（以太之光）[(名称, x, y), ...]；图上用淡青晶体标出，默认天外天垓 3 个
        self.aetherytes = [
            (str(a[0]), float(a[1]), float(a[2]))
            for a in (cfg.get("aetherytes") or DEFAULT_AETHERYTES)
            if len(a) >= 3
        ]
        # 冷却 key: "<dc>/<world>" -> 上次广播时间(monotonic)
        self._last_broadcast: dict[str, float] = {}
        # 正在处理的喊话，避免同一服多人同时喊导致重复广播
        self._inflight: set[str] = set()
        # 多绿点喊话挂起的选点等待 key: "<dc>/<world>" -> PendingPick
        self._pending: dict[str, PendingPick] = {}
        self._prefix = self._build_prefix_pattern()

    # ---- 配置自检 ----

    def validate(self) -> list[str]:
        """返回配置问题列表；空列表表示可用。"""
        problems: list[str] = []
        if not self.source_groups:
            problems.append("没有源群(source_groups)")
        if not self.target_groups:
            problems.append("没有目标群(target_groups)")
        if not self.points:
            problems.append("没有固定点位(points)")
        if not self.servers:
            problems.append("没有服务器表(servers)")
        if not self.map_image or not Path(self.map_image).exists():
            problems.append(f"地图图片不存在: {self.map_image}")
        if self._prefix is None:
            problems.append("服务器表为空，喊话无法匹配")
        return problems

    # ---- 喊话匹配 ----

    def _build_prefix_pattern(self):
        if not self.servers:
            return None
        # 长名优先，避免简称被长名截胡
        names = sorted(self.servers, key=len, reverse=True)
        alt = "|".join(re.escape(n) for n in names)
        return re.compile(r"^[\s【\[「『(（]*(?P<world>" + alt + r")")

    def dc_of(self, world: str) -> str:
        return self.servers.get(world, "")

    def match(self, text: str) -> str | None:
        """是踩芒果喊话就返回服务器名，否则返回 None。

        认这些写法（服务器名必须开头，允许【】包裹）：
          银泪湖 天外天垓 需要小异亚
          银泪湖天外天垓需要小异亚
          【银泪湖】天外天垓（狭缝）需要小异亚
          银泪湖 狭缝 需要一些小异亚
        「不需要小异亚」这类否定不算。
        """
        if self._prefix is None:
            return None
        m = self._prefix.match(text or "")
        if not m:
            return None
        world = m.group("world")
        rest = (text or "")[m.end():]
        if not any(word in rest for word in self.region_words):
            return None
        if not re.search(r"(?<![不别未])需要\s*[一此些的]*\s*小异亚", rest):
            return None
        return world

    # ---- 冷却 / 并发 ----

    def cooldown_left(self, dc: str, world: str) -> float:
        """剩余冷却秒数；0 表示可以喊。"""
        last = self._last_broadcast.get(f"{dc}/{world}")
        if last is None:
            return 0.0
        return max(0.0, self.cooldown - (time.monotonic() - last))

    def begin(self, dc: str, world: str) -> bool:
        """占用一个服的喊话处理；已在处理中返回 False。"""
        key = f"{dc}/{world}"
        if key in self._inflight:
            return False
        self._inflight.add(key)
        return True

    def finish(self, dc: str, world: str) -> None:
        self._inflight.discard(f"{dc}/{world}")

    def mark_broadcast(self, dc: str, world: str) -> None:
        self._last_broadcast[f"{dc}/{world}"] = time.monotonic()

    # ---- 多绿点选点等待 ----

    def set_pending(
        self, dc: str, world: str, greens, image_path=None, ttl: float | None = None
    ) -> None:
        """多绿点喊话：挂起选点等待（同一服再次喊话会刷新）。"""
        self._pending[f"{dc}/{world}"] = PendingPick(
            dc=dc,
            world=world,
            greens=list(greens),
            image_path=image_path,
            expires_at=time.monotonic()
            + (self.pending_ttl if ttl is None else float(ttl)),
        )

    def pending_for(self, dc: str, world: str) -> PendingPick | None:
        key = f"{dc}/{world}"
        pending = self._pending.get(key)
        if pending is None:
            return None
        if pending.expired():
            self._pending.pop(key, None)
            return None
        return pending

    def clear_pending(self, dc: str, world: str) -> None:
        self._pending.pop(f"{dc}/{world}", None)

    def has_pending(self) -> bool:
        """是否还有服在等待选点（顺手清掉过期的）。"""
        now = time.monotonic()
        for key, pending in list(self._pending.items()):
            if pending.expired(now):
                self._pending.pop(key, None)
        return bool(self._pending)

    def match_pending(self, text: str):
        """源群回复了坐标：匹配等待中的选点。

        返回 (dc, world, green)；没匹配上返回 None。
        消息里点名了服务器就只在该服的等待里找；没点名则要求坐标
        能唯一命中某个等待（各服固定点位相同，可能撞车）。
        """
        coords = parse_coords(text)
        if not coords:
            return None
        mentioned = [w for w in self.servers if w in (text or "")]
        if mentioned:
            for world in mentioned:
                for key, pending in list(self._pending.items()):
                    if pending.world != world:
                        continue
                    if pending.expired():
                        self._pending.pop(key, None)
                        continue
                    for x, y in coords:
                        green = pending.match(x, y)
                        if green is not None:
                            return pending.dc, pending.world, green
        hits = []
        for key, pending in list(self._pending.items()):
            if pending.expired():
                self._pending.pop(key, None)
                continue
            for x, y in coords:
                green = pending.match(x, y)
                if green is not None:
                    hits.append((pending.dc, pending.world, green))
                    break
        if len(hits) == 1:
            return hits[0]
        return None

    # ---- 取数 ----

    def fetch_points(self, dc: str, world: str):
        """拉某服狭缝实时点位。

        返回 (points, meta)；数据不可用返回 (None, 原因)。
        校验与站点一致：源/服/怪/线/版本/cutoff 对不上就整体作废。
        """
        query = urllib.parse.urlencode(
            {"dc": dc, "world": world, "hunt": self.hunt, "line": self.line}
        )
        req = urllib.request.Request(
            f"{self.api}?{query}",
            headers={
                "User-Agent": "HunHuanBot/mango-watch",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if (
            data.get("source") != "tracker"
            or data.get("world") != world
            or data.get("hunt") != self.hunt
            or data.get("line") != self.line
        ):
            return None, "接口返回与请求不服"
        if self.version is not None and data.get("version") != self.version:
            return None, f"点位版本不匹配({data.get('version')}≠{self.version})"
        points = data.get("points")
        if not isinstance(points, dict) or not points:
            return None, "接口没有点位数据"
        meta = {
            "observedAt": data.get("observedAt"),
            "cacheStatus": data.get("cacheStatus"),
            "cutoff": data.get("cutoff"),
        }
        return points, meta

    # ---- 绿点 ----

    def greens(self, points: dict) -> list[tuple[int, float, float]]:
        """绿点 = tracker 没记录踩过的固定点位（排除 B 怪点）。

        返回 [(点位号, x, y), ...] 按点位号升序——与站点 greenOf 同序，
        多个绿点时编号最小的在前（站点 scAutoPick 也是取第一个）。
        """
        out: list[tuple[int, float, float]] = []
        for idx, (x, y) in enumerate(self.points, start=1):
            if idx in self.bmob:
                continue
            p = points.get(str(idx))
            if p is None:
                p = points.get(idx)
            if p and p.get("state"):
                continue  # 已踩：红(verified) / 黄(未验证)
            out.append((idx, x, y))
        return out

    # ---- 文案 ----

    def build_text(self, world: str, greens, picked: bool = False) -> str:
        """广播文案；``picked`` 表示这个绿点是对方从多个里选定的。"""
        coords = "、".join(f"( {x:.1f} , {y:.1f} )" for _, x, y in greens)
        if len(greens) == 1:
            note = "选定的绿点" if picked else "对应的唯一绿点"
        else:
            # 多绿点不会直接广播（必须先选点），这里只是兜底
            note = f"当前还有 {len(greens)} 个绿点，任选一个即可"
        try:
            return self.template.format(
                world=world, coords=coords, note=note, count=len(greens)
            )
        except (KeyError, IndexError, ValueError):
            # 配置里写了坏模板也别把广播整个搞丢
            return DEFAULT_TEMPLATE.format(
                world=world, coords=coords, note=note, count=len(greens)
            )

    def build_pick_prompt(self, world: str, greens) -> str:
        """多绿点时的选点提示（逐行列坐标，请对方回复其中一个）。"""
        lines = [f"{idx} ( {x:.1f} , {y:.1f} )" for idx, x, y in greens]
        example = f"( {greens[0][1]:.1f} , {greens[0][2]:.1f} )" if greens else ""
        try:
            return self.pick_prompt.format(
                world=world,
                count=len(greens),
                list="\n".join(lines),
                example=example,
            )
        except (KeyError, IndexError, ValueError):
            return DEFAULT_PICK_PROMPT.format(
                world=world,
                count=len(greens),
                list="\n".join(lines),
                example=example,
            )

    # ---- 图片 ----

    def render(self, dc: str, world: str, greens) -> str:
        """Plain square map, prominent blue destinations, named aetherytes."""
        if not HAS_PIL:
            raise RuntimeError("pillow 未安装，无法生成点位图")
        style_dir = self.work_dir / "blue-map-v3"
        style_dir.mkdir(parents=True, exist_ok=True)
        key = "-".join(str(i) for i, _, _ in greens)
        path = style_dir / f"mango_{world}_{key}.jpg"
        if path.exists():
            return str(path)
        fonts = ["/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "C:/Windows/Fonts/msyh.ttc"]
        font_path = next((f for f in fonts if Path(f).exists()), None)
        if font_path is None:
            raise RuntimeError("没有可用的中文字体")
        size = 1024
        base = Image.open(self.map_image).convert("RGB").resize((size,size),Image.Resampling.LANCZOS)
        overlay = Image.new("RGBA",base.size,(0,0,0,0))
        draw = ImageDraw.Draw(overlay)
        def pos(x,y):
            return (x-1)/self.map_size*size,(y-1)/self.map_size*size
        for name,x,y in self.aetherytes:
            px,py=pos(x,y)
            hub="异亚村落" in name
            self._draw_gem(draw,px,py,size/2048,hub)
            draw.text((px,py-(69 if hub else 47)),name,font=ImageFont.truetype(font_path,26),anchor="mm",fill=(255,255,240),stroke_width=2,stroke_fill=(63,53,34))
        for _,x,y in greens:
            px,py=pos(x,y)
            r=17
            draw.ellipse((px-r,py-r,px+r,py+r),fill=(10,35,255,255))
        out=Image.alpha_composite(base.convert("RGBA"),overlay).convert("RGB")
        out.save(path,"JPEG",quality=95)
        return str(path)

    @staticmethod
    def _draw_gem(draw, px, py, s: float, hub: bool) -> None:
        """淡青多面体大水晶（参照图配色）：中央高 104s，两侧 56s。"""
        H = 104 * s if hub else 56 * s
        W = H * 0.62
        cy = py - H * 0.52  # 晶体几何中心，底座落在点位上
        top_y, bot_y = cy - H * 0.5, cy + H * 0.5

        # 柔光
        glow = H * 0.85
        draw.ellipse(
            [px - glow, cy - glow * 0.6, px + glow, cy + glow * 0.75],
            fill=(182, 230, 234, 46),
        )
        # 基座线
        draw.line(
            [(px - W * 0.9, bot_y), (px + W * 0.9, bot_y)],
            fill=_GEM_LINE,
            width=max(2, int(5 * s)),
        )
        # 晶体外形（上尖下宽的六边形）
        body = [
            (px, top_y),
            (px + W * 0.5, cy - H * 0.16),
            (px + W * 0.34, cy + H * 0.34),
            (px, bot_y),
            (px - W * 0.34, cy + H * 0.34),
            (px - W * 0.5, cy - H * 0.16),
        ]
        draw.polygon(body, fill=_GEM_MID, outline=_GEM_LINE)
        # 左暗面
        draw.polygon(
            [
                (px, top_y),
                (px - W * 0.5, cy - H * 0.16),
                (px - W * 0.34, cy + H * 0.34),
                (px, bot_y),
            ],
            fill=_GEM_DARK,
        )
        # 右亮面
        draw.polygon(
            [
                (px, top_y),
                (px + W * 0.5, cy - H * 0.16),
                (px + W * 0.34, cy + H * 0.34),
                (px, bot_y),
            ],
            fill=_GEM_LIGHT,
        )
        # 中央高光条
        hw = W * 0.13
        draw.polygon(
            [
                (px, top_y + H * 0.05),
                (px + hw, cy - H * 0.06),
                (px - hw * 0.4, cy + H * 0.3),
                (px - hw, cy - H * 0.06),
            ],
            fill=_GEM_HI,
        )
