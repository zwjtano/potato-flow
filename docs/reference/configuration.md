# 配置说明

## Docker 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `5001` | WebUI 与 API 端口 |
| `TZ` | `Asia/Shanghai` | 容器时区 |
| `AUTO_START_RECORDER` | `1` | 自动启动内部录制 worker |
| `POTATO_RECORDINGS_DIR` | `./recordings` | Docker 宿主机录播目录 |

## 录播桥接配置

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `danmaku_enabled` | `true` | 采集 XML 并生成 ASS |
| `danmaku_burn_in` | `false` | 是否把弹幕烧录进视频 |
| `delete_recording_after_upload` | `true` | 投稿成功后删除对应源录播 |
| `ai_danmaku_summary_enabled` | `true` | 使用弹幕生成稿件信息 |
| `post_description_comment` | `true` | 投稿后把简介发为评论 |
| `pin_description_comment` | `true` | 尝试置顶简介评论 |

示例文件位于仓库根目录 `bridge.config.example.json`。

## 单直播间设置

直播间自己的分段、分P、仅录制和 AI 提示词保存在持久化配置中，优先级高于系统默认值。

## 录播文件夹

“系统设置 → 运维与安全 → 录播文件夹”中的 `RECORDINGS_PATH` 控制程序内部使用的目录，默认值为 `recordings`，即项目根目录的 `potato-flow/recordings/`。Docker 用户更换宿主机磁盘时，还需要修改 `.env` 中的 `POTATO_RECORDINGS_DIR` 并重启容器。
