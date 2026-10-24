import asyncio
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger
from typing import Any
GroupMessageEvent = Any

from .runtime_paths import data_directory
from .completions import MonsterMapCompleter
from .decorations import TermDecorator
from .leak_fill import LeakFiller
from .mango_watch import HAS_PIL, MangoWatch, parse_coords
from .map_images import MapImageIndex
from .map_names import MapNameNormalizer
from .rules import ForwardRuleManager
from .spawn_watch import SpawnWatcher
from .hunt_alerts import HuntAlerts


@dataclass
class RecallEntry:
    source_group: str
    message_ids: list[str]
    expires_at: float
    timer: asyncio.TimerHandle | None = None
    recalling: bool = False
    # 允许发「撤回xxxx」的群集合；为 None 时退化为仅 source_group。
    recall_groups: set[str] | None = None


class MissingForwardMessageID(RuntimeError):
    """发送可能已经成功，不能因为缺少回执 ID 而再次发送。"""


class ForwardBotPlugin:
    def __init__(self, config, api):
        self.config, self.api = config, api
        self.logger = logger
    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def init_defaults(self, values):
        for key, value in values.items():
            self.config.setdefault(key, value)

    name = "ForwardBotPlugin"
    version = "0.11.7"
    author = "KirisameVanilla"
    recall_ttl = 120
    recall_command = re.compile(r"撤回\s*([0-9a-fA-F]{8})")
    manager: ForwardRuleManager

    async def on_load(self) -> None:
        self.init_defaults(
            {
                "send_interval_ms": 500,
                "rules": [],
                "map_images": {},
                "map_names": {},
                "decorations": {},
                "completions": {},
                "leak_fill": {},
                "spawn_watch": {},
                "mango_watch": {},
            }
        )
        self.manager = ForwardRuleManager(self.config)
        self._action_lock = asyncio.Lock()
        self._last_action_time = None
        self._recall_cache: dict[str, RecallEntry] = {}
        self._leak_tasks: set = set()
        # 行首的「注：」在转发时换成图标，例如 🪧；留空则关闭。
        note_emoji = str(self._config_section("map_names").get("note_emoji", "🪧") or "")
        self._note_pattern = re.compile(r"(?m)^[ \t　]*注\s*[:：][ \t　]*") if note_emoji else None
        self._note_emoji = note_emoji
        # 文本与地图图合并成一条消息发送；关闭时退回「转发 + 单独图片」。
        # 注意: get_config 只认一级键，子键要从取到的字典里再取。
        images_cfg = self._config_section("map_images")
        self._combine_map_image = bool(images_cfg.get("combine", True))
        map_aliases = self._collect_map_aliases()
        self._map_images = self._build_map_image_index(map_aliases)
        self._map_names = self._build_map_name_normalizer(map_aliases)
        self._decorator = self._build_decorator()
        self._completer = self._build_completer()
        # 「地图名 + 单 S」说明这条是 S 怪而不是 SS 前置，不该配 SS 点位图。
        self._s_rank_pattern = self._build_s_rank_pattern()
        # 更严格的兜底：只有文本里出现 SS（双 S）才配图。
        self._ss_context_pattern = self._build_ss_context_pattern()
        # 扩散群之间互相查漏补发。
        self._leak = self._build_leak_filler()
        # 踩芒果喊话：源群喊「<服> 天外天垓 需要小异亚」→ 广播高亮绿点图到目标群。
        self._mango = self._build_mango_watch()
        # S 怪触发上报采集：Sonar 播报 → 看板「已触发」数据文件。
        self._spawn = self._build_spawn_watcher()
        import json
        catalog = json.loads(Path(__file__).with_name('hunt_alert_catalog.json').read_text(encoding='utf-8'))
        alerts = self._config_section("hunt_alerts")
        self._hunt_alerts = None
        self._hunt_alert_task = None
        if alerts.get("enabled", False):
            self._hunt_alerts = HuntAlerts(catalog, map_aliases,
                root=alerts['root'], state=alerts['state'], output_dir=alerts.get('output_dir'))
            self._hunt_alerts.source_group = str(alerts.get("source_group", "1034151994"))
            self._hunt_alert_task = asyncio.create_task(self._hunt_alert_loop())

    async def _hunt_alert_loop(self):
        await asyncio.sleep(30)
        async def send(text):
            return await self._send_segments(int(self._config_section("hunt_alerts").get("notify_group", 126747198)), [{"type": "text", "data": {"text": text}}], "hunt_overdue", "S怪超时提醒", 0)
        while True:
            try:
                await self._hunt_alerts.tick(send)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("SS/超时提醒检查失败，本轮跳过")
            await asyncio.sleep(30)

    def _build_ss_context_pattern(self) -> re.Pattern | None:
        """SS 前置语境的判定，全角/半角、中间夹空格都算。

        群里的 SS 前置消息一律写成「地图名SS前置」，所以「文本里有双 S」可以当成
        「这条要 SS 点位图」的充要条件；反过来，S 怪播报、车序、报点都不配图。
        配置项 ``map_images.require_ss_context`` 为 false 时返回 None（不启用）。
        """
        if not self._config_section("map_images").get("require_ss_context", True):
            return None
        self.logger.info("地图图限定为 SS 前置专用：文本里没有 SS 就不附图")
        return re.compile(r"[SsＳｓ]\s*[SsＳｓ]")

    def _build_leak_filler(self) -> LeakFiller | None:
        """扩散群之间互相查漏：同一条播报缺了哪个群就补哪个群。"""
        config = self._config_section("leak_fill")
        if not config.get("enabled", False):
            return None
        groups = [int(g) for g in (config.get("groups") or [])]
        if not groups:
            self.logger.warning("leak_fill.enabled 为真但没有配置扩散群，已跳过")
            return None

        # 只关注以这些前缀开头的播报（Sonar 及同类机器人的播报）。
        prefixes = [str(p) for p in (config.get("prefixes") or ["[Sonar", "[银山雀儿"])]
        if not prefixes:
            self.logger.warning("leak_fill.enabled 为真但没有配置播报前缀，已跳过")
            return None

        filler = LeakFiller(
            groups,
            window_seconds=float(config.get("window_seconds", 10)),
            backlook_seconds=float(config.get("backlook_seconds", 60)),
            prefixes=prefixes,
        )
        # 补发成功后通知到哪个群；0/留空 = 不通知。
        filler.notify_group = int(config.get("notify_group") or 0)
        # 补发时是否把原播报里别人发的图片一并带过去。
        self._leak_attach_image = bool(config.get("attach_original_image", True))
        self.logger.info(
            "扩散群查漏补发已启用: %d 个群, 窗口 %.0fs 回看 %.0fs, 前缀 %s",
            len(groups),
            filler.window,
            filler.backlook,
            "、".join(prefixes),
        )
        return filler

    def _build_mango_watch(self) -> MangoWatch | None:
        """踩芒果喊话（狭缝 S 小异亚招募），当前跑在测试链路上。"""
        config = self._config_section("mango_watch")
        if not config.get("enabled", False):
            return None
        if not HAS_PIL:
            self.logger.warning("mango_watch.enabled 为真但 pillow 不可用，已跳过")
            return None
        watch = MangoWatch(config)
        problems = watch.validate()
        if problems:
            self.logger.warning(
                "mango_watch 配置不完整，已跳过: %s", "；".join(problems)
            )
            return None
        self.logger.info(
            "踩芒果喊话已启用: 源群 %s → 目标群 %s, 服 %d 个, 冷却 %.0fs",
            watch.source_groups,
            watch.target_groups,
            len(watch.servers),
            watch.cooldown,
        )
        return watch

    def _build_spawn_watcher(self) -> SpawnWatcher | None:
        """S 怪触发上报采集：把 Sonar 等机器人的触发播报写成看板数据文件。

        看板（/srank/）每分钟拉这个文件，与 tracker 推送合并（max 时间戳），
        把触发中的 S 怪标成「已触发」并在地图上标红点。
        """
        config = self._config_section("spawn_watch")
        if not config.get("enabled", False):
            return None
        path = str(config.get("path") or str(data_directory()/'bot_relay.json'))
        servers = [str(s) for s in (config.get("servers") or [])]
        if not servers:
            self.logger.warning("spawn_watch.enabled 为真但没有配置 servers，已跳过")
            return None
        prefixes = [str(p) for p in (config.get("prefixes") or ["[Sonar", "[银山雀儿"])]
        watcher = SpawnWatcher(
            path,
            servers=servers,
            prefixes=prefixes,
            dedupe_seconds=float(config.get("dedupe_seconds", 600)),
            keep_seconds=float(config.get("keep_seconds", 86400)),
        )
        self.logger.info(
            "S 怪触发上报采集已启用: %s（%d 服, %d 条历史记录）",
            path,
            len(watcher.servers),
            sum(len(v) for v in watcher.relay.values()),
        )
        return watcher

    def _spawn_leak_task(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._leak_tasks.add(task)
        task.add_done_callback(self._leak_tasks.discard)

    async def _leak_fill_task(self, text: str, origin_group: int, images=None) -> None:
        """扩散群里出现一条 S 怪播报：等窗口过后补发到仍缺的扩散群。

        ``images`` 为原播报携带的图片，补发时一并带过去。
        """
        try:
            missing = await self._leak.observe(text, origin_group, images)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("查漏补发检查异常")
            return
        if missing:
            await self._leak_send(text, missing, images, origin_group)

    async def _leak_sweep_task(self, text: str, images=None, source_group=None) -> None:
        """bot 转发完成后的主动巡检：顺带把发送失败漏掉的群补上。

        ``images`` 为原播报携带的图片，补发时一并带过去。
        ``source_group`` 为本次转发来源群；未配置管理群时撤回码改发到那里。
        """
        if self._leak.window > 0:
            await asyncio.sleep(self._leak.window)
        missing = self._leak.missing_groups(text)
        if missing:
            await self._leak_send(text, missing, images, source_group)

    async def _leak_send(
        self, text: str, groups: list[int], images=None, source_group=None
    ) -> None:
        """查漏：把播报补发到仍缺的扩散群，并在管理群（手摇机器人）留一条可撤回的通知。"""
        images = list(images) if images else self._leak.images_for(text)
        if images:
            self._leak.set_images(text, images)
        segments = [{"type": "text", "data": {"text": text}}]
        for file in images:
            segments.append({"type": "image", "data": {"file": file}})
        filled: list[int] = []
        sent_ids: list[str] = []
        for group in groups:
            message_id = await self._send_segments(
                group, segments, "leak_fill", "查漏补发", 1
            )
            if message_id is None and images:
                # 带原图整条发送失败（图可能已失效）：退回仅文字补发，别丢整条。
                message_id = await self._send_segments(
                    group,
                    [{"type": "text", "data": {"text": text}}],
                    "leak_fill",
                    "查漏补发(仅文字)",
                    1,
                )
            if message_id is not None:
                self._leak.record(text, group)
                filled.append(group)
                sent_ids.append(str(message_id))
                self.logger.info(
                    "已查漏补发: 群%s 文本%s 附图%d张",
                    group, text.strip()[:40], len(images),
                )
        if not filled:
            return

        # 撤回码只发在管理群（手摇机器人）；没配管理群时才退回来源群。
        notify_group = int(getattr(self._leak, "notify_group", 0) or 0)
        code_group = notify_group or (int(source_group) if source_group else 0)
        if not code_group:
            return
        recall_id = secrets.token_hex(4)
        while recall_id in self._recall_cache:
            recall_id = secrets.token_hex(4)
        self._cache_forwarded(
            str(code_group), sent_ids, {str(code_group)}, recall_id
        )
        recall_line = f"\n2 分钟内在本群发送「撤回{recall_id}」可撤回这些补发。"
        if notify_group:
            await self._leak_notify(text, filled, images, recall_line)
            return
        try:
            await self._send_segments(
                code_group,
                [{"type": "text", "data": {"text": "查漏补发已发出" + recall_line}}],
                "leak_fill",
                "补发撤回码",
                1,
            )
        except Exception:
            self.logger.exception("补发撤回码发送失败: 群%s", code_group)

    async def _leak_notify(
        self, text: str, filled: list[int], images=None, recall_line=""
    ) -> None:
        """补发成功后把详情（含撤回码）通知到管理群。"""
        names = []
        for group in filled:
            try:
                index = self._leak.groups.index(group) + 1
            except ValueError:
                names.append(str(group))
            else:
                names.append(f"扩散群{index}({group})")
        tail = f"\n（附图 {len(images)} 张）" if images else ""
        notice = (
            f"🔔 查漏补发：已把播报补发到 {len(filled)} 个群：{'、'.join(names)}\n"
            + text.strip()[:200]
            + tail
            + recall_line
        )
        try:
            await self._send_segments(
                self._leak.notify_group,
                [{"type": "text", "data": {"text": notice}}],
                "leak_fill",
                "补发通知",
                1,
            )
        except Exception:
            self.logger.exception("补发通知发送失败: 群%s", self._leak.notify_group)

    def _config_section(self, key: str) -> dict:
        """取一个配置段，保证返回 dict。"""
        value = self.get_config(key, {}) or {}
        return value if isinstance(value, dict) else {}

    def _collect_map_aliases(self) -> dict:
        """合并新旧两处别名配置；map_names 里的写法优先。"""
        aliases: dict = {}
        aliases.update(self._config_section("map_images").get("aliases") or {})
        aliases.update(self._config_section("map_names").get("aliases") or {})
        return aliases

    def _build_map_name_normalizer(self, aliases: dict) -> MapNameNormalizer | None:
        """群里常用的地图简称 -> 标准地图名，用于转发前改写文本。"""
        config = self._config_section("map_names")
        if not config.get("enabled", False):
            return None

        # 没有别名但也想统一加前缀的地图名：图片索引 + 补全表里的地图。
        extra: list[str] = []
        if self._map_images is not None:
            extra.extend(self._map_images.index.keys())
        extra.extend((self._config_section("completions").get("pairs") or {}).values())

        normalizer = MapNameNormalizer(
            aliases,
            prefix=str(config.get("prefix", "") or ""),
            extra_names=extra,
            suffix=str(config.get("suffix", "") or ""),
        )
        if not normalizer.size:
            self.logger.warning("map_names.enabled 为真但没有可用的别名映射，已跳过")
            return None
        self.logger.info("地图名规范化已启用: %d 条别名", normalizer.size)
        return normalizer

    def _build_map_image_index(self, aliases: dict) -> MapImageIndex | None:
        """按配置建立地图图片索引；未启用或目录为空时返回 None。"""
        config = self._config_section("map_images")
        if not config.get("enabled", False):
            return None

        directory = Path(str(config.get("dir", "data/map_images")))
        if not directory.is_absolute():
            # 以项目根目录为基准（本文件位于 <root>/plugins/forwardbot/）
            directory = Path(__file__).resolve().parent / directory

        index = MapImageIndex(directory, aliases)
        if not index.available:
            self.logger.warning("地图图片目录中没有可用图片: %s", directory)
            return None

        self.logger.info(
            "地图图片已加载 %d 张（%s模式），目录: %s",
            len(index.index),
            "图文合并" if self._combine_map_image else "转发+图片",
            directory,
        )
        return index

    def _build_decorator(self) -> TermDecorator | None:
        """给服务器/频道名、S 怪名等词条自动套括号。"""
        config = self._config_section("decorations")
        if not config.get("enabled", False):
            return None

        # 保护名单里的词不参与任何替换：单字简称最容易踩到底图地名。
        protected: list[str] = [str(item).strip() for item in config.get("protect") or []]
        if self._map_names is not None:
            protected.extend(self._map_names.canonicals)
        if self._map_images is not None:
            protected.extend(self._map_images.index.keys())
        protected.extend(self._collect_map_aliases().keys())

        protected_set = sorted({word for word in protected if word}, key=len, reverse=True)
        decorator = TermDecorator(config.get("groups") or [], protected_set)
        if not decorator.size:
            self.logger.warning("decorations.enabled 为真但没有词条，已跳过")
            return None
        self.logger.info(
            "自动加括号已启用: %d 种写法，%d 个保护词", decorator.size, len(protected_set)
        )
        return decorator

    def _build_completer(self) -> MonsterMapCompleter | None:
        """S 怪名与地图名互相补全：只说了一半的消息补上另一半。"""
        config = self._config_section("completions")
        if not config.get("enabled", False):
            return None

        # 已知地图名：正文里已经出现了别的地图就别再往里塞一个。
        # 只取 map_names 规范化出来的标准名，不含图片索引里的名字
        # （图片索引里有「银泪湖」这种群里当服务器名用的词）。
        known: set[str] = set()
        if self._map_names is not None:
            known.update(self._map_names.canonicals)

        completer = MonsterMapCompleter(
            config.get("pairs") or {},
            require_s_hint=bool(config.get("require_s_hint", True)),
            monster_braces=str(config.get("monster_braces", "[]")),
            aliases=config.get("aliases") or {},
            known_maps=known,
            map_prefix=str(self._config_section("map_names").get("prefix", "") or ""),
            map_suffix=str(self._config_section("map_names").get("suffix", "") or ""),
        )
        if not completer.size:
            self.logger.warning("completions.enabled 为真但没有配对表，已跳过")
            return None
        mode = "严格" if completer.require_s_hint else "宽松"
        self.logger.info("S怪/地图名补全已启用: %d 组配对（%s模式）", completer.size, mode)
        return completer

    def _build_s_rank_pattern(self) -> re.Pattern | None:
        """匹配「地图名 + 单 S」，用于区分 S 怪消息和 SS 前置消息。"""
        if not self._config_section("map_images").get("skip_s_rank", False):
            return None

        names: set[str] = set()
        if self._map_names is not None:
            names.update(self._map_names.canonicals)
        if self._map_images is not None:
            names.update(self._map_images.index.keys())
            names.update(needle for needle, _ in self._map_images.candidates)
        names.update(self._collect_map_aliases().values())
        ordered = sorted((name for name in names if name), key=len, reverse=True)
        if not ordered:
            self.logger.warning("map_images.skip_s_rank 已开启但没有可用地图名")
            return None

        self.logger.info("S 怪消息不配图已启用: %d 个地图名", len(ordered))
        # (?![Ss]) 排除「亚克特尔树海SS前置」这类双 S 的写法。
        return re.compile(
            "(" + "|".join(re.escape(name) for name in ordered) + r")\s*[Ss](?![SsA-Za-z])"
        )

    async def on_close(self) -> None:
        task = getattr(self, '_hunt_alert_task', None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        tasks = list(self._leak_tasks)
        for pending in tasks:
            pending.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._leak_tasks.clear()
        for entry in self._recall_cache.values():
            if entry.timer is not None:
                entry.timer.cancel()
        self._recall_cache.clear()

    async def _rate_limited(self, action, *args, **kwargs):
        """转发、回复和撤回共用同一把锁与全局发送间隔。"""
        async with self._action_lock:
            interval = max(0, float(self.get_config("send_interval_ms", 500))) / 1000
            if self._last_action_time is not None:
                remaining = interval - (time.monotonic() - self._last_action_time)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            try:
                return await action(*args, **kwargs)
            finally:
                self._last_action_time = time.monotonic()

    @staticmethod
    def _extract_message_id(result) -> str | None:
        """兼容字典式与对象式的发送结果。"""
        if result is None:
            return None
        getter = getattr(result, "get", None)
        value = getter("message_id") if callable(getter) else getattr(result, "message_id", None)
        if value is None or str(value) == "":
            return None
        return str(value)

    @staticmethod
    def _original_image_files(event: GroupMessageEvent) -> list[str]:
        """取原消息里的图片（用于图文合并时保留原图，避免丢内容）。"""
        files: list[str] = []
        try:
            for segment in event.message:
                if getattr(type(segment), "_type", None) != "image":
                    continue
                data = segment.to_dict().get("data") or {}
                value = data.get("file") or data.get("url")
                if value:
                    files.append(str(value))
        except Exception:  # noqa: BLE001
            # 取不到原图不影响主流程，只是合并不带原图。
            return []
        return files

    async def _reply(self, event: GroupMessageEvent, text: str) -> None:
        try:
            await self._rate_limited(event.reply, text=text, at_sender=False)
        except Exception:
            # 回复失败不能触发重发，否则会产生不在撤回缓存中的重复消息。
            self.logger.exception("回复源群消息失败: 群%s", event.group_id)

    async def safe_forward_message(
        self, target_group: int, message_id: str, rule_name: str, max_retries: int = 2
    ) -> str | None:
        """转发单条消息并返回目标消息 ID；失败时最多重试两次。"""
        for attempt in range(max_retries + 1):
            try:
                # forward_group_single_msg 不返回 ID；此接口读取原消息段后发送，
                # 每次只传入一个原消息 ID，确保拿到该次发送的唯一消息 ID。
                result = await self._rate_limited(
                    self.api.qq.send_group_forward_msg_by_id,
                    target_group,
                    [message_id],
                )
            except Exception as exc:
                self.logger.warning(
                    "转发失败: 群%s, 规则%s (尝试 %s/%s): %s",
                    target_group,
                    rule_name,
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
                if attempt < max_retries:
                    await asyncio.sleep(0.5)
                continue

            forwarded_id = self._extract_message_id(result)
            if forwarded_id is None:
                raise MissingForwardMessageID(f"群{target_group}未返回转发消息 ID")
            return forwarded_id
        return None

    async def safe_send_map_image(
        self, target_group: int, image_path, rule_name: str, max_retries: int = 2
    ) -> str | None:
        """单独发送一张地图图片并返回消息 ID；失败时最多重试两次。"""
        return await self._send_segments(
            target_group,
            [{"type": "image", "data": {"file": str(image_path)}}],
            rule_name,
            "地图图",
            max_retries,
        )

    async def safe_send_combined(
        self,
        target_group: int,
        text: str,
        image_path,
        original_images: list[str],
        rule_name: str,
        max_retries: int = 2,
    ) -> str | None:
        """把文字和图片合成一条消息发送，返回该消息 ID。

        先按「文字 + 原消息图片 + 地图图」发；若带原图失败，退回「文字 + 地图图」，
        避免因为原图地址失效而整条消息发不出去。``image_path`` 为 None 时只发文字，
        用于「只改写了地名、没有对应地图图」的情况。
        """
        def build(keep_original: bool) -> list[dict]:
            segments: list[dict] = []
            if text:
                segments.append({"type": "text", "data": {"text": text}})
            if keep_original:
                for file in original_images:
                    segments.append({"type": "image", "data": {"file": file}})
            # 没有命中地图图时（只做了简称改写），这一段会缺席。
            if image_path is not None:
                segments.append({"type": "image", "data": {"file": str(image_path)}})
            return segments

        message_id = await self._send_segments(
            target_group, build(bool(original_images)), rule_name, "图文合并", 0
        )
        if message_id is not None:
            return message_id
        if original_images:
            self.logger.warning(
                "群%s 带原图合并失败，退回仅文字+地图图重试", target_group
            )
        return await self._send_segments(
            target_group, build(False), rule_name, "图文合并", max_retries
        )

    async def _send_segments(
        self,
        target_group: int,
        segments: list[dict],
        rule_name: str,
        label: str,
        max_retries: int,
    ) -> str | None:
        for attempt in range(max_retries + 1):
            try:
                result = await self._rate_limited(
                    self.api.qq.messaging.send_group_msg, target_group, segments
                )
            except Exception as exc:
                self.logger.warning(
                    "%s发送失败: 群%s, 规则%s (尝试 %s/%s): %s",
                    label,
                    target_group,
                    rule_name,
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
                if attempt < max_retries:
                    await asyncio.sleep(0.5)
                continue

            message_id = self._extract_message_id(result)
            if message_id is None:
                # 消息可能已经发出，不能因为没有回执 ID 就再发一次。
                raise MissingForwardMessageID(f"群{target_group} {label}未返回消息 ID，不重发")
            return message_id
        return None

    def _forget_recall(self, recall_id: str, entry: RecallEntry) -> None:
        # 不删除意外碰撞后属于其他消息的条目。
        if self._recall_cache.get(recall_id) is entry:
            self._recall_cache.pop(recall_id)
        if entry.timer is not None:
            entry.timer.cancel()

    def _cache_forwarded(
        self, source_group: str, message_ids: list[str], recall_groups=None,
        recall_id=None,
    ) -> str:
        # 允许调用方预先生成撤回码（好把码并进同一条消息里）。
        if recall_id is None:
            recall_id = secrets.token_hex(4)
        while recall_id in self._recall_cache:
            recall_id = secrets.token_hex(4)
        entry = RecallEntry(
            source_group=str(source_group),
            message_ids=list(message_ids),
            expires_at=time.monotonic() + self.recall_ttl,
            recall_groups=set(recall_groups) if recall_groups else None,
        )
        self._recall_cache[recall_id] = entry
        entry.timer = asyncio.get_running_loop().call_later(
            self.recall_ttl, self._forget_recall, recall_id, entry
        )
        return recall_id

    async def recall_messages(self, event: GroupMessageEvent, recall_id: str) -> None:
        entry = self._recall_cache.get(recall_id)
        if entry is not None and time.monotonic() >= entry.expires_at:
            self._forget_recall(recall_id, entry)
            entry = None
        allowed = entry.recall_groups if entry.recall_groups is not None else {entry.source_group}
        if entry is None or str(event.group_id) not in allowed:
            await self._reply(event, "撤回码无效、已过期或不属于本群。")
            return
        if entry.recalling:
            await self._reply(event, "这条消息正在撤回，请勿重复操作。")
            return

        # 首次 await 前占用条目，防止多人同时撤回同一批消息。
        entry.recalling = True
        total = len(entry.message_ids)
        try:
            for message_id in list(entry.message_ids):
                try:
                    await self._rate_limited(
                        self.api.qq.messaging.delete_msg, message_id
                    )
                except Exception:
                    self.logger.exception("撤回失败: 消息%s", message_id)
                else:
                    # 只保留失败的 ID；有效期内再次发送同一码可重试。
                    entry.message_ids.remove(message_id)
        finally:
            entry.recalling = False
            if not entry.message_ids:
                self._forget_recall(recall_id, entry)

        failed = len(entry.message_ids)
        text = f"已撤回 {total - failed}/{total} 条转发消息。"
        if failed:
            text += f" {failed} 条撤回失败。"
            if time.monotonic() < entry.expires_at:
                text += " 可在原两分钟有效期内使用同一撤回码重试。"
        await self._reply(event, text)

    async def _handle_mango_shout(self, event: GroupMessageEvent, message: str) -> bool:
        """踩芒果喊话处理。

        返回 True 表示本条消息已由本功能消费（不再走转发规则）；
        返回 False 表示不是踩芒果喊话/选点回复或不在源群，交给后续流程。

        流程：
          1. 源群回复了等待中的绿点坐标 → 广播被选定的那一个点；
          2. 喊话匹配 → 拉实时点位：
             - 只有 1 个绿点 → 直接广播；
             - 多个绿点 → 不广播，回一张全绿点高亮图 + 选点提示，等对方回复坐标。
        广播出去的永远只有一个点位。
        """
        mango = self._mango
        source_group = int(event.group_id)
        if source_group not in mango.source_groups:
            return False

        # 1) 多绿点喊话后等待选点：对方回复了坐标就先处理选点
        picked = mango.match_pending(message)
        if picked is not None:
            dc, world, green = picked
            self.logger.info(
                "踩芒果选点: %s/%s -> ( %.1f , %.1f )", dc, world, green[1], green[2]
            )
            return await self._mango_broadcast(
                event, mango, dc, world, [green], picked=True
            )
        if parse_coords(message) and mango.has_pending():
            # 看着就是在回坐标但没命中绿点：接住并提示，别掉出去当普通消息
            await self._reply(event, "这个坐标不在当前绿点里，请按提示回复其中一个绿点坐标。")
            return True

        world = mango.match(message)
        if world is None:
            return False
        dc = mango.dc_of(world)

        remaining = mango.cooldown_left(dc, world)
        if remaining > 0:
            self.logger.info(
                "踩芒果喊话冷却中: %s/%s 剩余 %.0fs", dc, world, remaining
            )
            await self._reply(event, f"【{world}】刚喊过，{int(remaining)} 秒后可再喊。")
            return True
        if not mango.begin(dc, world):
            await self._reply(event, f"【{world}】正在喊了，稍等。")
            return True

        try:
            try:
                points, meta = await asyncio.to_thread(
                    mango.fetch_points, dc, world
                )
            except Exception as exc:
                self.logger.warning("踩芒果拉取点位失败: %s/%s: %s", dc, world, exc)
                await self._reply(event, "点位数据拉取失败，稍后再试。")
                return True
            if points is None:
                self.logger.info("踩芒果点位不可用: %s/%s: %s", dc, world, meta)
                await self._reply(event, f"【{world}】暂无可用的绿点数据（{meta}）。")
                return True

            greens = mango.greens(points)
            if not greens:
                self.logger.info("踩芒果无绿点: %s/%s", dc, world)
                await self._reply(event, f"【{world}】的点位都踩过了，没有绿点。")
                return True

            if len(greens) > 1:
                # 多个绿点：不直接广播，请对方从里面选一个。
                image_path = None
                try:
                    image_path = await asyncio.to_thread(
                        mango.render, dc, world, greens
                    )
                except Exception:
                    self.logger.exception("踩芒果选点图生成失败: %s/%s", dc, world)
                mango.set_pending(dc, world, greens, image_path)
                prompt = mango.build_pick_prompt(world, greens)
                await self.safe_send_combined(
                    source_group, prompt, image_path, [], "mango_watch"
                )
                self.logger.info(
                    "踩芒果等待选点: %s/%s %d 个绿点 %s",
                    dc,
                    world,
                    len(greens),
                    "、".join(f"( {x:.1f} , {y:.1f} )" for _, x, y in greens),
                )
                return True

            return await self._mango_broadcast(
                event, mango, dc, world, greens, held=True
            )
        finally:
            mango.finish(dc, world)

    async def _mango_broadcast(
        self,
        event: GroupMessageEvent,
        mango: MangoWatch,
        dc: str,
        world: str,
        greens,
        picked: bool = False,
        held: bool = False,
    ) -> bool:
        """把单个绿点的文案 + 高亮图广播到目标群。

        ``picked`` 为 True 时文案用「选定的绿点」（对方从多个绿点里选的）；
        ``held`` 为 True 表示调用方已占用 inflight（喊话流程里已经 begin 过）。
        """
        source_group = int(event.group_id)
        if not held:
            remaining = mango.cooldown_left(dc, world)
            if remaining > 0:
                self.logger.info(
                    "踩芒果选点广播冷却中: %s/%s 剩余 %.0fs", dc, world, remaining
                )
                await self._reply(
                    event, f"【{world}】刚广播过，{int(remaining)} 秒后可再选。"
                )
                return True
            if not mango.begin(dc, world):
                await self._reply(event, f"【{world}】正在处理，稍等。")
                return True

        try:
            image_path = None
            try:
                image_path = await asyncio.to_thread(
                    mango.render, dc, world, greens
                )
            except Exception:
                self.logger.exception("踩芒果图片生成失败: %s/%s", dc, world)
                await self._reply(event, "绿点图生成失败，本次只发文字。")

            text = mango.build_text(world, greens, picked=picked)
            sent_ids: list[str] = []
            failed = 0
            for group in mango.target_groups:
                message_id = await self.safe_send_combined(
                    group, text, image_path, [], "mango_watch"
                )
                if message_id is None:
                    failed += 1
                else:
                    sent_ids.append(message_id)

            mango.mark_broadcast(dc, world)
            mango.clear_pending(dc, world)
            coords = "、".join(f"( {x:.1f} , {y:.1f} )" for _, x, y in greens)
            self.logger.info(
                "踩芒果广播: %s/%s 绿点%s → 成功 %d 失败 %d",
                dc,
                world,
                coords,
                len(sent_ids),
                failed,
            )
            if sent_ids:
                # 撤回码在源群和目标群都能用，喊错了哪边都能撤。
                # 注意 recall_groups 里必须是字符串群号（撤回校验按 str 比较）。
                recall_id = self._cache_forwarded(
                    str(source_group),
                    sent_ids,
                    recall_groups={str(g) for g in mango.target_groups}
                    | {str(source_group)},
                )
                await self._reply(
                    event,
                    f"已广播到 {len(sent_ids)} 个群：{coords}"
                    f"\n2 分钟内发送「撤回{recall_id}」可撤回。",
                )
            else:
                await self._reply(event, "广播发送失败，请检查 bot 日志。")
            return True
        finally:
            if not held:
                mango.finish(dc, world)

    async def onGroupMessageReceived(self, event: GroupMessageEvent):
        message = "".join(seg.text for seg in event.message.filter_text())
        try:
            if self._hunt_alerts is not None:
                self._hunt_alerts.observe_ss(message, event.group_id, event.message_id, getattr(event, 'time', None))
        except Exception:
            self.logger.exception("SS前置采集失败")
        if str(event.user_id) == str(event.self_id):
            return
        # S 怪触发上报采集：任何群里的 Sonar 播报都可能是看板的数据来源。
        # 解析失败只记日志，绝不影响转发主流程。
        if self._spawn is not None:
            try:
                if self._spawn.observe(message):
                    self._spawn.flush()
            except Exception:
                self.logger.exception("触发上报采集失败")
        command = self.recall_command.fullmatch(message.strip())
        if command:
            await self.recall_messages(event, command[1].lower())
            return
        if message.lstrip().startswith("/"):
            return

        # 踩芒果喊话：源群喊「<服> 天外天垓 需要小异亚」→ 广播高亮绿点图到目标群。
        # 放在规则匹配之前并直接 return：喊话本身不该再被当普通播报转发。
        if self._mango is not None:
            if await self._handle_mango_shout(event, message):
                event.consumed = True
                return

        source_group = int(event.group_id)
        # 抓原消息里的图，补发时一并带过去，避免只补文字、丢了别人发的图。
        leak_images = (
            self._original_image_files(event)
            if (self._leak is not None and getattr(self, "_leak_attach_image", True))
            else []
        )

        # 扩散群之间的 S 怪播报查漏：后台等待窗口后补发，不阻塞本条消息处理。
        if (
            self._leak is not None
            and source_group in self._leak.groups
            and self._leak.matches(message)
        ):
            self._spawn_leak_task(self._leak_fill_task(message, source_group, leak_images))

        # 规则判定只用原文，避免关键词因为改写而失配。
        matching_rules = self.manager.find_matching_rules(message, source_group)
        if not matching_rules:
            return

        # 先把行首「注：」换成图标，再把地图简称替换成标准名，后续转发、配图都用改写后的文本。
        outbound = message
        if self._note_pattern is not None:
            outbound = self._note_pattern.sub(self._note_emoji, outbound)
        name_hits: list[str] = []
        if self._map_names is not None:
            # 注意必须基于已经替换过 🪧 的 outbound，否则上面的「注：」→🪧 会被覆盖掉。
            outbound, name_hits = self._map_names.normalize(outbound)
            if name_hits:
                self.logger.info(
                    "地图简称已规范化: %s | %s -> %s",
                    "、".join(name_hits),
                    message.strip()[:60],
                    outbound.strip()[:80],
                )

        # 只有 SS 前置才配 SS 点位图；S 怪播报、车序、纯报点一律不配。
        skip_map_image = False
        if self._s_rank_pattern is not None and self._s_rank_pattern.search(outbound):
            # 「地图名 + 单 S」：明确是 S 怪消息。
            skip_map_image = True
        elif (
            self._ss_context_pattern is not None
            and self._ss_context_pattern.search(outbound) is None
        ):
            # 连 SS 都没有：肯定不是 SS 前置。
            skip_map_image = True

        # 服务器/频道名、S 怪名等词条自动加括号。
        decorated: list[str] = []
        if self._decorator is not None:
            outbound, decorated = self._decorator.decorate(outbound)
            if decorated:
                self.logger.info("已自动加括号: %s", "、".join(decorated))
            if skip_map_image:
                self.logger.info(
                    "识别为 S 怪消息，本次不附图: %s", outbound.strip()[:40]
                )

        # 只有命中了需要附图的规则，才去消息里找地图名。
        map_name = None
        map_path = None
        if (
            self._map_images is not None
            and not skip_map_image
            and any(getattr(rule, "attach_map_image", False) for rule in matching_rules)
        ):
            map_name, map_path = self._map_images.match(outbound)
            if map_path is None:
                self.logger.info("未识别到地图名，本次不附图: %s", outbound.strip()[:40])

        # 只说了一半（只报怪名 / 只报地图）时补上另一半。
        # 放在配图之后，补出来的地图名不会影响是否附图，免得给 S 怪消息错配 SS 点位图。
        completion_notes: list[str] = []
        if self._completer is not None:
            outbound, completion_notes = self._completer.complete(outbound)
            if completion_notes:
                self.logger.info("已补全缺失信息: %s", "；".join(completion_notes))

        # 有地图图、文本被改写过（📍 前缀、🪧 图标、改地名、加括号、补全），
        # 都按「文本 + 图片」一条消息发出去，否则只能走转发气泡，写好的文本不会出现在目标群里。
        modified = (
            map_path is not None
            or bool(name_hits)
            or bool(decorated)
            or bool(completion_notes)
            or outbound != message
        )
        use_text_message = self._combine_map_image and modified
        original_images = self._original_image_files(event) if use_text_message else []

        sent_ids: list[str] = []
        separate_images = 0
        combined_images = 0
        failed = 0
        missing_ids = 0

        for rule in matching_rules:
            attach_image = bool(getattr(rule, "attach_map_image", False)) and map_path is not None
            for target_group in rule.target_groups:
                if not rule.can_forward_to(source_group, target_group):
                    continue

                if use_text_message:
                    # 一条消息里同时有原文和地图图，不再发转发气泡。
                    try:
                        combined_id = await self.safe_send_combined(
                            target_group,
                            outbound,
                            map_path if attach_image else None,
                            original_images,
                            rule.name,
                        )
                    except Exception:
                        self.logger.exception("图文合并异常: 群%s", target_group)
                        combined_id = None
                    if combined_id is None:
                        failed += 1
                        continue
                    sent_ids.append(combined_id)
                    combined_images += 1
                    if self._leak is not None:
                        # 文本消息发出去的内容就是改写后的 outbound。
                        self._leak.record(outbound, target_group)
                    self.logger.info(
                        "已%s: 群%s 规则%s 地图%s 消息%s",
                        "合并发送图文" if map_path is not None else "规范化发送文本",
                        target_group,
                        rule.name,
                        map_name or (name_hits[0] if name_hits else "-"),
                        combined_id,
                    )
                    continue

                try:
                    forwarded_id = await self.safe_forward_message(
                        target_group, event.message_id, rule.name
                    )
                except MissingForwardMessageID as exc:
                    missing_ids += 1
                    self.logger.error("%s；不重发以免重复发送", exc)
                    continue
                if forwarded_id is None:
                    failed += 1
                    continue

                sent_ids.append(forwarded_id)
                if self._leak is not None:
                    # 转发气泡发的是原始消息，文本与原文一致。
                    self._leak.record(message, target_group)

                if attach_image:
                    image_id = await self.safe_send_map_image(
                        target_group, map_path, rule.name
                    )
                    if image_id is not None:
                        sent_ids.append(image_id)
                        separate_images += 1
                        self.logger.info(
                            "已附加地图图: 群%s 规则%s 地图%s 消息%s",
                            target_group,
                            rule.name,
                            map_name,
                            image_id,
                        )
                    else:
                        failed += 1

        if sent_ids:
            # bot 自己转发的 S 怪播报也巡检一遍：个别群发送失败时自动补发。
            if self._leak is not None:
                leak_text = outbound if use_text_message else message
                if self._leak.matches(leak_text):
                    self._spawn_leak_task(self._leak_sweep_task(leak_text, leak_images, source_group))
            recall_id = self._cache_forwarded(str(source_group), sent_ids)
            sent_count = len(sent_ids) - separate_images
            notes: list[str] = []
            if combined_images and map_name:
                notes.append(f"{combined_images} 条已附带「{map_name}」地图图")
            elif combined_images:
                notes.append(f"{combined_images} 条已改写转发文本")
            elif separate_images:
                notes.append(f"已附加 {separate_images} 张「{map_name}」地图图")
            if skip_map_image:
                notes.append("识别为 S 怪消息，未附图")
            if completion_notes:
                notes.append("已补全 " + "；".join(completion_notes))
            text = f"已转发 {sent_count} 条消息。"
            if notes:
                text += " " + "；".join(notes) + "。"
            text += f"\n2 分钟内在本群发送「撤回{recall_id}」可撤回。"
        elif failed or missing_ids:
            text = "本次没有可缓存的转发消息 ID。"
        else:
            return
        if failed:
            text += f"\n{failed} 条发送失败。"
        if missing_ids:
            text += f"\n{missing_ids} 次转发未返回消息 ID，无法通过撤回码撤回。"
        await self._reply(event, text)
