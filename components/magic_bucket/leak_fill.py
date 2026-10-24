"""扩散群 S 怪播报查漏补发。

思路：同一条 S 怪播报应该出现在所有扩散群。监听这些群的消息，
一条疑似 S 怪播报出现后等 window_seconds 秒，再检查其余扩散群
是否出现过**完全相同**的文本；没有就把这条文本补发到缺的群。

bot 自己转发/补发成功的消息也记录进索引（由 plugin 在发送成功处调用
record），所以 bot 转发到扩散群时个别群失败，也能在转发完成后的
主动巡检（sweep）里被发现并补发。
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict


class LeakFiller:
    def __init__(
        self,
        groups,
        window_seconds: float = 10,
        backlook_seconds: float = 60,
        prefixes=(),
    ):
        self.groups = [int(g) for g in groups]
        self.window = max(0.0, float(window_seconds))
        self.backlook = max(0.0, float(backlook_seconds))
        # 只有以这些前缀开头的消息才算播报（如 [Sonar]、[银山雀儿]）。
        self.prefixes = tuple(str(p) for p in prefixes if str(p))
        # 补发成功后把详情通知到这个群；0 表示不发。
        self.notify_group = 0
        # 文本(strip 后) -> {群号: 最近出现时间(monotonic)}
        self.seen: dict[str, dict[int, float]] = defaultdict(dict)
        # 正在等待窗口的文本，避免同一文本触发多个检查任务。
        self.pending: set[str] = set()
        # 文本(strip 后) -> 该播报携带的原图（本地缓存路径/url）；补发时一并带过去。
        self.images: dict[str, list[str]] = {}

    # ---- 判定 ----

    def matches(self, text: str) -> bool:
        """是否是值得查漏的播报消息。"""
        if not self.prefixes:
            return False
        return (text or "").strip().startswith(self.prefixes)

    # ---- 记录 ----

    def record(self, text: str, group, images=None) -> None:
        key = (text or "").strip()
        if not key:
            return
        if images:
            self.images[key] = list(images)
        self.seen[key][int(group)] = time.monotonic()
        self._prune()

    def set_images(self, text: str, images) -> None:
        """记录某条播报携带的原图；补发时一并带过去。"""
        key = (text or "").strip()
        if key and images:
            self.images[key] = list(images)

    def images_for(self, text: str) -> list[str]:
        """取某条播报之前记录的原图；没有返回空列表。"""
        return list(self.images.get((text or "").strip(), []))

    def _prune(self) -> None:
        keep = max(self.window + self.backlook, 600.0)
        cutoff = time.monotonic() - keep
        for key in list(self.seen):
            groups = self.seen[key]
            for group in list(groups):
                if groups[group] < cutoff:
                    del groups[group]
            if not groups:
                del self.seen[key]
                self.images.pop(key, None)

    # ---- 查漏 ----

    def missing_groups(self, text: str, exclude=()) -> list[int]:
        """哪些扩散群最近没出现过这段文本。

        「最近」= window + backlook 秒之内；更早的记录视为过期，
        同一文本再次出现（如二次刷新）时仍会触发查漏。
        """
        key = (text or "").strip()
        if not key:
            return []
        now = time.monotonic()
        fresh = now - (self.window + self.backlook)
        exclude = {int(g) for g in exclude}
        missing = []
        for group in self.groups:
            if group in exclude:
                continue
            seen_at = self.seen.get(key, {}).get(group)
            if seen_at is None or seen_at < fresh:
                missing.append(group)
        return missing

    async def observe(self, text: str, origin_group, images=None) -> list[int]:
        """扩散群出现一条消息：记录后等待窗口，返回仍缺这条文本的群。

        同一文本在窗口内从别的群再次出现时不再起检查任务，
        统一由第一个任务负责检查全部扩散群。``images`` 为原消息携带的图片，
        补发时一并带过去。
        """
        key = (text or "").strip()
        if not key:
            return []
        origin_group = int(origin_group)
        self.record(key, origin_group, images)
        if self.window > 0:
            if key in self.pending:
                return []
            self.pending.add(key)
            try:
                await asyncio.sleep(self.window)
            finally:
                self.pending.discard(key)
        return self.missing_groups(key, exclude={origin_group})
