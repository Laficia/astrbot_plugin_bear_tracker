"""关键词自动加括号。

群消息里有些词希望转发后能被一眼看出来，例如::

    水晶塔  -> 【水晶塔】（服务器/频道名）
    咕尔呱洛斯 -> [咕尔呱洛斯]（S 怪名）

规则写在插件配置的 ``decorations.groups`` 里::

    decorations:
      enabled: true
      groups:
        - braces: "【】"
          terms: [水晶塔, 红茶川]
        - braces: "[]"
          terms: [咕尔呱洛斯, 南迪]

已经带了相同括号的词不会再套一层；处在别的成对括号里的词（例如
``[狗-水晶塔]``）也不会被强行插入，避免出现 ``[狗-【水晶塔】]`` 这种怪东西。

每条配置组还可以带 ``aliases``，先还原简称再加括号::

    - braces: "【】"
      aliases: {水塔: 水晶塔, 茶: 红茶川}
      terms: [水晶塔, 红茶川]

``protect`` 里的内容（例如所有地图名）不参与任何替换，避免把
``基拉巴尼亚湖区`` 改成 ``基拉巴尼亚伊修加德区`` —— 单字简称最容易踩这个坑。
"""

from __future__ import annotations

import re

PLACEHOLDER = "\x00"

# 已经处在成对括号内部的内容不再处理。这里只需要覆盖常见成对符号。
_BRACKET_PAIRS = (("【", "】"), ("[", "]"), ("（", "）"), ("(", ")"), ("「", "」"))
_OPEN_TO_CLOSE = dict(_BRACKET_PAIRS)
_CLOSERS = frozenset(close for _, close in _BRACKET_PAIRS)


def _inside_spans(text: str) -> set[int]:
    """返回成对括号「内部」的字符下标集合。

    未闭合的开括号不算，避免出现半个括号就把后面整段吃掉的情况。
    """
    inside: set[int] = set()
    stack: list[tuple[int, str]] = []
    for index, char in enumerate(text):
        close = _OPEN_TO_CLOSE.get(char)
        if close is not None:
            stack.append((index, close))
            continue
        if char in _CLOSERS and stack:
            open_index, expected = stack.pop()
            if expected == char:
                inside.update(range(open_index + 1, index))
            # 括号类型不匹配时把这层丢掉，按没有括号处理。
    return inside


def _term_spans(text: str, terms) -> set[int]:
    """返回所有保护词覆盖到的字符下标，长词优先。"""
    ordered = sorted((term for term in terms if term), key=len, reverse=True)
    if not ordered:
        return set()
    pattern = re.compile("|".join(re.escape(term) for term in ordered))
    spans: set[int] = set()
    for match in pattern.finditer(text):
        spans.update(range(match.start(), match.end()))
    return spans


class TermDecorator:
    """按配置给命中的词条套上指定括号，重复套用不会叠加。"""

    def __init__(self, groups=None, protected=None):
        self.size = 0
        self._protected = list(protected or [])
        self._rules: list[tuple[str, str, re.Pattern, dict[str, str]]] = []
        for group in groups or []:
            if not isinstance(group, dict):
                continue
            braces = str(group.get("braces") or "").strip()
            if len(braces) < 2:
                continue
            # braces 允许带前缀图标：最后一位是闭括号，其余都是开前缀。
            # "【】" -> (【, 】)；"👾「」" -> (👾「, 」)
            open_char, close_char = braces[:-1], braces[-1]
            aliases = {
                str(alias).strip(): str(term).strip()
                for alias, term in (group.get("aliases") or {}).items()
                if str(alias).strip() and str(term).strip()
            }
            terms: list[str] = []
            for item in group.get("terms") or []:
                term = str(item).strip()
                if term and term not in terms:
                    terms.append(term)
            if not terms:
                continue

            # 词条本身也算一种「写法」，和 aliases 一起建立 needle -> 标准名 的映射。
            needles: dict[str, str] = {term: term for term in terms}
            for alias, term in aliases.items():
                if term in terms and alias != term:
                    needles[alias] = term
            # 长写法优先：「水晶塔」不能被「水」抢走。
            ordered_needles = sorted(needles, key=len, reverse=True)
            pattern = re.compile(
                "(?:" + re.escape(open_char)
                + ")?("
                + "|".join(re.escape(needle) for needle in ordered_needles)
                + ")"
                + "(?:" + re.escape(close_char)
                + ")?"
            )
            self._rules.append((open_char, close_char, pattern, needles))
            self.size += len(needles)

    def decorate(self, text: str) -> tuple[str, list[str]]:
        """给文本里的词条加括号，返回 ``(新文本, 命中词条列表)``。"""
        if not text or not self._rules:
            return text, []

        working = text
        slots: dict[str, str] = {}
        hits: list[str] = []

        for rule_index, (open_char, close_char, pattern, needles) in enumerate(self._rules):
            # 括号里的内容和保护名单里的词都不许动。
            blocked = _inside_spans(working) | _term_spans(working, self._protected)
            matches = [
                match
                for match in pattern.finditer(working)
                if not set(range(match.start(), match.end())) & blocked
            ]
            # 从后往前替换，前面的下标才不会失效。
            for offset, match in enumerate(reversed(matches)):
                needle = match.group(1)
                term = needles.get(needle, needle)
                slot = f"{PLACEHOLDER}D{rule_index}P{offset}{PLACEHOLDER}"
                piece = f"{open_char}{term}{close_char}"
                # 「地图名S怪名」写法里，S 与怪名之间补一个空格
                # （normalize 给地图名加了后缀空格时这里自然不会重复加）。
                if match.start() > 0 and working[match.start() - 1] in "Ss":
                    piece = " " + piece
                slots[slot] = piece
                if term not in hits:
                    hits.append(term)
                working = working[: match.start()] + slot + working[match.end() :]

        if not slots:
            return text, []

        # 长占位符先还原，避免 P1 抢在 P12 前面把后者拆坏。
        for slot in sorted(slots, key=len, reverse=True):
            working = working.replace(slot, slots[slot])
        return working, hits
