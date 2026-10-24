# 魔法水桶 · AstrBot 移植插件

依据原有 NcatBot `ForwardBotPlugin 0.11.7` 移植，读取日期为 2026-10-24。包含原有功能和地图资源；公开发行默认群规则留空，不依赖 NcatBot，也不需要 SSH 密码。

## 安装

1. 在 AstrBot 的插件管理中上传 `astrbot_plugin_magic_bucket-1.1.0.zip`，安装依赖并重载。也可把目录放到 `data/plugins/astrbot_plugin_magic_bucket/`。
2. 接入 **aiocqhttp / OneBot v11**，QQ 协议端使用 NapCat 或兼容实现，消息格式设置为 array。机器人应加入相关源群、目标群。
3. 默认 `enabled=false`。确认配置后打开“启用魔法水桶”，保存并重载。原 NcatBot 若同时处理相同群规则，会重复发送；正式接管时应只保留一套转发程序处理这些规则。
4. `bot_uin` 默认留空，跟随 AstrBot 接入的 OneBot 账号；新增或替换账号无需改代码，也可填写 QQ 号限制账号。多个实例的撤回、缓存、生成文件分别隔离。

面向 AstrBot 4.10.4 及以后版本。实际开发环境通过本机 AstrBot SDK 导入与入口调用验证；未向真实 QQ 群发送验收消息。

## 已移植功能

| 功能 | 行为 |
|---|---|
| 群消息转发 | 原文前缀/关键词匹配，自定义群规则；忽略自身消息、斜杠命令和同群目标 |
| 撤回码 | 8 位随机十六进制码，有效 120 秒；普通转发限源群，补发限通知群，芒果广播允许源群和目标群撤回 |
| 撤回失败重试 | 仅保留失败 ID，不延长有效期；重载后旧码失效 |
| 地图规范化 | 简称替换全名，添加 📍，行首“注：”替换为 🪧 |
| 词条修饰 | 服务器和 S 怪自动加括号，保护地图词条，避免重复括号 |
| S 怪/地图补全 | 按原关系表双向补全；“地图 + 单 S”才反查怪名，车序和 SS 前置不误补 |
| SS 地图 | 附带 18 张原有地图，只有 SS 语境才配图；支持与原图合并发送 |
| 查漏补发 | 扩散群间等待窗口、回看、去重补发；保留原图和管理群撤回通知 |
| S 怪触发采集 | 解析 Sonar/银山雀儿播报，记录触发时间、坐标，去重并持久化为 `bot_relay.json` |
| SS 前置采集 | 按原规则筛除疑问、取消、测试等消息，输出 `ss_events.json`；随 `hunt_alerts` 启用 |
| 超时提醒 | 首次触发 30 分钟后核查熊站击杀历史，结合人工击杀和维护记录抑制误报；保留提醒状态 |
| 踩芒果 | 服务器开头 + 天外天垓/狭缝等 + 小异亚招募；单绿点直接广播，多绿点先回图等选点；B 怪点排除，支持冷却、超时和撤回 |

## 配置

`profile.json` 保存默认规则、别名、词条、补全表、芒果文案等。建议在 WebUI 使用以下字段修改，避免更新插件时丢失修改：

- `enabled`：发送总开关。
- `bot_uin`：机器人账号筛选。
- `send_interval_ms`：转发、回复、撤回共用限速，默认 500 毫秒。
- `rules_json`：JSON 数组，留空使用原 4 条规则；填 `[]` 关闭规则转发。每项字段为 `name`、`type`、`source_groups`、`target_groups`、`keywords`、`forward_prefix`、`attach_map_image`。`forward_prefix` 与原程序一样，仅兼容保存，实际不添加。
- `overrides_json`：JSON 对象，递归覆盖 `profile.json` 的任意配置段；数组整体替换。

例如只使用两个测试群，并关闭查漏：

