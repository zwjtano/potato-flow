# 直播录制

直播录制页负责直播间、录制引擎和当前生成文件；已结束的任务统一进入“上传任务”。

<div class="screenshot">
  <img src="../../assets/screenshots/live-recording.png" alt="直播录制页面">
  <div class="shot-caption">每张直播间卡片显示主播、平台、直播状态、录制状态和当前文件。</div>
</div>

## 添加直播间

1. 点击 **新增直播间**。
2. 粘贴完整房间链接。
3. 等待系统识别平台、主播名、头像、标题和真实房间号。
4. 检查识别结果后保存。

支持：

```text
https://live.bilibili.com/123456
https://www.douyu.com/9999
https://live.douyin.com/123456
```

斗鱼靓号会解析成平台真实房间号，但界面仍保留你熟悉的直播间入口。

## 手动开始与停止

- 录制引擎开启后会持续检测房间。
- 单个直播间可随时手动开始录制。
- 手动停止会先安全收尾当前视频与 XML，再把该文件送入 ASS、AI 和投稿流程。
- “仅录制不投稿”开启时，只保存录播和弹幕，不创建上传任务。

## 单直播间设置

<div class="screenshot">
  <img src="../../assets/screenshots/recording-settings.png" alt="直播间录制设置">
  <div class="shot-caption">分段、分P和仅录制均按直播间保存，录制中修改会在当前分段安全结束后生效。</div>
</div>

每个直播间可独立设置：

- 是否分段（默认开启）
- 分段时长（默认 60 分钟，范围 1–1440）
- 是否合并为分P（默认关闭）
- 是否仅录制不投稿（默认关闭）
