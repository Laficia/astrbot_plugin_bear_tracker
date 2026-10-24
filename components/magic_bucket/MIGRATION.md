# 移植核对记录

核对日期：2026-10-24（北京时间）。机器人账号：3923833280。

读取对象：`/home/ubuntu/hunhuan/plugins/forwardbot`，manifest 版本 0.11.7，运行进程为 NcatBot；服务器仅进行了文件读取与资源下载。

| 原模块 | 移植模块 | 处理 |
|---|---|---|
| plugin.py | engine.py | 保留主消息流程、限速、回执、撤回、查漏及芒果编排，移除 NcatBot 基类与装饰器 |
| rules.py | rules.py | 保留前缀/关键词规则及同群排除 |
| map_names.py | map_names.py | 保留最长词优先、简称及全名保护 |
| decorations.py | decorations.py | 保留括号、别名和保护词逻辑 |
| completions.py | completions.py | 保留 47 组 S 怪/地图补全关系 |
| map_images.py | map_images.py | 保留图片索引，资源路径改为插件目录 |
| leak_fill.py | leak_fill.py | 保留等待窗口、回看、图片缓存和补发去重 |
| spawn_watch.py | spawn_watch.py | 保留播报解析、触发去重和 JSON 输出；默认写入 AstrBot 数据目录 |
| mango_watch.py | mango_watch.py | 保留点位版本校验、绿点选择、单点广播和 Pillow 渲染 |
| hunt_alerts.py | hunt_alerts.py | 保留历史核查、提醒状态和 SS 数据输出；curl 改为 Python HTTPS，源群可配置 |
| config.yaml | profile.json | 保留现有规则及词表；回环 API 改服务器地址，本地文件改可配置路径 |
| NcatBot QQ API | adapter.py | 映射 get_msg/send_group_msg/delete_msg，图片跨机器以 base64 提交 |
| NcatBot 生命周期 | main.py | 映射 AstrBot 群事件与 terminate；按平台实例、账号分别创建引擎 |

额外修正：原代码在图文回执缺少消息 ID 时可能进入降级重发，移植改成终止本次发送；合并图文按每条规则的 attach_map_image 决定配图；重载时取消并等待后台查漏任务，避免卸载后继续发消息。

资源：18 张原 SS 地图和 1 张天外天垓底图。内置配置：4 条转发规则、51 种地图写法、47 组怪物地图关系、13 个芒果服务器映射。

验证结果：18 个测试全部通过。`test_port.py` 为核心及 OneBot 模拟验证；`test_host.py` 使用本机实际 AstrBot SDK 验证加载、默认关闭、账号筛选和状态隔离，不连接真实 QQ。

1.1.0 更新：添加 AstrBot 内置控制台、配置验证及持久化、文字和地图预览、深浅主题。默认不绑定原账号，按用户要求跟随接入的 OneBot 账号；OneBot 调用显式指定事件账号，避免新增账号后串号发送。21 个自动化测试通过，并验证安装后真实配置 API 的保存与预览。

尚需实机验收：OneBot 连接、群权限、QQ 图片显示、QQ 撤回期限、看板共享目录权限、在线点位 API 可用性。超时提醒默认关闭，需按 README 配置共享数据目录后启用；没有把外部看板服务打包进插件。

未更改、停用或替换原服务器进程，未向 QQ 群发送测试消息，安装包不包含 SSH 密码、QQ 登录凭据或协议端 Token。
