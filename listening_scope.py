"""Validate selected data centers and resolve their server lists."""
from __future__ import annotations

CENTERS = ("豆豆柴", "陆行鸟", "猫小胖", "莫古力")
DEFAULT_SCOPES = [{"data_center": "豆豆柴", "servers": []}]
DEFAULT_WORLDS = ("水晶塔", "银泪湖", "太阳海岸", "伊修加德", "红茶川")


def center_worlds(resources):
    entries = resources.get("DataCenters", {})
    return {dc: list(entries.get(dc, {}).get("Names", DEFAULT_WORLDS if dc == "豆豆柴" else [])) for dc in CENTERS}


def validate_scopes(scopes, resources=None):
    if not isinstance(scopes, list) or len(scopes) > 100:
        raise ValueError("监听范围必须是列表，最多100项")
    known = center_worlds(resources) if resources is not None else None
    merged = {}
    for item in scopes:
        if not isinstance(item, dict) or item.get("data_center") not in CENTERS:
            raise ValueError("请选择豆豆柴、陆行鸟、猫小胖或莫古力")
        dc, servers = item["data_center"], item.get("servers", [])
        if not isinstance(servers, list) or len(servers) > 30 or not all(isinstance(w, str) and w.strip() == w and w for w in servers):
            raise ValueError("服务器列表格式无效")
        if known is not None and any(w not in known[dc] for w in servers):
            raise ValueError("服务器不属于所选大区：" + dc)
        servers = list(dict.fromkeys(servers))
        if dc not in merged:
            merged[dc] = servers
        elif not merged[dc] or not servers:
            merged[dc] = []
        else:
            merged[dc] = list(dict.fromkeys(merged[dc] + servers))
    return [{"data_center": dc, "servers": servers} for dc, servers in merged.items()]


def resolve_scope(resources, scopes=None):
    known = center_worlds(resources)
    selected = validate_scopes(DEFAULT_SCOPES if scopes is None else scopes,
                               resources if "DataCenters" in resources else None)
    worlds = {}
    for item in selected:
        for world in item["servers"] or known[item["data_center"]]:
            worlds[world] = item["data_center"]
    return worlds, list(dict.fromkeys(worlds.values()))
