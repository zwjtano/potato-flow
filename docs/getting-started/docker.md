# Docker 安装

Docker 是 PotatoFlow 在 Linux 上的推荐安装方式。仓库只启动一个容器，对外只开放 `5001`。

## 环境要求

- 64 位 Linux（推荐 Ubuntu 22.04/24.04 或 Debian 12/13）
- Docker Engine 24 或更高版本
- Docker Compose v2
- 至少 8 GB 可用磁盘空间用于首次构建
- 能访问直播平台、哔哩哔哩和你选择的 AI API

## 安装

```bash
git clone https://github.com/zwjtano/potato-flow.git
cd potato-flow

sudo mkdir -p "/vol1/1000/media/录播"
sudo chown 1000:1000 "/vol1/1000/media/录播"

docker compose up -d --build
```

浏览器打开：

```text
http://服务器IP:5001/
```

## 修改录播目录

默认宿主机目录是 `/vol1/1000/media/录播`。要换目录，在项目根目录创建 `.env`：

```dotenv
POTATO_RECORDINGS_DIR=/你的录播目录
```

然后重建：

```bash
docker compose up -d --build
```

## 检查状态

```bash
docker compose ps
docker compose logs --tail=200 potato-flow
curl http://127.0.0.1:5001/api/version
```

正常时版本接口会返回当前版本，容器状态持续为 `Up`，且没有重启循环。

!!! warning "不要删除持久化目录"
    `docker-data/` 保存 Cookie、AI 设置、直播间、任务数据库和日志；录播文件保存在 `POTATO_RECORDINGS_DIR`。删除容器不会删除它们，但手动删除目录会丢失数据。

