#!/usr/bin/env python3
"""Bridge finalized biliup segments to Y2A-Auto uploaders."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
DEFAULT_TITLE_TEMPLATE = "【直播回放】{streamer}｜{ai_topic}｜{date}"
WORKSPACE_ROOT = Path(__file__).resolve().parent
YYF_COVER_REFERENCE = WORKSPACE_ROOT / "assets" / "streamer-references" / "yyf.png"
YYF_STREAMER_ALIASES = {"yyf", "yyfyyf", "月夜枫", "枫哥", "姜岑"}
GUOXIAOGUO_COVER_REFERENCE = (
    WORKSPACE_ROOT / "assets" / "streamer-references" / "guoxiaoguo.png"
)
GUOXIAOGUO_STREAMER_ALIASES = {"果小果", "果小果是个弟弟"}


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


def recording_part_title(video: Path, index: int) -> str:
    match = re.search(r"20\d{2}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})", video.stem)
    clock = ":".join(match.groups()) if match else ""
    return f"P{max(1, index)} {clock}".strip()


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
            db.execute(
                """CREATE TABLE IF NOT EXISTS multipart_sessions (
                    session_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                                  ("ass", "pending"), ("ai", "pending"),
                                  ("cover", "pending"), ("upload", "pending")):
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

    def multipart_session(self, session_key: str, *, include_closed: bool = False) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT status, result_json FROM multipart_sessions WHERE session_key=?",
                (session_key,),
            ).fetchone()
        if not row or (row["status"] != "open" and not include_closed):
            return {}
        try:
            value = json.loads(row["result_json"])
            if isinstance(value, dict):
                value["_session_status"] = row["status"]
                return value
            return {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def save_multipart_session(
        self,
        session_key: str,
        result: dict[str, Any],
        *,
        status: str = "open",
    ) -> None:
        now = utc_now()
        stored_result = {key: value for key, value in result.items() if key != "_session_status"}
        payload = json.dumps(stored_result, ensure_ascii=False, default=str)
        with self.connect() as db:
            db.execute(
                """INSERT INTO multipart_sessions
                   (session_key, status, result_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(session_key) DO UPDATE SET
                     status=excluded.status, result_json=excluded.result_json,
                     updated_at=excluded.updated_at""",
                (session_key, status, payload, now, now),
            )

    def upload_session_key(self, key: str) -> str:
        result = self.results(key)
        return str(result.get("multipart_session") or "")

    def close_multipart_session(self, session_key: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE multipart_sessions SET status='closed', updated_at=? "
                "WHERE session_key=? AND status='open'",
                (utc_now(), session_key),
            )
        return cursor.rowcount > 0

    def delete_multipart_session(self, session_key: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM multipart_sessions WHERE session_key=?",
                (session_key,),
            )
        return cursor.rowcount > 0

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


def recording_cover_headline(title: str, ai_topic: str = "") -> str:
    """Extract a cover-safe headline without dates, clocks or template chrome."""
    candidate = str(ai_topic or "").strip()
    if not candidate:
        parts = [part.strip() for part in re.split(r"[｜|]", str(title or "")) if part.strip()]
        candidate = parts[1] if len(parts) >= 2 else (parts[0] if parts else "直播精彩内容")
    candidate = re.sub(r"【[^】]*(?:直播|回放)[^】]*】", "", candidate)
    candidate = re.sub(r"\b20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\b", "", candidate)
    candidate = re.sub(r"\b\d{1,2}[:：]\d{2}(?::\d{2})?\b", "", candidate)
    candidate = re.sub(r"\b(?:上午|下午|凌晨|早上|晚上|深夜)?\d{1,2}\s*[点时]\b", "", candidate)
    candidate = re.sub(r"(?:今天|今日|今晚|昨天|明天|凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|深夜)", "", candidate)
    candidate = re.sub(r"[\r\n｜|]+", " ", candidate)
    candidate = re.sub(r"\s{2,}", " ", candidate).strip(" -_｜|·")
    return (candidate or "直播精彩内容")[:24]


def recording_cover_reference(streamer: str) -> tuple[str, Path] | None:
    """Return a curated identity reference for a known streamer."""
    normalized = re.sub(r"[\s_\-]+", "", str(streamer or "")).casefold()
    if normalized in YYF_STREAMER_ALIASES or re.fullmatch(r"yyf(?:yyf)?\d*", normalized):
        if YYF_COVER_REFERENCE.is_file():
            return "YYF", YYF_COVER_REFERENCE
    if normalized in GUOXIAOGUO_STREAMER_ALIASES or normalized.startswith("果小果"):
        if GUOXIAOGUO_COVER_REFERENCE.is_file():
            return "果小果", GUOXIAOGUO_COVER_REFERENCE
    return None


def generate_recording_cover_with_ai(
    title: str,
    ai_topic: str,
    description: str,
    streamer: str,
    cfg: dict[str, Any],
    work_dir: Path,
) -> tuple[Path | None, dict[str, Any]]:
    """Generate a 16:10 Bilibili cover from the final AI-assisted title."""
    root = resolve_path(str(cfg.get("y2a_root", "y2a-auto")), cfg)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from modules.ai_enhancer import get_openai_client  # type: ignore
    from modules.config_manager import load_config as load_y2a_config  # type: ignore

    ai_cfg = load_y2a_config()
    enabled = bool(ai_cfg.get("AI_GENERATE_RECORDING_COVER", False))
    headline = recording_cover_headline(title, ai_topic)
    details: dict[str, Any] = {
        "ai_cover_enabled": enabled,
        "ai_cover_headline": headline,
        "ai_cover_excludes_time": True,
    }
    if not enabled:
        return None, details
    if not ai_cfg.get("OPENAI_API_KEY"):
        raise ValueError("未配置 AI API Key，无法生成录播封面")

    image_model = str(ai_cfg.get("OPENAI_IMAGE_MODEL_NAME") or "gpt-image-2").strip()
    image_base_url = str(ai_cfg.get("OPENAI_IMAGE_BASE_URL") or "").strip()
    client_config = dict(ai_cfg)
    if image_base_url:
        client_config["OPENAI_BASE_URL"] = image_base_url
    reference = recording_cover_reference(streamer)
    reference_name = reference[0] if reference else ""
    prompt = f"""
为哔哩哔哩直播回放生成一张横向 16:10 视频封面，画面精致、主体明确、对比强烈，在手机缩略图尺寸下仍清晰。
主播：{streamer or "主播"}
AI 生成的核心标题：{headline}
内容摘要：{str(description or "")[:500]}

只围绕核心标题设计画面，可将“{headline}”作为唯一标题文字；不要出现完整投稿标题。
{f"上传的参考照片是主播 {reference_name} 本人。必须以照片中的人物为唯一人物原型，保持其脸型、五官、发型和身份辨识度；可以根据直播主题更换背景、服装和姿势，但不要生成成其他人。" if reference else ""}
绝对禁止出现日期、年份、月份、星期、钟表、具体时间、时间戳、倒计时、房间号、视频时长、平台界面、二维码和水印。
不要添加“直播回放”、主播开播时间或任何数字日期信息。避免大段文字，中文必须清楚易读。
""".strip()
    image_client = get_openai_client(client_config).images
    image_size = str(ai_cfg.get("OPENAI_IMAGE_SIZE") or "1536x1024")
    if reference:
        with reference[1].open("rb") as reference_handle:
            response = image_client.edit(
                model=image_model,
                image=reference_handle,
                prompt=prompt,
                size=image_size,
            )
        details.update({
            "ai_cover_reference_used": True,
            "ai_cover_reference_name": reference_name,
            "ai_cover_reference_path": str(reference[1]),
        })
    else:
        response = image_client.generate(
            model=image_model,
            prompt=prompt,
            size=image_size,
        )
    item = response.data[0] if getattr(response, "data", None) else None
    if item is None:
        raise RuntimeError("图片模型没有返回封面")
    encoded = getattr(item, "b64_json", None)
    image_url = str(getattr(item, "url", "") or "").strip()
    if encoded:
        raw = base64.b64decode(encoded)
    elif image_url:
        request = urllib.request.Request(image_url, headers={"User-Agent": "PotatoFlow/1.0"})
        with urllib.request.urlopen(request, timeout=180) as remote:
            raw = remote.read()
    else:
        raise RuntimeError("图片模型返回结果中没有图片数据")
    if not raw:
        raise RuntimeError("图片模型返回了空图片")

    work_dir.mkdir(parents=True, exist_ok=True)
    source = work_dir / "ai_cover_source.png"
    cover = work_dir / "ai_cover.jpg"
    source.write_bytes(raw)
    ffmpeg = str(cfg.get("ffmpeg", "ffmpeg"))
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-vf", "scale=1146:717:force_original_aspect_ratio=increase,crop=1146:717",
        "-frames:v", "1", "-q:v", "2", str(cover),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if completed.returncode != 0 or not cover.is_file():
        message = completed.stderr.strip()[-1000:]
        raise RuntimeError(f"AI 封面尺寸处理失败: {message}")
    details.update({
        "ai_cover_generated": True,
        "ai_cover_model": image_model,
        "ai_cover_path": str(cover),
        "ai_cover_prompt": prompt,
    })
    return cover, details


def cleanup_uploaded_recording(
    video: Path,
    danmaku_xml: Path | None,
    upload_video: Path,
) -> dict[str, Any]:
    """Remove large recording inputs after the complete upload state is durable."""
    candidates = [
        ("video", video),
        ("danmaku_xml", danmaku_xml),
    ]
    if upload_video.resolve() != video.resolve():
        candidates.append(("upload_video", upload_video))
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    seen: set[Path] = set()
    for kind, candidate in candidates:
        if candidate is None:
            continue
        path = candidate.resolve()
        if path in seen:
            continue
        seen.add(path)
        try:
            path.unlink(missing_ok=True)
            deleted.append(str(path))
        except OSError as exc:
            failed.append({"kind": kind, "path": str(path), "error": str(exc)})
    return {"deleted": deleted, "failed": failed}


def recording_metadata_values(
    video: Path,
    cfg: dict[str, Any],
    ai_topic: str = "",
) -> dict[str, str]:
    stem = video.stem
    date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", stem)
    time_match = re.search(r"20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(.+)$", stem)
    marker_match = re.match(r"(.+?)_[0-9a-f]{6}(?=20\d{2}-\d{2}-\d{2})", stem, re.IGNORECASE)
    streamer = str(cfg.get("streamer_name") or "").strip()
    if not streamer and marker_match:
        streamer = marker_match.group(1).strip("_- ")
    live_title = time_match.group(1).strip("_- ") if time_match else ""
    topic = re.sub(r"[\r\n｜|]+", " ", str(ai_topic or live_title or "直播精彩内容")).strip()
    return {
        "stem": stem,
        "name": video.name,
        "suffix": video.suffix.lstrip("."),
        "streamer": streamer or "主播",
        "ai_topic": topic[:28],
        "date": date_match.group(1) if date_match else (
            datetime.fromtimestamp(video.stat().st_mtime).strftime("%Y-%m-%d")
            if video.exists()
            else datetime.now().strftime("%Y-%m-%d")
        ),
        "live_title": live_title,
    }


def render_metadata(
    video: Path,
    cfg: dict[str, Any],
    ai_topic: str = "",
) -> tuple[str, str, list[str]]:
    values = recording_metadata_values(video, cfg, ai_topic)
    title = str(cfg.get("title_template") or DEFAULT_TITLE_TEMPLATE).format_map(values).strip()
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


def enhance_recording_metadata(
    title: str,
    description: str,
    existing_tags: list[str],
    cover: Path,
    fallback_partition_id: str,
    cfg: dict[str, Any],
) -> tuple[list[str], str, dict[str, Any]]:
    """Apply Y2A's tag and Bilibili partition automation to a recording."""
    root = resolve_path(str(cfg.get("y2a_root", "y2a-auto")), cfg)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from modules.ai_enhancer import (  # type: ignore
        generate_acfun_tags,
        recommend_bilibili_partition,
    )
    from modules.bilibili_zones import get_zone_list_sub  # type: ignore
    from modules.config_manager import load_config as load_y2a_config  # type: ignore

    ai_cfg = load_y2a_config()
    generate_tags_enabled = bool(ai_cfg.get("GENERATE_TAGS", False))
    recommend_partition_enabled = bool(ai_cfg.get("RECOMMEND_PARTITION", False))
    include_cover = bool(ai_cfg.get("RECOMMEND_PARTITION_WITH_COVER", False))
    openai_config = {
        "OPENAI_API_KEY": ai_cfg.get("OPENAI_API_KEY", ""),
        "OPENAI_BASE_URL": ai_cfg.get("OPENAI_BASE_URL", ""),
        "OPENAI_MODEL_NAME": ai_cfg.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo"),
        "OPENAI_THINKING_ENABLED": ai_cfg.get("OPENAI_THINKING_ENABLED", False),
        "OPENAI_TIMEOUT_SECONDS": ai_cfg.get("OPENAI_TIMEOUT_SECONDS", 600),
        "FIXED_PARTITION_ID": ai_cfg.get("FIXED_PARTITION_ID", ""),
        "FIXED_PARTITION_ID_BILIBILI": ai_cfg.get("FIXED_PARTITION_ID_BILIBILI", ""),
        "RECOMMEND_PARTITION_WITH_COVER": include_cover,
    }

    generated_tags: list[str] = []
    final_tags = [str(tag).strip() for tag in existing_tags if str(tag).strip()]
    if generate_tags_enabled:
        generated_tags = [
            str(tag).strip()
            for tag in (
                generate_acfun_tags(
                    title,
                    description,
                    openai_config=openai_config,
                    task_id=None,
                )
                or []
            )
            if str(tag).strip()
        ][:6]
        seen = {tag.casefold() for tag in final_tags}
        for tag in generated_tags:
            if tag.casefold() not in seen:
                final_tags.append(tag)
                seen.add(tag.casefold())

    partition_id = str(fallback_partition_id or "").strip()
    selection: dict[str, Any] = {}
    if recommend_partition_enabled:
        zone_data = get_zone_list_sub()
        if zone_data:
            selection = recommend_bilibili_partition(
                title,
                description,
                zone_data,
                tags=final_tags,
                openai_config=openai_config,
                task_id=None,
                cover_path=str(cover),
                include_cover_for_ai=include_cover,
            ) or {}
            recommended = str(selection.get("id") or "").strip()
            if recommended:
                partition_id = recommended

    details = {
        "tag_generation_enabled": generate_tags_enabled,
        "generated_tags": generated_tags,
        "final_tags": final_tags,
        "partition_recommendation_enabled": recommend_partition_enabled,
        "recommended_partition_id": str(selection.get("id") or "").strip() or None,
        "selected_partition_id": partition_id or None,
        "partition_source": selection.get("source"),
        "partition_confidence": selection.get("confidence"),
        "partition_reason": selection.get("reason_summary") or "",
        "partition_alternatives": selection.get("alternatives") or [],
        "cover_for_partition_ai": bool(
            recommend_partition_enabled and include_cover and cover.is_file()
        ),
        "partition_cover_path": (
            str(cover)
            if recommend_partition_enabled and include_cover and cover.is_file()
            else None
        ),
    }
    return final_tags, partition_id, details


def generate_danmaku_metadata_with_ai(
    comments,
    base_description: str,
    cfg: dict[str, Any],
) -> tuple[str, str]:
    """Generate a grounded description and concise title topic from danmaku."""
    if not comments or not bool(cfg.get("ai_danmaku_summary_enabled", True)):
        return base_description, ""
    try:
        root = resolve_path(str(cfg.get("y2a_root", "y2a-auto")), cfg)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from modules.ai_enhancer import get_openai_client, _request_json_object  # type: ignore
        from modules.config_manager import load_config as load_y2a_config  # type: ignore

        ai_cfg = load_y2a_config()
        if not ai_cfg.get("OPENAI_API_KEY"):
            print("WARN 未配置 Y2A OPENAI_API_KEY，跳过弹幕 AI 简介", file=sys.stderr)
            return base_description, ""
        selected = select_summary_comments(comments, int(cfg.get("ai_danmaku_max_comments", 400)))
        payload = {
            "base_description": base_description,
            "comment_count": len(comments),
            "sampled_comments": format_comments_for_ai(selected),
        }
        system_prompt = str(cfg.get("ai_danmaku_prompt") or """
你是直播录播编辑。根据按时间采样的观众弹幕，为哔哩哔哩录播生成核心主题和简洁中文简介。
只能总结弹幕能支持的主题、高潮时刻和观众反应，不得虚构主播说过的话或未出现的事件。
不要引用用户名、UID、广告或重复刷屏。保留 base_description 中有用的基础信息。
title_topic 是适合放进标题的自然短语，不加书名号、不含日期和主播名，最多 18 个中文字符。
返回 JSON 对象：{"title_topic":"...","description":"..."}，description 不超过 1200 个中文字符。
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
        title_topic = re.sub(
            r"[\r\n｜|]+",
            " ",
            str((result or {}).get("title_topic", "")).strip(),
        )[:28].strip()
        return description[:1800] if description else base_description, title_topic
    except Exception as exc:
        print(f"WARN 弹幕 AI 简介生成失败，使用原简介: {exc}", file=sys.stderr)
        return base_description, ""


def upload_one(video: Path, base_cfg: dict[str, Any], store: StateStore,
               dry_run: bool = False, retry: bool = False,
               danmaku_xml: Path | None = None,
               session_key: str = "") -> bool:
    cfg = effective_config(base_cfg, video)
    platform = "bilibili"
    wait_until_stable(video, int(cfg.get("stable_checks", 2)), float(cfg.get("stable_interval_seconds", 2)))
    danmaku_xml = danmaku_xml or find_danmaku_xml(video)
    key = fingerprint(video, danmaku_xml)
    if retry and not session_key:
        previous_session_key = store.upload_session_key(key)
        previous_session = (
            store.multipart_session(previous_session_key, include_closed=True)
            if previous_session_key
            else {}
        )
        # A session without a BVID only represents an unfinished first part.
        # Retrying through it would block forever on its own pending_first_video.
        # Retry that file as an independent submission instead.
        if isinstance(previous_session.get("bilibili"), dict):
            session_key = previous_session_key
    prior_result = store.results(key)
    if not store.claim(key, video, platform, retry=retry):
        print(f"SKIP 已处理或正在处理: {video}")
        return True

    multipart = (
        store.multipart_session(session_key, include_closed=retry)
        if session_key
        else {}
    )
    session_status = str(multipart.pop("_session_status", "open")) if multipart else "open"
    if session_key and not multipart:
        multipart = {
            "pending_first_video": str(video.resolve()),
            "title": "",
            "description": "",
            "tags": [],
            "source_url": str(cfg.get("source_url", "")).strip(),
        }
        if not dry_run:
            store.save_multipart_session(session_key, multipart)
    pending_first_video = str(multipart.get("pending_first_video") or "")
    blocked_by_pending_part = bool(
        session_key
        and pending_first_video
        and Path(pending_first_video).resolve() != video.resolve()
        and not multipart.get("bilibili")
    )
    existing_submission = multipart.get("bilibili") if multipart else None
    part_number = (
        int(existing_submission.get("part_count") or 0) + 1
        if isinstance(existing_submission, dict)
        else 1
    )
    store.finish(key, "processing", {
        **prior_result,
        "multipart_session": session_key or None,
        "part_number": part_number,
    })
    work_dir = store.path.parent / "artifacts" / key[:16]
    current_stage = "ass"
    try:
        if blocked_by_pending_part:
            current_stage = "upload"
            raise RuntimeError("前一分P尚未上传成功，请先重试前一分P")

        title, description, tags = render_metadata(video, cfg)
        original_cover = find_cover(video, cfg, work_dir)
        cover = original_cover
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
        ai_topic = ""
        ai_details: dict[str, Any] = {}
        if comments and not dry_run and bool(cfg.get("ai_danmaku_summary_enabled", True)):
            store.stage(key, "ai", "running", {"comment_count": len(comments)})
            description, ai_topic = generate_danmaku_metadata_with_ai(comments, description, cfg)
            title, _, _ = render_metadata(video, cfg, ai_topic=ai_topic)
            ai_details.update({
                "title_topic": ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                "title": title,
                "description": description,
                "comment_count": len(comments),
            })
        else:
            reason = "试运行" if dry_run else ("未配置可分析弹幕" if not comments else "AI 简介未启用")
            ai_details.update({"reason": reason, "title": title, "description": description})

        partition = str(cfg.get("bilibili_partition_id", "")).strip()
        metadata_automation: dict[str, Any] = {}
        if not dry_run and not existing_submission:
            store.stage(key, "ai", "running", ai_details)
            try:
                tags, partition, metadata_automation = enhance_recording_metadata(
                    title,
                    description,
                    tags,
                    original_cover,
                    partition,
                    cfg,
                )
                ai_details.update(metadata_automation)
            except Exception as exc:
                metadata_automation = {"metadata_automation_error": str(exc)}
                ai_details.update(metadata_automation)
                print(f"WARN 录播 AI 标签或分区推荐失败，使用原配置: {exc}", file=sys.stderr)

        if multipart:
            title = str(multipart.get("title") or title)
            description = str(multipart.get("description") or description)
            tags = list(multipart.get("tags") or tags)
            source_url = str(multipart.get("source_url") or source_url)
            partition = str(multipart.get("partition_id") or partition)
            if isinstance(multipart.get("metadata_automation"), dict):
                metadata_automation = dict(multipart["metadata_automation"])
                ai_details.update(metadata_automation)

        ai_details.update({
            "title": title,
            "description": description,
            "final_tags": tags,
            "selected_partition_id": partition or None,
        })
        ai_was_used = bool(
            comments and bool(cfg.get("ai_danmaku_summary_enabled", True))
        ) or bool(
            metadata_automation.get("tag_generation_enabled")
            or metadata_automation.get("partition_recommendation_enabled")
            or metadata_automation.get("metadata_automation_error")
        )
        store.stage(key, "ai", "completed" if ai_was_used else "skipped", ai_details)

        current_stage = "cover"
        cover_generation: dict[str, Any] = {}
        session_cover = str(multipart.get("cover_path") or "").strip() if multipart else ""
        if session_cover and Path(session_cover).is_file():
            cover = Path(session_cover)
            cover_generation = dict(multipart.get("cover_generation") or {})
            cover_generation.update({
                "ai_cover_reused": True,
                "ai_cover_path": str(cover),
                "original_cover_path": str(original_cover),
            })
            store.stage(key, "cover", "completed", cover_generation)
        elif not dry_run and not existing_submission:
            store.stage(key, "cover", "running", {
                "title": title,
                "title_topic": ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                "original_cover_path": str(original_cover),
            })
            try:
                generated_cover, cover_generation = generate_recording_cover_with_ai(
                    title=title,
                    ai_topic=ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                    description=description,
                    streamer=recording_metadata_values(video, cfg)["streamer"],
                    cfg=cfg,
                    work_dir=work_dir,
                )
                if generated_cover:
                    cover = generated_cover
                cover_generation.update({
                    "cover_used_for_upload": str(cover),
                    "original_cover_path": str(original_cover),
                })
                cover_status = (
                    "completed"
                    if cover_generation.get("ai_cover_generated")
                    else "skipped"
                )
                store.stage(key, "cover", cover_status, cover_generation)
            except Exception as exc:
                cover_generation = {
                    "ai_cover_enabled": True,
                    "ai_cover_generated": False,
                    "ai_cover_error": str(exc),
                    "cover_fallback": "视频截图",
                    "cover_used_for_upload": str(original_cover),
                    "original_cover_path": str(original_cover),
                }
                cover = original_cover
                store.stage(key, "cover", "skipped", cover_generation)
                print(f"WARN AI 录播封面生成失败，回退视频截图: {exc}", file=sys.stderr)
        else:
            reason = "试运行" if dry_run else "后续分P沿用当前稿件封面"
            cover_generation = {
                "reason": reason,
                "cover_used_for_upload": str(cover),
                "original_cover_path": str(original_cover),
            }
            store.stage(key, "cover", "skipped", cover_generation)

        summary = {"video": str(video), "upload_video": str(upload_video),
                   "danmaku_xml": str(danmaku_xml) if danmaku_xml else None,
                   "ass_path": str(ass_path) if ass_path else None,
                   "danmaku_count": len(comments), "cover": str(cover),
                   "original_cover": str(original_cover), "platform": platform,
                   "title": title, "description": description, "tags": tags, "source_url": source_url,
                   "partition_id": partition, "metadata_automation": metadata_automation,
                   "cover_generation": cover_generation,
                   "multipart_session": session_key or None, "part_number": part_number}
        if dry_run:
            store.stage(key, "upload", "skipped", {"reason": "试运行未投稿"})
            store.finish(key, "dry_run", summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return True

        current_stage = "upload"
        store.stage(key, "upload", "running", {
            "title": title,
            "cover": str(cover),
            "tags": tags,
            "partition_id": partition,
            "part_number": part_number,
            "existing_bvid": (
                existing_submission.get("bvid")
                if isinstance(existing_submission, dict)
                else None
            ),
        })
        BilibiliUploader, _ = import_y2a(cfg)
        cookie = resolve_path(str(cfg.get("bilibili_cookies", "")), cfg)
        if not cookie.is_file() or not partition:
            raise ValueError("bilibili 需要有效的 bilibili_cookies 和 bilibili_partition_id")
        previous = store.results(key)
        previous.update({
            "tags": tags,
            "partition_id": partition,
            "metadata_automation": metadata_automation,
            "cover_generation": cover_generation,
            "cover_path": str(cover),
        })
        result = previous.get("bilibili")
        if not isinstance(result, dict) or not result.get("bvid"):
            uploader = BilibiliUploader(cookie_file=str(cookie))
            ok, result = uploader.upload_video(
                video_file_path=str(upload_video), cover_file_path=str(cover), title=title,
                description=description, tags=tags, partition_id=partition,
                youtube_url=source_url, task_id=None,
                page_titles=[recording_part_title(video, part_number)],
                existing_submission=existing_submission,
            )
            if not ok:
                raise RuntimeError(f"bilibili 上传失败: {result}")
            previous.update({"bilibili": result, "ass_path": str(ass_path) if ass_path else None})
            # Persist the BVID immediately so a process restart cannot create a
            # duplicate video submission.
            store.finish(key, "video_uploaded", previous)

        if session_key:
            session_state = {
                "bilibili": previous.get("bilibili"),
                "title": title,
                "description": description,
                "tags": tags,
                "source_url": source_url,
                "partition_id": partition,
                "metadata_automation": metadata_automation,
                "cover_generation": cover_generation,
                "cover_path": str(cover),
                "last_video": str(video),
            }
            store.save_multipart_session(
                session_key,
                session_state,
                status=session_status if retry else "open",
            )

        store.stage(key, "upload", "completed", {
            "title": title, "description": description, "cover": str(cover),
            "tags": tags, "partition_id": partition,
            "bilibili": previous.get("bilibili"),
            "part_number": part_number,
        })
        store.finish(key, "completed", previous)
        if bool(cfg.get("delete_recording_after_upload", True)):
            previous["source_cleanup"] = cleanup_uploaded_recording(video, danmaku_xml, upload_video)
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
    ingest.add_argument("--session-key", default="", help="将分段追加到同一场直播稿件")
    sub.add_parser("retry", help="重试失败记录")
    finalize_session = sub.add_parser(
        "finalize-session",
        help="导入手动停止时的最终录制文件，然后结束分P追加会话",
    )
    finalize_session.add_argument("paths", nargs="*")
    finalize_session.add_argument("--session-key", required=True)
    close_session = sub.add_parser("close-session", help="结束直播的分P追加会话")
    close_session.add_argument("--session-key", required=True)
    status = sub.add_parser("status", help="显示最近记录")
    status.add_argument("--limit", type=int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_linux_ca_environment()
    args = build_parser().parse_args(argv)
    cfg = load_config(Path(args.config))
    state_path = resolve_path(str(cfg.get("state_db", ".bridge/state.sqlite3")), cfg)
    store = StateStore(state_path)

    if args.command == "close-session":
        closed = store.close_multipart_session(str(args.session_key))
        print(f"OK 分P会话已结束: {args.session_key}" if closed else f"SKIP 没有活动分P会话: {args.session_key}")
        return 0

    if args.command == "status":
        for row in store.recent(max(1, args.limit)):
            error = f" error={row['error']}" if row["error"] else ""
            print(f"{row['updated_at']} {row['status']:10} attempts={row['attempts']} "
                  f"{row['platform']:9} {row['video_path']}{error}")
        return 0

    retry = args.command == "retry" or bool(getattr(args, "retry", False))
    received_paths = store.failed_paths() if args.command == "retry" else input_paths(args.paths)
    paths = [path for path in received_paths if path.suffix.lower() in VIDEO_EXTENSIONS]
    if args.command == "finalize-session" and not paths:
        closed = store.close_multipart_session(str(args.session_key))
        print(
            f"OK 分P会话已结束: {args.session_key}"
            if closed
            else f"SKIP 没有活动分P会话: {args.session_key}"
        )
        return 0
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
            session_key=str(getattr(args, "session_key", "") or ""),
        ) and ok
    if args.command == "finalize-session":
        if ok:
            closed = store.close_multipart_session(str(args.session_key))
            print(
                f"OK 最终分段已导入，分P会话已结束: {args.session_key}"
                if closed
                else f"OK 最终分段已导入，无需关闭空会话: {args.session_key}"
            )
        else:
            store.delete_multipart_session(str(args.session_key))
            print(
                f"WARN 最终分段导入失败，失败任务已保留且旧分P关系已解除: {args.session_key}",
                file=sys.stderr,
            )
        # The failed task is persisted and retryable in WebUI. Recording has
        # already stopped, so do not turn safe finalization into a recorder
        # process failure.
        return 0
    if not ok and str(getattr(args, "session_key", "") or "") and not retry:
        # A failed segment is already visible and retryable in the WebUI.  Do
        # not abort biliup's live event stream here: later segments still need
        # to be recorded, and the end-of-stream hook must close this session so
        # the next broadcast cannot append to the old submission.
        print("WARN 分P处理失败已记录，录制与后续分段将继续", file=sys.stderr)
        return 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
