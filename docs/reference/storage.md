# 存储结构

## 宿主机

```text
potato-flow/
├── docker-data/                  # Cookie、数据库、配置、日志、AI 产物
│   └── recordings/               # 默认录播视频、XML 与 ASS
├── docker-compose.yml
├── .env.example
└── .env                          # 可选：自定义宿主机录播目录

docker-data/recordings/
└── 主播名_直播间标题_YYYY-MM-DD_HH-MM/
    ├── 主播名_直播间标题_YYYY-MM-DD_HH-MM.flv
    ├── 主播名_直播间标题_YYYY-MM-DD_HH-MM.xml
    └── 主播名_直播间标题_YYYY-MM-DD_HH-MM.ass
```

## 容器内挂载

| 宿主机 | 容器内 | 内容 |
|---|---|---|
| `./docker-data` | `/data` | 持久化应用数据 |
| `${POTATO_RECORDINGS_DIR}` | `/data/recordings` | 视频、XML、ASS |

## 命名规则

每场直播创建独立文件夹：

```text
主播名 + 直播间标题 + 直播开始时间
```

同一场直播的分段放在同一个文件夹内。文件名不应暴露内部房间哈希；历史任务中的短哈希仅用于兼容旧数据。
