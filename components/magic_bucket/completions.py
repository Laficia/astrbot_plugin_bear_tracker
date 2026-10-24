"""S 怪名与地图名互相补全。

群里经常只说一半::

    湖多智兽车头领              -> 只有 S 怪名，没有地图名
    注：太阳海岸 遗产之地S      -> 只有地图名，没有 S 怪名

前者在看消息的人眼里少了一个关键信息（去哪打），后者少了要打谁。
``MonsterMapCompleter`` 按一张「S 怪名 -> 地图名」的对照表把缺的那一半
**直接写进正文**，而不是在末尾追加一行说明::

    安穆艾兰[多智兽]车头领
    注：太阳海岸 遗产之地S[先驱勇士 阿提卡斯]

插入位置遵循群里「地图名 + 怪名」的写法：缺地图名时把地图名插到怪名前面
（插在怪名自己的括号外面），缺怪名时把怪名插到「地图名 + S」后面
（群里写的是「湖区S盐和光」）。

对照表写在插件配置的 ``completions.pairs`` 里，反向关系由代码自动生成。

为什么补 **地图名** 不做额外限制: 出现 S 怪名本身就是很强的信号，补地图几乎
不会误伤。真要收紧，可以给插件传入 ``known_maps``（所有已知地图名），
只要正文里已经出现了别的地图名，就不再往里塞地图名。

为什么补 **S 怪名** 要求「地图名 + 单 S」: 群里聊地图的场景太多了（车序、
早车资讯、SS 前置），只要提到地图就补 S 怪名会把这些消息刷得一团糟。加了这条
限制后 ``剩余车序：xxx`` 这类不会被打扰，``遗产之地S`` 才会补。实在想放开就把
``require_s_hint`` 设为 false。
"""

from __future__ import annotations

import re

# 「地图名 + 单 S」里的那个 S；双 S（SS 前置）不算。
_S_HINT = re.compile(r"\s*[Ss](?![SsA-Za-z])")


def _compile(names) -> re.Pattern | None:
    """长名优先的匹配表：「得到宽恕的炫学」不能被短写法抢走。"""
    ordered = sorted((name for name in names if name), key=len, reverse=True)
    if not ordered:
        return None
    return re.compile("|".join(re.escape(name) for name in ordered))


