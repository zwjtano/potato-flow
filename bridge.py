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
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from danmaku_pipeline import (
    build_ass,
    burn_ass,
    format_comments_for_ai,
    inspect_biliup_xml,
    parse_biliup_xml,
    probe_video_size,
    select_summary_comments,
)
from dota2_abilities import (
    build_dota2_ability_reference_sheet,
    dota2_ability_prompt_instruction,
    match_dota2_abilities,
)
from dota2_items import (
    build_dota2_item_reference_sheet,
    dota2_item_prompt_instruction,
    match_dota2_items,
)
from dota2_heroes import build_dota2_hero_reference
from runtime_environment import configure_linux_ca_environment

VIDEO_EXTENSIONS = {".mp4", ".flv", ".mkv", ".webm", ".ts", ".m2ts", ".mov"}
DEFAULT_TITLE_TEMPLATE = "{streamer}｜{ai_topic}｜{date}"
DEFAULT_DESCRIPTION_TEMPLATE = "{recording_intro}"
DEFAULT_RECORDING_TITLE_AI_PROMPT = (
    "根据本段直播的实际内容和弹幕反应提炼一个自然、有信息量的核心主题；"
    "突出关键对局、英雄、事件或节目效果，不使用夸张的虚假结论。"
    "不要包含主播名、日期、时间和“直播回放”，最多18个中文字符。"
)
DEFAULT_RECORDING_DESCRIPTION_AI_PROMPT = (
    "生成可直接用于哔哩哔哩投稿、内容充实的完整中文简介：先用两至四段概括主要内容、"
    "事件发展、关键时刻和观众反应，再选出有完整弹幕证据的重要事件。"
    "不要在简介正文中手写时间点；程序会回到完整 XML 定位最早证据、补偿反应延迟并统一格式化。"
    "没有足够证据时宁可少列，不得编造时间或事件。只使用输入能够支持的事实，不虚构主播"
    "原话、比赛结果或人物。不要出现文件名、任务编号、内部路径和机械化套话，不超过1800字。"
)
DOTA2_METADATA_DISAMBIGUATION = (
    "Dota 2 术语消歧：弹幕或直播内容中的“老奶奶”指英雄"
    "电炎绝手（Snapfire），不得理解为普通老年女性。"
)
DEFAULT_RECORDING_COVER_AI_PROMPT = (
    "围绕本段最核心的对局、英雄或节目效果构图，主体醒目、对比清楚、适合手机缩略图；"
    "人物形象与指定参考图保持一致，DOTA2 英雄和技能必须符合游戏原设；"
    "装备只允许依据系统随附的 Valve 官方装备图标参考，缺少官方参考时不得表现具体装备，"
    "禁止自绘或仿冒装备图标。"
    "画面不要出现日期、时间、房间号、平台界面、二维码或水印。"
)
WORKSPACE_ROOT = Path(__file__).resolve().parent
YYF_COVER_REFERENCE = WORKSPACE_ROOT / "assets" / "streamer-references" / "yyf.png"
YYF_STREAMER_ALIASES = {
    "yyf", "yyfyyf", "月夜枫", "枫哥", "峰哥", "姜岑", "FG", "胖头", "胖头鱼"
}
GUOXIAOGUO_COVER_REFERENCE = (
    WORKSPACE_ROOT / "assets" / "streamer-references" / "guoxiaoguo.png"
)
GUOXIAOGUO_STREAMER_ALIASES = {"果小果", "果小果是个弟弟"}
DOTA2_STREAMER_ALIAS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "YYF",
        (
            "YYF",
            "yyfyyf",
            "月夜枫",
            "枫哥",
            "峰哥",
            "姜岑",
            "FG",
            "胖头",
            "胖头鱼",
            "石佛",
            "僵尸王",
            "毒瘤枫",
            "吃人枫",
            "姜瘤儿",
        ),
    ),
    ("BurNIng", ("BurNIng", "B神", "徐志雷")),
    ("xiao8", ("xiao8", "小八", "八师傅", "张宁")),
    ("Zhou", ("Zhou", "周神", "陈尧")),
    ("Hao", ("Hao", "豪哥", "陈智豪")),
    ("Mu", ("Mu", "Mu神", "张盼")),
    ("Faith_bian", ("Faith_bian", "faithbian", "小明鞭", "张睿达")),
    ("Somnus", ("Somnus", "Maybe", "超哥", "路垚")),
    ("Chalice", ("Chalice", "查理斯", "杨沈仪")),
    ("fy", ("fy", "fy神", "徐林森")),
    ("Ame", ("Ame", "萧瑟", "王淳煜")),
    ("XinQ", ("XinQ", "赵子星")),
    ("Sccc", ("Sccc", "军体拳", "宋淳")),
    ("Paparazi", ("Paparazi", "Eurus", "拒绝者", "张成俊")),
    ("Monet", ("Monet", "圣子华炼", "杜鹏")),
    ("Ori", ("Ori", "曾焦阳")),
    ("Dy", ("Dy", "丁聪")),
    ("Kaka", ("Kaka", "卡卡", "胡良智")),
    ("LaNm", ("LaNm", "国土", "张志成")),
    ("LongDD", ("LongDD", "龙神", "龙弟弟", "黄翔")),
    ("820", ("820", "八二零", "邹倚天")),
    ("DDC", ("DDC", "大狗", "梁发")),
    ("PIS", ("PIS", "Pis", "姚羿成")),
    ("Inflame", ("Inflame", "小书童", "何雍正")),
    ("川神", ("川神", "叫我老陈就好了")),
)


