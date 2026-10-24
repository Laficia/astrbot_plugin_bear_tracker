"""Accurate FFXIV map pins on Bear Tracker's unmodified map textures.

``x`` and ``y`` must be in-game map coordinates, NOT world X/Z.  The inverse
of Dalamud's MapLinkPayload conversion is ``u=(x-1)*(SizeFactor/100)/41``.
Map offsets have already been applied when world positions become map XY;
applying them here again would move the pin twice.  Bear's frontend rounds
Heavensward's map extent to 43 and applies a CSS Y adjustment; neither belongs
to the coordinate system of the original, square texture used here.

Sources and the map-row audit are recorded in ``map_metadata.json``.  Unknown
regions, ambiguous regions and out-of-range coordinates fail explicitly.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError


BASE = "https://tracker.beartoolkit.com"


class MapRenderError(ValueError):
    """There is insufficient reliable data to produce a map pin."""


def map_xy_to_pixel(x, y, size_factor, width, height):
    """Convert game map XY to full texture pixels, preserving subpixel precision.

    Coordinates are interpreted literally (no arbitrary 0.05/0.1 centering).
    A one-decimal reported position naturally has the same precision as its
    source.  Invalid values are rejected, never clamped to a plausible point.
    """
    try:
        if isinstance(x, bool) or isinstance(y, bool):
            raise ValueError("boolean coordinates")
        x, y, factor = float(x), float(y), float(size_factor)
    except (TypeError, ValueError) as exc:
        raise MapRenderError("坐标不是有效数字，不能标注地图") from exc
    if not all(math.isfinite(v) for v in (x, y, factor)) or factor <= 0:
        raise MapRenderError("地图比例或坐标无效，不能标注地图")
    u, v = (x - 1) * factor / 4100, (y - 1) * factor / 4100
    if not (0 <= u < 1 and 0 <= v < 1):
        raise MapRenderError("坐标超出该地图范围，不能标注地图")
    if width <= 0 or height <= 0:
        raise MapRenderError("地图图片尺寸无效")
    return u * width, v * height


class BearMapRenderer:
    """Blocking Pillow renderer; call through ``asyncio.to_thread`` in AstrBot.

    ``resources`` is ``syncSession``'s resources dictionary.  No hunt spawn
    point is guessed when a live report lacks its own coordinates.
    """

    def __init__(self, cache_dir, *, font_path=None, map_size=512, timeout=25):
        self.cache_dir = Path(cache_dir)
        self.map_dir = self.cache_dir / "maps"
        self.output_dir = self.cache_dir / "rendered"
        self.map_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.map_size = max(512, min(int(map_size), 1600))
        self.timeout = timeout
        self.font_path = self._find_font(font_path)
        self._lock = threading.RLock()
        data = json.loads(Path(__file__).with_name("map_metadata.json").read_text("utf-8"))
        self.maps = data["maps"]
        reference = json.loads(Path(__file__).with_name("reference_catalog.json").read_text("utf-8"))
        self.reference_regions = {key: row["region"] for key, row in reference["hunts"].items()}

    @staticmethod
    def _find_font(explicit):
        candidates = [explicit] if explicit else []
        candidates += [
            os.environ.get("BEAR_MAP_FONT"),
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/arphic/ukai.ttc",
        ]
        return next((str(p) for p in candidates if p and Path(p).is_file()), None)

    def _font(self, size):
        if self.font_path:
            try:
                return ImageFont.truetype(self.font_path, size)
            except OSError:
                pass
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1; retain a usable English fallback.
            return ImageFont.load_default()

    def _resolve_region(self, resources, hunt_key):
        definition = resources.get("DatabaseHunt", {}).get(hunt_key)
        if not isinstance(definition, dict):
            raise MapRenderError("网站没有该怪物的地图资料")
        regions = definition.get("Regions") or definition.get("Region")
        if isinstance(regions, str):
            regions = [regions]
        if not isinstance(regions, (list, tuple)) or len(set(regions)) != 1:
            raise MapRenderError("该怪物对应多个地图，无法确定正确地图")
        region = regions[0]
        if hunt_key in self.reference_regions and self.reference_regions[hunt_key] != region:
            raise MapRenderError("怪物地图与已校准资料不一致，停止标点")
        if region not in self.maps:
            raise MapRenderError("该地图尚未校验坐标比例，不能可靠标点")
        # The whitelist stores the exact game map row and texture level.  A
        # future multi-floor/unknown region must be audited before adding it.
        return region, self.maps[region]

    @staticmethod
    def _read_map(path):
        try:
            with Image.open(path) as img:
                # These public assets are original 2048-square game textures.
                # A resized/cropped/new format must be re-audited, not guessed.
                if img.size != (2048, 2048):
                    raise MapRenderError("网站地图尺寸已变化，已停止标点以避免位置错误")
                img.load()
                return img.convert("RGB")
        except (UnidentifiedImageError, OSError) as exc:
            raise MapRenderError("地图文件无法读取") from exc

    def _map_image(self, region):
        key = hashlib.sha256(region.encode("utf-8")).hexdigest()[:24]
        path = self.map_dir / (key + ".png")
        with self._lock:
            # Maps are immutable game textures.  Revalidate weekly, retaining a
            # previously verified map when a transient outage affects the CDN.
            if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
                try:
                    return self._read_map(path)
                except MapRenderError:
                    path.unlink(missing_ok=True)
            url = BASE + "/static/images/HuntRegions/" + urllib.parse.quote(region, safe="") + ".png"
            request = urllib.request.Request(url, headers={
                "User-Agent": "AstrBot-BearTracker/2.0 (+https://tracker.beartoolkit.com/timer)",
                "Referer": BASE + "/timer",
            })
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read(20 * 1024 * 1024 + 1)
                if len(payload) > 20 * 1024 * 1024:
                    raise MapRenderError("地图文件超出大小限制")
                img = self._read_map(io.BytesIO(payload))
            except (OSError, urllib.error.URLError) as exc:
                if path.exists():
                    return self._read_map(path)
                raise MapRenderError("地图下载失败，无法附加可靠的地图标点") from exc
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
            return img

    @staticmethod
    def _fit_text(draw, text, x, y, max_width, font, fill):
        text = str(text).replace("\n", " ").replace("\r", " ")
        while text and draw.textlength(text, font=font) > max_width:
            text = text[:-2] + "…" if len(text) > 2 else ""
        draw.text((x, y), text, font=font, fill=fill)

    def render(self, resources, hunt_key, x, y, *, server="", hunt_name="", region_name="", output_path=None):
        """Return a square map PNG with only a blue point at the reported XY.

        No titles, footer, coordinate labels or other overlays are drawn.  ``region_name``
        only changes its display label; the texture is always resolved from the
        authoritative hunt definition, so a translated name cannot choose it.
        """
        region, metadata = self._resolve_region(resources, hunt_key)
        px, py = map_xy_to_pixel(x, y, metadata["size_factor"], self.map_size, self.map_size)
        image = self._map_image(region).resize((self.map_size, self.map_size), Image.Resampling.LANCZOS)
        canvas = image
        draw = ImageDraw.Draw(canvas)
        radius = max(6, round(self.map_size / 64))
        draw.ellipse((px-radius, py-radius, px+radius, py+radius), fill=(0, 0, 255))
        if output_path is None:
            fingerprint = json.dumps([region, hunt_key, x, y, server, hunt_name, region_name], ensure_ascii=False)
            key = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
            output_path = self.output_dir / (key + ".png")
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Distinct calls can refer to the same spawn; serialize the atomic write.
        with self._lock:
            temporary = output_path.with_suffix(".tmp")
            canvas.save(temporary, format="PNG", optimize=True)
            temporary.replace(output_path)
            if output_path.parent == self.output_dir:
                self._prune_outputs()
        return output_path

    def _prune_outputs(self):
        # Avoid unbounded image growth during months of announcements.  Fresh
        # images remain available for asynchronous message adapters to read.
        files = sorted(self.output_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        threshold = time.time() - 3600
        for path in files[256:]:
            try:
                if path.stat().st_mtime < threshold:
                    path.unlink()
            except OSError:
                pass
