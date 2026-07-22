#!/usr/bin/env python3
"""Bridge finalized biliup segments to Y2A-Auto uploaders."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bilibili_danmaku_importer import BilibiliDanmakuImporter
from danmaku_pipeline import (
    build_ass,
    burn_ass,
    format_comments_for_ai,
    parse_biliup_xml,
    probe_video_size,
    select_summary_comments,
)
from runtime_environment import configure_linux_ca_environment

VIDEO_EXTENSIONS = {".mp4", ".flv", ".mkv", ".webm", ".ts", ".m2ts", ".mov"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    with path.open("r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    if not isinstance(cfg, dict):
        raise ValueError("配置文件根节点必须是 JSON object")
    cfg["_config_dir"] = str(path.parent)
    return cfg


def resolve_path(value: str | os.PathLike[str], cfg: dict[str, Any]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(cfg["_config_dir"]) / path
    return path.resolve()


def effective_config(base: dict[str, Any], video: Path) -> dict[str, Any]:
    cfg = dict(base)
    cfg.pop("profiles", None)
    for profile in base.get("profiles", []) or []:
        if isinstance(profile, dict) and fnmatch.fnmatch(video.name, str(profile.get("match", ""))):
            cfg.update({key: value for key, value in profile.items() if key != "match"})
            break
    return cfg


def stdin_paths() -> list[Path]:
    if sys.stdin.isatty():
        return []
    return [Path(line.strip()).expanduser() for line in sys.stdin if line.strip()]


def input_paths(values: list[str], include_stdin: bool = True) -> list[Path]:
    raw = [Path(value).expanduser() for value in values]
    if include_stdin:
        raw.extend(stdin_paths())
    result: list[Path] = []
    seen: set[Path] = set()
    for path in raw:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def video_paths(values: list[str], include_stdin: bool = True) -> list[Path]:
    return [path for path in input_paths(values, include_stdin) if path.suffix.lower() in VIDEO_EXTENSIONS]


def find_danmaku_xml(video: Path, paths: list[Path] | None = None) -> Path | None:
    candidates = [path for path in (paths or []) if path.suffix.lower() == ".xml"]
    candidates.extend((video.with_suffix(".xml"), video.parent / "danmaku" / f"{video.stem}.xml"))
    for candidate in candidates:
        if candidate.stem == video.stem and candidate.is_file():
            return candidate.resolve()
    return None


def wait_until_stable(path: Path, checks: int, interval: float) -> None:
    previous: tuple[int, int] | None = None
    stable = 0
    while stable < max(1, checks):
        stat = path.stat()
        current = (stat.st_size, stat.st_mtime_ns)
        if stat.st_size <= 0:
            stable = 0
        elif current == previous:
            stable += 1
        else:
            stable = 0
        previous = current
        if stable < max(1, checks):
            time.sleep(max(0.1, interval))


def fingerprint(path: Path, sidecar: Path | None = None) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            handle.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    if sidecar and sidecar.is_file():
        digest.update(sidecar.read_bytes())
    return digest.hexdigest()


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS uploads (
                    fingerprint TEXT PRIMARY KEY,
                    video_path TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS upload_stages (
                    fingerprint TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT,
                    error TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (fingerprint, stage),
                    FOREIGN KEY (fingerprint) REFERENCES uploads(fingerprint)
                )"""
            )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def claim(self, key: str, path: Path, platform: str, retry: bool = False) -> bool:
        now = utc_now()
        with self.connect() as db:
            # Serialize the read/claim pair. Multiple biliup workers may finish
            # segments at nearly the same instant.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM uploads WHERE fingerprint = ?", (key,)).fetchone()
            if row and row["status"] == "completed":
                return False
            if row and row["status"] == "processing" and not retry:
                return False
            db.execute(
                """INSERT INTO uploads
                   (fingerprint, video_path, platform, status, attempts, created_at, updated_at)
                   VALUES (?, ?, ?, 'processing', 1, ?, ?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                     video_path=excluded.video_path, platform=excluded.platform,
                     status='processing', attempts=uploads.attempts + 1,
                     error=NULL, updated_at=excluded.updated_at""",
                (key, str(path), platform, now, now),
            )
            for stage, status in (("detect", "completed"), ("record", "completed"),
                                  ("ass", "pending"), ("ai", "pending"), ("upload", "pending")):
                db.execute(
                    """INSERT INTO upload_stages
                       (fingerprint, stage, status, updated_at, started_at, finished_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(fingerprint, stage) DO UPDATE SET
                         status=CASE WHEN excluded.stage IN ('detect', 'record') THEN 'completed'
                                     WHEN upload_stages.status='completed' THEN upload_stages.status
                                     ELSE excluded.status END,
                         error=NULL, updated_at=excluded.updated_at""",
                    (key, stage, status, now, now if status == "completed" else None,
                     now if status == "completed" else None),
                )
            db.execute(
                """UPDATE upload_stages SET details_json=?
                   WHERE fingerprint=? AND stage='record'""",
                (json.dumps({"video_path": str(path), "size_bytes": path.stat().st_size}, ensure_ascii=False), key),
            )
        return True

    def stage(self, key: str, stage: str, status: str, details: Any = None,
              error: str | None = None) -> None:
        now = utc_now()
        started_at = now if status == "running" else None
        finished_at = now if status in {"completed", "failed", "skipped"} else None
        with self.connect() as db:
            db.execute(
                """INSERT INTO upload_stages
                   (fingerprint, stage, status, details_json, error, started_at, finished_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(fingerprint, stage) DO UPDATE SET
                     status=excluded.status,
                     details_json=COALESCE(excluded.details_json, upload_stages.details_json),
                     error=excluded.error,
                     started_at=CASE WHEN excluded.status='running' THEN excluded.started_at
                                     ELSE upload_stages.started_at END,
                     finished_at=excluded.finished_at,
                     updated_at=excluded.updated_at""",
                (key, stage, status,
                 json.dumps(details, ensure_ascii=False, default=str) if details is not None else None,
                 error, started_at, finished_at, now),
            )

    def finish(self, key: str, status: str, result: Any = None, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET status=?, result_json=COALESCE(?, result_json), error=?, updated_at=? WHERE fingerprint=?",
                (status, json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                 error, utc_now(), key),
            )

    def results(self, key: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT result_json FROM uploads WHERE fingerprint=?", (key,)).fetchone()
        if not row or not row["result_json"]:
            return {}
        try:
            value = json.loads(row["result_json"])
            return value if isinstance(value, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def failed_paths(self) -> list[Path]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT video_path FROM uploads WHERE status='failed' ORDER BY updated_at"
            ).fetchall()
        return [Path(row["video_path"]) for row in rows]

    def recent(self, limit: int = 30) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM uploads ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()


def find_cover(video: Path, cfg: dict[str, Any], work_dir: Path) -> Path:
    configured = str(cfg.get("cover_path", "")).strip()
    if configured:
        cover = resolve_path(configured, cfg)
        if not cover.is_file():
            raise FileNotFoundError(f"封面不存在: {cover}")
        return cover

    candidates: list[Path] = []
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidates.extend((video.with_suffix(ext), video.parent / "cover" / f"{video.stem}{ext}"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    work_dir.mkdir(parents=True, exist_ok=True)
    cover = work_dir / "cover.jpg"
    ffmpeg = str(cfg.get("ffmpeg", "ffmpeg"))
    seek = str(max(0, int(cfg.get("cover_seek_seconds", 10))))
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", seek,
               "-i", str(video), "-frames:v", "1", "-q:v", "2", str(cover)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if completed.returncode != 0 or not cover.is_file():
        message = completed.stderr.strip()[-1000:]
        raise RuntimeError(f"FFmpeg 自动截取封面失败: {message}")
    return cover


def render_metadata(video: Path, cfg: dict[str, Any]) -> tuple[str, str, list[str]]:
    values = {"stem": video.stem, "name": video.name, "suffix": video.suffix.lstrip(".")}
    title = str(cfg.get("title_template", "{stem}")).format_map(values).strip()
    description = str(cfg.get("description_template", "{stem}")).format_map(values).strip()
    tags = [str(tag).strip() for tag in cfg.get("tags", []) if str(tag).strip()]
    if not title:
        raise ValueError("渲染后的标题为空")
    return title, description, tags


def import_y2a(cfg: dict[str, Any]):
    root = resolve_path(str(cfg.get("y2a_root", "y2a-auto")), cfg)
    if not (root / "modules").is_dir():
        raise FileNotFoundError(f"Y2A 目录无效: {root}")
    sys.path.insert(0, str(root))
    from modules.bilibili_uploader import BilibiliUploader  # type: ignore
    from modules.config_manager import load_config as load_y2a_config  # type: ignore
    return BilibiliUploader, load_y2a_config


def summarize_danmaku_with_ai(comments, base_description: str, cfg: dict[str, Any]) -> str:
    """Generate a grounded description with Y2A's existing OpenAI-compatible client."""
    if not comments or not bool(cfg.get("ai_danmaku_summary_enabled", True)):
        return base_description
    try:
        root = resolve_path(str(cfg.get("y2a_root", "y2a-auto")), cfg)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from modules.ai_enhancer import get_openai_client, _request_json_object  # type: ignore
        from modules.config_manager import load_config as load_y2a_config  # type: ignore

        ai_cfg = load_y2a_config()
        if not ai_cfg.get("OPENAI_API_KEY"):
            print("WARN 未配置 Y2A OPENAI_API_KEY，跳过弹幕 AI 简介", file=sys.stderr)
            return base_description
        selected = select_summary_comments(comments, int(cfg.get("ai_danmaku_max_comments", 400)))
        payload = {
            "base_description": base_description,
            "comment_count": len(comments),
            "sampled_comments": format_comments_for_ai(selected),
        }
        system_prompt = str(cfg.get("ai_danmaku_prompt") or """
你是直播录播编辑。根据按时间采样的观众弹幕，为哔哩哔哩录播生成简洁中文简介。
只能总结弹幕能支持的主题、高潮时刻和观众反应，不得虚构主播说过的话或未出现的事件。
不要引用用户名、UID、广告或重复刷屏。保留 base_description 中有用的基础信息。
返回 JSON 对象：{"description":"..."}，description 不超过 1200 个中文字符。
""").strip()
        result = _request_json_object(
            client=get_openai_client(ai_cfg),
            model_name=str(ai_cfg.get("OPENAI_MODEL_NAME", "gpt-4o-mini")),
            system_prompt=system_prompt,
            payload=payload,
            max_tokens=900,
            temperature=0.2,
            thinking_enabled=bool(ai_cfg.get("OPENAI_THINKING_ENABLED", False)),
            logger_obj=None,
            scene_name="biliup_danmaku_summary",
        )
        description = str((result or {}).get("description", "")).strip()
        return description[:1800] if description else base_description
    except Exception as exc:
        print(f"WARN 弹幕 AI 简介生成失败，使用原简介: {exc}", file=sys.stderr)
        return base_description


def upload_one(video: Path, base_cfg: dict[str, Any], store: StateStore,
               dry_run: bool = False, retry: bool = False,
               danmaku_xml: Path | None = None) -> bool:
    cfg = effective_config(base_cfg, video)
    platform = "bilibili"
    wait_until_stable(video, int(cfg.get("stable_checks", 2)), float(cfg.get("stable_interval_seconds", 2)))
    danmaku_xml = danmaku_xml or find_danmaku_xml(video)
    key = fingerprint(video, danmaku_xml)
    if not store.claim(key, video, platform, retry=retry):
        print(f"SKIP 已处理或正在处理: {video}")
        return True

    work_dir = store.path.parent / "artifacts" / key[:16]
    current_stage = "ass"
    try:
        title, description, tags = render_metadata(video, cfg)
        cover = find_cover(video, cfg, work_dir)
        source_url = str(cfg.get("source_url", "")).strip()
        if not source_url:
            raise ValueError("Y2A 的 bilibili 上传强制使用转载模式，必须配置 source_url")

        upload_video = video
        ass_path = None
        comments = []
        store.stage(key, "ass", "running", {"danmaku_xml": str(danmaku_xml) if danmaku_xml else None})
        if danmaku_xml and bool(cfg.get("danmaku_enabled", True)):
            comments = parse_biliup_xml(danmaku_xml)
            if comments:
                width, height = probe_video_size(video, str(cfg.get("ffprobe", "ffprobe")))
                ass_path = build_ass(
                    comments,
                    work_dir / f"{video.stem}.ass",
                    width=width,
                    height=height,
                    font_name=str(cfg.get("danmaku_font_name", "Noto Sans CJK SC")),
                    font_size=int(cfg.get("danmaku_font_size", 42)),
                    duration=float(cfg.get("danmaku_duration_seconds", 9)),
                    opacity=float(cfg.get("danmaku_opacity", 0.92)),
                )
                if bool(cfg.get("danmaku_burn_in", False)) and not dry_run:
                    upload_video = burn_ass(
                        video,
                        ass_path,
                        work_dir / f"{video.stem}.danmaku.mp4",
                        ffmpeg=str(cfg.get("ffmpeg", "ffmpeg")),
                        fonts_dir=resolve_path(
                            str(cfg.get("danmaku_fonts_dir", "y2a-auto/fonts")), cfg
                        ),
                        preset=str(cfg.get("danmaku_encode_preset", "medium")),
                        crf=int(cfg.get("danmaku_encode_crf", 20)),
                    )
                store.stage(key, "ass", "completed", {
                    "danmaku_xml": str(danmaku_xml), "ass_path": str(ass_path),
                    "danmaku_count": len(comments), "burn_in": bool(cfg.get("danmaku_burn_in", False)),
                })
            else:
                print(f"WARN 弹幕 XML 中没有可用弹幕: {danmaku_xml}", file=sys.stderr)
                store.stage(key, "ass", "skipped", {"danmaku_xml": str(danmaku_xml), "reason": "XML 中没有可用弹幕"})
        else:
            store.stage(key, "ass", "skipped", {"reason": "未找到弹幕 XML 或弹幕处理未启用"})

        current_stage = "ai"
        if comments and not dry_run and bool(cfg.get("ai_danmaku_summary_enabled", True)):
            store.stage(key, "ai", "running", {"comment_count": len(comments)})
            description = summarize_danmaku_with_ai(comments, description, cfg)
            store.stage(key, "ai", "completed", {"description": description, "comment_count": len(comments)})
        else:
            reason = "试运行" if dry_run else ("未配置可分析弹幕" if not comments else "AI 简介未启用")
            store.stage(key, "ai", "skipped", {"reason": reason, "description": description})

        summary = {"video": str(video), "upload_video": str(upload_video),
                   "danmaku_xml": str(danmaku_xml) if danmaku_xml else None,
                   "ass_path": str(ass_path) if ass_path else None,
                   "danmaku_count": len(comments), "cover": str(cover), "platform": platform,
                   "title": title, "description": description, "tags": tags, "source_url": source_url}
        if dry_run:
            store.stage(key, "upload", "skipped", {"reason": "试运行未投稿"})
            store.finish(key, "dry_run", summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return True

        current_stage = "upload"
        store.stage(key, "upload", "running", {"title": title, "cover": str(cover)})
        BilibiliUploader, _ = import_y2a(cfg)
        cookie = resolve_path(str(cfg.get("bilibili_cookies", "")), cfg)
        partition = str(cfg.get("bilibili_partition_id", "")).strip()
        if not cookie.is_file() or not partition:
            raise ValueError("bilibili 需要有效的 bilibili_cookies 和 bilibili_partition_id")
        previous = store.results(key)
        result = previous.get("bilibili")
        if not isinstance(result, dict) or not result.get("bvid"):
            uploader = BilibiliUploader(cookie_file=str(cookie))
            ok, result = uploader.upload_video(
                video_file_path=str(upload_video), cover_file_path=str(cover), title=title,
                description=description, tags=tags, partition_id=partition,
                youtube_url=source_url, task_id=None,
            )
            if not ok:
                raise RuntimeError(f"bilibili 上传失败: {result}")
            previous.update({"bilibili": result, "ass_path": str(ass_path) if ass_path else None})
            # Persist the BVID before importing danmaku. If the latter fails,
            # retry must not create a duplicate video submission.
            store.finish(key, "video_uploaded", previous)

        if (
            comments
            and bool(cfg.get("danmaku_native_import", True))
            and not previous.get("danmaku_import")
        ):
            root = resolve_path(str(cfg.get("y2a_root", "y2a-auto")), cfg)
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            from modules.bilibili_auth import load_credential_from_file  # type: ignore

            importer = BilibiliDanmakuImporter(load_credential_from_file(str(cookie)))
            imported = importer.import_comments(
                str(result["bvid"]),
                comments,
                max_comments=int(cfg.get("danmaku_native_max_comments", 0)),
                interval_seconds=float(cfg.get("danmaku_native_interval_seconds", 0.6)),
                cid_wait_seconds=int(cfg.get("danmaku_cid_wait_seconds", 300)),
            )
            previous["danmaku_import"] = {
                "cid": imported.cid,
                "requested": imported.requested,
                "imported": imported.imported,
                "skipped": imported.skipped,
            }

        store.stage(key, "upload", "completed", {
            "title": title, "description": description, "cover": str(cover),
            "bilibili": previous.get("bilibili"),
            "danmaku_import": previous.get("danmaku_import"),
        })
        store.finish(key, "completed", previous)
        print(f"OK 上传完成: {video}")
        return True
    except Exception as exc:
        store.stage(key, current_stage, "failed", error=str(exc))
        store.finish(key, "failed", error=str(exc))
        print(f"ERROR {video}: {exc}", file=sys.stderr)
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将 biliup 录制产物交给 Y2A-Auto 上传")
    parser.add_argument("--config", default="bridge.config.json", help="JSON 配置文件")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="处理参数或 stdin 中的视频路径")
    ingest.add_argument("paths", nargs="*")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--retry", action="store_true", help="允许重试指定的失败任务")
    sub.add_parser("retry", help="重试失败记录")
    status = sub.add_parser("status", help="显示最近记录")
    status.add_argument("--limit", type=int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_linux_ca_environment()
    args = build_parser().parse_args(argv)
    cfg = load_config(Path(args.config))
    state_path = resolve_path(str(cfg.get("state_db", ".bridge/state.sqlite3")), cfg)
    store = StateStore(state_path)

    if args.command == "status":
        for row in store.recent(max(1, args.limit)):
            error = f" error={row['error']}" if row["error"] else ""
            print(f"{row['updated_at']} {row['status']:10} attempts={row['attempts']} "
                  f"{row['platform']:9} {row['video_path']}{error}")
        return 0

    retry = args.command == "retry" or bool(getattr(args, "retry", False))
    received_paths = store.failed_paths() if retry else input_paths(args.paths)
    paths = [path for path in received_paths if path.suffix.lower() in VIDEO_EXTENSIONS]
    if not paths:
        print("没有收到可处理的视频路径", file=sys.stderr)
        return 2
    ok = True
    for path in paths:
        if not path.is_file():
            print(f"ERROR 文件不存在: {path}", file=sys.stderr)
            ok = False
            continue
        danmaku_xml = find_danmaku_xml(path, received_paths)
        ok = upload_one(
            path, cfg, store,
            dry_run=bool(getattr(args, "dry_run", False)),
            retry=retry,
            danmaku_xml=danmaku_xml,
        ) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
