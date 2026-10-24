"""Framework-independent forwarding behavior, implemented from the upstream spec."""

import asyncio
import copy
from astrbot.api import logger
import math
import re
import secrets
import time
from dataclasses import dataclass

log = logger
RECALL = re.compile(r"撤回\s*([0-9a-fA-F]{8})")


def group_ids(values):
    if not isinstance(values, list) or not values:
        raise ValueError("群号必须是非空列表")
    result = tuple(int(v) for v in values)
    if any(v <= 0 for v in result):
        raise ValueError("群号必须是正整数")
    return result


@dataclass(frozen=True)
class Rule:
    name: str
    kind: str
    sources: tuple
    targets: tuple
    words: tuple
    platform_id: str = ""
    enabled: bool = True

    @classmethod
    def parse(cls, data):
        name, kind = data["name"], data["type"]
        words = data["keywords"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("规则名不能为空")
        if kind not in ("prefix", "keyword"):
            raise ValueError("type 只支持 prefix 或 keyword")
        if not isinstance(words, list) or not words:
            raise ValueError("关键词必须是非空列表")
        if any(not isinstance(w, str) for w in words):
            raise ValueError("关键词必须为字符串")
        return cls(
            name,
            kind,
            group_ids(data["source_groups"]),
            group_ids(data["target_groups"]),
            tuple(words),
            str(data.get("platform_id", "")),
            data.get("enabled", True),
        )

    def destinations(self, group, text, platform_id=None):
        text = text.strip()
        if not self.enabled or (self.platform_id and platform_id != self.platform_id):
            return ()
        if group not in self.sources or not text:
            return ()
        hit = any(
            text.startswith(w) if self.kind == "prefix" else w in text
            for w in self.words
        )
        return tuple(g for g in self.targets if g != group) if hit else ()


@dataclass
class Batch:
    scope: tuple
    ids: list
    deadline: float
    timer: object = None
    busy: bool = False


class ForwardEngine:
    ttl = 120
    retry_delay = 0.5

    def __init__(self, config):
        self.config = config
        self.rules = []
        self.errors = []
        for index, item in enumerate(config.get("rules", [])):
            try:
                self.rules.append(Rule.parse(item))
            except (KeyError, TypeError, ValueError) as exc:
                self.errors.append(f"第 {index + 1} 条规则无效：{exc}")
        for error in self.errors:
            log.warning(error)
        interval = float(config.get("send_interval_ms", 500))
        if not math.isfinite(interval):
            raise ValueError("send_interval_ms 必须是有限数值")
        self.interval = max(0, interval) / 1000
        self.lock = asyncio.Lock()
        self.last_action = None
        self.cache = {}
        self.closed = False

    async def limited(self, action, *args, **kwargs):
        # One queue covers forwards, receipts and deletion, even across events.
        async with self.lock:
            if self.closed:
                raise RuntimeError("插件已卸载")
            if self.last_action is not None:
                await asyncio.sleep(
                    max(0, self.interval - (time.monotonic() - self.last_action))
                )
            try:
                return await action(*args, **kwargs)
            finally:
                self.last_action = time.monotonic()

    async def reply(self, transport, text):
        try:
            await self.limited(transport.reply, text)
        except Exception:
            log.exception("源群回执发送失败")

    def forget(self, token, batch):
        if self.cache.get(token) is batch:
            del self.cache[token]
        if batch.timer:
            batch.timer.cancel()

    def remember(self, scope, ids):
        while True:
            token = secrets.token_hex(4)
            if token not in self.cache:
                break
        batch = Batch(scope, list(ids), time.monotonic() + self.ttl)
        self.cache[token] = batch
        batch.timer = asyncio.get_running_loop().call_later(
            self.ttl, self.forget, token, batch
        )
        return token

    async def recall(self, transport, scope, token):
        batch = self.cache.get(token)
        if batch and time.monotonic() >= batch.deadline:
            self.forget(token, batch)
            batch = None
        if batch is None or batch.scope != scope:
            await self.reply(transport, "撤回码无效、已过期或不属于本群。")
            return
        if batch.busy:
            await self.reply(transport, "这条消息正在撤回，请勿重复操作。")
            return
        # Reserve before yielding. A batch accepted before expiry runs to completion.
        batch.busy = True
        total = len(batch.ids)
        try:
            for mid in list(batch.ids):
                try:
                    await self.limited(transport.delete, mid)
                except Exception:
                    log.exception("撤回目标消息失败：%s", mid)
                else:
                    batch.ids.remove(mid)
        finally:
            batch.busy = False
            if not batch.ids:
                self.forget(token, batch)
        failed = len(batch.ids)
        result = f"已撤回 {total - failed}/{total} 条转发消息。"
        if failed:
            result += f" {failed} 条撤回失败。"
            if time.monotonic() < batch.deadline:
                result += "可在原两分钟有效期内使用同一撤回码重试。"
        await self.reply(transport, result)

    async def process(self, transport, scope, group, text, message_id):
        if self.closed:
            return False
        command = RECALL.fullmatch(text.strip())
        if command:
            await self.recall(transport, scope, command[1].lower())
            return True
        if text.lstrip().startswith("/"):
            return False
        # Preserve repeated targets across matching rules, as in upstream.
        targets = [
            target
            for rule in self.rules
            for target in rule.destinations(group, text, scope[0])
        ]
        if not targets:
            return False
        ids, failed, missing = [], 0, 0
        for target in targets:
            for attempt in range(3):
                try:
                    result = await self.limited(transport.forward, target, message_id)
                except Exception:
                    log.exception("转发到群 %s 失败（第 %s 次）", target, attempt + 1)
                    if attempt < 2:
                        await asyncio.sleep(self.retry_delay)
                    else:
                        failed += 1
                else:
                    # A missing ID must never cause another send.
                    if result is None or str(result) == "":
                        missing += 1
                    else:
                        ids.append(str(result))
                    break
        if ids:
            token = self.remember(scope, ids)
            result = (
                f"已转发 {len(ids)} 条消息。\n2 分钟内在本群发送「撤回{token}」可撤回。"
            )
        else:
            result = "本次没有可缓存的转发消息 ID。"
        if failed:
            result += f"\n{failed} 条转发失败。"
        if missing:
            result += f"\n{missing} 次转发未返回消息 ID，无法通过撤回码撤回。"
        await self.reply(transport, result)
        return False

    def close(self):
        self.closed = True
        for token, batch in list(self.cache.items()):
            self.forget(token, batch)


class OneBotTransport:
    """Keep raw OneBot segments instead of AstrBot's normalized text/media chain."""

    def __init__(self, bot, group, message_id, self_id=None):
        self.bot, self.group, self.message_id = bot, int(group), message_id
        self.route = {"self_id": int(self_id)} if self_id else {}

    async def call(self, action, **params):
        result = await self.bot.api.call_action(action, **params, **self.route)
        # aiocqhttp normally unwraps data. Also accept an intact OneBot response.
        if isinstance(result, dict) and ("retcode" in result or "status" in result):
            if result.get("retcode", 0) != 0 or result.get("status", "ok") != "ok":
                raise RuntimeError(f"OneBot {action} failed: {result.get('retcode')}")
            return result.get("data")
        return result

    async def forward(self, group, message_id):
        original = await self.call("get_msg", message_id=int(message_id))
        if not original:
            return None
        segments = original.get("message", [])
        if not isinstance(segments, list):
            raise TypeError("NapCat 的消息格式须为 array")
        result = await self.call(
            "send_group_msg", group_id=int(group), message=copy.deepcopy(segments)
        )
        return result.get("message_id") if isinstance(result, dict) else None

    async def reply(self, text):
        return await self.call(
            "send_group_msg",
            group_id=self.group,
            message=[
                {"type": "reply", "data": {"id": str(self.message_id)}},
                {"type": "text", "data": {"text": text}},
            ],
        )

    async def delete(self, message_id):
        return await self.call("delete_msg", message_id=int(message_id))
