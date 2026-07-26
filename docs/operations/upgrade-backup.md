# 更新、备份与恢复

## 更新前备份

先确认没有正在安全收尾的录制，再备份持久化目录和 Compose 配置：

```bash
docker compose ps
docker compose logs --tail=100 potato-flow

cp docker-compose.yml docker-compose.yml.bak
tar -czf potato-flow-data-$(date +%Y%m%d-%H%M).tar.gz docker-data
```

录播目录通常很大，建议使用快照、增量备份或 NAS 自带备份功能，不要每次完整打包。

## 更新

```bash
git pull --ff-only
docker compose build --no-cache potato-flow
docker compose up -d
```

验证：

```bash
docker compose ps
curl http://127.0.0.1:5001/api/version
docker compose logs --tail=200 potato-flow
```

## 回滚

回到之前验证过的 Git 标签或提交，再用原来的 Compose 和数据卷重建。不要删除：

- `docker-data/`
- `docker-data/recordings/` 或 `.env` 中指定的录播目录
- 自定义 `.env`

!!! warning
    不要使用 `git reset --hard` 处理包含本地配置修改的生产目录。优先保留备份并切换到明确的发布标签。
