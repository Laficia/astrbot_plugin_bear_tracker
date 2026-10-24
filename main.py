import asyncio
import json
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .alert_engine import AlertEngine
from .service import BearClient, format_result
from .webui import BearWebUI
from .trigger_monitor import TriggerMonitor
from .component_host import MergedComponents
from .train_schedule import TrainFeature
from .train_webui import TrainWebUI
from .train_hunt import HuntTrainFeature
from .quiet_hours import delivery_allowed


@register("astrbot_plugin_bear_tracker", "Laficia", "莫古环的魔法水桶：FF14狩猎播报、车表与群消息管理", "2.9.0")
class BearTrackerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.client = BearClient()
        self.config = config if config is not None else {}
        self.alerts = AlertEngine(context, self.client, self.config,
                                  StarTools.get_data_dir("astrbot_plugin_bear_tracker"))
        self.client.reference.load_cache(self.alerts.data_dir / "reference_catalog.json")
        self.triggers = TriggerMonitor(self.alerts)
        self.trains = TrainFeature(self)
        self.train_hunt = HuntTrainFeature(self)
        self.train_webui = TrainWebUI(self)
        self.webui = BearWebUI(self)
        self.components = MergedComponents(self)

    async def initialize(self):
        self.alerts.start()
        self.triggers.start()
        self.train_hunt.start()

    async def terminate(self):
        await self.train_hunt.close()
        try:
            try:
                await self.trains.close()
            finally:
                await self.components.close()
        finally:
            await self.triggers.close()
            await self.alerts.close()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def merged_group_message(self, event: AstrMessageEvent):
        await self.components.on_group(event)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=20000)
    async def train_commands(self, event: AstrMessageEvent):
        await self.trains.on_event(event)

    @staticmethod
    def group_origin(event):
        # QQ may isolate a session by sender. Subscribe the group once, using
        # the platform instance ID, while preserving other platforms' topics.
        if event.get_platform_name() in ("aiocqhttp", "qq_official"):
            return f"{event.get_platform_id()}:GroupMessage:{event.get_group_id()}"
        return event.unified_msg_origin

    @filter.command("S播报")
    async def alert_command(self, event: AstrMessageEvent, action: str = "状态"):
        """/S播报 开启、关闭、状态；开启/关闭需要 AstrBot 管理员。"""
        if not delivery_allowed(self.config):event.stop_event();return
        if not event.get_group_id():
            yield event.plain_result("请在需要接收通知的群内使用：/S播报 开启。需要 AstrBot 管理员权限。")
            return
        umo = self.group_origin(event)
        if action in ("状态", "status", ""):
            yield event.plain_result(self.alerts.status(umo))
            return
        if not event.is_admin():
            yield event.plain_result("开启或关闭播报需要 AstrBot 管理员权限；可使用 /S播报 状态。")
            return
        if action in ("开启", "订阅", "on"):
            self.alerts.subscribe(umo, str(event.get_group_id()))
            suffix = "\n插件总开关当前关闭，请同时在插件配置中开启 enabled。" if not self.config.get("enabled", True) else ""
            yield event.plain_result("本群已订阅 S 怪实时播报，监听范围可在控制台设置。\n新出现时发送服务器、地图、怪名、坐标和标点地图。\n开始监听前已有的报告不会补发。" + suffix)
        elif action in ("关闭", "退订", "off"):
            if umo in self.config.get("target_sessions", []):
                yield event.plain_result("本群由插件配置 target_sessions 指定。请在配置中删除该会话以停止播报。")
                return
            self.alerts.unsubscribe(umo)
            yield event.plain_result("本群已关闭 S 怪播报。")
        else:
            yield event.plain_result("用法：/S播报 开启｜/S播报 关闭｜/S播报 状态")

    async def run(self, method, *args):
        try:
            return json.dumps(await asyncio.to_thread(method, *args), ensure_ascii=False)
        except Exception as exc:
            logger.warning("[BearTracker] 查询失败: %s", exc)
            return "熊工具箱查询失败，当前无法确认实时数据，请稍后重试。不要编造计时结果。错误类型：" + type(exc).__name__

    @filter.regex(r"^/?熊(?:资料|计时)(?:\s|$)|怎么触发|如何触发|触发条件|刷新条件|怎么刷|计时|刷新时间|什么时候刷|多久刷|何时刷新", priority=10000)
    async def direct_query(self, event: AstrMessageEvent):
        text = event.get_message_str().strip()
        command = bool(re.match(r"^/?熊(?:资料|计时)(?:\s|$)", text))
        addressed = event.is_at_or_wake_command or text.startswith("莫古环") or event.is_private_chat()
        if not command and not addressed:
            return
        if not command and not self.client.natural_target(text):
            return
        if not delivery_allowed(self.config):event.stop_event();return
        try:
            result = await asyncio.to_thread(self.client.direct_answer, text)
        except Exception as exc:
            logger.warning("[BearTracker] 直接查询失败: %s", exc)
            result = "熊工具箱暂时查询失败，请稍后重试。错误类型：" + type(exc).__name__
        if result:
            if not delivery_allowed(self.config):event.stop_event();return
            logger.info("[BearTracker] 直接查询已处理，无需LLM: %s", "命令" if command else "狩猎问答")
            await event.send(event.plain_result(result))
            event.stop_event()

    @filter.llm_tool(name="query_bear_tracker_timers")
    async def timers(self, event: AstrMessageEvent, server: str, target: str = "", category: str = "S", limit: int = 12) -> str:
        """查询熊工具箱 Bear Tracker 网站当前的FF14狩猎/FATE计时、击杀记录和刷新窗口。用户问某服某怪何时刷新、S/A计时、熊工具箱数据时必须调用此工具，不能凭记忆回答。未提供服务器需先询问；FATE需指定category=FATE。窗口不表示已经出现。

        Args:
            server(string): 完整服务器名或大区名，例如水晶塔、白银乡、陆行鸟。不要猜测用户服务器。
            target(string): 目标中文名、英文名或地图名称；空字符串列出该服目标。
            category(string): S、A或FATE，默认S。
            limit(int): 最多显示数量，1到25，默认12。
        """
        if not delivery_allowed(self.config):event.stop_event();return ''
        result=await self.run(self.client.query, server, target, category, limit)
        if not delivery_allowed(self.config):event.stop_event();return ''
        return result

    @filter.llm_tool(name="search_bear_tracker_knowledge")
    async def knowledge(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """查阅熊工具箱网站资料：FF14狩猎/FATE的中英文名、地图、触发条件、刷新规则、服务器列表与网站使用说明。查询当前计时必须改用query_bear_tracker_timers。

        Args:
            keyword(string): 目标名称或地图，或“服务器”“使用说明”；空字符串返回网站说明。
        """
        if not delivery_allowed(self.config):event.stop_event();return ''
        result=await self.run(self.client.knowledge, keyword)
        if not delivery_allowed(self.config):event.stop_event();return ''
        return result

    @filter.command("熊计时")
    async def timer_command(self, event: AstrMessageEvent, server: str = "", target: str = "", category: str = "S"):
        """用法：/熊计时 水晶塔 雷德罗巨蛇 S；/熊计时 水晶塔 奥丁 FATE"""
        if not delivery_allowed(self.config):event.stop_event();return
        if not server:
            yield event.plain_result("用法：/熊计时 服务器名 目标名称 S或A或FATE\n例：/熊计时 水晶塔 雷德罗巨蛇 S\n也可直接问莫古环：查一下水晶塔雷德罗巨蛇的刷新时间。")
            return
        result = await self.run(self.client.query, server, target, category, 12)
        if not delivery_allowed(self.config):event.stop_event();return
        yield event.plain_result(format_result(json.loads(result)) if result.startswith("{") else result)

    @filter.command("熊资料")
    async def knowledge_command(self, event: AstrMessageEvent, keyword: str = ""):
        """用法：/熊资料 雷德罗巨蛇"""
        if not delivery_allowed(self.config):event.stop_event();return
        result = await self.run(self.client.knowledge, keyword)
        if not delivery_allowed(self.config):event.stop_event();return
        yield event.plain_result(format_result(json.loads(result)) if result.startswith("{") else result)
