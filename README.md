# biliup × Y2A 录播上传中心

把 [biliup](https://github.com/biliup/biliup) 的直播录制能力与 [Y2A-Auto](https://github.com/fqscfqj/Y2A-Auto) 的下载、AI 处理和投稿能力整合进同一个 WebUI。

本项目的边界很明确：

- 录制平台仅保留 **哔哩哔哩直播**和**斗鱼**；
- 投稿平台仅使用 **哔哩哔哩**；
- 保留 Y2A 的 YouTube 下载、频道监控、字幕、翻译和 AI 功能；
- 录播结束后自动生成 ASS、根据弹幕生成 AI 简介并投稿；
- 默认不烧录弹幕，直接上传原视频，避免额外转码时间和性能消耗；
- 投稿完成后导入 B站原生弹幕：默认全部导入，每条间隔 0.6 秒。
- Linux 上只运行一个主服务、只开放一个 Web 端口；biliup 以无 HTTP 的内部 worker 运行。

## 工作流程

```text
B站直播 / 斗鱼
        ↓
biliup 录制视频 + XML 弹幕
        ↓
录制文件稳定检测
        ↓
生成 ASS ──→ AI 读取弹幕并生成投稿简介
        ↓
Y2A 投稿到哔哩哔哩
        ↓
按时间轴导入 B站原生弹幕
```

ASS 会保存在 `.bridge/artifacts/` 中供归档，不会烧进视频。B站投稿接口不能直接附加 ASS 文件，因此最终播放器中的弹幕通过原生弹幕接口导入。

## 主要功能

### 直播录制

- 统一管理 B站、斗鱼直播间；
- 一键启动或停止内置 biliup 录制引擎；
- 搜索直播间并按“监控中 / 已停止”筛选；
- 录制 B站与斗鱼 XML 弹幕；
- 录制完成后自动触发上传流水线；
- WebUI 显示真实 biliup 进程状态，原始日志中的签名参数会自动隐藏。

### 弹幕处理

- XML 转 ASS，保留原视频，不执行烧录；
- AI 会对弹幕去重、抽样并生成有依据的投稿简介；
- 默认完整导入所有有效弹幕；
- `danmaku_native_max_comments` 设为正整数时，才会按全场时间轴均匀采样；
- 视频已经投稿但弹幕导入失败时，重试不会重复投稿视频。

### YouTube 与上传

- YouTube 单视频、播放列表下载；
- 频道和关键词监控；
- 字幕下载、翻译、质检及语音识别；
- AI 标题、简介、标签和分区建议；
- 所有新任务的投稿目标固定为哔哩哔哩。

## 系统要求

正式支持环境：

- Ubuntu 22.04/24.04、Debian 12/13 或兼容的 64 位 Linux；
- Python 3.11–3.13；
- 最新稳定版 Rust（需要支持 Rust 2024 edition，建议通过 rustup 安装）；
- FFmpeg 与 FFprobe；
- 至少 8 GB 可用磁盘空间用于首次 Rust 构建和 Python 依赖安装；
- 可正常访问直播平台、YouTube、哔哩哔哩以及你配置的 AI API。

macOS 仅用于开发验证；Windows 不属于当前整合版支持范围。

## 安装

### 1. 获取源码

```bash
git clone https://github.com/zwjtano/biliup-y2a-recorder.git
cd biliup-y2a-recorder
```

### 2. Linux 一键安装

安装脚本会安装系统依赖、创建 Python 虚拟环境并构建无端口录制 worker：

```bash
./scripts/install-linux.sh
```

ARM64 Linux 请使用安装脚本在目标机器原生构建。

安装完成后可直接启动：

```bash
./y2a-auto/.venv/bin/python run.py
```

### 3. 手动安装（Ubuntu / Debian）

```bash
sudo apt update
sudo apt install -y ca-certificates curl python3 python3-venv python3-pip \
  ffmpeg build-essential pkg-config libssl-dev
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
```

确认版本：

```bash
python3 --version
cargo --version
ffmpeg -version
```

### 4. 构建定制 biliup

```bash
cd upstream-biliup
cargo build --release -p biliup-cli
cd ..
```

首次 Rust Release 构建耗时较长。如果只是本地试用，可以改用：

```bash
cd upstream-biliup
cargo build --bin biliup
cd ..
```

主程序会优先使用 `upstream-biliup/target/release/biliup`，不存在时自动回退到 Debug 版本。也可以通过环境变量指定已有二进制：

```bash
export BILIUP_BIN=/absolute/path/to/biliup
```

### 5. 安装 Python 依赖

```bash
cd y2a-auto
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cd ..
```

### 6. 创建桥接配置

```bash
cp bridge.config.example.json bridge.config.json
```

默认配置已经启用：

```json
{
  "danmaku_enabled": true,
  "danmaku_burn_in": false,
  "danmaku_native_import": true,
  "danmaku_native_max_comments": 0,
  "danmaku_native_interval_seconds": 0.6,
  "ai_danmaku_summary_enabled": true
}
```

`0` 表示完整导入弹幕；如只想导入最多 200 条，改为：

```json
"danmaku_native_max_comments": 200
```

## 启动

在项目根目录运行：

```bash
python3 run.py
```

浏览器打开：

- 管理后台：<http://127.0.0.1:5001>
- 直播录制：<http://127.0.0.1:5001/live-recording>

修改端口：

```bash
PORT=8080 python3 run.py
```

程序首次启动会自动创建 Y2A 配置和数据库，并自动启动无 HTTP 的录制 worker。整个应用只监听 `5001`（或 `PORT` 指定的端口），不再使用 `19159`。

### 使用 systemd 常驻运行

完成一键安装后执行：

```bash
./scripts/install-systemd.sh
```

脚本会根据当前项目路径和用户生成 `biliup-y2a.service`。systemd 只管理一个主服务，并通过 `KillMode=control-group` 管理其内部录制 worker。

```bash
sudo systemctl status biliup-y2a
journalctl -u biliup-y2a -f
sudo systemctl restart biliup-y2a
```

防火墙只需放行主端口，例如：

```bash
sudo ufw allow 5001/tcp
```

## 首次配置

### 1. 登录哔哩哔哩

进入“系统设置”，使用 B站二维码登录或上传 Cookie 文件。默认 Cookie 路径为：

```text
y2a-auto/cookies/bili_cookies.json
```

Cookie、API Key、数据库和录播文件均已加入 `.gitignore`，不会提交到 GitHub。

### 2. 配置 AI

在“系统设置”中填写：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL_NAME`
- `OPENAI_THINKING_ENABLED`（可选）

AI 弹幕简介只会发送弹幕时间与文本，不发送弹幕 UID 或用户名。未配置 API Key、弹幕为空或请求失败时，系统会保留模板简介并继续投稿。

### 3. 配置投稿

至少确认：

- B站 Cookie 有效；
- `FIXED_PARTITION_ID_BILIBILI` 或目标分区设置正确；
- 转载来源 URL 有效；
- 标题、简介和标签符合 B站投稿要求。

当前上传器按“转载”投稿，因此来源 URL 不能为空。直播录制页面会为每个直播间自动同步来源 URL 和主播标签。

### 4. 添加直播间

进入“直播录制” → “新增直播间”，填写主播名称和以下任一格式：

```text
https://live.bilibili.com/123456
https://www.douyu.com/123456
```

添加完成后点击录制引擎的播放按钮。检测到开播后，biliup 会自动录制；下播或分段完成后会自动进入 ASS、AI 简介、投稿和弹幕导入流程。

## 手动验证桥接器

仅检查配置和文件，不上传：

```bash
y2a-auto/.venv/bin/python bridge.py \
  --config bridge.config.json \
  ingest --dry-run /absolute/path/to/video.mp4
```

真实处理并上传：

```bash
y2a-auto/.venv/bin/python bridge.py \
  --config bridge.config.json \
  ingest /absolute/path/to/video.mp4
```

视频同目录存在同名 XML 时会自动匹配：

```text
主播_2026-07-23_20-00-00.flv
主播_2026-07-23_20-00-00.xml
```

## 状态与重试

```bash
# 查看桥接任务状态
y2a-auto/.venv/bin/python bridge.py --config bridge.config.json status

# 重试失败任务
y2a-auto/.venv/bin/python bridge.py --config bridge.config.json retry
```

状态保存在 `.bridge/state.sqlite3`。如果视频已经上传并记录了 BVID，重试只继续未完成的弹幕导入，不会重复投稿。

## 项目结构

```text
.
├── run.py                         # 统一启动入口
├── bridge.py                      # biliup → Y2A 桥接器
├── danmaku_pipeline.py            # XML、ASS 与 AI 弹幕摘要
├── bilibili_danmaku_importer.py   # B站原生弹幕导入
├── bridge.config.example.json     # 可提交的配置模板
├── upstream-biliup/               # 定制录制引擎，仅 B站/斗鱼
├── y2a-auto/                      # 主 WebUI、YouTube 和 B站上传
└── tests/                         # 整合层测试
```

## 测试

整合层测试：

```bash
y2a-auto/.venv/bin/python -m unittest discover -s tests -v
```

biliup 平台限制测试：

```bash
cd upstream-biliup
cargo test builtin_plugin_tests::only_bilibili_and_douyu_are_enabled
```

## 常见问题

### 页面提示“录制引擎尚未构建”

确认以下任一文件存在且可执行：

```text
upstream-biliup/target/release/biliup
upstream-biliup/target/debug/biliup
```

也可以设置 `BILIUP_BIN` 指向其他位置。

### 找不到 FFmpeg / FFprobe

先确认：

```bash
ffmpeg -version
ffprobe -version
```

如果没有加入 `PATH`，可在 Y2A 设置页填写 `FFMPEG_LOCATION`，并在 `bridge.config.json` 中分别设置 `ffmpeg` 和 `ffprobe` 的绝对路径。

### 录制结束后没有自动上传

依次检查：

1. “原始日志”中是否出现 `postprocessor`；
2. `bridge.config.json` 中的 Cookie、分区和来源 URL；
3. `.bridge/state.sqlite3` 对应任务是否为失败；
4. 使用 `retry` 命令重试。

### Bilibili 登录提示 curl 60 / SSL certificate problem

Linux 是本项目的主要部署目标。程序会优先使用系统 CA bundle（Debian/Ubuntu、RHEL/CentOS/Fedora 和 openSUSE 的常见路径均已支持），因此通过系统方式安装的企业或代理 CA 会自动生效；找不到系统 CA 时回退到 `certifi`。macOS 会合并系统钥匙串证书作为本地开发兼容。

Debian / Ubuntu 先确认 CA 包已经安装：

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates
sudo update-ca-certificates
```

如果使用企业或代理 CA，可将 PEM 格式且扩展名为 `.crt` 的根证书加入系统信任库：

```bash
sudo cp company-root-ca.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
```

然后重启 WebUI，再重新发起扫码登录。

不希望修改系统信任库时，也可显式指定完整 CA bundle：

```bash
export BILIBILI_CA_BUNDLE=/absolute/path/to/company-ca-bundle.pem
python3 run.py
```

也兼容 `CURL_CA_BUNDLE`、`SSL_CERT_FILE` 和 `REQUESTS_CA_BUNDLE`。不要通过关闭 SSL 校验规避证书错误。

### 弹幕导入很慢

完整导入会按 0.6 秒间隔逐条发送。10,000 条弹幕理论上至少需要约 100 分钟，还可能受 B站限流影响。可以将 `danmaku_native_max_comments` 设为 `200` 或其他正整数。

### 想把 ASS 烧进视频

将 `danmaku_burn_in` 设为 `true`。这会重新编码整个视频，显著增加 CPU/GPU 占用和处理时间，因此默认关闭。

### 端口 5001 被占用

统一服务只使用一个端口，可通过 `PORT` 修改：

```bash
PORT=8080 ./y2a-auto/.venv/bin/python run.py
```

如果升级前运行过旧版，请停止旧的 `biliup server`；新版不会监听或连接 `19159`。

## 数据与安全

不要提交以下内容：

- B站、YouTube Cookie；
- OpenAI 或其他服务 API Key；
- `bridge.config.json`；
- `y2a-auto/config/`、`db/`、`logs/`、`recordings/`；
- `.bridge/` 状态库；
- 下载或录制的视频、XML、ASS 文件。

公开部署时请在设置中启用 Web 密码保护，并通过反向代理提供 HTTPS。当前 Flask 自带服务器适合本地使用，不建议直接暴露到公网。

## 上游版本与许可证

本仓库基于：

- biliup：`adf6a1c03be9f777a76c8c501038c27f3d90a097`，MIT License；
- Y2A-Auto：`4419498d365414f5cef6842c78d75f43b7172292`，GNU GPL v3。

定制源码分别保留在 `upstream-biliup/` 与 `y2a-auto/`，并保留各自的许可证文件。整合分发遵循 GNU GPL v3；使用时同时遵守直播平台、视频网站及内容版权的相关规则。
