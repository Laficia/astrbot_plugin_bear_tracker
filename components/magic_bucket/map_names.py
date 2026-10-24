"""地图名规范化模块。

把群里常用的地图简称替换成标准地图名，例如::

    拉低  -> 拉诺西亚低地
    中森  -> 黑衣森林中央林区
    北萨  -> 北萨纳兰

别名映射写在插件配置的 ``map_names.aliases`` 里（旧配置 ``map_images.aliases``
也会被读取，两者合并后一起生效）。
"""

from __future__ import annotations

PLACEHOLDER = "\x00"


class MapNameNormalizer:
    """按长度降序把文本里的地图简称替换成标准地图名。

    ``prefix`` 会加在**所有**出现的标准地图名前面（包括原文里就写对的全称），
    例如 📍；``extra_names`` 是没有别名但也想统一加前缀的地图名。
    """

    def __init__(self, aliases=None, prefix: str = "", extra_names=None, suffix: str = ""):
        self.prefix = str(prefix or "")
        self.suffix = str(suffix or "")
        self.pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for alias, canonical in (aliases or {}).items():
            alias = str(alias).strip()
            canonical = str(canonical).strip()
            # 简写和标准名相同的值只是「标准名」，不需要替换。
            if not alias or not canonical or alias == canonical:
                continue
            key = (alias, canonical)
            if key in seen:
                continue
            seen.add(key)
            self.pairs.append(key)
        # 长名优先：「西拉诺西」不能被「西拉」抢先命中。
        self.pairs.sort(key=lambda item: -len(item[0]))
        # 已经写对全称时要把整段保护起来，避免简称在全称内部再套一层。
        canonicals = {canonical for _, canonical in self.pairs}
        for name in extra_names or ():
            name = str(name).strip()
            if name:
                canonicals.add(name)
        self.canonicals = sorted(canonicals, key=len, reverse=True)

    @property
    def size(self) -> int:
        return len(self.pairs)

    def find(self, text: str) -> list[str]:
        """返回文本里命中的标准地图名（去重，保持配置顺序）。"""
        if not text:
            return []
        hits: list[str] = []
        for alias, canonical in self.pairs:
            if alias in text and canonical not in hits:
                hits.append(canonical)
        return hits

    def normalize(self, text: str) -> tuple[str, list[str]]:
        """把文本里的简称换成标准名，返回 ``(新文本, 命中标准名列表)``。

        先用占位符占住替换过的位置，避免短简称把刚写入的长标准名内部又改一遍，
        也能防止同一条标准名被反复插入。
        """
        if not text or not self.pairs:
            return text, []

        working = text
        slots: dict[str, str] = {}
        hits: list[str] = []
        protected: dict[str, str] = {}

        # 1) 先把写对的全称占住：「拉凯提卡大森林」不会再被「拉凯提卡」重复套一层。
        for index, canonical in enumerate(self.canonicals):
            if canonical in working:
                slot = f"{PLACEHOLDER}K{index}{PLACEHOLDER}"
                working = working.replace(canonical, slot)
                protected[slot] = canonical

        # 2) 再把简称换成标准名。
        for index, (alias, canonical) in enumerate(self.pairs):
            if alias not in working:
                continue
            slot = f"{PLACEHOLDER}{index}{PLACEHOLDER}"
            working = working.replace(alias, slot)
            slots[slot] = canonical
            if canonical not in hits:
                hits.append(canonical)

        if not slots and not self.prefix and not self.suffix:
            return text, []

        # 统一还原：长占位符先处理，避免 index 10 被 index 1 误伤。
        # 后缀只在前缀/后缀开启时追加，且后面已有空白或到行尾时不再加。
        use_gap = bool(self.prefix or self.suffix)
        for slot in sorted(slots, key=len, reverse=True):
            working = working.replace(
                slot, self._wrap(slots[slot], self._tail(working, slot) if use_gap else False)
            )
        for slot in sorted(protected, key=len, reverse=True):
            working = working.replace(
                slot, self._wrap(protected[slot], self._tail(working, slot) if use_gap else False)
            )
        # 前缀只加一层：原文/上级步骤已经带过前缀的不再叠加。
        if self.prefix:
            doubled = self.prefix + self.prefix
            while doubled in working:
                working = working.replace(doubled, self.prefix)
        if not slots:
            return (text if working == text else working), []
        return working, hits

    def _tail(self, working: str, slot: str) -> bool:
        """slot 后面是不是「空白或行尾」。是的话就不补空格，避免双空格/尾随空格。"""
        pos = working.find(slot)
        if pos < 0:
            return False
        after = pos + len(slot)
        return after >= len(working) or working[after] in " \t　\n\r"

    def _wrap(self, name: str, skip_suffix: bool) -> str:
        piece = self.prefix + name
        if self.suffix and not skip_suffix:
            piece += self.suffix
        return piece