class MonsterMapCompleter:
    """按对照表补齐缺失的地图名 / S 怪名。"""

    def __init__(
        self,
        pairs=None,
        require_s_hint: bool = True,
        monster_braces: str = "[]",
        aliases=None,
        known_maps=None,
        map_prefix: str = "",
        map_suffix: str = "",
    ):
        self.pairs: dict[str, str] = {}
        for monster, map_name in (pairs or {}).items():
            monster = str(monster).strip()
            map_name = str(map_name).strip()
            if monster and map_name:
                self.pairs[monster] = map_name

        # 群里的简称写法（例如「厌忌之人」->「厌忌之人 奇里格」）也要认得，
        # 但不进反向表，免得一张地图补出两个怪名。
        needles: dict[str, str] = {name: name for name in self.pairs}
        for alias, canonical in (aliases or {}).items():
            alias = str(alias).strip()
            canonical = str(canonical).strip()
            if alias and canonical in self.pairs:
                needles[alias] = canonical
        self.needles = needles

        self.require_s_hint = bool(require_s_hint)
        braces = str(monster_braces or "[]")
        # 允许带前缀图标：最后一位是闭括号，其余都是开前缀（"👾「」" -> open=👾「, close=」）。
        self.open_char = braces[:-1] if len(braces) >= 2 else "["
        self.close_char = braces[-1] if len(braces) >= 2 else "]"
        self.map_prefix = str(map_prefix or "")
        self.map_suffix = str(map_suffix or "")
        self.size = len(self.pairs)

        self._monster_re = _compile(self.needles.keys())
        self._map_re = _compile(set(self.pairs.values()))
        # 「地图名 + 单 S」才是 S 怪语境；双 S 是 SS 前置，不补 S 怪名。
        self._s_hint_re = None
        map_names = set(self.pairs.values())
        if map_names:
            ordered = sorted(map_names, key=len, reverse=True)
            self._s_hint_re = re.compile(
                "(" + "|".join(re.escape(name) for name in ordered) + r")\s*[Ss](?![SsA-Za-z])"
            )

        # 正文里出现过的「所有已知地图名」，用来判断是不是已经提过地图。
        self._known_map_re = _compile(set(known_maps or []) | map_names)

        monsters_by_map: dict[str, list[str]] = {}
        for monster, map_name in self.pairs.items():
            monsters_by_map.setdefault(map_name, []).append(monster)
        self.monsters_by_map = monsters_by_map

    def wrap(self, monster: str) -> str:
        return f"{self.open_char}{monster}{self.close_char}"

    def _map_piece(self, text: str, pos: int, map_name: str) -> str:
        """补进正文的地图名：带前缀，后面留一个空格（后方已有空白/到结尾则不加）。"""
        piece = self.map_prefix + map_name
        if self.map_suffix and pos < len(text) and text[pos] not in " \t　\n\r":
            piece += self.map_suffix
        return piece

    @staticmethod
    def _find_spans(text: str, pattern: re.Pattern | None) -> list[tuple[int, int, str]]:
        """按顺序找出文本里出现过的词，返回 ``(起点, 终点, 词)``。

        重叠部分只算一次，长词优先。
        """
        if pattern is None:
            return []
        found: list[tuple[int, int, str]] = []
        covered = -1
        for match in pattern.finditer(text):
            if match.start() <= covered:
                continue
            covered = match.end() - 1
            found.append((match.start(), match.end(), match.group(0)))
        return found

    def is_s_rank_context(self, text: str) -> bool:
        """文本里有没有「地图名 + 单 S」这种明确的 S 怪语境。"""
        return self._s_hint_re is not None and self._s_hint_re.search(text) is not None

    def has_known_map(self, text: str) -> bool:
        """正文里是不是已经出现过某个地图名。"""
        return self._known_map_re is not None and self._known_map_re.search(text) is not None

    @staticmethod
    def _insert_pos(text: str, end: int) -> int:
        """地图名后面若紧跟一个单 S，怪名插在这个 S 之后。

        群里写的是「湖区S盐和光」而不是「湖区盐和光S」。
        """
        match = _S_HINT.match(text, end)
        return match.end() if match else end

    @staticmethod
    def _outer_bracket_start(text: str, pos: int) -> int:
        """找到包住 ``pos`` 的最外层开括号位置，没有就返回 ``pos``。

        免得把地图名插进 ``[狗-多智兽]`` 这种括号里。
        """
        stack: list[int] = []
        for index in range(pos):
            char = text[index]
            if char in "【[（(「":
                stack.append(index)
            elif char in "】])）」" and stack:
                stack.pop()
        return stack[-1] if stack else pos

    def complete(self, text: str) -> tuple[str, list[str]]:
        """补齐缺失的一半，直接写进正文，返回 ``(新文本, 补充说明列表)``。"""
        if not text or self._monster_re is None or not self.pairs:
            return text, []

        monster_spans = self._find_spans(text, self._monster_re)
        map_spans = self._find_spans(text, self._map_re)
        notes: list[str] = []
        edits: list[tuple[int, str]] = []

        if monster_spans and not map_spans:
            # 提到了 S 怪却没说地图：把地图名插到怪名前面。
            # 正文里如果已经出现了别的地图名，就不插（免得拼出两个地图）。
            if self.has_known_map(text):
                return text, []
            seen: set[str] = set()
            for start, _end, matched in monster_spans:
                canonical = self.needles.get(matched)
                if not canonical or canonical in seen:
                    continue
                seen.add(canonical)
                map_name = self.pairs.get(canonical)
                if not map_name:
                    continue
                # 别插进用户自己写的括号里（👾「 是两段，一起跳过）。
                pos = start
                if pos > 0 and text[pos - 1] == "「" and pos >= 2 and text[pos - 2] == "👾":
                    pos -= 2
                elif pos > 0 and text[pos - 1] in "【[（(「":
                    pos -= 1
                edits.append((pos, self._map_piece(text, pos, map_name)))
                notes.append(f"{self.wrap(matched)} → {map_name}")

        elif map_spans and not monster_spans:
            # 提到了地图却没说 S 怪：只在明确的 S 怪语境里补，免得车序被刷屏。
            if self.require_s_hint and not self.is_s_rank_context(text):
                return text, []
            seen = set()
            for _start, end, matched in map_spans:
                if matched in seen:
                    continue
                seen.add(matched)
                related = self.monsters_by_map.get(matched) or []
                if not related:
                    continue
                pos = self._insert_pos(text, end)
                # 严格模式下，只有「地图名 + 单 S」那个地图才补；
                # 同一句里顺带提到的别的地图不补，免得凭空多出怪名。
                if self.require_s_hint and pos == end:
                    continue
                # 补出来的怪名不会被「加括号」那一步处理到（它跑在前面），
                # 所以这里自己套上括号，格式跟群里手写的一样。
                # 插入点紧跟 S 时补一个空格（S 👾「怪名」）。
                joined = "、".join(self.wrap(name) for name in related)
                if pos > 0 and text[pos - 1] not in " \t　\n\r":
                    joined = " " + joined
                edits.append((pos, joined))
                notes.append(f"{matched} → {joined.lstrip()}")

        if not edits:
            return text, []

        # 从后往前插入，前面位置的下标才不会失效。
        working = text
        for pos, snippet in sorted(edits, key=lambda item: -item[0]):
            working = working[:pos] + snippet + working[pos:]
        return working, notes