def _compact_alias(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def normalize_dota2_streamer_name(streamer: str) -> str:
    """Return a stable public name for known Dota 2 streamer aliases."""
    original = str(streamer or "").strip()
    normalized = _compact_alias(original)
    if re.fullmatch(r"yyf(?:yyf)?\d*", normalized):
        return "YYF"
    if normalized.startswith("果小果"):
        return "果小果"
    for canonical_name, aliases in DOTA2_STREAMER_ALIAS_GROUPS:
        if any(normalized == _compact_alias(alias) for alias in aliases):
            return canonical_name
    return original


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_pipeline_process_group() -> None:
    """Give one bridge task its own process group so it can be stopped safely."""
    if os.name != "posix":
        return
    try:
        if os.getpgrp() != os.getpid():
            os.setsid()
    except OSError:
        # Retry workers are already session leaders because they are spawned
        # with ``start_new_session=True``.
        pass


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


def emit_recording_task_added_notification(
    cfg: dict[str, Any],
    *,
    fingerprint_value: str,
    video: Path,
    task_kind: str,
) -> None:
    """Queue a TASK_ADDED notification for a newly claimed recording job."""
    try:
        y2a_root = resolve_path(
            str(cfg.get("y2a_root") or "y2a-auto"),
            cfg,
        )
        if str(y2a_root) not in sys.path:
            sys.path.insert(0, str(y2a_root))
        from modules.notifications import (
            EVENT_TASK_ADDED,
            NotificationEvent,
            emit_notification_event,
        )

        emit_notification_event(
            NotificationEvent(
                event_type=EVENT_TASK_ADDED,
                payload={
                    "task_id": fingerprint_value,
                    "task_kind": task_kind,
                    "video_path": str(video),
                    "video_file": video.name,
                    "streamer": normalize_dota2_streamer_name(
                        str(cfg.get("streamer_name") or "")
                    ),
                    "source_url": str(cfg.get("source_url") or ""),
                    "upload_target": (
                        "local"
                        if task_kind == "record_only"
                        else "bilibili"
                    ),
                },
            )
        )
    except Exception as exc:
        # 通知失败不能阻塞 ASS、AI 或投稿流水线。
        print(f"WARN 录播任务新增通知写入失败: {exc}", file=sys.stderr)


def emit_recording_task_result_notification(
    cfg: dict[str, Any],
    *,
    fingerprint_value: str,
    video: Path,
    task_kind: str,
    status: str,
    result: dict[str, Any] | None = None,
    error: str = "",
    stage: str = "",
    title: str = "",
) -> None:
    """Queue a completion/failure notification for a recording job."""
    if status not in {"completed", "failed"}:
        return
    try:
        y2a_root = resolve_path(
            str(cfg.get("y2a_root") or "y2a-auto"),
            cfg,
        )
        if str(y2a_root) not in sys.path:
            sys.path.insert(0, str(y2a_root))
        from modules.notifications import (
            EVENT_TASK_COMPLETED,
            EVENT_TASK_FAILED,
            NotificationEvent,
            emit_notification_event,
        )

        normalized_result = dict(result or {})
        bilibili_result = normalized_result.get("bilibili")
        bilibili_result = bilibili_result if isinstance(bilibili_result, dict) else {}
        emit_notification_event(
            NotificationEvent(
                event_type=(
                    EVENT_TASK_COMPLETED
                    if status == "completed"
                    else EVENT_TASK_FAILED
                ),
                payload={
                    "task_id": fingerprint_value,
                    "task_kind": task_kind,
                    "video_path": str(video),
                    "video_file": video.name,
                    "streamer": normalize_dota2_streamer_name(
                        str(cfg.get("streamer_name") or "")
                    ),
                    "source_url": str(cfg.get("source_url") or ""),
                    "upload_target": (
                        "local"
                        if task_kind == "record_only"
                        else "bilibili"
                    ),
                    "status": status,
                    "stage": str(stage or ""),
                    "error_message": str(error or ""),
                    "bvid": str(bilibili_result.get("bvid") or ""),
                    "bilibili_url": str(
                        bilibili_result.get("url")
                        or (
                            f"https://www.bilibili.com/video/{bilibili_result.get('bvid')}"
                            if bilibili_result.get("bvid")
                            else ""
                        )
                    ),
                    "title": str(title or ""),
                    "final_video_path": str(
                        normalized_result.get("final_video_path") or ""
                    ),
                },
            )
        )
    except Exception as exc:
        # 通知失败不能改变已经落库的流水线结果。
        print(f"WARN 录播任务结果通知写入失败: {exc}", file=sys.stderr)


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
    # Older recorder builds could finalize a manually stopped XML with the
    # stop timestamp instead of the video's start timestamp.  A session has
    # its own directory, so the closest recently-written XML is a safe
    # fallback when the exact sidecar name is missing.
    try:
        video_mtime = video.stat().st_mtime
        session_xml = [
            candidate
            for candidate in video.parent.glob("*.xml")
            if candidate.is_file()
        ]
        if session_xml:
            closest = min(
                session_xml,
                key=lambda candidate: abs(candidate.stat().st_mtime - video_mtime),
            )
            if abs(closest.stat().st_mtime - video_mtime) <= 120:
                return closest.resolve()
    except OSError:
        pass
    return None


def wait_for_danmaku_xml(
    video: Path,
    paths: list[Path] | None = None,
    *,
    timeout: float = 8.0,
    interval: float = 0.25,
) -> Path | None:
    """Wait for biliup to finish rolling the XML before ASS generation."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        danmaku_xml = find_danmaku_xml(video, paths)
        if danmaku_xml is not None:
            try:
                wait_until_stable(danmaku_xml, checks=2, interval=interval)
            except (FileNotFoundError, OSError):
                pass
            else:
                return danmaku_xml
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.05, interval))


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


def recording_part_title(video: Path, index: int, topic: str = "") -> str:
    match = re.search(r"20\d{2}-\d{2}-\d{2}_(\d{2})-(\d{2})", video.stem)
    clock = f"{match.group(1)}:{match.group(2)}" if match else f"{max(1, index):02d}"
    clean_topic = re.sub(r"[\r\n｜|]+", " ", str(topic or "")).strip()
    clean_topic = clean_topic or "直播精彩内容"
    return f"{clock} {clean_topic[:60]}"[:80]


def strip_recording_intro(description: str) -> str:
    """Remove the generic AI/template lead-in from a recording summary."""
    return re.sub(
        r"^直播录播[：:].*?[。.!！]\s*",
        "",
        str(description or "").strip(),
        count=1,
    ).strip()


def strip_ai_timeline_lines(description: str) -> str:
    """Keep AI prose but discard model-formatted timestamps and headings."""
    lines = str(description or "").splitlines()
    timestamp_line = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s+")
    section_heading = re.compile(r"\s*重要(?:时间点|事件)\s*[：:]?\s*")
    kept: list[str] = []
    for line in lines:
        if section_heading.fullmatch(line):
            break
        if not timestamp_line.match(line):
            kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def render_grounded_danmaku_timeline(
    timeline: Any,
    selected_comments: list[Any],
    all_comments: list[Any],
    *,
    delay_seconds: int = 8,
    duration_seconds: float | None = None,
) -> str:
    """Render only timeline anchors that copy an exact XML evidence pair."""
    if not isinstance(timeline, list):
        return ""
    sampled_texts = {
        str(comment.text).strip()
        for comment in selected_comments
        if str(getattr(comment, "text", "")).strip()
    }
    delay = max(0, min(60, int(delay_seconds or 0)))
    maximum = None if duration_seconds is None else max(0.0, float(duration_seconds))
    verified: dict[int, str] = {}

    for item in timeline:
        if not isinstance(item, dict):
            continue
        raw_evidence_texts = item.get("evidence_texts")
        evidence_texts = (
            [str(text or "").strip() for text in raw_evidence_texts[:3]]
            if isinstance(raw_evidence_texts, list)
            else [str(item.get("evidence_text") or "").strip()]
        )
        if not evidence_texts or any(
            not text or text not in sampled_texts
            for text in evidence_texts
        ):
            continue
        raw_keywords = item.get("evidence_keywords")
        if not isinstance(raw_keywords, list):
            continue
        keywords = list(dict.fromkeys(
            re.sub(r"\s+", " ", str(keyword or "")).strip()
            for keyword in raw_keywords[:4]
            if len(re.sub(r"\s+", "", str(keyword or ""))) >= 2
        ))
        evidence_corpus = "\n".join(evidence_texts).casefold()
        if not keywords or any(keyword.casefold() not in evidence_corpus for keyword in keywords):
            continue
        matching_comments = [
            comment
            for comment in all_comments
            if all(
                keyword.casefold() in str(getattr(comment, "text", "")).casefold()
                for keyword in keywords
            )
        ]
        if not matching_comments:
            continue
        event = re.sub(r"\s+", " ", str(item.get("event") or "")).strip()
        event = re.sub(r"^\d{1,2}:\d{2}(?::\d{2})?\s+", "", event)
        if not event:
            continue
        earliest = min(matching_comments, key=lambda comment: float(comment.time))
        corrected = max(0, int(float(earliest.time)) - delay)
        if maximum is not None and corrected > maximum + 1:
            continue
        verified.setdefault(corrected, event[:120])

    if not verified:
        return ""

    def format_timestamp(total: int) -> str:
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    lines = [
        f"{format_timestamp(second)} {verified[second]}"
        for second in sorted(verified)
    ]
    return "重要时间点\n" + "\n".join(lines)


def _multipart_summary_body(description: str) -> str:
    return strip_recording_intro(description)


def render_multipart_description(parts: list[dict[str, Any]], intro: str = "") -> str:
    """Build one Bilibili archive description containing each part's own summary."""
    normalized = [
        item for item in parts
        if isinstance(item, dict) and int(item.get("part_number") or 0) > 0
    ]
    normalized.sort(key=lambda item: int(item.get("part_number") or 0))
    if not normalized:
        return strip_recording_intro(intro)[:1900]

    headings = []
    for item in normalized:
        fields = [f"P{int(item.get('part_number') or 1)}"]
        topic = re.sub(r"[\r\n｜|]+", " ", str(item.get("title_topic") or "")).strip()
        recorded_at = str(item.get("recorded_at") or "").strip()
        if topic:
            fields.append(topic[:40])
        if recorded_at:
            fields.append(recorded_at)
        headings.append(f"【{'｜'.join(fields)}】")

    clean_intro = strip_recording_intro(intro)
    overhead = len(clean_intro) + sum(len(item) + 2 for item in headings)
    body_budget = max(80, (1850 - overhead) // max(1, len(normalized)))
    sections = []
    for heading, item in zip(headings, normalized):
        body = _multipart_summary_body(str(item.get("description") or ""))
        sections.append(f"{heading}\n{body[:body_budget].rstrip()}")
    return "\n\n".join(([clean_intro] if clean_intro else []) + sections)[:1900].rstrip()


def strip_live_stats_from_description(description: str, stats_text: str) -> str:
    """Return the editorial body without pipeline-owned live statistics."""
    stats = str(stats_text or "").strip()
    body = strip_recording_intro(description)
    if not stats:
        return body

    # Old tasks and model responses may contain one or more canonical copies.
    # Keep this migration tolerant so retrying a historical task repairs it.
    while body == stats or body.startswith(f"{stats}\n"):
        body = body[len(stats):].lstrip()
    return strip_recording_intro(body)


def prepend_live_stats_to_description(
    description: str,
    stats_text: str,
    limit: int = 1900,
) -> str:
    """Put live statistics first while keeping the archive description in range."""
    stats = str(stats_text or "").strip()
    body = strip_live_stats_from_description(description, stats)
    if not stats:
        return body[:limit].rstrip()
    if len(stats) >= limit:
        return stats[:limit].rstrip()

    separator = "\n\n" if body else ""
    body_budget = max(0, limit - len(stats) - len(separator))
    return f"{stats}{separator}{body[:body_budget].rstrip()}".rstrip()


def live_stats_stage_details(stats_text: str) -> dict[str, Any]:
    """Persist the human-readable statistics instead of only its length."""
    summary = str(stats_text or "").strip()
    return {
        "stats_collected": bool(summary),
        "stats_summary": summary,
        "stats_length": len(summary),
        "outcome": "matched" if summary else "no_data",
    }


def danmaku_stage_details(
    video: Path,
    danmaku_xml: Path,
    comments: list[Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Describe XML coverage and flag implausibly sparse long recordings."""
    details = inspect_biliup_xml(danmaku_xml, comments)
    duration = video_duration_seconds(video, str(cfg.get("ffprobe", "ffprobe")))
    duration_minutes = max(0.0, float(duration or 0.0) / 60.0)
    rate = len(comments) / duration_minutes if duration_minutes > 0 else 0.0
    minimum_duration = max(
        0.0,
        float(cfg.get("danmaku_sparse_warning_min_duration_seconds", 1800) or 1800),
    )
    minimum_rate = max(
        0.0,
        float(cfg.get("danmaku_sparse_warning_min_per_minute", 2.0) or 2.0),
    )
    suspected = bool(
        duration is not None
        and duration >= minimum_duration
        and rate < minimum_rate
    )
    details.update({
        "video_duration_seconds": round(float(duration), 3) if duration is not None else None,
        "danmaku_rate_per_minute": round(rate, 3),
        "danmaku_integrity": "suspected_incomplete" if suspected else "ok",
    })
    if suspected:
        details["danmaku_integrity_reason"] = (
            f"{duration_minutes:.1f} 分钟录播仅保存 {len(comments)} 条有效弹幕，"
            f"低于完整性预警阈值 {minimum_rate:g} 条/分钟；已保留源 XML 供核查"
        )
    return details


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o750)
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
            db.execute(
                """CREATE TABLE IF NOT EXISTS recording_review_overrides (
                    fingerprint TEXT PRIMARY KEY,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (fingerprint) REFERENCES uploads(fingerprint)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS recording_exclusions (
                    video_path TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
        try:
            self.path.chmod(0o640)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def cleanup_expired_retained_xml(self) -> list[str]:
        """Delete only XML files explicitly retained by completed upload tasks."""
        now = datetime.now(timezone.utc)
        deleted: list[str] = []
        with self.connect() as db:
            rows = db.execute(
                """SELECT fingerprint, details_json FROM upload_stages
                   WHERE stage='cleanup' AND status='completed'
                     AND details_json LIKE '%retained_xml_until%'"""
            ).fetchall()
            for row in rows:
                try:
                    details = json.loads(row["details_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                retained_until = str(details.get("retained_xml_until") or "")
                retained_path = str(details.get("retained_xml_path") or "")
                if (
                    not retained_until
                    or not retained_path
                    or details.get("retained_xml_deleted_at")
                ):
                    continue
                try:
                    expires = datetime.fromisoformat(retained_until.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if expires > now:
                    continue
                path = Path(retained_path)
                try:
                    existed = path.exists() or path.is_symlink()
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    details["retained_xml_cleanup_error"] = str(exc)
                else:
                    if existed:
                        deleted.append(str(path))
                    details["retained_xml_deleted_at"] = now.isoformat()
                    details.pop("retained_xml_cleanup_error", None)
                    details["retained"] = [
                        item for item in details.get("retained", [])
                        if str(item) != str(path)
                    ]
                db.execute(
                    """UPDATE upload_stages SET details_json=?, updated_at=?
                       WHERE fingerprint=? AND stage='cleanup'""",
                    (
                        json.dumps(details, ensure_ascii=False, default=str),
                        utc_now(),
                        row["fingerprint"],
                    ),
                )
        return deleted

    def upload_exists(self, key: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM uploads WHERE fingerprint = ? LIMIT 1",
                    (key,),
                ).fetchone()
                is not None
            )

    def exclude_recording(self, path: Path, room_id: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO recording_exclusions
                   (video_path, room_id, reason, created_at)
                   VALUES (?, ?, 'record_only', ?)
                   ON CONFLICT(video_path) DO UPDATE SET
                     room_id=excluded.room_id,
                     reason=excluded.reason""",
                (str(path.expanduser().resolve()), room_id, utc_now()),
            )

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
            for stage, status in (
                ("detect", "completed"), ("record", "completed"),
                ("ass", "pending"), ("live_stats", "pending"),
                ("xml_identity", "pending"), ("ai", "pending"),
                ("cover_16x9", "pending"), ("cover_4x3", "pending"),
                ("upload", "pending"), ("cleanup", "pending"),
            ):
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

    def claim_record_only(
        self,
        key: str,
        path: Path,
        room_id: str,
        danmaku_xml: Path | None,
    ) -> bool:
        """Create an inspectable task for local record-only post-processing."""
        now = utc_now()
        result = {
            "room_id": room_id,
            "record_only": True,
            "worker_pid": os.getpid(),
        }
        stages = ("record", "ass", "cover", "remux", "verify", "cleanup")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM uploads WHERE fingerprint = ?",
                (key,),
            ).fetchone()
            if row and row["status"] in {"completed", "processing"}:
                return False
            db.execute(
                """INSERT INTO uploads
                   (fingerprint, video_path, platform, status, attempts, result_json,
                    created_at, updated_at)
                   VALUES (?, ?, 'record_only', 'processing', 1, ?, ?, ?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                     video_path=excluded.video_path,
                     platform='record_only',
                     status='processing',
                     attempts=uploads.attempts + 1,
                     result_json=excluded.result_json,
                     error=NULL,
                     updated_at=excluded.updated_at""",
                (
                    key,
                    str(path),
                    json.dumps(result, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            db.execute("DELETE FROM upload_stages WHERE fingerprint = ?", (key,))
            for stage in stages:
                completed = stage == "record" and danmaku_xml is not None
                details = None
                if completed:
                    details = json.dumps(
                        {
                            "video_path": str(path),
                            "size_bytes": path.stat().st_size,
                            "danmaku_xml": str(danmaku_xml),
                            "safe_finalized": True,
                        },
                        ensure_ascii=False,
                    )
                db.execute(
                    """INSERT INTO upload_stages
                       (fingerprint, stage, status, details_json, error,
                        started_at, finished_at, updated_at)
                       VALUES (?, ?, ?, ?, NULL, ?, ?, ?)""",
                    (
                        key,
                        stage,
                        "completed" if completed else "pending",
                        details,
                        now if completed else None,
                        now if completed else None,
                        now,
                    ),
                )
        return True

    def stage(self, key: str, stage: str, status: str, details: Any = None,
              error: str | None = None) -> None:
        now = utc_now()
        started_at = now if status == "running" else None
        finished_at = now if status in {"completed", "failed", "skipped", "warning"} else None
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

    def stage_state(self, key: str, stage: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """SELECT status, details_json, error, updated_at
                   FROM upload_stages WHERE fingerprint=? AND stage=?""",
                (key, stage),
            ).fetchone()
        if not row:
            return {}
        try:
            details = json.loads(row["details_json"]) if row["details_json"] else {}
        except (TypeError, json.JSONDecodeError):
            details = {}
        return {
            "status": str(row["status"] or ""),
            "details": details if isinstance(details, dict) else {},
            "error": str(row["error"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

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

    def review_override(self, key: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT metadata_json FROM recording_review_overrides WHERE fingerprint=?",
                (key,),
            ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row["metadata_json"])
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
    configured_seek = max(0, int(cfg.get("cover_seek_seconds", 10)))
    seek_candidates = list(dict.fromkeys((configured_seek, 3, 1, 0)))
    errors: list[str] = []
    for seek_seconds in seek_candidates:
        cover.unlink(missing_ok=True)
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(seek_seconds), "-i", str(video),
            "-frames:v", "1", "-q:v", "2", str(cover),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if completed.returncode == 0 and cover.is_file() and cover.stat().st_size > 0:
            return cover
        message = completed.stderr.strip()[-500:]
        errors.append(f"{seek_seconds}秒: {message or '未生成图片'}")
    raise RuntimeError(f"FFmpeg 自动截取封面失败（已尝试多个时间点）: {' | '.join(errors)[-1600:]}")


def recording_cover_headline(title: str, ai_topic: str = "") -> str:
    """Extract a cover-safe headline without dates, clocks or template chrome."""
    candidate = str(ai_topic or "").strip()
    if not candidate:
        parts = [part.strip() for part in re.split(r"[｜|]", str(title or "")) if part.strip()]
        candidate = parts[1] if len(parts) >= 2 else (parts[0] if parts else "直播精彩内容")
    candidate = re.sub(r"【[^】]*(?:直播|回放)[^】]*】", "", candidate)
    candidate = re.sub(r"\b20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\b", "", candidate)
    candidate = re.sub(r"\b\d{1,2}月\d{1,2}日\b", "", candidate)
    candidate = re.sub(r"\b\d{1,2}[:：]\d{2}(?::\d{2})?\b", "", candidate)
    candidate = re.sub(r"\b(?:上午|下午|凌晨|早上|晚上|深夜)?\d{1,2}\s*[点时]\b", "", candidate)
    candidate = re.sub(r"(?:今天|今日|今晚|昨天|明天|凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|深夜)", "", candidate)
    candidate = re.sub(r"[\r\n｜|]+", " ", candidate)
    candidate = re.sub(r"\s{2,}", " ", candidate).strip(" -_｜|·")
    return (candidate or "直播精彩内容")[:24]


def recording_cover_reference(streamer: str) -> tuple[str, Path] | None:
    """Return a curated identity reference for a known streamer."""
    normalized = normalize_dota2_streamer_name(streamer)
    if normalized == "YYF":
        if YYF_COVER_REFERENCE.is_file():
            return "YYF", YYF_COVER_REFERENCE
    if normalized == "果小果":
        if GUOXIAOGUO_COVER_REFERENCE.is_file():
            return "果小果", GUOXIAOGUO_COVER_REFERENCE
    return None


def recording_cover_reference_instruction(reference_name: str) -> str:
    if reference_name == "YYF":
        return (
            "上传的参考图是主播 YYF 的唯一固定 Q 版角色形象，必须以该角色为封面人物原型。"
            "严格保留黑色短发、深蓝色眼睛、右侧脸颊小痣、黑红色连帽外套和胸前红色 YYF 字样；"
            "最重要的标志是完整的蓝色鱼形头套：头套顶部有提环和鱼鳍，正面有一对大眼睛与浅蓝色"
            "鱼嘴，两侧鱼鳍内部为粉色，帽檐也是粉色。保持精致的二次元 Q 版插画风格和粗黑描边，"
            "禁止改成真人、普通蓝帽、鲨鱼玩偶、蓝猫或其他角色。可以根据本段对局改变表情、动作、"
            "服装细节和横向背景，但上述人物特征、鱼形头套及 YYF 身份标志必须始终清晰可辨。"
        )
    if reference_name == "果小果":
        return (
            "上传的参考图是主播果小果的固定角色形象。必须以图中角色为唯一原型，"
            "保留深棕色长发、红棕色星光大眼、脸颊红晕、两侧红色蝴蝶结和头顶荷包蛋发饰；"
            "头顶标志必须是荷包蛋发饰：不规则白色蛋白包住圆润的金黄色蛋黄，荷包蛋下方是"
            "醒目的红色大蝴蝶结；绝对不能画成蛋壳、破壳小鸡、普通帽子、花朵或只剩黄色圆点。"
            "保持二次元 Q 版插画风格，禁止改成真人，也不要生成成其他角色。"
            "可以根据直播主题更换背景、服装和姿势。"
        )
    return (
        f"上传的参考照片是主播 {reference_name} 本人。必须以照片中的人物为唯一人物原型，"
        "保持其脸型、五官、发型和身份辨识度；可以根据直播主题更换背景、服装和姿势，"
        "但不要生成成其他人。"
    )


def download_recording_avatar_reference(url: str, cfg: dict[str, Any]) -> Path:
    """Download and persist a room avatar for reuse by later recording parts."""
    avatar_url = str(url or "").strip()
    if not re.match(r"^https?://", avatar_url, re.IGNORECASE):
        raise ValueError("直播间头像地址无效")
    configured_cache = str(cfg.get("avatar_cache_dir") or "").strip()
    if configured_cache:
        cache_root = resolve_path(configured_cache, cfg)
    else:
        # The bridge state directory is writable and persistent in native and
        # Docker deployments. This avoids the old /data/.avatar-cache owner.
        state_path = resolve_path(
            str(cfg.get("state_db") or ".bridge/state.sqlite3"),
            cfg,
        )
        cache_root = state_path.parent / "avatar-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    destination = cache_root / f"{hashlib.sha256(avatar_url.encode()).hexdigest()[:24]}.jpg"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    request = urllib.request.Request(
        avatar_url,
        headers={"User-Agent": "Mozilla/5.0 PotatoFlow/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as remote:
        raw = remote.read(8 * 1024 * 1024 + 1)
    if not raw:
        raise ValueError("直播间头像为空")
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("直播间头像超过 8 MB")
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(raw)
    temporary.replace(destination)
    return destination


def recording_avatar_reference_instruction(streamer: str) -> str:
    return (
        f"上传的参考图是主播 {streamer or '主播'} 的直播间头像。请优先以头像中的人物、"
        "角色、吉祥物或标志性形象作为封面主体底稿，保持发型、五官、配色、服装特征和"
        "角色辨识度；可以根据直播主题扩展横向背景与动作，但不要替换成无关人物或角色。"
    )


_DOTA2_HERO_ALIAS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("电炎绝手（Snapfire）", ("老奶奶", "电炎绝手", "snapfire")),
    ("风暴之灵（Storm Spirit）", ("蓝猫", "storm spirit")),
    ("灰烬之灵（Ember Spirit）", ("火猫", "ember spirit")),
    ("大地之灵（Earth Spirit）", ("土猫", "earth spirit")),
    ("虚无之灵（Void Spirit）", ("紫猫", "void spirit")),
    ("变体精灵（Morphling）", ("水人", "morphling")),
    ("天穹守望者（Arc Warden）", ("电狗", "arc warden")),
    ("狙击手（Sniper）", ("火枪", "矮子", "sniper")),
    ("裂魂人（Spirit Breaker）", ("白牛", "spirit breaker")),
    ("撼地者（Earthshaker）", ("小牛", "神牛", "earthshaker")),
    ("上古巨神（Elder Titan）", ("大牛", "elder titan")),
    ("熊战士（Ursa）", ("拍拍", "拍拍熊", "ursa")),
    ("影魔（Shadow Fiend）", ("影魔", "sf", "shadow fiend")),
    ("敌法师（Anti-Mage）", ("敌法", "am", "anti-mage")),
    ("幻影长矛手（Phantom Lancer）", ("猴子", "pl", "phantom lancer")),
    ("幻影刺客（Phantom Assassin）", ("幻刺", "pa", "phantom assassin")),
    ("圣堂刺客（Templar Assassin）", ("圣堂", "ta", "templar assassin")),
    ("矮人直升机（Gyrocopter）", ("飞机", "gyrocopter")),
    ("编织者（Weaver）", ("蚂蚁", "weaver")),
    ("斯拉克（Slark）", ("小鱼", "小鱼人", "slark")),
    ("斯拉达（Slardar）", ("大鱼", "大鱼人", "slardar")),
    ("卓尔游侠（Drow Ranger）", ("小黑", "drow ranger")),
    ("美杜莎（Medusa）", ("大娜迦", "美杜莎", "medusa")),
    ("娜迦海妖（Naga Siren）", ("小娜迦", "娜迦", "naga siren")),
    ("克林克兹（Clinkz）", ("骨弓", "小骷髅", "clinkz")),
    ("帕格纳（Pugna）", ("骨法", "pugna")),
    ("水晶室女（Crystal Maiden）", ("冰女", "cm", "crystal maiden")),
    ("莉娜（Lina）", ("火女", "lina")),
    ("痛苦女王（Queen of Pain）", ("女王", "qop", "queen of pain")),
    ("殁境神蚀者（Outworld Destroyer）", ("黑鸟", "od", "outworld destroyer")),
    ("祈求者（Invoker）", ("卡尔", "invoker")),
    ("修补匠（Tinker）", ("tk", "修补匠", "tinker")),
    ("死亡先知（Death Prophet）", ("死亡先知", "dp", "death prophet")),
    ("帕克（Puck）", ("仙女龙", "puck")),
    ("莱席拉克（Leshrac）", ("老鹿", "leshrac")),
    ("食人魔魔法师（Ogre Magi）", ("蓝胖", "ogre magi")),
    ("光之守卫（Keeper of the Light）", ("光法", "kotl", "keeper of the light")),
    ("瘟疫法师（Necrophos）", ("瘟疫法师", "死灵法", "死灵法师", "nec", "necrophos")),
    ("自然先知（Nature's Prophet）", ("先知", "furion", "nature's prophet")),
    ("暗影萨满（Shadow Shaman）", ("小y", "小歪", "shadow shaman")),
    ("干扰者（Disruptor）", ("萨尔", "disruptor")),
    ("戴泽（Dazzle）", ("暗牧", "戴泽", "dazzle")),
    ("工程师（Techies）", ("炸弹人", "炸弹", "techies")),
    ("赏金猎人（Bounty Hunter）", ("赏金", "bh", "bounty hunter")),
    ("力丸（Riki）", ("隐刺", "力丸", "riki")),
    ("噬魂鬼（Lifestealer）", ("小狗", "噬魂鬼", "lifestealer")),
    ("齐天大圣（Monkey King）", ("大圣", "mk", "monkey king")),
    ("主宰（Juggernaut）", ("剑圣", "jugg", "juggernaut")),
    ("冥魂大帝（Wraith King）", ("骷髅王", "wk", "wraith king")),
    ("混沌骑士（Chaos Knight）", ("混沌", "ck", "chaos knight")),
    ("露娜（Luna）", ("月骑", "露娜", "luna")),
    ("恐怖利刃（Terrorblade）", ("tb", "恐怖利刃", "terrorblade")),
    ("虚空假面（Faceless Void）", ("虚空", "faceless void")),
    ("巨魔战将（Troll Warlord）", ("巨魔", "troll", "troll warlord")),
    ("龙骑士（Dragon Knight）", ("龙骑", "dk", "dragon knight")),
    ("钢背兽（Bristleback）", ("钢背", "刚背", "刚被", "bristleback")),
    ("半人马战行者（Centaur Warrunner）", ("人马", "centaur", "centaur warrunner")),
    ("马格纳斯（Magnus）", ("猛犸", "马格纳斯", "magnus")),
    ("潮汐猎人（Tidehunter）", ("潮汐", "tide", "tidehunter")),
    ("军团指挥官（Legion Commander）", ("军团", "lc", "legion commander")),
    ("末日使者（Doom）", ("末日", "doom")),
    ("昆卡（Kunkka）", ("船长", "kunkka")),
    ("孽主（Underlord）", ("大屁股", "孽主", "underlord")),
    ("石鳞剑士（Pangolier）", ("滚滚", "pangolier")),
    ("伐木机（Timbersaw）", ("伐木机", "花母鸡", "timbersaw")),
    ("发条技师（Clockwerk）", ("发条", "clockwerk")),
    ("炼金术士（Alchemist）", ("炼金", "alchemist")),
    ("沙王（Sand King）", ("沙王", "sk", "sand king")),
    ("剃刀（Razor）", ("电魂", "电棍", "razor")),
    ("哈斯卡（Huskar）", ("神灵", "huskar")),
    ("蝙蝠骑士（Batrider）", ("蝙蝠", "batrider")),
    ("兽王（Beastmaster）", ("兽王", "beastmaster")),
    ("斧王（Axe）", ("斧王", "axe")),
    ("帕吉（Pudge）", ("屠夫", "胖子", "pudge")),
)


def _tag_identity_key(value: object) -> str:
    """Return a conservative semantic key for short recording tags."""
    key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())
    if len(key) >= 4 and len(key) % 2 == 0:
        half = len(key) // 2
        if key[:half] == key[half:]:
            key = key[:half]
    return key


def dedupe_recording_tags(tags: Iterable[object], limit: int | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        tag = str(raw_tag or "").strip()
        key = _tag_identity_key(tag)
        if not tag or not key or key in seen:
            continue
        result.append(tag)
        seen.add(key)
        if limit is not None and len(result) >= limit:
            break
    return result


def _contains_unverified_dota2_hero(value: object) -> bool:
    folded = str(value or "").casefold()
    for canonical_name, aliases in _DOTA2_HERO_ALIAS_GROUPS:
        terms = (canonical_name.split("（", 1)[0], *aliases)
        for term in terms:
            candidate = str(term or "").strip().casefold()
            if not candidate:
                continue
            if re.fullmatch(r"[a-z][a-z0-9' -]*", candidate):
                if len(candidate) >= 3 and re.search(
                    rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])",
                    folded,
                ):
                    return True
            elif candidate in folded:
                return True
    return False


def filter_unverified_dota2_metadata(
    title_topic: str,
    description: str,
    tags: Iterable[object],
) -> tuple[str, str, list[str], dict[str, Any]]:
    """Remove hero claims when XML/GSI did not identify the streamer uniquely."""
    filtered_topic = "" if _contains_unverified_dota2_hero(title_topic) else title_topic
    filtered_lines: list[str] = []
    for line in str(description or "").splitlines():
        sentences = re.split(r"(?<=[。！？!?])", line)
        filtered_lines.append("".join(
            sentence for sentence in sentences
            if not _contains_unverified_dota2_hero(sentence)
        ))
    filtered_description = "\n".join(filtered_lines).strip()
    original_tags = [str(tag or "").strip() for tag in tags if str(tag or "").strip()]
    hero_tags = [tag for tag in original_tags if _contains_unverified_dota2_hero(tag)]
    filtered_tags = dedupe_recording_tags(tag for tag in original_tags if tag not in hero_tags)
    details = {
        "unverified_hero_topic_removed": filtered_topic != title_topic,
        "unverified_hero_description_removed": filtered_description != str(description or "").strip(),
        "unverified_hero_tags_removed": hero_tags,
    }
    return filtered_topic, filtered_description, filtered_tags, details


_DOTA2_ITEM_CONTEXT_ALIASES = (
    "bkb",
    "mkb",
    "a杖",
    "a魔晶",
    "跳刀",
    "力量跳",
    "敏捷跳",
    "智力跳",
    "羊刀",
    "大根",
    "大灵匣",
    "小灵匣",
    "大吹风",
    "推推",
    "大推推",
    "大炮",
    "小炮",
    "大电锤",
    "小电锤",
    "大隐刀",
    "大晕锤",
    "小晕锤",
    "大散失",
    "大骨灰",
    "大支配",
    "大勋章",
)


def recording_cover_has_dota2_context(streamer: str, *content: str) -> bool:
    """Avoid treating ordinary words as Dota items on unrelated streams."""
    normalized_streamer = normalize_dota2_streamer_name(streamer)
    known_streamers = {
        canonical_name
        for canonical_name, _aliases in DOTA2_STREAMER_ALIAS_GROUPS
    }
    known_streamers.add("果小果")
    if normalized_streamer in known_streamers:
        return True
    combined = "\n".join(str(value or "") for value in content).casefold()
    if re.search(r"(?<![a-z0-9])dota\s*2?(?![a-z0-9])|刀塔", combined):
        return True
    return any(alias.casefold() in combined for alias in _DOTA2_ITEM_CONTEXT_ALIASES)


def recording_cover_dota2_instruction(*content: str) -> str:
    """Resolve common Chinese Dota 2 hero nicknames for the image prompt."""
    combined = "\n".join(str(value or "") for value in content)
    folded = combined.casefold()
    matched: list[str] = []
    for canonical_name, aliases in _DOTA2_HERO_ALIAS_GROUPS:
        for alias in sorted(aliases, key=len, reverse=True):
            alias_folded = alias.casefold()
            if re.fullmatch(r"[a-z][a-z0-9' -]*", alias_folded):
                found = re.search(
                    rf"(?<![a-z0-9]){re.escape(alias_folded)}(?![a-z0-9])",
                    folded,
                )
            else:
                found = alias_folded in folded
            if found:
                matched.append(f"{alias}＝{canonical_name}")
                break

    resolved = "；".join(matched) if matched else "本次未检出可确定的英雄俗称"
    storm_spirit_rule = ""
    if any("Storm Spirit" in item for item in matched):
        storm_spirit_rule = (
            "特别注意：蓝猫只能是风暴之灵（Storm Spirit）——蓝色皮肤、宽体型男性元素之灵、"
            "蓝色东方长袍与圆帽、环绕闪电能量；绝对不能画成蓝色猫、猫咪吉祥物或其他作品的猫。"
        )
    return (
        "Dota 2 游戏角色消歧规则：如果标题或摘要涉及 DOTA、Dota 2、刀塔，或出现英雄俗称，"
        "必须把它理解为 Valve《Dota 2》的对应英雄，并按该英雄在 Dota 2 中可辨识的体型、"
        "服装、主色、武器与技能特效来设计；禁止按词语字面画成动物、普通人物，也禁止混入"
        "《英雄联盟》、宝可梦或其他作品的角色。"
        f"本次识别结果：{resolved}。"
        f"{storm_spirit_rule}"
        "若摘要里还有未列出的 Dota 2 俗称，应先在语义上还原为该英雄的中英文正式名再作画；"
        "无法确定时宁可使用 Dota 2 对局氛围和技能特效，不要凭字面臆造角色。"
    )


def recording_cover_dota2_streamer_instruction(
    streamer: str,
    *content: str,
) -> str:
    """Resolve common Dota 2 streamer nicknames without replacing the cover subject."""
    combined = "\n".join((str(streamer or ""), *(str(value or "") for value in content)))
    folded = combined.casefold()
    normalized_streamer = normalize_dota2_streamer_name(streamer)
    matched: list[str] = []
    seen: set[str] = set()
    for canonical_name, aliases in DOTA2_STREAMER_ALIAS_GROUPS:
        found_alias = ""
        for alias in sorted(aliases, key=len, reverse=True):
            alias_folded = alias.casefold()
            if re.fullmatch(r"[a-z][a-z0-9_ -]*", alias_folded):
                found = re.search(
                    rf"(?<![a-z0-9]){re.escape(alias_folded)}(?![a-z0-9])",
                    folded,
                )
            else:
                found = alias_folded in folded
            if found:
                found_alias = alias
                break
        if (
            canonical_name == normalized_streamer
            or found_alias
        ) and canonical_name not in seen:
            seen.add(canonical_name)
            matched.append(
                f"{found_alias or streamer}＝Dota 2 主播/选手 {canonical_name}"
            )
    if not matched:
        return (
            "斗鱼 Dota 2 主播昵称规则：遇到主播昵称或职业选手外号时，应结合 Dota 2 语境理解，"
            "不要把昵称按字面画成动物、职业或陌生虚构人物；无法确认身份时不要擅自换脸。"
        )
    return (
        "斗鱼 Dota 2 主播昵称消歧："
        + "；".join(matched)
        + "。这些映射只用于理解标题和事件；封面主体仍必须以当前直播间主播及上传的头像/"
        "专用参考图为准，其他被提及选手不能取代主播成为另一张脸。"
    )


def recording_cover_streamer_expression_instruction(
    streamer: str,
    *content: str,
) -> str:
    """Let known streamer references react to the segment without losing identity."""
    if normalize_dota2_streamer_name(streamer) != "YYF":
        return ""
    context = "\n".join(str(value or "") for value in content)
    return (
        "YYF 表情与本段对局联动：先根据核心标题和内容摘要判断本段最主要的比赛情绪，再调整"
        "参考角色中 YYF 的表情与轻微姿态。优势、高光或连胜可表现为兴奋、自信或得意；"
        "失误、被翻盘或惨败可表现为震惊、懊恼、无奈或气急；逆风、关键团战或翻盘过程可表现为"
        "紧张、专注、坚定；欢乐整活或节目效果可表现为大笑、憋笑或夸张惊讶。"
        "没有明确结果时使用专注、自然的对局表情。表情强度要适合视频封面、清楚但不过度扭曲；"
        "必须保持该 Q 版角色的脸型、五官比例、黑色短发、右脸小痣、蓝色鱼形头套和身份辨识度，"
        "不能换脸、真人化或变成另一个卡通人物，也不能仅照抄底稿中的原始表情。"
        f"本段判断依据：{context[:600]}"
    )


def generate_recording_cover_with_ai(
    title: str,
    ai_topic: str,
    description: str,
    streamer: str,
    cfg: dict[str, Any],
    work_dir: Path,
    target_size: tuple[int, int] | None = None,
    output_path: Path | None = None,
    recording_dir: Path | None = None,
    game_context: dict[str, Any] | None = None,
    game_context_locked: bool = False,
) -> tuple[Path | None, dict[str, Any]]:
    """Generate one independent AI cover for the requested Bilibili aspect ratio."""
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
    image_api_key = str(
        ai_cfg.get("OPENAI_IMAGE_API_KEY")
        or ai_cfg.get("OPENAI_API_KEY")
        or ""
    ).strip()
    if not image_api_key:
        raise ValueError("未配置图片或全局 AI API Key，无法生成录播封面")

    image_model = str(ai_cfg.get("OPENAI_IMAGE_MODEL_NAME") or "gpt-image-2").strip()
    image_base_url = str(ai_cfg.get("OPENAI_IMAGE_BASE_URL") or "").strip()
    client_config = dict(ai_cfg)
    client_config["OPENAI_API_KEY"] = image_api_key
    if image_base_url:
        client_config["OPENAI_BASE_URL"] = image_base_url
    custom_reference_value = str(cfg.get("cover_reference_path") or "").strip()
    custom_reference_path = Path(custom_reference_value).expanduser()
    if custom_reference_value and not custom_reference_path.is_absolute():
        custom_reference_path = WORKSPACE_ROOT / custom_reference_path
    custom_reference = (
        (normalize_dota2_streamer_name(streamer) or streamer, custom_reference_path)
        if custom_reference_value and custom_reference_path.is_file()
        else None
    )
    reference = custom_reference or recording_cover_reference(streamer)
    reference_name = reference[0] if reference else ""
    reference_kind = "custom" if custom_reference else ("dedicated" if reference else "")
    reference_paths: list[Path] = [reference[1]] if reference else []
    avatar_url = str(cfg.get("streamer_avatar_url") or "").strip()
    if avatar_url and not reference:
        try:
            avatar_reference = download_recording_avatar_reference(avatar_url, cfg)
            if avatar_reference not in reference_paths:
                reference_paths.append(avatar_reference)
            if not reference:
                reference = (streamer or "直播间头像", avatar_reference)
                reference_name = reference[0]
                reference_kind = "avatar"
        except (OSError, ValueError) as exc:
            details["ai_cover_avatar_reference_error"] = str(exc)
    if reference_kind == "dedicated":
        reference_instruction = recording_cover_reference_instruction(reference_name)
    elif reference_kind == "custom":
        reference_instruction = (
            f"上传的参考图是用户为主播 {streamer or '主播'} 指定的人物形象底稿，"
            "必须把图中的人物或角色作为封面唯一主角，严格保持脸部、发型、服装、"
            "标志性配饰、主色与画风的辨识度。可以根据本段内容调整表情、动作和背景，"
            "但不得换脸、真人化或替换成其他角色。"
        )
    elif reference_kind == "avatar":
        reference_instruction = recording_avatar_reference_instruction(streamer)
    else:
        reference_instruction = ""
    dota2_instruction = recording_cover_dota2_instruction(
        title,
        ai_topic,
        description,
    )
    # Prefer Douyu's explicit streamer-view hero and its final in-recording
    # equipment snapshot. XML identity is retained only for legacy snapshots.
    tooltip_hero = ""
    tooltip_items: list[str] = []
    tooltip_kda_instruction = ""
    tooltip_context_enabled = bool(cfg.get("douyu_stats_enabled", True)) and bool(
        cfg.get("douyu_stats_cover_context_enabled", True)
    )
    details["ai_cover_tooltip_context_enabled"] = tooltip_context_enabled
    if tooltip_context_enabled and (game_context_locked or recording_dir is not None):
        try:
            anchor = game_context
            if not game_context_locked and recording_dir is not None:
                from modules.douyu_stats_formatter import get_game_for_cover  # type: ignore
                anchor = get_game_for_cover(recording_dir)
            if anchor:
                tooltip_hero = str(anchor.get("hero") or "")
                tooltip_items = [
                    str(item) for item in anchor.get("items", [])[:6] if str(item)
                ]
                if anchor.get("neutral"):
                    tooltip_items.append(str(anchor["neutral"]))
                if anchor.get("scepter"):
                    tooltip_items.append("A杖")
                if anchor.get("shard"):
                    tooltip_items.append("魔晶")
                if all(key in anchor for key in ("kills", "deaths", "assists")):
                    tooltip_kda_instruction = (
                        f"主播本局最终 K/D/A 为 {anchor['kills']}/{anchor['deaths']}/"
                        f"{anchor['assists']}，KDA 为 {anchor.get('kda')}。"
                    )
                    details["ai_cover_streamer_kda"] = {
                        "kills": anchor["kills"],
                        "deaths": anchor["deaths"],
                        "assists": anchor["assists"],
                        "kda": anchor.get("kda"),
                    }
                details["ai_cover_identity_source"] = str(
                    anchor.get("identity_source") or ""
                )
        except Exception as exc:
            details["ai_cover_tooltip_error"] = str(exc)

    if tooltip_hero or tooltip_items:
        if tooltip_items:
            dota2_item_instruction = (
                f"主播本局最终六格主装备（最后一次有效阵容快照）："
                f"{', '.join(tooltip_items)}。"
                "只能表现这份列表中的主装备，数量不得超过列表数量；不得额外添加第七件装备。"
                "装备名称只用于身份识别，禁止按中文或英文名称的字面含义自行设计外形。"
                "禁止在封面底部或任何位置生成物品栏、装备卡槽、装备图标排布或游戏 UI；"
                "装备只可作为角色造型与场景语义参考，不得绘制仿冒的装备图标。"
            )
        else:
            dota2_item_instruction = ""
        if tooltip_hero:
            dota2_instruction = (
                f"主播本局使用的英雄为 {tooltip_hero}（来自斗鱼主播视角数据）。"
                + tooltip_kda_instruction
                + dota2_instruction
            )
        details["ai_cover_tooltip_hero"] = tooltip_hero
        details["ai_cover_tooltip_items"] = tooltip_items
        details["ai_cover_dota2_source"] = "tooltip"
        dota2_item_matches = match_dota2_items(*tooltip_items)
        dota2_item_instruction += dota2_item_prompt_instruction(dota2_item_matches)
    elif game_context_locked:
        dota2_item_matches = []
        dota2_instruction = (
            "本段没有可靠匹配到主播同一场对局。禁止展示、猜测或补画任何具体 "
            "DOTA 2 英雄；如需游戏氛围，只能使用不含角色身份的抽象场景。"
        )
        dota2_item_instruction = (
            "本段没有可靠匹配到主播同一场对局的英雄与装备数据。"
            "禁止展示、猜测或补画任何具体 DOTA 2 英雄和装备图标。"
        )
        details["ai_cover_dota2_source"] = "locked_no_match"
    else:
        dota2_item_matches = (
            match_dota2_items(title, ai_topic, description)
            if recording_cover_has_dota2_context(
                streamer,
                title,
                ai_topic,
                description,
            )
            else []
        )
        dota2_item_instruction = dota2_item_prompt_instruction(dota2_item_matches)
        details["ai_cover_dota2_source"] = "text_match"
    if dota2_item_matches:
        item_reference_path, item_reference_errors = build_dota2_item_reference_sheet(
            dota2_item_matches,
            Path("/data/cache/dota2/items"),
            work_dir / "dota2_item_references.png",
        )
        details["ai_cover_dota2_items"] = [
            {
                "alias": match.alias,
                "chinese_name": match.item.chinese_name,
                "english_name": match.item.english_name,
                "icon_slug": match.item.icon_slug,
            }
            for match in dota2_item_matches
        ]
        details["ai_cover_dota2_item_reference_errors"] = item_reference_errors
        if item_reference_path is not None:
            reference_paths.append(item_reference_path)
            details["ai_cover_dota2_item_reference_used"] = True
            details["ai_cover_dota2_item_reference_path"] = str(item_reference_path)
        else:
            details["ai_cover_dota2_item_reference_used"] = False
            dota2_item_instruction = (
                "本局装备的官方图标参考不可用。为避免画错装备，禁止展示任何具体装备图标。"
            )
    if tooltip_hero:
        hero_reference_path, official_hero, hero_reference_error = (
            build_dota2_hero_reference(
                tooltip_hero,
                Path("/data/cache/dota2/heroes"),
                work_dir / "dota2_hero_reference.png",
            )
        )
        details["ai_cover_dota2_official_hero"] = (
            {
                "chinese_name": official_hero.chinese_name,
                "english_name": official_hero.english_name,
                "icon_slug": official_hero.icon_slug,
            }
            if official_hero
            else None
        )
        details["ai_cover_dota2_hero_reference_error"] = hero_reference_error
        if hero_reference_path is not None:
            reference_paths.append(hero_reference_path)
            details["ai_cover_dota2_hero_reference_used"] = True
            details["ai_cover_dota2_hero_reference_path"] = str(hero_reference_path)
            dota2_instruction = (
                f"随附的 DOTA 2 OFFICIAL HERO REFERENCE 是 {tooltip_hero} 的 Valve 官方英雄参考。"
                "若画面出现该英雄，必须保持官方脸部、体型、护甲、武器、轮廓和主色特征；"
                "不得替换成其他英雄、其他游戏角色或仅凭中文名称臆造。"
                + dota2_instruction
            )
        else:
            details["ai_cover_dota2_hero_reference_used"] = False
            dota2_instruction = (
                "本局英雄的官方参考图不可用。为避免画错英雄，禁止展示任何具体英雄。"
            )
    dota2_ability_matches = (
        match_dota2_abilities(title, ai_topic, description)
        if recording_cover_has_dota2_context(
            streamer,
            title,
            ai_topic,
            description,
        )
        else []
    )
    dota2_ability_instruction = dota2_ability_prompt_instruction(
        dota2_ability_matches
    )
    if dota2_ability_matches:
        ability_reference_path, ability_reference_errors = (
            build_dota2_ability_reference_sheet(
                dota2_ability_matches,
                resolve_path(".dota2-ability-cache", cfg),
                work_dir / "dota2_ability_references.png",
            )
        )
        details["ai_cover_dota2_abilities"] = [
            {
                "alias": match.alias,
                "hero_chinese_name": match.ability.hero_chinese_name,
                "hero_english_name": match.ability.hero_english_name,
                "chinese_name": match.ability.chinese_name,
                "english_name": match.ability.english_name,
                "icon_slug": match.ability.icon_slug,
            }
            for match in dota2_ability_matches
        ]
        details["ai_cover_dota2_ability_reference_errors"] = (
            ability_reference_errors
        )
        if ability_reference_path is not None:
            reference_paths.append(ability_reference_path)
            details["ai_cover_dota2_ability_reference_used"] = True
            details["ai_cover_dota2_ability_reference_path"] = str(
                ability_reference_path
            )
        else:
            details["ai_cover_dota2_ability_reference_used"] = False
    dota2_streamer_instruction = recording_cover_dota2_streamer_instruction(
        streamer,
        title,
        ai_topic,
        description,
    )
    streamer_expression_instruction = recording_cover_streamer_expression_instruction(
        streamer,
        title,
        ai_topic,
        description,
    )
    target_width, target_height = target_size or (1146, 717)
    orientation = "横向" if target_width >= target_height else "竖向"
    aspect_label = (
        "16:10"
        if target_size is None
        else f"{target_width}:{target_height}"
    )
    if abs((target_width / target_height) - (16 / 9)) < 0.02:
        composition_instruction = (
            "这是个人空间横向封面。主体和唯一标题必须完整留在 16:9 横向安全区域，"
            "左右保留呼吸空间，适合个人空间大图展示。"
        )
        cover_variant = "16x9"
    elif abs((target_width / target_height) - (4 / 3)) < 0.02:
        composition_instruction = (
            "这是首页推荐 4:3 卡片封面。重新采用更集中、更紧凑的独立构图，"
            "主体和唯一标题靠近视觉中心，不能沿用或模拟 16:9 封面的裁切结果。"
        )
        cover_variant = "4x3"
    else:
        composition_instruction = "请针对目标画幅独立构图，主体和标题均保持完整。"
        cover_variant = aspect_label.replace(":", "x")
    prompt = f"""
为直播录播生成一张{orientation} {aspect_label} 视频封面，画面精致、主体明确、对比强烈，在缩略图尺寸下仍清晰。
主播：{streamer or "主播"}
AI 生成的核心标题：{headline}
内容摘要：{str(description or "")[:500]}

只围绕核心标题设计画面，可将“{headline}”作为唯一标题文字；不要出现完整投稿标题。
{composition_instruction}
{dota2_instruction}
{dota2_item_instruction}
{dota2_ability_instruction}
{dota2_streamer_instruction}
{streamer_expression_instruction}
{reference_instruction}
绝对禁止出现日期、年份、月份、星期、钟表、具体时间、时间戳、倒计时、房间号、视频时长、平台界面、二维码和水印。
不要添加“直播回放”、主播开播时间或任何数字日期信息。避免大段文字，中文必须清楚易读。
本直播间的封面创作要求：{str(cfg.get("ai_cover_prompt") or DEFAULT_RECORDING_COVER_AI_PROMPT).strip()}
""".strip()
    image_client = get_openai_client(client_config).images
    requested_ratio = (target_width / target_height) if target_height else 0
    if abs(requested_ratio - (16 / 9)) < 0.02:
        image_size_key = "OPENAI_IMAGE_SIZE_16X9"
    elif abs(requested_ratio - (4 / 3)) < 0.02:
        image_size_key = "OPENAI_IMAGE_SIZE_4X3"
    else:
        image_size_key = "OPENAI_IMAGE_SIZE"
    image_size = str(
        ai_cfg.get(image_size_key)
        or ai_cfg.get("OPENAI_IMAGE_SIZE")
        or "1536x1024"
    )
    if reference_paths:
        with ExitStack() as stack:
            reference_handles = [
                stack.enter_context(path.open("rb"))
                for path in reference_paths
            ]
            response = image_client.edit(
                model=image_model,
                image=(
                    reference_handles
                    if len(reference_handles) > 1
                    else reference_handles[0]
                ),
                prompt=prompt,
                size=image_size,
            )
        details.update({
            "ai_cover_reference_used": True,
            "ai_cover_reference_name": reference_name,
            "ai_cover_reference_path": str(reference_paths[0]),
            "ai_cover_reference_paths": [str(path) for path in reference_paths],
            "ai_cover_reference_count": len(reference_paths),
            "ai_cover_reference_kind": reference_kind,
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
    source = work_dir / f"ai_cover_{cover_variant}_source.png"
    cover = output_path or (work_dir / "ai_cover.jpg")
    cover.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(raw)
    ffmpeg = str(cfg.get("ffmpeg", "ffmpeg"))
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-vf",
        (
            f"scale={target_width}:{target_height}:"
            "force_original_aspect_ratio=increase,"
            f"crop={target_width}:{target_height}"
        ),
        "-frames:v", "1", "-q:v", "2", str(cover),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if output_path is not None:
        source.unlink(missing_ok=True)
        try:
            work_dir.rmdir()
        except OSError:
            pass
    if completed.returncode != 0 or not cover.is_file():
        message = completed.stderr.strip()[-1000:]
        raise RuntimeError(f"AI 封面尺寸处理失败: {message}")
    details.update({
        "ai_cover_generated": True,
        "ai_cover_model": image_model,
        "ai_cover_path": str(cover),
        "ai_cover_prompt": prompt,
        "ai_cover_width": target_width,
        "ai_cover_height": target_height,
        "ai_cover_variant": cover_variant,
        "ai_cover_requested_size": image_size,
    })
    return cover, details


def cleanup_uploaded_recording(
    video: Path,
    danmaku_xml: Path | None,
    upload_video: Path,
    artifact_dir: Path | None = None,
    retained_paths: Iterable[Path | None] = (),
    xml_retention_hours: float = 0.0,
) -> dict[str, Any]:
    """Remove recording inputs and generated artifacts after upload is durable."""
    retained_paths = tuple(retained_paths)
    retained_resolved = {
        candidate.resolve()
        for candidate in retained_paths
        if candidate is not None
    }
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
        if path in retained_resolved:
            continue
        try:
            existed = path.exists() or path.is_symlink()
            path.unlink(missing_ok=True)
            if existed and not path.exists() and not path.is_symlink():
                deleted.append(str(path))
        except OSError as exc:
            failed.append({"kind": kind, "path": str(path), "error": str(exc)})
    if artifact_dir is not None:
        artifact_path = artifact_dir.resolve()
        try:
            if artifact_path.is_dir():
                artifact_files = [
                    str(item.resolve())
                    for item in artifact_path.rglob("*")
                    if item.is_file() or item.is_symlink()
                ]
                shutil.rmtree(artifact_path)
                deleted.extend(
                    item for item in artifact_files
                    if item not in deleted and not Path(item).exists()
                )
                if not artifact_path.exists():
                    deleted.append(str(artifact_path))
        except OSError as exc:
            failed.append({
                "kind": "artifacts",
                "path": str(artifact_path),
                "error": str(exc),
            })
    retained = []
    for candidate in retained_paths:
        if candidate is None:
            continue
        path = candidate.resolve()
        if path.is_file() and str(path) not in retained:
            retained.append(str(path))
    result: dict[str, Any] = {
        "deleted": deleted,
        "retained": retained,
        "failed": failed,
    }
    if (
        danmaku_xml is not None
        and danmaku_xml.resolve() in retained_resolved
        and danmaku_xml.is_file()
        and xml_retention_hours > 0
    ):
        result["retained_xml_path"] = str(danmaku_xml.resolve())
        result["retained_xml_until"] = (
            datetime.now(timezone.utc) + timedelta(hours=float(xml_retention_hours))
        ).isoformat()
    return result


def persist_pipeline_cover(
    store: StateStore,
    key: str,
    cover: Path,
    variant: str = "16x9",
    video: Path | None = None,
) -> Path:
    """Persist the cover next to the recording (or in the artifact dir as fallback)."""
    source = cover.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"投稿封面不存在: {source}")
    suffix = source.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    safe_variant = "4x3" if variant == "4x3" else "16x9"
    if video is not None and video.parent.is_dir():
        stem = video.stem
        name = f"{stem}_4x3{suffix}" if safe_variant == "4x3" else f"{stem}{suffix}"
        target = (video.parent / name).resolve()
    else:
        target_dir = store.path.parent / "artifacts" / "task-covers"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_dir.chmod(0o750)
        target = (target_dir / f"{key}-{safe_variant}{suffix}").resolve()
    if source != target:
        shutil.copy2(source, target)
    target.chmod(0o640)
    return target


def recording_metadata_values(
    video: Path,
    cfg: dict[str, Any],
    ai_topic: str = "",
) -> dict[str, str]:
    stem = video.stem
    datetime_match = re.search(r"(20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}(?:-\d{2})?)", stem)
    time_match = re.search(r"20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(.+)$", stem)
    current_filename_match = re.match(
        r"(.+?)_[0-9a-f]{6}_(.+)_(20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}(?:-\d{2})?)$",
        stem,
        re.IGNORECASE,
    )
    marker_match = re.match(r"(.+?)_[0-9a-f]{6}(?=20\d{2}-\d{2}-\d{2})", stem, re.IGNORECASE)
    streamer = str(cfg.get("streamer_name") or "").strip()
    if not streamer:
        if current_filename_match:
            streamer = current_filename_match.group(1).strip("_- ")
        elif marker_match:
            streamer = marker_match.group(1).strip("_- ")
    streamer = normalize_dota2_streamer_name(streamer)
    if current_filename_match:
        live_title = current_filename_match.group(2).strip("_- ")
    else:
        live_title = time_match.group(1).strip("_- ") if time_match else ""
    topic = re.sub(r"[\r\n｜|]+", " ", str(ai_topic or live_title or "直播精彩内容")).strip()
    if datetime_match:
        recorded_text = datetime_match.group(1)
        recorded_format = (
            "%Y-%m-%d_%H-%M-%S"
            if re.search(r"_\d{2}-\d{2}-\d{2}$", recorded_text)
            else "%Y-%m-%d_%H-%M"
        )
        recorded_at = datetime.strptime(recorded_text, recorded_format)
    elif video.exists():
        recorded_at = datetime.fromtimestamp(video.stat().st_mtime)
    else:
        recorded_at = datetime.now()
    return {
        "stem": stem,
        "name": video.name,
        "suffix": video.suffix.lstrip("."),
        "streamer": streamer or "主播",
        "ai_topic": topic[:28],
        "date": recorded_at.strftime("%m-%d %H:%M"),
        "live_title": live_title,
        "recording_intro": (
            f"直播录播：{streamer or '主播'}《{live_title}》。"
            if live_title
            else f"直播录播：{streamer or '主播'}。"
        ),
    }


def topic_mentions_streamer(topic: str, streamer: str) -> bool:
    """Return whether a topic already names the streamer or a known alias."""
    topic_key = _compact_alias(topic)
    streamer_key = _compact_alias(streamer)
    if not topic_key or not streamer_key or streamer_key == _compact_alias("主播"):
        return False
    candidates = {str(streamer or "").strip(), normalize_dota2_streamer_name(streamer)}
    for canonical_name, aliases in DOTA2_STREAMER_ALIAS_GROUPS:
        keys = {_compact_alias(canonical_name), *(_compact_alias(alias) for alias in aliases)}
        if streamer_key in keys:
            candidates.add(canonical_name)
            candidates.update(aliases)
    return any(
        (candidate_key := _compact_alias(candidate))
        and candidate_key in topic_key
        for candidate in candidates
    )


def render_metadata(
    video: Path,
    cfg: dict[str, Any],
    ai_topic: str = "",
) -> tuple[str, str, list[str]]:
    values = recording_metadata_values(video, cfg, ai_topic)
    title = str(cfg.get("title_template") or DEFAULT_TITLE_TEMPLATE).format_map(values).strip()
    if topic_mentions_streamer(values["ai_topic"], values["streamer"]):
        redundant_prefix = f"{values['streamer']}｜"
        if title.startswith(redundant_prefix):
            title = title[len(redundant_prefix):].lstrip()
    description = str(
        cfg.get("description_template") or DEFAULT_DESCRIPTION_TEMPLATE
    ).format_map(values).strip()
    tags = dedupe_recording_tags(cfg.get("tags", []))
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
        generate_video_tags,
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
        "FIXED_PARTITION_ID_BILIBILI": ai_cfg.get("FIXED_PARTITION_ID_BILIBILI", ""),
        "RECOMMEND_PARTITION_WITH_COVER": include_cover,
    }

    generated_tags: list[str] = []
    final_tags = dedupe_recording_tags(existing_tags)
    if generate_tags_enabled:
        generated_tags = [
            str(tag).strip()
            for tag in (
                generate_video_tags(
                    title,
                    description,
                    openai_config=openai_config,
                    task_id=None,
                )
                or []
            )
            if str(tag).strip()
        ][:6]
        final_tags = dedupe_recording_tags([*final_tags, *generated_tags])

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
    grounding_context: dict[str, Any] | None = None,
    timeline_duration_seconds: float | None = None,
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
            "sampled_comment_evidence": [
                {
                    "timestamp_seconds": max(0, int(float(comment.time))),
                    "text": str(comment.text),
                }
                for comment in selected
            ],
            "verified_live_context": grounding_context or {},
            "timestamp_reaction_delay_seconds": max(
                0,
                min(60, int(cfg.get("ai_danmaku_reaction_delay_seconds", 8) or 0)),
            ),
        }
        legacy_prompt = str(cfg.get("ai_danmaku_prompt") or "").strip()
        title_prompt = str(
            cfg.get("ai_title_prompt") or DEFAULT_RECORDING_TITLE_AI_PROMPT
        ).strip()
        description_prompt = str(
            cfg.get("ai_description_prompt") or DEFAULT_RECORDING_DESCRIPTION_AI_PROMPT
        ).strip()
        legacy_instruction = (
            f"本直播间旧版自定义要求：{legacy_prompt}"
            if legacy_prompt
            else ""
        )
        system_prompt = f"""
你是直播录播编辑。根据按时间采样的观众弹幕，为哔哩哔哩录播生成核心主题和内容充实的中文简介。
只能总结弹幕能支持的主题、高潮时刻和观众反应，不得虚构主播说过的话或未出现的事件。
verified_live_context 是在 AI 之前完成的直播统计与主播同场对局识别结果；英雄、装备和 KDA
只能使用其中已经确认的数据，禁止从弹幕、标题或常识猜测，且不得把其他对局的数据混入本段。
verified_live_context.live_stats 只作为事实参考。description 严禁复制或输出“直播数据”区块、礼物、
在线人数、英雄装备统计表；这些内容由投稿流程在最后一步独立渲染，并且只渲染一次。
不要引用用户名、UID、广告或重复刷屏。base_description 是已清理好的主播和直播标题前缀。
description 只返回弹幕总结正文，不要重复 base_description，也不要输出文件名、内部编号或录制时间。
description 只写两至四段事件总结，不要包含“重要时间点”标题或任何手写时间。
timeline 只选择 sampled_comment_evidence 有直接证据的事件。每项返回 event、evidence_texts
和 evidence_keywords；evidence_texts 必须一字不改地复制输入中 1 至 3 条 text，
evidence_keywords 是这些弹幕中足以支持整个 event 的 1 至 4 个原文关键词。
不要返回时间戳；程序会使用 evidence_keywords 回到完整 XML 查找第一条匹配弹幕。
完整 XML 中必须存在一条同时包含所有关键词的弹幕；否则必须省略该事件，不得用宽泛关键词拼凑结论。
弹幕时间晚于画面事件：应选最早一批明确相关弹幕作为证据锚点，不要选择刷屏高峰；程序会按
timestamp_reaction_delay_seconds 将最终时间统一前移，请勿在 AI 内再次手动减秒。
title_topic 是适合放进标题的自然短语，不加书名号、不含日期和主播名，最多 18 个中文字符。
{DOTA2_METADATA_DISAMBIGUATION}
本直播间的标题要求：{title_prompt}
本直播间的简介要求：{description_prompt}
{legacy_instruction}
返回 JSON 对象：{{"title_topic":"...","description":"...","timeline":[{{"event":"...","evidence_texts":["..."],"evidence_keywords":["..."]}}]}}，description 不超过 1600 个中文字符。
""".strip()
        result = _request_json_object(
            client=get_openai_client(ai_cfg),
            model_name=str(ai_cfg.get("OPENAI_MODEL_NAME", "gpt-4o-mini")),
            system_prompt=system_prompt,
            payload=payload,
            max_tokens=1400,
            temperature=0.2,
            thinking_enabled=bool(ai_cfg.get("OPENAI_THINKING_ENABLED", False)),
            logger_obj=None,
            scene_name="biliup_danmaku_summary",
        )
        generated_description = str((result or {}).get("description", "")).strip()
        generated_description = strip_live_stats_from_description(
            generated_description,
            str((grounding_context or {}).get("live_stats") or ""),
        )
        generated_description = re.sub(
            r"^直播录播[：:].*?[。.!！]\s*",
            "",
            generated_description,
            count=1,
        ).strip()
        generated_description = strip_ai_timeline_lines(generated_description)
        timeline_text = render_grounded_danmaku_timeline(
            (result or {}).get("timeline"),
            selected,
            comments,
            delay_seconds=int(cfg.get("ai_danmaku_reaction_delay_seconds", 8) or 0),
            duration_seconds=timeline_duration_seconds,
        )
        if timeline_text:
            generated_description = "\n\n".join(
                part for part in (generated_description, timeline_text) if part
            )
        description = (
            f"{base_description}{generated_description}"
            if generated_description
            else base_description
        )
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
    review_override = store.review_override(key)
    prior_ai_stage = store.stage_state(key, "ai") if retry else {}
    prior_cover16_stage = store.stage_state(key, "cover_16x9") if retry else {}
    prior_cover43_stage = store.stage_state(key, "cover_4x3") if retry else {}
    if retry and not prior_cover16_stage:
        prior_cover16_stage = store.stage_state(key, "cover")
    if retry and not prior_cover43_stage:
        prior_cover43_stage = store.stage_state(key, "cover")
    is_new_task = not store.upload_exists(key)
    if not store.claim(key, video, platform, retry=retry):
        print(f"SKIP 已处理或正在处理: {video}")
        return True
    if is_new_task and not dry_run:
        emit_recording_task_added_notification(
            cfg,
            fingerprint_value=key,
            video=video,
            task_kind="recording_upload",
        )

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
    recording_duration_seconds = video_duration_seconds(
        video,
        str(cfg.get("ffprobe", "ffprobe")),
    )
    store.finish(key, "processing", {
        **prior_result,
        "worker_pid": os.getpid(),
        "multipart_session": session_key or None,
        "part_number": part_number,
        "video_duration_seconds": recording_duration_seconds,
    })
    work_dir = store.path.parent / "artifacts" / key[:16]
    current_stage = "ass"
    try:
        if blocked_by_pending_part:
            current_stage = "upload"
            raise RuntimeError("前一分P尚未上传成功，请先重试前一分P")

        title, description, tags = render_metadata(video, cfg)
        manual_cover_path = str(review_override.get("cover_path") or "").strip()
        manual_cover43_path = str(review_override.get("cover43_path") or "").strip()
        if manual_cover_path and Path(manual_cover_path).is_file():
            original_cover = Path(manual_cover_path)
        else:
            current_stage = "cover_16x9"
            original_cover = find_cover(video, cfg, work_dir)
        cover = original_cover
        cover43: Path | None = (
            Path(manual_cover43_path)
            if manual_cover43_path and Path(manual_cover43_path).is_file()
            else None
        )
        source_url = str(cfg.get("source_url", "")).strip()

        current_stage = "ass"
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
                ass_details = danmaku_stage_details(video, danmaku_xml, comments, cfg)
                ass_details.update({
                    "danmaku_xml": str(danmaku_xml),
                    "ass_path": str(ass_path),
                    "burn_in": bool(cfg.get("danmaku_burn_in", False)),
                })
                store.stage(
                    key,
                    "ass",
                    "warning"
                    if ass_details["danmaku_integrity"] == "suspected_incomplete"
                    else "completed",
                    ass_details,
                )
            else:
                print(f"WARN 弹幕 XML 中没有可用弹幕: {danmaku_xml}", file=sys.stderr)
                ass_details = danmaku_stage_details(video, danmaku_xml, comments, cfg)
                ass_details.update({
                    "danmaku_xml": str(danmaku_xml),
                    "reason": "XML 中没有可用弹幕",
                })
                store.stage(
                    key,
                    "ass",
                    "warning"
                    if ass_details["danmaku_integrity"] == "suspected_incomplete"
                    else "skipped",
                    ass_details,
                )
        else:
            store.stage(
                key,
                "ass",
                "skipped",
                {
                    "reason": "未找到弹幕 XML 或弹幕处理未启用",
                    "video_duration_seconds": recording_duration_seconds,
                },
            )

        # Collect stable live context before AI so metadata and both cover
        # variants are grounded in the same recording and the same game.
        y2a_root = resolve_path(str(cfg.get("y2a_root", "y2a-auto")), cfg)
        if str(y2a_root) not in sys.path:
            sys.path.insert(0, str(y2a_root))
        stats_enabled = bool(cfg.get("douyu_stats_enabled", True))
        append_stats_enabled = bool(cfg.get("douyu_stats_append_description", True))
        cover_context_enabled = bool(cfg.get("douyu_stats_cover_context_enabled", True))
        stats_text = ""
        live_stats_prepared = True
        current_stage = "live_stats"
        if not stats_enabled:
            store.stage(key, "live_stats", "skipped", {"reason": "斗鱼直播数据统计已关闭", "outcome": "disabled"})
        else:
            store.stage(key, "live_stats", "running", {"description_before_length": len(description)})
            try:
                from modules.douyu_stats_formatter import get_stats_for_description  # type: ignore
                stats_text = str(get_stats_for_description(str(video.parent)) or "")[:1900]
                if stats_text:
                    store.stage(
                        key,
                        "live_stats",
                        "completed",
                        live_stats_stage_details(stats_text),
                    )
                else:
                    store.stage(key, "live_stats", "skipped", {"reason": "本次录播时间内没有匹配的直播数据", "outcome": "no_data"})
            except Exception as exc:
                store.stage(key, "live_stats", "warning", {"reason": "直播数据整理失败，但不阻断投稿", "outcome": "failed_non_blocking"}, error=str(exc))

        locked_game_context: dict[str, Any] | None = None
        identity_prepared = True
        current_stage = "xml_identity"
        if not stats_enabled:
            store.stage(key, "xml_identity", "skipped", {"reason": "斗鱼直播数据统计已关闭", "outcome": "disabled"})
        elif not cover_context_enabled:
            store.stage(key, "xml_identity", "skipped", {"reason": "XML 主播英雄与装备识别已关闭", "outcome": "disabled"})
        else:
            store.stage(key, "xml_identity", "running", {"danmaku_xml": str(danmaku_xml or ""), "comment_count": len(comments)})
            try:
                from modules.douyu_stats_formatter import get_game_for_cover, get_identity_diagnostics  # type: ignore
                locked_game_context = get_game_for_cover(str(video.parent))
                identity_diagnostics = get_identity_diagnostics(str(video.parent))
                if locked_game_context:
                    anchor = locked_game_context
                    store.stage(key, "xml_identity", "completed", {
                        "danmaku_xml": str(danmaku_xml or ""), "comment_count": len(comments),
                        **identity_diagnostics, "streamer_hero": str(anchor.get("hero") or ""),
                        "streamer_items": [str(item) for item in anchor.get("items", [])[:6] if str(item)],
                        "streamer_neutral": str(anchor.get("neutral") or ""),
                        "streamer_scepter": bool(anchor.get("scepter")),
                        "streamer_shard": bool(anchor.get("shard")),
                        "equipment_snapshot_unix_ts": float(anchor.get("equipment_snapshot_unix_ts") or 0),
                        "gsi_observed_seconds": float(anchor.get("gsi_observed_seconds") or 0),
                        "xml_mention_score": int(anchor.get("xml_mention_score") or 0),
                        "xml_runner_up_score": int(anchor.get("xml_runner_up_score") or 0),
                        "xml_mention_share": float(anchor.get("xml_mention_share") or 0),
                        "identity_source": str(anchor.get("identity_source") or ""),
                        "kills": anchor.get("kills"), "deaths": anchor.get("deaths"),
                        "assists": anchor.get("assists"), "kda": anchor.get("kda"),
                        "outcome": "matched",
                    })
                else:
                    store.stage(key, "xml_identity", "skipped", {**identity_diagnostics, "reason": "未形成唯一可靠的主播同场对局证据", "outcome": "no_data"})
            except Exception as exc:
                store.stage(key, "xml_identity", "warning", {"reason": "主播英雄识别失败，但不阻断投稿", "outcome": "failed_non_blocking"}, error=str(exc))

        verified_live_context: dict[str, Any] = {"live_stats": stats_text}
        if locked_game_context:
            verified_live_context["game"] = {
                key_name: locked_game_context.get(key_name)
                for key_name in ("hero", "items", "neutral", "scepter", "shard", "kills", "deaths", "assists", "kda", "identity_source")
                if locked_game_context.get(key_name) not in (None, "", [])
            }

        current_stage = "ai"
        ai_topic = ""
        ai_details: dict[str, Any] = {}
        prior_ai_details = (
            prior_ai_stage.get("details")
            if isinstance(prior_ai_stage.get("details"), dict)
            else {}
        )
        reuse_ai = bool(
            retry
            and prior_ai_stage.get("status") in {"completed", "skipped"}
            and prior_ai_details.get("title")
            and (
                prior_ai_details.get("description_body")
                or prior_ai_details.get("description")
            )
        )
        partition = str(cfg.get("bilibili_partition_id", "")).strip()
        metadata_automation: dict[str, Any] = {}
        if reuse_ai:
            ai_details = dict(prior_ai_details)
            title = str(ai_details.get("title") or title)
            # New tasks persist the editorial body separately. For old tasks,
            # migrate the previously composed description back to a clean body
            # before the one and only submission composition step below.
            description = strip_live_stats_from_description(
                str(
                    ai_details.get("description_body")
                    or ai_details.get("description")
                    or description
                ),
                stats_text,
            )
            ai_details["description_body"] = description
            ai_topic = str(ai_details.get("title_topic") or "")
            previous_tags = ai_details.get("final_tags")
            if isinstance(previous_tags, list):
                tags = [
                    str(tag).strip()
                    for tag in previous_tags
                    if str(tag).strip()
                ]
            partition = str(
                ai_details.get("selected_partition_id")
                or prior_result.get("partition_id")
                or partition
            ).strip()
            previous_automation = prior_result.get("metadata_automation")
            if isinstance(previous_automation, dict):
                metadata_automation = dict(previous_automation)
            ai_details["reused_on_retry"] = True
            store.stage(
                key,
                "ai",
                str(prior_ai_stage.get("status") or "completed"),
                ai_details,
            )
        else:
            if comments and not dry_run and bool(cfg.get("ai_danmaku_summary_enabled", True)):
                store.stage(key, "ai", "running", {"comment_count": len(comments)})
                description, ai_topic = generate_danmaku_metadata_with_ai(
                    comments,
                    description,
                    cfg,
                    verified_live_context,
                    recording_duration_seconds,
                )
                title, _, _ = render_metadata(video, cfg, ai_topic=ai_topic)
                ai_details.update({
                    "title_topic": ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                    "title": title,
                    "description": description,
                    "description_body": description,
                    "comment_count": len(comments),
                })
            else:
                reason = "试运行" if dry_run else ("未配置可分析弹幕" if not comments else "AI 简介未启用")
                ai_details.update({
                    "reason": reason,
                    "title": title,
                    "description": description,
                    "description_body": description,
                })

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

        metadata_values_for_evidence = recording_metadata_values(video, cfg)
        if not locked_game_context and recording_cover_has_dota2_context(
            metadata_values_for_evidence["streamer"],
            title,
            description,
            *tags,
        ):
            original_ai_topic = ai_topic
            filtered_topic, filtered_description, filtered_tags, evidence_filter = (
                filter_unverified_dota2_metadata(ai_topic, description, tags)
            )
            ai_topic = filtered_topic
            if filtered_description:
                description = filtered_description
            tags = filtered_tags
            if ai_topic != original_ai_topic:
                title, _, _ = render_metadata(video, cfg, ai_topic=ai_topic)
            ai_details.update(evidence_filter)
            ai_details["title_topic"] = ai_topic or metadata_values_for_evidence["ai_topic"]
            ai_details["title"] = title
            ai_details["description"] = description
            ai_details["final_tags"] = tags
            metadata_automation["final_tags"] = tags

        part_values = recording_metadata_values(video, cfg, ai_topic=ai_topic)
        part_topic = str(ai_topic or part_values["ai_topic"]).strip()
        part_description = description
        part_generated_title = title
        if multipart:
            title = str(multipart.get("title") or title)
            tags = list(multipart.get("tags") or tags)
            source_url = str(multipart.get("source_url") or source_url)
            partition = str(multipart.get("partition_id") or partition)
            if isinstance(multipart.get("metadata_automation"), dict):
                metadata_automation = dict(multipart["metadata_automation"])
                ai_details.update(metadata_automation)

        if review_override:
            title = str(review_override.get("title") or title).strip()
            description = strip_live_stats_from_description(
                str(review_override.get("description") or description),
                stats_text,
            )
            part_description = description
            override_tags = review_override.get("tags")
            if isinstance(override_tags, list):
                tags = dedupe_recording_tags(override_tags, limit=6)
            partition = str(review_override.get("partition_id") or partition).strip()

        tags = dedupe_recording_tags(tags, limit=12)

        page_title = recording_part_title(video, part_number, part_topic)
        multipart_parts: list[dict[str, Any]] = []
        recording_intro = part_values["recording_intro"]
        if multipart:
            multipart_parts = [
                dict(item)
                for item in (multipart.get("parts") or [])
                if isinstance(item, dict)
            ]
            # Upgrade an active session created before per-part metadata existed.
            if existing_submission and not multipart_parts and multipart.get("description"):
                legacy_title = str(multipart.get("title") or "")
                legacy_fields = [field.strip() for field in legacy_title.split("｜")]
                legacy_topic = legacy_fields[1] if len(legacy_fields) > 1 else "直播精彩内容"
                multipart_parts.append({
                    "part_number": 1,
                    "title_topic": legacy_topic,
                    "page_title": f"P1｜{legacy_topic}",
                    "description": str(multipart.get("description") or ""),
                    "recorded_at": "",
                })
            multipart_parts = [
                item for item in multipart_parts
                if int(item.get("part_number") or 0) != part_number
            ]
            multipart_parts.append({
                "part_number": part_number,
                "title_topic": part_topic,
                "page_title": page_title,
                "title": part_generated_title,
                "description": part_description,
                "recorded_at": part_values["date"],
            })
            recording_intro = str(
                multipart.get("recording_intro") or recording_intro
            ).strip()
            description = render_multipart_description(
                multipart_parts,
                recording_intro,
            )

        description_body = strip_live_stats_from_description(description, stats_text)
        description = description_body
        ai_details.update({
            "title_topic": part_topic,
            "part_title": part_generated_title,
            "part_description": part_description,
            "page_title": page_title,
            "title": title,
            "description": description,
            "description_body": description_body,
            "final_tags": tags,
            "selected_partition_id": partition or None,
        })

        if review_override:
            ai_details.update({
                "manual_review_applied": True,
                "manual_review_updated_at": review_override.get("updated_at"),
            })
        ai_was_used = bool(
            comments and bool(cfg.get("ai_danmaku_summary_enabled", True))
        ) or bool(
            metadata_automation.get("tag_generation_enabled")
            or metadata_automation.get("partition_recommendation_enabled")
            or metadata_automation.get("metadata_automation_error")
        )
        ai_stage_status = "completed" if ai_was_used else "skipped"
        store.stage(key, "ai", ai_stage_status, ai_details)

        y2a_root = resolve_path(str(cfg.get("y2a_root", "y2a-auto")), cfg)
        if str(y2a_root) not in sys.path:
            sys.path.insert(0, str(y2a_root))
        stats_enabled = bool(cfg.get("douyu_stats_enabled", True))
        cover_context_enabled = bool(cfg.get("douyu_stats_cover_context_enabled", True))

        current_stage = "xml_identity"
        if identity_prepared:
            pass
        elif not stats_enabled:
            store.stage(key, "xml_identity", "skipped", {
                "reason": "斗鱼直播数据统计已关闭",
                "outcome": "disabled",
            })
        elif not cover_context_enabled:
            store.stage(key, "xml_identity", "skipped", {
                "reason": "XML 主播英雄与装备识别已关闭",
                "outcome": "disabled",
            })
        else:
            store.stage(key, "xml_identity", "running", {
                "danmaku_xml": str(danmaku_xml or ""),
                "comment_count": len(comments),
            })
            try:
                from modules.douyu_stats_formatter import (  # type: ignore
                    get_game_for_cover,
                    get_identity_diagnostics,
                )

                anchor = get_game_for_cover(str(video.parent))
                identity_diagnostics = get_identity_diagnostics(str(video.parent))
                if anchor:
                    store.stage(key, "xml_identity", "completed", {
                        "danmaku_xml": str(danmaku_xml or ""),
                        "comment_count": len(comments),
                        **identity_diagnostics,
                        "streamer_hero": str(anchor.get("hero") or ""),
                        "streamer_items": [
                            str(item) for item in anchor.get("items", [])[:6] if str(item)
                        ],
                        "streamer_neutral": str(anchor.get("neutral") or ""),
                        "streamer_scepter": bool(anchor.get("scepter")),
                        "streamer_shard": bool(anchor.get("shard")),
                        "equipment_snapshot_unix_ts": float(
                            anchor.get("equipment_snapshot_unix_ts") or 0
                        ),
                        "xml_mention_score": int(anchor.get("xml_mention_score") or 0),
                        "xml_runner_up_score": int(anchor.get("xml_runner_up_score") or 0),
                        "xml_mention_share": float(anchor.get("xml_mention_share") or 0),
                        "gsi_observed_seconds": float(
                            anchor.get("gsi_observed_seconds") or 0
                        ),
                        "identity_source": str(anchor.get("identity_source") or ""),
                        "kills": anchor.get("kills"),
                        "deaths": anchor.get("deaths"),
                        "assists": anchor.get("assists"),
                        "kda": anchor.get("kda"),
                        "kda_available": all(
                            field in anchor for field in ("kills", "deaths", "assists")
                        ),
                        "outcome": "matched",
                    })
                else:
                    if not identity_diagnostics["stats_available"]:
                        reason = "未找到斗鱼 Dota2 统计快照"
                    elif (
                        identity_diagnostics["type_tooltips_messages"]
                        + identity_diagnostics["type_tooltips_http_snapshots"]
                    ) == 0:
                        reason = "本次录制未获取 Dota2 阵容数据"
                    elif identity_diagnostics["type_tooltips_valid_snapshots"] == 0:
                        reason = "Dota2 数据未形成完整的 10 人阵容"
                    elif identity_diagnostics["type_tooltips_game_snapshots"] == 0:
                        reason = "Dota2 数据尚未形成稳定对局快照"
                    else:
                        reason = "斗鱼未提供主播视角英雄，XML 也未形成唯一可靠证据"
                    store.stage(key, "xml_identity", "skipped", {
                        "danmaku_xml": str(danmaku_xml or ""),
                        "comment_count": len(comments),
                        **identity_diagnostics,
                        "reason": reason,
                        "outcome": "no_data",
                    })
            except Exception as exc:
                store.stage(key, "xml_identity", "warning", {
                    "reason": "主播英雄识别失败，但不阻断投稿",
                    "outcome": "failed_non_blocking",
                }, error=str(exc))

        current_stage = "live_stats"
        append_stats_enabled = bool(cfg.get("douyu_stats_append_description", True))
        if live_stats_prepared and stats_text:
            description_body = strip_live_stats_from_description(
                description_body,
                stats_text,
            )
            if append_stats_enabled:
                description = prepend_live_stats_to_description(description, stats_text)
                ai_details["stats_appended"] = True
                ai_details["stats_prepended"] = True
                ai_details["stats_position"] = "start"
                print("[bridge] 预先整理的直播统计数据已置于简介开头", file=sys.stderr)
            else:
                description = description_body
                ai_details["stats_appended"] = False
                ai_details["stats_prepended"] = False
                ai_details["stats_position"] = None
            store.stage(key, "live_stats", "completed", {
                "stats_appended": append_stats_enabled,
                "stats_prepended": append_stats_enabled,
                "stats_position": "start" if append_stats_enabled else None,
                "description_length": len(description),
                **live_stats_stage_details(stats_text),
            })
        else:
            description = description_body
            ai_details["stats_appended"] = False
            ai_details["stats_prepended"] = False
            ai_details["stats_position"] = None

        # Persist both representations. Retries always reuse description_body;
        # description is the exact value sent to Bilibili and shown in details.
        ai_details["description_body"] = description_body
        ai_details["description"] = description
        store.stage(key, "ai", ai_stage_status, ai_details)

        current_stage = "cover_16x9"
        cover_generation: dict[str, Any] = {}
        cover16_status = "skipped"
        cover43_status = "skipped"
        session_cover = str(multipart.get("cover_path") or "").strip() if multipart else ""
        session_cover43 = str(multipart.get("cover43_path") or "").strip() if multipart else ""
        prior_cover16_details = (
            prior_cover16_stage.get("details")
            if isinstance(prior_cover16_stage.get("details"), dict)
            else {}
        )
        prior_cover43_details = (
            prior_cover43_stage.get("details")
            if isinstance(prior_cover43_stage.get("details"), dict)
            else {}
        )
        retry_cover_path = ""
        retry_cover43_path = ""
        if retry:
            for value in (
                review_override.get("cover_path"),
                prior_result.get("cover_path"),
                prior_cover16_details.get("ai_cover_path"),
                prior_cover16_details.get("cover_used_for_upload"),
            ):
                candidate = str(value or "").strip()
                if candidate and Path(candidate).is_file():
                    retry_cover_path = candidate
                    break
            for value in (
                review_override.get("cover43_path"),
                prior_result.get("cover43_path"),
                prior_cover43_details.get("ai_cover_4x3_path"),
                prior_cover43_details.get("cover43_used_for_upload"),
            ):
                candidate = str(value or "").strip()
                if candidate and Path(candidate).is_file():
                    retry_cover43_path = candidate
                    break
        if manual_cover_path and Path(manual_cover_path).is_file():
            cover = Path(manual_cover_path)
            cover_generation = {
                "manual_review_cover": True,
                "ai_cover_path": str(cover),
                "cover_used_for_upload": str(cover),
                "original_cover_path": str(original_cover),
            }
            cover16_status = "completed"
        elif session_cover and Path(session_cover).is_file():
            cover = Path(session_cover)
            cover_generation = dict(multipart.get("cover_generation") or {})
            cover_generation.update({
                "ai_cover_reused": True,
                "ai_cover_path": str(cover),
                "original_cover_path": str(original_cover),
            })
            cover16_status = "completed"
        elif retry_cover_path:
            cover = Path(retry_cover_path)
            cover_generation = dict(prior_cover16_details)
            cover_generation.update({
                "ai_cover_reused": True,
                "reused_on_retry": True,
                "ai_cover_path": str(cover),
                "cover_used_for_upload": str(cover),
                "original_cover_path": str(original_cover),
            })
            cover16_status = "completed"
        elif not dry_run and not existing_submission:
            store.stage(key, "cover_16x9", "running", {
                "title": title,
                "title_topic": ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                "original_cover_path": str(original_cover),
            })
            try:
                generated_cover, cover_generation = generate_recording_cover_with_ai(
                    title=title,
                    ai_topic=ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                    description=description_body,
                    streamer=recording_metadata_values(video, cfg)["streamer"],
                    cfg=cfg,
                    work_dir=work_dir,
                    target_size=(1920, 1080),
                    output_path=work_dir / "ai_cover_16x9.jpg",
                    recording_dir=video.parent,
                    game_context=locked_game_context,
                    game_context_locked=True,
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
                cover16_status = cover_status
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
                cover16_status = "warning"
                print(f"WARN AI 录播封面生成失败，回退视频截图: {exc}", file=sys.stderr)
        else:
            reason = "试运行" if dry_run else "后续分P沿用当前稿件封面"
            cover_generation = {
                "reason": reason,
                "cover_used_for_upload": str(cover),
                "original_cover_path": str(original_cover),
            }
            cover16_status = "skipped"

        if not dry_run:
            cover = persist_pipeline_cover(store, key, cover, "16x9", video=video)
            cover_generation["cover_used_for_upload"] = str(cover)
            cover_generation["ai_cover_16x9_path"] = str(cover)
            if cover_generation.get("ai_cover_generated") or cover_generation.get("ai_cover_path"):
                cover_generation["ai_cover_path"] = str(cover)
        store.stage(key, "cover_16x9", cover16_status, cover_generation)

        # The homepage 4:3 cover is a second, independent model request. It is
        # optional for upload and is never synthesized from the 16:9 image.
        current_stage = "cover_4x3"
        cover43_generation: dict[str, Any] = {}
        if manual_cover43_path and Path(manual_cover43_path).is_file():
            cover43 = Path(manual_cover43_path)
            cover43_status = "completed"
            cover43_generation = {
                "manual_review_cover43": True,
                "ai_cover_4x3_path": str(cover43),
                "cover43_used_for_upload": str(cover43),
            }
        elif session_cover43 and Path(session_cover43).is_file():
            cover43 = Path(session_cover43)
            cover43_status = "completed"
            cover43_generation = {
                "ai_cover_4x3_reused": True,
                "ai_cover_4x3_path": str(cover43),
                "cover43_used_for_upload": str(cover43),
            }
        elif retry_cover43_path:
            cover43 = Path(retry_cover43_path)
            cover43_status = "completed"
            cover43_generation = dict(prior_cover43_details)
            cover43_generation.update({
                "ai_cover_4x3_reused": True,
                "reused_on_retry": True,
                "ai_cover_4x3_path": str(cover43),
                "cover43_used_for_upload": str(cover43),
            })
        elif not dry_run and not existing_submission:
            store.stage(key, "cover_4x3", "running", {
                "title": title,
                "title_topic": ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
            })
            try:
                generated_cover43, cover43_details = generate_recording_cover_with_ai(
                    title=title,
                    ai_topic=ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                    description=description_body,
                    streamer=recording_metadata_values(video, cfg)["streamer"],
                    cfg=cfg,
                    work_dir=work_dir,
                    target_size=(1600, 1200),
                    output_path=work_dir / "ai_cover_4x3.jpg",
                    recording_dir=video.parent,
                    game_context=locked_game_context,
                    game_context_locked=True,
                )
                if generated_cover43:
                    cover43 = generated_cover43
                cover43_generation.update({
                    f"ai_cover_4x3_{key_name.removeprefix('ai_cover_')}": value
                    for key_name, value in cover43_details.items()
                })
                cover43_status = (
                    "completed"
                    if cover43_generation.get("ai_cover_4x3_generated")
                    else "skipped"
                )
            except Exception as exc:
                cover43_generation.update({
                    "ai_cover_4x3_generated": False,
                    "ai_cover_4x3_error": str(exc),
                    "reason": "4:3 首页推荐封面生成失败，但不阻断投稿",
                    "outcome": "failed_non_blocking",
                })
                cover43_status = "warning"
                print(f"WARN AI 4:3 首页推荐封面生成失败（不影响 16:9 投稿）: {exc}", file=sys.stderr)
        else:
            cover43_status = "skipped"
            cover43_generation = {
                "reason": "试运行" if dry_run else "后续分P沿用当前稿件封面",
                "outcome": "skipped",
            }

        if not dry_run and cover43 is not None and cover43.is_file():
            try:
                cover43 = persist_pipeline_cover(store, key, cover43, "4x3", video=video)
                cover43_generation["ai_cover_4x3_path"] = str(cover43)
                cover43_generation["cover43_used_for_upload"] = str(cover43)
            except Exception as exc:
                cover43 = None
                cover43_status = "warning"
                cover43_generation.update({
                    "ai_cover_4x3_error": str(exc),
                    "reason": "4:3 首页推荐封面保存失败，但不阻断投稿",
                    "outcome": "failed_non_blocking",
                })
        store.stage(key, "cover_4x3", cover43_status, cover43_generation)
        cover_generation.update(cover43_generation)

        summary = {"video": str(video), "upload_video": str(upload_video),
                   "danmaku_xml": str(danmaku_xml) if danmaku_xml else None,
                   "ass_path": str(ass_path) if ass_path else None,
                   "danmaku_count": len(comments), "cover": str(cover),
                   "cover_path": str(cover),
                   "cover43": str(cover43) if cover43 else None,
                   "cover43_path": str(cover43) if cover43 else None,
                   "original_cover": str(original_cover), "platform": platform,
                   "title": title, "description": description, "tags": tags, "source_url": source_url,
                   "partition_id": partition, "metadata_automation": metadata_automation,
                   "bilibili_account_id": str(cfg.get("bilibili_account_id") or ""),
                   "bilibili_account_name": str(cfg.get("bilibili_account_name") or ""),
                   "cover_generation": cover_generation,
                   "multipart_session": session_key or None, "part_number": part_number,
                   "page_title": page_title, "part_title": part_generated_title,
                   "part_description": part_description}
        if dry_run:
            store.stage(key, "upload", "skipped", {"reason": "试运行未投稿"})
            store.stage(key, "cleanup", "skipped", {"reason": "试运行不清理源文件"})
            store.finish(key, "dry_run", summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return True

        current_stage = "upload"
        upload_stage_details = {
            "title": title,
            "cover": str(cover),
            "tags": tags,
            "partition_id": partition,
            "part_number": part_number,
            "page_title": page_title,
            "bilibili_account_id": str(cfg.get("bilibili_account_id") or ""),
            "bilibili_account_name": str(cfg.get("bilibili_account_name") or ""),
            "existing_bvid": (
                existing_submission.get("bvid")
                if isinstance(existing_submission, dict)
                else None
            ),
        }
        upload_stage_details["worker_pid"] = os.getpid()
        store.stage(key, "upload", "queued", upload_stage_details)
        BilibiliUploader, _ = import_y2a(cfg)
        cookie = resolve_path(str(cfg.get("bilibili_cookies", "")), cfg)
        if not cookie.is_file():
            raise ValueError(f"bilibili Cookie 文件不存在：{cookie}")
        if not partition:
            raise ValueError("bilibili 未配置有效的投稿分区 ID")
        previous = store.results(key)
        previous.update({
            "tags": tags,
            "partition_id": partition,
            "metadata_automation": metadata_automation,
            "cover_generation": cover_generation,
            "cover_path": str(cover),
            "cover43_path": str(cover43) if cover43 else None,
        })
        result = previous.get("bilibili")
        uploader = None
        uploaded_now = False
        peak_upload_speed = 0.0
        final_upload_progress: dict[str, Any] | None = None
        if not isinstance(result, dict) or not result.get("bvid"):
            uploader = BilibiliUploader(cookie_file=str(cookie))

            def _on_upload_progress(progress: dict) -> None:
                nonlocal peak_upload_speed, final_upload_progress
                current_speed = float(
                    progress.get("speed_bytes_per_second")
                    or progress.get("speed_bytes_per_sec")
                    or 0
                )
                peak_upload_speed = max(peak_upload_speed, current_speed)
                final_upload_progress = {
                    **progress,
                    "peak_speed_bytes_per_second": peak_upload_speed,
                }
                store.stage(
                    key,
                    "upload",
                    "running",
                    {**upload_stage_details, "upload_progress": final_upload_progress},
                )

            def _on_upload_queue_status(status: str) -> None:
                store.stage(
                    key,
                    "upload",
                    "running" if status == "uploading" else "queued",
                    upload_stage_details,
                )

            ok, result = uploader.upload_video(
                video_file_path=str(upload_video), cover_file_path=str(cover), title=title,
                cover43_file_path=str(cover43) if cover43 else "",
                description=description, tags=tags, partition_id=partition,
                youtube_url=source_url, task_id=key[:12],
                page_titles=[page_title],
                existing_submission=existing_submission,
                is_original=True,
                progress_detail_callback=_on_upload_progress,
                queue_status_callback=_on_upload_queue_status,
            )
            if not ok:
                raise RuntimeError(f"bilibili 上传失败: {result}")
            previous.update({"bilibili": result, "ass_path": str(ass_path) if ass_path else None})
            uploaded_now = True
            # Persist the BVID immediately so a process restart cannot create a
            # duplicate video submission.
            store.finish(key, "video_uploaded", previous)

        description_comment = previous.get("description_comment")
        if (
            uploaded_now
            and not (
                isinstance(existing_submission, dict)
                and existing_submission.get("bvid")
            )
            and bool(cfg.get("post_description_comment", True))
            and not (
                isinstance(description_comment, dict)
                and description_comment.get("posted")
            )
        ):
            if hasattr(uploader, "publish_description_comment"):
                description_comment = uploader.publish_description_comment(
                    result=previous.get("bilibili") or {},
                    description=description,
                    pin=bool(cfg.get("pin_description_comment", True)),
                )
            else:
                description_comment = {
                    "enabled": True,
                    "posted": False,
                    "pinned": False,
                    "error": "当前上传器不支持简介评论",
                }
            previous["description_comment"] = description_comment
            # 评论失败不回滚已经成功的投稿，但保留原因供任务详情查看。
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
                "cover43_path": str(cover43) if cover43 else None,
                "last_video": str(video),
                "recording_intro": recording_intro,
                "parts": multipart_parts,
            }
            store.save_multipart_session(
                session_key,
                session_state,
                status=session_status if retry else "open",
            )

        completed_upload_progress = (
            {
                **final_upload_progress,
                "speed_bytes_per_second": 0,
                "eta_seconds": 0,
            }
            if final_upload_progress
            else None
        )
        store.stage(key, "upload", "completed", {
            "title": title, "description": description, "cover": str(cover),
            "cover43": str(cover43) if cover43 else None,
            "tags": tags, "partition_id": partition,
            "bilibili": previous.get("bilibili"),
            "description_comment": previous.get("description_comment"),
            "part_number": part_number,
            "page_title": page_title,
            "part_title": part_generated_title,
            "part_description": part_description,
            "upload_progress": completed_upload_progress,
            "peak_speed_bytes_per_second": peak_upload_speed or None,
        })
        # The upload result is durable, but the task has not reached its
        # terminal state until the configured source cleanup has finished.
        # Keeping the top-level status at video_uploaded also prevents file
        # management endpoints from treating the source as deletable.
        store.finish(key, "video_uploaded", previous)
        if bool(cfg.get("delete_recording_after_upload", True)):
            current_stage = "cleanup"
            store.stage(key, "cleanup", "running", {
                "video_path": str(video),
                "danmaku_xml": str(danmaku_xml) if danmaku_xml else None,
                "upload_video_path": str(upload_video),
            })
            xml_retention_hours = max(
                0.0,
                float(
                    24
                    if cfg.get("danmaku_xml_retention_hours") is None
                    else cfg["danmaku_xml_retention_hours"]
                ),
            )
            previous["source_cleanup"] = cleanup_uploaded_recording(
                video,
                danmaku_xml,
                upload_video,
                artifact_dir=work_dir,
                retained_paths=(
                    cover,
                    cover43,
                    danmaku_xml if xml_retention_hours > 0 else None,
                ),
                xml_retention_hours=xml_retention_hours,
            )
            store.stage(
                key,
                "cleanup",
                "completed",
                previous["source_cleanup"],
            )
        else:
            store.stage(
                key,
                "cleanup",
                "skipped",
                {"reason": "配置为上传后保留录播源文件"},
            )
        store.finish(key, "completed", previous)
        emit_recording_task_result_notification(
            cfg,
            fingerprint_value=key,
            video=video,
            task_kind="recording_upload",
            status="completed",
            result=previous,
            title=title,
        )
        print(f"OK 上传完成: {video}")
        return True
    except Exception as exc:
        store.stage(key, current_stage, "failed", error=str(exc))
        store.finish(key, "failed", error=str(exc))
        if not dry_run:
            emit_recording_task_result_notification(
                cfg,
                fingerprint_value=key,
                video=video,
                task_kind="recording_upload",
                status="failed",
                error=str(exc),
                stage=current_stage,
            )
        print(f"ERROR {video}: {exc}", file=sys.stderr)
        return False


def generate_record_only_ass(
    video: Path,
    base_cfg: dict[str, Any],
    received_paths: list[Path] | None = None,
) -> Path | None:
    """Generate a side-by-side ASS file without creating an upload task."""
    cfg = effective_config(base_cfg, video)
    danmaku_xml = wait_for_danmaku_xml(
        video,
        received_paths,
        timeout=float(cfg.get("record_only_xml_wait_seconds", 8)),
    )
    if danmaku_xml is None:
        print(f"WARN 仅录制文件未找到同名 XML，无法生成 ASS: {video}", file=sys.stderr)
        return None
    comments = parse_biliup_xml(danmaku_xml)
    if not comments:
        print(
            f"ERROR 弹幕 XML 中没有可用弹幕，未生成空 ASS: {danmaku_xml}",
            file=sys.stderr,
        )
        return None
    width, height = probe_video_size(video, str(cfg.get("ffprobe", "ffprobe")))
    # Media servers infer an external subtitle's language from its filename.
    # A plain ``video.ass`` is commonly shown as English/unknown, while
    # ``video.zh-CN.ass`` is recognised as Simplified Chinese.
    ass_path = video.with_name(f"{video.stem}.zh-CN.ass")
    generated = build_ass(
        comments,
        ass_path,
        width=width,
        height=height,
        font_name=str(cfg.get("danmaku_font_name", "Noto Sans CJK SC")),
        font_size=int(cfg.get("danmaku_font_size", 42)),
        duration=float(cfg.get("danmaku_duration_seconds", 9)),
        opacity=float(cfg.get("danmaku_opacity", 0.92)),
    )
    legacy_path = video.with_suffix(".ass")
    if legacy_path != generated:
        legacy_path.unlink(missing_ok=True)
    return generated


def generate_record_only_cover(video: Path, base_cfg: dict[str, Any]) -> Path:
    """Generate an AI cover beside the video using its native resolution."""
    cfg = effective_config(base_cfg, video)
    title, description, _ = render_metadata(video, cfg)
    ai_topic = recording_metadata_values(video, cfg)["ai_topic"]
    danmaku_xml = find_danmaku_xml(video)
    if danmaku_xml and bool(cfg.get("ai_danmaku_summary_enabled", True)):
        comments = parse_biliup_xml(danmaku_xml)
        if comments:
            description, ai_topic = generate_danmaku_metadata_with_ai(
                comments,
                description,
                cfg,
                timeline_duration_seconds=video_duration_seconds(
                    video,
                    str(cfg.get("ffprobe", "ffprobe")),
                ),
            )
            title, _, _ = render_metadata(video, cfg, ai_topic=ai_topic)
    width, height = probe_video_size(video, str(cfg.get("ffprobe", "ffprobe")))
    cover = video.with_suffix(".jpg")
    generated, details = generate_recording_cover_with_ai(
        title=title,
        ai_topic=ai_topic,
        description=description,
        streamer=recording_metadata_values(video, cfg)["streamer"],
        cfg=cfg,
        work_dir=video.parent / ".potato-cover-artifacts",
        target_size=(width, height),
        output_path=cover,
        recording_dir=video.parent,
    )
    if not generated or not details.get("ai_cover_generated"):
        raise RuntimeError("录播 AI 封面未启用或图片模型没有生成封面")
    return generated


def remux_record_only_flv_with_cover(
    video: Path,
    cover: Path,
    base_cfg: dict[str, Any],
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> Path:
    """Remux an FLV to MP4 and attach its sidecar cover without re-encoding."""
    if video.suffix.lower() != ".flv":
        return video
    if not cover.is_file():
        raise RuntimeError(f"内嵌封面不存在: {cover}")

    cfg = effective_config(base_cfg, video)
    output = video.with_suffix(".mp4")
    temporary = video.with_name(f".{video.stem}.potato-remux.mp4")
    temporary.unlink(missing_ok=True)
    command = [
        str(cfg.get("ffmpeg", "ffmpeg")),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(cover),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "1:v:0",
        "-c",
        "copy",
        "-disposition:v:1",
        "attached_pic",
        "-metadata:s:v:1",
        "title=PotatoFlow cover",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=float(cfg.get("record_only_remux_timeout_seconds", 3600)),
        )
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
            detail = (completed.stderr or completed.stdout or "FFmpeg 未生成 MP4").strip()
            raise RuntimeError(f"FLV 转 MP4 失败: {detail}")
        if progress_callback:
            progress_callback(
                "remux_completed",
                {
                    "output_path": str(output),
                    "temporary_path": str(temporary),
                    "copy_mode": "-c copy",
                    "size_bytes": temporary.stat().st_size,
                },
            )
            progress_callback("verify_running", {"output_path": str(output)})

        probe = subprocess.run(
            [
                str(cfg.get("ffprobe", "ffprobe")),
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream_disposition=attached_pic",
                "-of",
                "json",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        try:
            streams = json.loads(probe.stdout or "{}").get("streams", [])
        except json.JSONDecodeError:
            streams = []
        if probe.returncode != 0 or not any(
            int(stream.get("disposition", {}).get("attached_pic", 0)) == 1
            for stream in streams
            if isinstance(stream, dict)
        ):
            raise RuntimeError("MP4 已生成，但未检测到内嵌封面")
        if progress_callback:
            progress_callback(
                "verify_completed",
                {"output_path": str(output), "attached_pic": 1},
            )
            progress_callback(
                "cleanup_running",
                {"original_flv": str(video), "output_path": str(output)},
            )

        temporary.replace(output)
        video.unlink()
        if progress_callback:
            progress_callback(
                "cleanup_completed",
                {
                    "original_flv": str(video),
                    "final_video_path": str(output),
                    "original_flv_deleted": True,
                },
            )
        return output
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将 biliup 录制产物交给 Y2A-Auto 上传")
    parser.add_argument("--config", default="bridge.config.json", help="JSON 配置文件")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="处理参数或 stdin 中的视频路径")
    ingest.add_argument("paths", nargs="*")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--retry", action="store_true", help="允许重试指定的失败任务")
    ingest.add_argument("--session-key", default="", help="将分段追加到同一场直播稿件")
    record_only = sub.add_parser("record-only", help="登记仅录制文件，永久跳过自动投稿")
    record_only.add_argument("paths", nargs="*")
    record_only.add_argument("--room-id", required=True)
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


def video_duration_seconds(path: Path, ffprobe: str = "ffprobe") -> float | None:
    """Return media duration, or None when the recorder file cannot be probed."""
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return None
        duration = float(completed.stdout.strip())
        return duration if duration >= 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def main(argv: list[str] | None = None) -> int:
    ensure_pipeline_process_group()
    configure_linux_ca_environment()
    args = build_parser().parse_args(argv)
    cfg = load_config(Path(args.config))
    state_path = resolve_path(str(cfg.get("state_db", ".bridge/state.sqlite3")), cfg)
    store = StateStore(state_path)
    if args.command not in {"status", "close-session"}:
        store.cleanup_expired_retained_xml()

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

    if args.command == "record-only":
        received_paths = input_paths(args.paths)
        paths = [path for path in received_paths if path.suffix.lower() in VIDEO_EXTENSIONS]
        if not paths:
            print("没有收到可登记的录播文件", file=sys.stderr)
            return 2
        ok = True
        for path in paths:
            if not path.is_file():
                print(f"ERROR 文件不存在: {path}", file=sys.stderr)
                ok = False
                continue
            store.exclude_recording(path, str(args.room_id))
            record_cfg = effective_config(cfg, path)
            danmaku_xml = wait_for_danmaku_xml(
                path,
                received_paths,
                timeout=float(record_cfg.get("record_only_xml_wait_seconds", 8)),
            )
            key = fingerprint(path)
            is_new_task = not store.upload_exists(key)
            if not store.claim_record_only(
                key,
                path,
                str(args.room_id),
                danmaku_xml,
            ):
                print(f"SKIP 仅录制任务已存在或正在处理: {path}")
                continue
            if is_new_task:
                emit_recording_task_added_notification(
                    record_cfg,
                    fingerprint_value=key,
                    video=path,
                    task_kind="record_only",
                )
            if danmaku_xml is None:
                error = "录制已结束，但未找到稳定的 XML 弹幕文件"
                store.stage(key, "record", "failed", {
                    "video_path": str(path),
                    "size_bytes": path.stat().st_size,
                    "safe_finalized": False,
                }, error=error)
                store.finish(key, "failed", error=error)
                emit_recording_task_result_notification(
                    record_cfg,
                    fingerprint_value=key,
                    video=path,
                    task_kind="record_only",
                    status="failed",
                    error=error,
                    stage="record",
                )
                print(f"ERROR 仅录制文件未找到 XML，已保留原 FLV: {path}", file=sys.stderr)
                ok = False
                continue
            current_stage = "ass"
            try:
                store.stage(key, "ass", "running", {"danmaku_xml": str(danmaku_xml)})
                ass_path = generate_record_only_ass(path, cfg, received_paths)
                if ass_path is None:
                    raise RuntimeError("弹幕 XML 为空或无有效弹幕，未生成 ASS 字幕")
                store.stage(
                    key,
                    "ass",
                    "completed",
                    {"danmaku_xml": str(danmaku_xml), "ass_path": str(ass_path)},
                )

                current_stage = "cover"
                store.stage(key, "cover", "running")
                cover_path = generate_record_only_cover(path, cfg)
                store.stage(
                    key,
                    "cover",
                    "completed",
                    {"ai_cover_path": str(cover_path)},
                )

                def update_remux_progress(
                    event: str,
                    details: dict[str, Any],
                ) -> None:
                    nonlocal current_stage
                    if event == "remux_completed":
                        store.stage(key, "remux", "completed", details)
                    elif event == "verify_running":
                        current_stage = "verify"
                        store.stage(key, "verify", "running", details)
                    elif event == "verify_completed":
                        store.stage(key, "verify", "completed", details)
                    elif event == "cleanup_running":
                        current_stage = "cleanup"
                        store.stage(key, "cleanup", "running", details)
                    elif event == "cleanup_completed":
                        store.stage(key, "cleanup", "completed", details)

                current_stage = "remux"
                store.stage(
                    key,
                    "remux",
                    "running",
                    {"source_flv": str(path), "cover_path": str(cover_path)},
                )
                final_video = remux_record_only_flv_with_cover(
                    path,
                    cover_path,
                    cfg,
                    progress_callback=update_remux_progress,
                )
                if final_video != path:
                    store.exclude_recording(final_video, str(args.room_id))
                result = {
                    "room_id": str(args.room_id),
                    "record_only": True,
                    "video_path": str(path),
                    "final_video_path": str(final_video),
                    "danmaku_xml": str(danmaku_xml),
                    "ass_path": str(ass_path),
                    "cover_path": str(cover_path),
                    "attached_pic": 1,
                    "original_flv_deleted": final_video != path,
                    "video_duration_seconds": video_duration_seconds(
                        final_video,
                        str(record_cfg.get("ffprobe", "ffprobe")),
                    ),
                }
                store.finish(key, "completed", result)
                emit_recording_task_result_notification(
                    record_cfg,
                    fingerprint_value=key,
                    video=path,
                    task_kind="record_only",
                    status="completed",
                    result=result,
                )
            except Exception as exc:
                store.stage(key, current_stage, "failed", error=str(exc))
                store.finish(key, "failed", error=str(exc))
                emit_recording_task_result_notification(
                    record_cfg,
                    fingerprint_value=key,
                    video=path,
                    task_kind="record_only",
                    status="failed",
                    error=str(exc),
                    stage=current_stage,
                )
                print(
                    f"ERROR 仅录制本地处理失败，已保留原 FLV: {path}: {exc}",
                    file=sys.stderr,
                )
                ok = False
                continue
            print(
                f"OK 仅录制文件已保留并跳过自动投稿: "
                f"{final_video}，ASS: {ass_path}，封面: {cover_path}"
            )
        return 0 if ok else 1

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
        minimum_duration = max(
            0.0,
            float(cfg.get("MIN_RECORDING_UPLOAD_DURATION_SECONDS", 60) or 60),
        )
        duration = video_duration_seconds(path, str(cfg.get("ffprobe", "ffprobe")))
        if duration is not None and duration < minimum_duration:
            print(
                f"SKIP 视频时长 {duration:.1f} 秒，小于 {minimum_duration:.0f} 秒："
                f"不创建任务、不进行 AI 处理或投稿: {path}",
                file=sys.stderr,
            )
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
