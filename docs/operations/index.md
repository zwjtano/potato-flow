# 部署运维

<p class="docs-lead">面向已经运行的 PotatoFlow 服务。运维文档按“检查—变更—验证—恢复”组织，与配置字段参考和故障症状分开。</p>

## 运维任务

<div class="task-table">
  <div class="task-row"><strong>例行检查</strong><span>确认容器、版本、录制任务和磁盘空间正常</span><a href="#routine-check">查看清单 →</a></div>
  <div class="task-row"><strong>更新版本</strong><span>在没有活动录制时备份、更新并验证服务</span><a href="upgrade-backup/">执行更新 →</a></div>
  <div class="task-row"><strong>备份恢复</strong><span>保护 docker-data、录播目录、Compose 与环境配置</span><a href="upgrade-backup/">备份与恢复 →</a></div>
  <div class="task-row"><strong>出现异常</strong><span>不要在运维页猜原因，按可见症状进入排障</span><a href="../troubleshooting/">故障排查 →</a></div>
</div>

## 例行检查清单 {#routine-check}

```bash
docker compose ps
curl http://127.0.0.1:5001/api/version
docker compose logs --tail=100 potato-flow
df -h
```

检查时应确认：容器没有重启循环、版本接口可访问、没有持续错误日志、录播盘剩余空间充足。

!!! warning "先确认录制状态"
    更新、重启或迁移前，先确认没有活动 `.part` 文件、录制进程或正在收尾的处理任务。
