# 配置说明

## Docker 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `5001` | WebUI 与 API 端口 |
| `TZ` | `Asia/Shanghai` | 容器时区 |
| `AUTO_START_RECORDER` | `1` | 自动启动内部录制 worker |
| `POTATO_RECORDINGS_DIR` | `./docker-data/recordings` | Docker 宿主机录播目录 |

## 录播桥接配置

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `danmaku_enabled` | `true` | 采集 XML 并生成 ASS |
| `danmaku_burn_in` | `false` | 是否把弹幕烧录进视频 |
| `delete_recording_after_upload` | `true` | 投稿成功后删除对应源录播 |
| `danmaku_xml_retention_hours` | `24` | 投稿成功后继续保留弹幕 XML 的小时数，便于核查完整性 |
| `ai_danmaku_summary_enabled` | `true` | 使用弹幕生成稿件信息 |
| `post_description_comment` | `true` | 投稿后把简介发为评论 |
| `pin_description_comment` | `true` | 尝试置顶简介评论 |

示例文件位于仓库根目录 `bridge.config.example.json`。

## 单直播间设置

直播间自己的分段、分P、仅录制和 AI 提示词保存在持久化配置中，优先级高于系统默认值。

## 录播文件夹

“系统设置 → 运维与安全 → 录播文件夹”中的 `RECORDINGS_PATH` 控制程序内部使用的目录，默认值为 `docker-data/recordings`，即项目根目录的 `potato-flow/docker-data/recordings/`。点击“选择文件夹”可以直接浏览服务器目录，无需手动输入。Docker 用户更换宿主机磁盘时，还需要修改 `.env` 中的 `POTATO_RECORDINGS_DIR` 并重启容器。

## Telegram 消息通知

在“系统设置 → 消息通知”中开启 Telegram Bot 通知，并填写：

| 字段 | 说明 |
|---|---|
| `NOTIFY_TELEGRAM_BOT_TOKEN` | 通过 Telegram 的 `@BotFather` 创建 Bot 后获得的 Token |
| `NOTIFY_TELEGRAM_CHAT_ID` | 接收消息的个人、群组或频道数字 ID；群组和频道通常以 `-100` 开头 |
| `NETWORK_PROXY_URL` | 可选的通用代理地址；与 YouTube 下载和监控 API 共用，留空时直连 |
| `NETWORK_PROXY_USERNAME` | 可选的通用代理用户名 |
| `NETWORK_PROXY_PASSWORD` | 可选的通用代理密码 |

通用代理统一在“系统设置 → 账号与网络”中填写，支持 HTTP、HTTPS、SOCKS5 和 SOCKS5H。先保存设置，再点击“发送测试消息”。Bot 必须已经加入目标群组或频道，并具有发言权限。Telegram 与其他通知渠道共用事件开关和失败重试队列，不会阻塞录制与投稿。

### Telegram 机器人远程控制

远程控制与消息通知总开关相互独立，默认关闭。开启“允许机器人控制”后，还必须填写：

| 字段 | 说明 |
|---|---|
| `TELEGRAM_CONTROL_ENABLED` | 是否允许 Telegram Bot 执行管理命令 |
| `TELEGRAM_CONTROL_ADMIN_USER_IDS` | 管理员数字 User ID 白名单；多人使用逗号分隔 |

命令必须来自配置的 `NOTIFY_TELEGRAM_CHAT_ID`，并且发送者 User ID 必须在白名单中。群组内其他成员无法操作。User ID 可通过 Telegram 的 `@userinfobot` 查询。

| 命令 | 作用 |
|---|---|
| `/rooms` | 查看全部直播间编号、状态和录制模式 |
| `/add <直播间链接>` | 识别并添加哔哩哔哩、斗鱼或抖音直播间 |
| `/start <编号>` | 恢复指定直播间检测，开播后自动录制 |
| `/stop <编号>` | 安全停止指定直播间并收尾视频、XML 等文件 |
| `/delete <编号>` | 删除直播间；必须在两分钟内点击二次确认 |
| `/tasks` | 查看最近录播投稿和仅录制任务 |
| `/task <编号/任务ID>` | 查看任务步骤、错误、上传百分比、速度和剩余时间 |
| `/retry <编号/任务ID>` | 重试失败、试运行或已暂停任务；复用已有 AI 简介与封面 |
| `/pause <编号/任务ID>` | 暂停等待投稿或正在投稿的任务并保留全部产物 |
| `/delete_task <编号/任务ID>` | 删除任务记录；二次确认且默认保留全部文件 |
| `/files` | 查看最近录播视频、XML 弹幕和 ASS 字幕及占用空间 |
| `/engine` | 查看录制引擎状态 |
| `/engine start` | 启动录制引擎 |
| `/engine stop` | 二次确认后安全停止整个录制引擎 |
| `/status` | 查看录制引擎、直播间数量和磁盘空间 |

删除直播间只移除监控配置，不会删除已经生成的录播文件。删除任务记录同样保留原始录播、字幕和封面。控制功能只调用 PotatoFlow 内部管理器，不新增公网端口，也不允许执行任意系统命令。
