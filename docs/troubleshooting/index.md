# 故障排查

<p class="docs-lead">从你看到的症状开始，而不是从内部模块名称开始。先完成对应检查，再根据日志和页面状态决定是否重启、重试或修改配置。</p>

## 快速定位

<div class="docs-card-grid">
  <a class="docs-card" href="#page-unreachable"><strong>页面打不开</strong><span>检查监听端口、本机接口、防火墙和云安全组。</span></a>
  <a class="docs-card" href="#container-restarting"><strong>容器持续重启</strong><span>检查重启次数、最近日志、目录权限和端口冲突。</span></a>
  <a class="docs-card" href="#recording-not-processing"><strong>录制没有进入处理</strong><span>确认 .part、安全收尾、任务状态和“仅录制”设置。</span></a>
  <a class="docs-card" href="#upload-failed"><strong>投稿失败</strong><span>核对 Cookie、风控提示、稿件字段、网络和源文件。</span></a>
</div>

## 页面打不开 {#page-unreachable}

```bash
ss -lntp | grep 5001
curl -v http://127.0.0.1:5001/api/version
sudo ufw allow 5001/tcp
```

确认访问的是 `http://服务器IP:5001/`，并检查云服务商安全组是否允许对应端口。

## 容器持续重启 {#container-restarting}

```bash
docker compose ps
docker inspect potato-flow --format '{{.RestartCount}}'
docker compose logs --tail=300 potato-flow
```

常见原因是发布文件损坏、Python 导入失败、挂载目录权限或端口冲突。源码环境可以进一步运行：

```bash
python -m compileall -q potatoflow-app
```

## B站登录态校验出现 curl 60

这是系统 CA 证书问题：

```bash
sudo apt-get update
sudo apt-get install --reinstall ca-certificates
sudo update-ca-certificates
docker compose restart
```

不要通过关闭证书校验绕过。

## 录制停止后没有进入处理 {#recording-not-processing}

1. 查看文件管理中是否仍为 `.part`。
2. 在任务页手动刷新。
3. 查看容器日志中的“安全收尾”“生成 ASS”和任务 ID。
4. 检查直播间是否开启了“仅录制不投稿”。

## AI 封面卡住

检查图像模型、API 额度、Base URL 是否支持图像接口。任务失败后会进入人工审核，可先使用直播间头像或本地封面继续投稿。

## 投稿失败 {#upload-failed}

在任务详情展开“投稿 B站”，优先检查：

- B站 Cookie 是否有效。
- 是否出现验证码或风控提示。
- 标题、标签和分区是否合法。
- 服务器到 B站上传节点的网络质量。
- 磁盘文件是否仍存在且可读。

## 常见问题

### 重启容器会删除录播吗？

不会。正常重启不会删除持久化目录和录播文件；手动删除 `docker-data/` 或录播目录才会造成数据丢失。

### 失败任务可以重试吗？

可以。任务进入人工审核后，可修改稿件信息并重试。重试前先确认源视频、字幕与封面仍然存在。

### 抖音录制后可以自动投稿到抖音吗？

不可以。抖音当前用于直播检测、录制和弹幕采集，投稿目标仍为哔哩哔哩。
