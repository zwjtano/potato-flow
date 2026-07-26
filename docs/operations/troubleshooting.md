# 故障排查

## 容器持续重启

```bash
docker compose ps
docker inspect potato-flow --format '{{.RestartCount}}'
docker compose logs --tail=300 potato-flow
```

常见原因是发布文件损坏、Python 导入失败、挂载目录权限或端口冲突。可在源码目录运行：

```bash
python -m compileall -q y2a-auto bridge.py
```

## 页面打不开

```bash
ss -lntp | grep 5001
curl -v http://127.0.0.1:5001/api/version
sudo ufw allow 5001/tcp
```

确认访问的是 `http://服务器IP:5001/`，并检查云服务商安全组。

## B站登录态校验出现 curl 60

这是系统 CA 证书问题：

```bash
sudo apt-get update
sudo apt-get install --reinstall ca-certificates
sudo update-ca-certificates
docker compose restart
```

不要通过关闭证书校验绕过。

## 手动停止后没有进入下一步

1. 查看文件管理中是否仍为 `.part`。
2. 在任务页手动刷新。
3. 查看容器日志中的“安全收尾”“生成 ASS”和任务 ID。
4. 检查直播间是否开启了“仅录制不投稿”。

## AI 封面卡住

检查图像模型、API 额度、Base URL 是否支持图像接口。任务失败后会进入人工审核，可先使用直播间头像或本地封面继续投稿。

## 投稿失败

在任务详情展开“投稿 B站”，优先检查：

- B站 Cookie 是否有效
- 验证码/风控提示
- 标题、标签、分区是否合法
- 服务器到 B站上传节点的网络质量
- 磁盘文件是否仍存在且可读

