# 快速开始

<p class="docs-lead">用最短路径完成安装、账号连接和第一条直播间配置。完成本节后，你应该能够打开 WebUI，并看到直播间进入正常检测状态。</p>

<figure class="local-ui-shot">
  <img src="../assets/screenshots/local-overview-v1619.webp" alt="部署成功后的本地 PotatoFlow 工作台">
  <figcaption>部署成功后，打开本地 5001 应看到完整工作台。图中人物、直播间与任务均为虚构。</figcaption>
</figure>

## 推荐路径

<div class="task-table">
  <div class="task-row"><strong>准备服务器</strong><span>确认 Linux、Docker、磁盘和网络条件</span><a href="docker/">查看要求 →</a></div>
  <div class="task-row"><strong>启动服务</strong><span>运行安装脚本并通过版本与容器检查</span><a href="docker/">Docker 安装 →</a></div>
  <div class="task-row"><strong>完成配置</strong><span>连接 B站、配置 AI、添加第一个直播间</span><a href="first-run/">首次配置 →</a></div>
</div>

## 完成标准

- 浏览器可以访问 `http://服务器IP:5001/`。
- `docker compose ps` 显示容器持续运行，没有重启循环。
- B站登录态校验通过。
- 至少一个直播间已经保存并处于检测状态。

!!! tip "国内服务器"
    安装脚本默认使用国内 Docker、Debian、PyPI、PyTorch、Cargo 与 GitHub 下载源；境外服务器可以关闭国内镜像选项。
