"""Read-only reference data; never execute JavaScript from the source page."""
from __future__ import annotations
import json
import math
import re
import time
import urllib.request
from pathlib import Path

REFERENCE_URL = "https://119.29.119.179/Srank/"
GEOMETRY_URL = "https://119.29.119.179/api/tracker-points/resources"


def decode_reference(page, geometry):
    match = re.search(r"window\.TRIGGER_MAP\s*=\s*", page)
    if not match:
        raise ValueError("参考网页缺少触发条件，保留已有校准资料")
    triggers, _ = json.JSONDecoder().raw_decode(page[match.end():])
    decoder = json.JSONDecoder()
    definitions = {}
    for marker in re.finditer(r"const DATA\s*=\s*", page):
        try:
            block, _ = decoder.raw_decode(page[marker.end():])
        except ValueError:
            continue
        for hunt in block.get("hunts", []):
            if hunt.get("patch") and hunt.get("region"):
                definitions.setdefault(hunt["k"], hunt)
    if geometry.get("source") != "tracker" or not isinstance(geometry.get("maps"), dict):
        raise ValueError("参考点位来源不匹配")
    rows = {}
    for key, trigger in triggers.items():
        definition, geo = definitions.get(key, {}), geometry["maps"].get(key, {})
        if not definition or geo.get("region") != definition.get("region"):
            raise ValueError("地图归属无法核实：" + key)
        points, ids = geo.get("pts", []), geo.get("pointIds", [])
        if not points or len(points) != len(ids):
            raise ValueError("点位编号无法核实：" + key)
        for point in points:
            if len(point) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) and 1 <= v < 45 for v in point):
                raise ValueError("参考点位坐标无效")
        method, notes = trigger.get("method"), trigger.get("notes", [])
        if not isinstance(method, str) or not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
            raise ValueError("触发条件格式无效")
        rows[key] = {"key": key, "name": definition.get("cn") or trigger.get("cn") or key,
                     "region": geo["region"], "region_name": definition.get("regionCn", geo["region"]),
                     "patch": definition["patch"], "method": method, "notes": notes,
                     "points": points, "point_ids": ids, "b_only": geo.get("bmob", []),
                     "dimensions": geo.get("dimensions"), "version": geo.get("version")}
    if len(rows) != 47:
        raise ValueError("参考资料数量发生变化，需重新核对")
    return {"source": REFERENCE_URL, "geometry_source": GEOMETRY_URL,
            "fetched_at": time.time(), "geometry_observed_at": geometry.get("observedAt"), "hunts": rows}


def download_reference():
    def fetch(url):
        req = urllib.request.Request(url, headers={"User-Agent": "AstrBot-BearTracker/2.1"})
        with urllib.request.urlopen(req, timeout=30) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("参考资料超出大小限制")
        return data
    return decode_reference(fetch(REFERENCE_URL).decode("utf-8"), json.loads(fetch(GEOMETRY_URL)))


class ReferenceCatalog:
    def __init__(self):
        self.data = json.loads(Path(__file__).with_name("reference_catalog.json").read_text("utf-8"))

    def load_cache(self, path):
        self.path = Path(path)
        if self.path.exists():
            data = json.loads(self.path.read_text("utf-8"))
            if data.get("source") != REFERENCE_URL or len(data.get("hunts", {})) != 47:
                raise ValueError("已缓存的参考资料格式无效")
            self.data = data

    def refresh(self):
        data = download_reference()
        if getattr(self, "path", None):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            tmp.replace(self.path)
        self.data = data
        return data

    def audit(self, resources):
        result = {"checked": 0, "points_checked": 0, "issues": []}
        for key, ref in self.data["hunts"].items():
            hunt = resources.get("DatabaseHunt", {}).get(key, {})
            if hunt.get("Region") != ref["region"]:
                result["issues"].append(key + "：地图归属不同")
                continue
            result["checked"] += 1
            geometry = resources.get("SpawnPoint", {}).get(key, {})
            for point_id, point in zip(ref["point_ids"], ref["points"]):
                actual = geometry.get(f"SpawnPoint{int(point_id):02d}")
                if actual is None or len(actual) != 2 or max(abs(float(a) - b) for a, b in zip(actual, point)) > 0.0001:
                    result["issues"].append(f"{key}：点位{point_id}资料有差异")
                else:
                    result["points_checked"] += 1
        return result
