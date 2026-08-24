# PotatoFlow AI 下一阶段任务

本文件把规划包中的任务看板落到代码仓库。状态以代码、测试和生产验证为准，不能只因建立接口就标记完成。

## 当前进行中

- [ ] **PF-AI-001｜AI / ASR 调用账本**
  - 已完成：`ai_runs` / `ai_requests` SQLite；逻辑调用与物理请求映射；重试、状态、延迟、Provider Request ID、Token、缓存 Token；录播任务聚合摘要；不保存提示词正文。
  - 已完成：OpenAI 客户端连接池，字幕翻译复用同一连接池。
  - 已完成：16:9 / 4:3 图片生成与编辑请求接入同一账本。
  - 已完成：通过 JSON/文件配置模型输入、缓存输入和输出单价；每次物理请求保存价格快照与美元成本。
  - 已完成：录播任务详情接口返回 AI 用量；保留天数可配置且默认关闭，启用后只清理已完成/失败记录，不删除运行中调用。
  - Exit gate：任一录播可回答文本、ASR、图片调用次数、Token、缓存命中、成本和失败原因。

- [x] **PF-ASR-001｜TranscriptDocument v1**
  - 已完成：版本化文档、Segment、Word、Speaker、置信度、Provider、模型、音频哈希字段；原子持久化与恢复读取。
  - 已完成：每个视频独立转录资产，避免多分P共用字幕。
  - 已完成：标准 SRT 原子导出；0.9 草稿 Schema 到 v1 的迁移与未来版本拒绝测试。
  - 已完成：现有 Whisper / Voxtral 最终字幕主流程同时原子写入私有 TranscriptDocument，不影响原 SRT 输出。
  - Exit gate：所有字幕/ASR Provider 都只向下游交付同一种文档。

- [ ] **PF-ASR-002｜SubtitleSourceResolver**
  - 已完成：同名 SRT、媒体内嵌字幕、持久化缓存、斗鱼 AI 字幕的优先级解析。
  - 已完成：内嵌字幕使用 ffprobe/ffmpeg，无浏览器、无音频转码。
  - 已完成：斗鱼录制中字幕缓存增量刷新；缓存/旁挂字幕损坏时非阻断回退。
  - 已完成：本地 WebVTT 旁挂字幕适配，支持带小时和不带小时的时间戳。
  - 已完成：来源质量评分写入 Transcript 元数据和任务诊断，但不推翻既定来源优先级。
  - 待完成：B站/YouTube 平台字幕适配；云 ASR fallback。
  - Exit gate：任意录播都能返回最佳 TranscriptDocument，或明确说明没有可用字幕源。

- [x] **PF-DAN-001｜弹幕降级为显著性和验证信号**
  - 已完成：存在 TranscriptDocument 时，字幕作为简介/标题主证据，弹幕只做术语、事件和观众反应交叉验证。
  - 已完成：Transcript Segment ID 和稳定弹幕 ID 进入 AI 输入与时间线回查；字幕主链路缺少 Evidence ID 时拒绝事件。
  - 已完成：弹幕显著性峰值使用独立的密度数据结构，不携带或确认事件事实。
  - 已完成：无字幕时单条弹幕不能直接生成事实，要求至少两条明确 Evidence ID 或同窗三条一致刷屏。
  - Exit gate：最终事实均能追溯字幕/GSI/视觉证据，弹幕不能单独确认事实。

## 待开始

- [ ] **PF-ASR-004｜腾讯云长录音 PoC（暂缓）**：如以后需要云端兜底，再按实际 Provider 直接设计，不预建统一异步框架。
- [ ] **PF-ASR-005｜OpenAI Diarization PoC（暂缓）**：分块、已知主播参考音频、说话人命名和标准化。
- [ ] **PF-ASR-006｜Speaker Profile（暂缓）**：参考音频、来源、哈希、质量、版本、启用状态和人工确认。
- [ ] **PF-EVAL-001｜10–20 段离线评测集**：单人、多人、音乐、游戏语音、稀疏弹幕、专有名词和长沉默。
- [ ] **PF-EVID-001｜Evidence Index + FTS5**：按全文、时间、说话人、来源和置信度检索。
- [ ] **PF-CODEX-001｜Codex EditorialPlan PoC**：一个 Job/Purpose 一个 Thread，只读 MCP，Structured Output，对照 Legacy。
- [ ] **PF-IMG-001｜Master Art 单图封面 PoC**：一次无文字生图，本地派生 16:9 / 4:3，程序化文字与 Logo。

## 当前验收基线

- 字幕来源顺序：同名人工字幕 → 已持久化 Transcript → 内嵌字幕 → 斗鱼 AI 字幕 → 旧链路回退。斗鱼缓存会用仍可读取的最新快照刷新，源暂时不可用时保留已持久化结果。
- 简介和标题：Transcript 优先，弹幕验证；无可用 Transcript 时保持兼容回退。
- 隐私：调用账本只保存摘要、指纹、用量和错误，不保存 Prompt、字幕正文、API Key 或音频。
- 恢复：Transcript 按视频原子持久化；调用账本使用 WAL、忙等待和并发安全的物理请求序号，并可在账本文件被替换后自动重建 Schema。
