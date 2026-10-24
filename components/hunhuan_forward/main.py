"""AstrBot entry point for HunHuan-compatible group forwarding."""

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context
from ._host import Component as Star

from .core import ForwardEngine, OneBotTransport


class HunHuanForwardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.engine = ForwardEngine(config)
        self.tasks = set()
        from .webui import ForwardWebUI

        self.webui = ForwardWebUI(self, config)

    async def on_group(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp" or not event.get_group_id():
            return
        if self.engine.closed:
            return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict) or raw.get("post_type", "message") != "message":
            return
        self_id = raw.get("self_id") or event.message_obj.self_id
        if str(event.get_sender_id()) == str(self_id):
            return
        # AstrBot strips some text segments; upstream matches the original text.
        segments = raw.get("message")
        if not isinstance(segments, list):
            logger.warning("[HunHuan] 忽略非 array 消息，请检查 NapCat 配置。")
            return
        text = "".join(
            s.get("data", {}).get("text", "")
            for s in segments
            if s.get("type") == "text"
        )
        group = int(event.get_group_id())
        scope = (event.get_platform_id(), str(self_id), str(group))
        transport = OneBotTransport(
            event.bot, group, event.message_obj.message_id, self_id
        )
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            consumed = await self.engine.process(
                transport, scope, group, text, event.message_obj.message_id
            )
            if consumed:
                event.stop_event()
        except Exception:  # noqa: BLE001 - isolate failures at the plugin event boundary
            logger.exception("[HunHuan] 处理群消息失败")
        finally:
            self.tasks.discard(task)

    async def terminate(self):
        self.engine.close()
        pending = [t for t in self.tasks if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
