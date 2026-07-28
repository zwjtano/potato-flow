# 抖音录制

抖音沿用 biliup 的原生直播解析与弹幕采集方式，不依赖 Chromium 或 Playwright，也不提供扫码登录。

## 添加直播间

在“新增直播间”粘贴公开抖音直播间链接，PotatoFlow 会尝试识别主播昵称、头像、标题和真实房间号。

## Cookie（可选）

多数公开直播间无需 Cookie。遇到平台风控或必须登录的直播间时：

1. 在其他可信环境获取自己的抖音 Cookie。
2. 进入 **系统设置 → 账号与网络 → 平台账号**。
3. 上传 JSON 或纯文本 Cookie 文件。
4. 保存后，系统仅提取 biliup 要求的 `__ac_nonce`、`__ac_signature`
   和 `sessionid`，再写入 biliup 配置的 `user.douyin_cookie`。

浏览器导出的其他 Cookie 不会保存。缺少上述任一字段时，系统会拒绝导入并提示重新导出。

默认保存路径：

```text
y2a-auto/cookies/douyin_cookies.json
```

!!! danger "保护账号"
    Cookie 等同登录凭证。不要上传到 GitHub、发送到聊天或写入文档截图；账号异常时立即在平台退出所有设备并重新登录。

## 平台边界

- 抖音：直播检测、视频录制、XML 弹幕
- 哔哩哔哩：最终投稿