`rules_json`：
```json
[{"name":"测试","type":"keyword","source_groups":[123456],"target_groups":[654321],"keywords":["重要","SS前置"],"forward_prefix":"","attach_map_image":true}]
```

`overrides_json`：
```json
{"leak_fill":{"enabled":false},"mango_watch":{"source_groups":[123456],"target_groups":[654321]}}
```

默认地图从插件自带资源读取；芒果点位 API 已由服务器回环地址改成 `http://119.29.119.179/api/tracker-points`，校验服、怪、线路与数据版本后才使用。

## 看板与超时提醒

这部分依赖原服务器的文件数据，默认仅在 AstrBot 自己的数据目录保存触发记录，**不会自动回写原服务器看板**。`spawn_watch.path` 可设置为看板使用的可写绝对路径。

超时提醒及 SS 前置发布默认关闭。若 AstrBot 部署在原服务器，配置如下即可使用原数据源：

```json
{
  "spawn_watch":{"path":"/var/www/srank/bot_relay.json"},
  "hunt_alerts":{
    "enabled":true,
    "root":"/var/www/srank",
    "source_group":"1034151994",
    "notify_group":126747198
  }
}
```

进程需要对应文件的读写权限。原数据位置包括 `/var/lib/srank-alerts/tracker-presence.json`、`/var/lib/srank-manual/manual-reports.json` 和可选的维护记录。不要让原程序和移植插件同时写同一份文件。

跨机器部署时，可把看板数据以共享目录提供，并将 `hunt_alerts.root` 指向该目录；目录内使用 `bot_relay.json`、`tracker-presence.json`、`manual-reports.json`、`maintenance-reports.json` 文件名。插件不包含远程文件同步器。提醒状态默认单独保存在 AstrBot 数据目录，不自动继承旧程序的提醒回执；如需保留历史，先复制旧 state.json，再配置 `hunt_alerts.state`。

## 验证与边界

```bash
python -m unittest discover -s tests -v
```

测试覆盖实际移植引擎的转发、SS 配图、补全、图片编码、撤回隔离和失败重试、无回执不重发、查漏、触发文件、提醒核查与芒果选点。安装了 AstrBot SDK 时还会验证真实 SDK 的入口与多账号隔离。

跨机器的本地图片转为 OneBot base64 发送。网络失败重试仍可能遇到服务端已发送但回执丢失的情况；能确认缺少回执 ID 时不会再以降级图文重复发送。图文改写沿用原程序的“文字 + 图片”行为，其他类型的原消息段在未改写转发时保留。

群白名单、AstrBot 事件过滤、其他插件拦截、QQ 发图与撤回权限均影响实机结果。移植插件不包含原服务器独立的离线邮件看门狗、LLBot/QQ 登录器或狩猎网页服务。

## 来源

依据用户授权读取的 `/home/ubuntu/hunhuan/plugins/forwardbot/` 私有部署快照移植，原作者标注 `KirisameVanilla`；移植保留原功能模块并添加 AstrBot/OneBot 适配。未替原代码声明新的开源许可证。

AstrBot 结构参考：[官方插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)。功能对照见 [MIGRATION.md](MIGRATION.md)。

+
+## 控制台
+
+安装后从 AstrBot 侧栏打开“魔法水桶控制台”，或进入插件详情中的页面入口。包括运行概览、规则卡片、地图与词条、查漏采集、芒果广播、狩猎提醒、效果预览、完整 JSON 配置和深浅主题。
+
+保存后即时生效。更改功能配置会重建账号引擎并清理旧撤回缓存。预览使用内存中的发送替身执行实际格式化流程，不调用 QQ 发消息接口。
+
+当前插件遵循 AstrBot 的全局插件加载范围，自动处理接入的 OneBot QQ 账号；QQ 官方机器人不支持此插件所需的跨群发送、按消息 ID 读取与撤回协议。多账号仍需各自加入源群和目标群。
+