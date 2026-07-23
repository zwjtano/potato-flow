# PotatoFlow（土豆录播姬）

土豆的直播录制与 AI 投稿流水线。PotatoFlow 把 [biliup](https://github.com/biliup/biliup) 的直播录制能力与 [Y2A-Auto](https://github.com/fqscfqj/Y2A-Auto) 的下载、AI 处理和投稿能力整合进同一个 WebUI。

本项目的边界很明确：

- 录制平台仅保留 **哔哩哔哩直播**和**斗鱼**；
- 投稿平台仅使用 **哔哩哔哩**；
- 保留 Y2A 的 YouTube 下载、频道监控、字幕、翻译和 AI 功能；
- 每个录制分段完成后立即生成 ASS、根据弹幕生成 AI 简介并投稿，不必等待下播；
- 同一场直播逐文件投稿：P1 创建稿件，后续录好的文件自动追加为同一 BVID 的 P2、P3；
- 默认不烧录弹幕，直接上传原视频，避免额外转码时间和性能消耗；
- ASS 作为独立文件保留，可在文件管理中查看和下载，不导入 B站原生弹幕；
- Linux 上只运行一个主服务、只开放一个 Web 端口；biliup 以无 HTTP 的内部 worker 运行。

## 工作流程

```text
B站直播 / 斗鱼
        ↓
biliup 录制视频 + XML 弹幕（每 1 小时自动分段）
        ↓
每个分段落盘后立即处理，录制继续
        ↓
生成 ASS ──→ AI 读取弹幕并生成投稿简介
        ↓
P1 创建 B站稿件；P2、P3…追加到同一个稿件
```

ASS 会保存在 `.bridge/artifacts/` 中供归档、查看和下载，不会烧进视频，也不会逐条导入 B站原生弹幕。

## 主要功能

### 直播录制

- 统一管理 B站、斗鱼直播间；
- 添加直播间时只需粘贴链接，自动识别平台、真实房间号、主播名称和头像；
- 一键启动或停止内置 biliup 录制引擎；
- 搜索直播间并按“监控中 / 已停止”筛选；
- 录制 B站与斗鱼 XML 弹幕；
- 每满 1 小时自动结束当前录制分段并立即触发上传流水线，后续录制不受影响；手动停止时，不足 1 小时的最后一段也会正常处理；
- 同一场直播的首个文件创建 B站稿件，后续文件依次追加为该稿件的分P；下播后关闭本场分P会话；
- 每个录播文件都有独立的五阶段记录，可手动查看状态、时间、输入、产物和错误；
- 历史任务可从下拉框切换，失败任务可在对应步骤详情中一键重试；
- 录播任务会同步显示在“概览”和“上传任务”页，与 YouTube / 手动任务统一查看；
- 页面内置录播文件管理，统一查看视频、XML 弹幕和 ASS 字幕，并支持搜索、筛选与下载；
- 文件管理支持多选、全选筛选结果和批量删除；正在录制或流水线处理中的文件会自动锁定；
- 投稿完成后，自动删除源视频和 XML，ASS 与任务日志继续保留；
- WebUI 显示真实 biliup 进程状态，原始日志中的签名参数会自动隐藏。

### 弹幕处理

- XML 转 ASS，保留原视频，不执行烧录；
- AI 会对弹幕去重、抽样并生成有依据的投稿简介；
- 录播沿用 Y2A 设置中的“自动生成标签”和“自动推荐分区”；推荐分区时可将当前分段自动截取的原视频封面一并交给支持图片输入的 AI；
- 同一场直播只在 P1 执行一次标签与分区推荐，后续分P沿用同一组投稿元数据；
- 可开启“AI 生成录播封面”：系统先根据弹幕生成核心主题和最终标题，再调用 `gpt-image-2` 生成 16:10 投稿封面；日期、年份、具体时间、时间戳和房间号会从封面主题与提示词中排除；
- AI 封面只在 P1 生成一次，后续分P复用首个封面；生成失败会自动使用视频截图继续投稿；
- ASS 可在录播文件管理中查看和下载；
- 不执行耗时且容易触发限流的 B站原生弹幕逐条导入。

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

### Docker 安装（推荐）

需要 Docker Engine 24+ 和 Docker Compose v2。仓库根目录只定义一个容器，容器内的 biliup 是无 HTTP 端口的子进程，对外只映射 `5001`。

可以从 [GitHub Releases](https://github.com/zwjtano/potato-flow/releases/latest) 下载最新版本的 **Source code (tar.gz)**，解压后进入项目目录运行：

```bash
tar -xzf potato-flow-*.tar.gz
cd potato-flow-*/
docker compose up -d --build
```

也可以直接克隆仓库：

```bash
git clone https://github.com/zwjtano/potato-flow.git
cd potato-flow
docker compose up -d --build
```

启动后打开 `http://服务器IP:5001/`。容器支持 AMD64 和 ARM64 原生构建。首次会编译 Rust 录制核心并安装 AI 依赖，之后会直接使用本地镜像。

常用命令：

```bash
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
docker compose up -d --build   # 更新源码后重建
```

所有需要保留的数据位于仓库根目录的 `docker-data/`，包括录制房间、Cookie、授权数据、任务数据库、日志、录播文件、ASS 弹幕和 AI 处理状态。重建或删除容器不会删除此目录，不要将它提交到 Git。

### 查看录播处理的每一步

打开“直播录制”，选择直播间后，在“录播处理流水线”的“录播任务”下拉框中选择一个录播文件。六个步骤都可以点击：

1. **直播检测**：查看检测完成时间；
2. **视频录制**：查看原始录播文件、大小和完成时间；
3. **生成 ASS**：查看 XML、ASS 路径、弹幕数量以及是否烧录（默认否）；
4. **AI 简介**：查看参与分析的弹幕数量、核心主题、最终标题、简介、标签和推荐分区；
5. **AI 封面**：直接预览 `gpt-image-2` 生成的封面，并查看无日期时间标题、完整提示词、模型及回退情况；
6. **投稿 B站**：查看最终标题、封面和 BVID。

阶段状态和产物写入 `.bridge/state.sqlite3`（Docker 中持久化到 `docker-data/bridge/state.sqlite3`）。处理中刷新页面或重启容器不会丢失记录。某一步失败后，详情中会显示原始错误和“重试失败任务”按钮。

如果之前安装过 systemd 版本，需先释放 `5001` 端口：

```bash
sudo systemctl disable --now biliup-y2a
docker compose up -d --build
```

### 原生 Linux 安装

#### 1. 获取源码

```bash
git clone https://github.com/zwjtano/potato-flow.git
cd potato-flow
```

#### 2. Linux 一键安装

安装脚本会安装系统依赖、创建 Python 虚拟环境并构建无端口录制 worker：

```bash
./scripts/install-linux.sh
```

ARM64 Linux 请使用安装脚本在目标机器原生构建。

安装完成后可直接启动：

```bash
./y2a-auto/.venv/bin/python run.py
```

#### 3. 手动安装（Ubuntu / Debian）

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

#### 4. 构建定制 biliup

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

#### 5. 安装 Python 依赖

```bash
cd y2a-auto
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cd ..
```

#### 6. 创建桥接配置

```bash
cp bridge.config.example.json bridge.config.json
```

默认配置已经启用：

```json
{
  "title_template": "【直播回放】{streamer}｜{ai_topic}｜{date}",
  "danmaku_enabled": true,
  "danmaku_burn_in": false,
  "delete_recording_after_upload": true,
  "ai_danmaku_summary_enabled": true
}
```

默认投稿标题为 `【直播回放】{streamer}｜{ai_topic}｜{date}`。其中 `{streamer}` 是主播名，`{ai_topic}` 是 AI 根据弹幕生成的核心主题，`{date}` 是录制日期；AI 不可用或没有有效弹幕时，核心主题会回退为直播标题。

`delete_recording_after_upload` 默认开启：视频投稿完成后删除源视频与 XML；如需保留原文件，将它设为 `false`。生成的 ASS 与任务日志不会删除。

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

脚本会根据当前项目路径和用户生成 `potato-flow.service`。systemd 只管理一个主服务，并通过 `KillMode=control-group` 管理其内部录制 worker。

```bash
sudo systemctl status potato-flow
journalctl -u potato-flow -f
sudo systemctl restart potato-flow
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

进入“直播录制” → “新增直播间”，只需粘贴以下任一格式的直播间链接：

```text
https://live.bilibili.com/123456
https://www.douyu.com/123456
```

系统会先显示识别到的平台、真实房间号、主播名称、头像和当前直播标题，确认识别成功后即可添加，不需要手动填写主播名称。

添加完成后点击录制引擎的播放按钮。检测到开播后，biliup 会自动录制并每 1 小时分段。每个分段落盘后会立即进入 ASS、AI 简介和投稿流程：第一个文件创建稿件，之后录好的文件逐个追加为同一稿件的 P2、P3，无需等待整场直播结束。手动停止时，不足 1 小时的最后一段也会进入流水线；下播后系统会关闭本场分P会话，主播下次开播时创建新稿件。

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

状态保存在 `.bridge/state.sqlite3`。如果上传进程在得到 BVID 后意外中断，已记录的投稿结果可避免重复投稿。

## 项目结构

```text
.
├── run.py                         # 统一启动入口
├── bridge.py                      # biliup → Y2A 桥接器
├── danmaku_pipeline.py            # XML、ASS 与 AI 弹幕摘要
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
