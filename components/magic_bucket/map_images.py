"""地图图片匹配模块

扫描图片目录，按文件名建立「地图名 -> 图片路径」索引；
再从消息文本中识别地图名，返回对应图片。

图片文件名形如 ``100S 亚克特尔树海.png``，前缀的等级部分会被去掉，
剩下的 ``亚克特尔树海`` 作为标准地图名。
"""

from __future__ import annotations

import re
from pathlib import Path

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
LEVEL_PREFIX = re.compile(r"^\s*\d+\s*[Ss]\s+")


class MapImageIndex:
    """地图名索引：支持别名，长名优先匹配。"""

    def __init__(self, directory, aliases=None):
        self.directory = Path(directory)
        self.aliases = dict(aliases or {})
        self.index: dict[str, Path] = {}
        self.candidates: list[tuple[str, str]] = []
        self.refresh()

    def refresh(self) -> int:
        """重新扫描目录，返回索引到的地图数量。"""
        index: dict[str, Path] = {}
        if self.directory.is_dir():
            for path in sorted(self.directory.iterdir()):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                name = LEVEL_PREFIX.sub("", path.stem).strip()
                if name:
                    index.setdefault(name, path)

        self.index = index

        # 别名可能比标准名长（少见），统一按长度降序，避免短名抢先命中。
        candidates: list[tuple[str, str]] = [(name, name) for name in index]
        for alias, canonical in self.aliases.items():
            alias = str(alias).strip()
            canonical = str(canonical).strip()
            if alias and canonical in index:
                candidates.append((alias, canonical))
        candidates.sort(key=lambda item: -len(item[0]))
        self.candidates = candidates
        return len(index)

    @property
    def available(self) -> bool:
        return bool(self.index)

    def match(self, text: str):
        """在文本中找地图名，返回 ``(标准地图名, 图片路径)``，找不到返回 ``(None, None)``。"""
        if not text or not self.candidates:
            return None, None
        for needle, canonical in self.candidates:
            if needle and needle in text:
                return canonical, self.index[canonical]
        return None, None
