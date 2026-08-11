"""Unified read-model helpers for the mixed upload task queue."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil
from typing import Iterable


QUEUE_FILTERS = ("all", "active", "queued", "review", "failed", "paused", "completed")
SOURCE_FILTERS = ("all", "recording", "youtube")
RECORDING_TYPE_FILTERS = ("all", "upload", "record_only")
RECORDING_TIME_FILTERS = ("all", "today", "3d", "7d", "30d")

_YOUTUBE_ACTIVE = {
    "fetching_info",
    "info_fetched",
    "translating",
    "tagging",
    "partitioning",
    "moderating",
    "downloading",
    "downloaded",
    "asr_transcribing",
    "translating_subtitle",
    "encoding_video",
    "uploading",
}


def normalize_queue_filter(value: str | None) -> str:
    normalized = str(value or "all").strip().lower()
    return normalized if normalized in QUEUE_FILTERS else "all"


def normalize_source_filter(value: str | None) -> str:
    normalized = str(value or "all").strip().lower()
    return normalized if normalized in SOURCE_FILTERS else "all"


def normalize_recording_type_filter(value: str | None) -> str:
    normalized = str(value or "all").strip().lower()
    return normalized if normalized in RECORDING_TYPE_FILTERS else "all"


def normalize_recording_time_filter(value: str | None) -> str:
    normalized = str(value or "all").strip().lower()
    return normalized if normalized in RECORDING_TIME_FILTERS else "all"


def recording_room_options(items: Iterable[dict]) -> list[dict[str, str]]:
    rooms: dict[str, str] = {}
    for item in items:
        name = str(item.get("room_name") or "未匹配直播间").strip()
        room_id = str(item.get("room_id") or "").strip()
        value = room_id or f"name:{name}"
        rooms[value] = name
    return [
        {"value": value, "name": name}
        for value, name in sorted(rooms.items(), key=lambda pair: pair[1].casefold())
    ]


def _recording_created_at(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def filter_recording_jobs(
    items: Iterable[dict],
    *,
    keyword: str = "",
    room: str = "all",
    task_type: str = "all",
    time_range: str = "all",
    now: datetime | None = None,
) -> list[dict]:
    values = list(items)
    clean_keyword = str(keyword or "").strip().casefold()
    clean_room = str(room or "all").strip()
    clean_type = normalize_recording_type_filter(task_type)
    clean_time = normalize_recording_time_filter(time_range)
    current = now or datetime.now().astimezone()

    def matches(item: dict) -> bool:
        if clean_keyword:
            searchable = "\n".join(
                str(item.get(key) or "")
                for key in ("title", "video_name", "display_id", "bvid")
            ).casefold()
            if clean_keyword not in searchable:
                return False
        if clean_room != "all":
            room_id = str(item.get("room_id") or "").strip()
            room_name = str(item.get("room_name") or "未匹配直播间").strip()
            if clean_room not in {room_id, f"name:{room_name}"}:
                return False
        if clean_type == "upload" and item.get("record_only"):
            return False
        if clean_type == "record_only" and not item.get("record_only"):
            return False
        if clean_time != "all":
            created = _recording_created_at(item.get("created_at"))
            if created is None:
                return False
            comparison_now = current
            if created.tzinfo is None:
                comparison_now = current.replace(tzinfo=None)
            elif current.tzinfo is None:
                comparison_now = current.replace(tzinfo=created.tzinfo)
            else:
                created = created.astimezone(current.tzinfo)
            if clean_time == "today":
                if created.date() != comparison_now.date():
                    return False
            else:
                days = int(clean_time.removesuffix("d"))
                if created < comparison_now - timedelta(days=days):
                    return False
        return True

    return [item for item in values if matches(item)]


def youtube_queue_bucket(task: dict) -> str:
    status = str(task.get("status") or "").strip().lower()
    if status in _YOUTUBE_ACTIVE:
        return "active"
    if status == "awaiting_manual_review":
        return "review"
    if status == "failed":
        return "failed"
    if status == "paused":
        return "paused"
    if status == "completed":
        return "completed"
    return "queued"


def recording_queue_bucket(job: dict) -> str:
    status = str(job.get("status") or "").strip().lower()
    if job.get("ai_queued") or job.get("burn_queued") or job.get("upload_queued"):
        return "queued"
    if status in {"processing", "video_uploaded"}:
        return "active"
    if status == "failed":
        return "failed" if job.get("record_only") else "review"
    if status == "paused":
        return "paused"
    if status == "completed":
        return "completed"
    return "queued"


def build_queue_summary(
    youtube_tasks: Iterable[dict],
    recording_jobs: Iterable[dict],
) -> dict[str, int]:
    summary = {key: 0 for key in QUEUE_FILTERS}
    for task in youtube_tasks:
        summary[youtube_queue_bucket(task)] += 1
        summary["all"] += 1
    for job in recording_jobs:
        summary[recording_queue_bucket(job)] += 1
        summary["all"] += 1
    return summary


def filter_queue_items(
    items: Iterable[dict],
    queue_filter: str,
    bucket_resolver,
) -> list[dict]:
    normalized = normalize_queue_filter(queue_filter)
    values = list(items)
    if normalized == "all":
        return values
    return [item for item in values if bucket_resolver(item) == normalized]


def paginate_items(items: Iterable[dict], page: int, per_page: int) -> dict:
    values = list(items)
    per_page = max(1, int(per_page))
    total = len(values)
    total_pages = ceil(total / per_page) if total else 0
    normalized_page = max(1, min(int(page or 1), total_pages or 1))
    offset = (normalized_page - 1) * per_page
    page_items = values[offset:offset + per_page]
    return {
        "tasks": page_items,
        "total": total,
        "page": normalized_page,
        "per_page": per_page,
        "total_pages": total_pages,
        "has_prev": normalized_page > 1,
        "has_next": normalized_page < total_pages,
        "prev_page": normalized_page - 1 if normalized_page > 1 else None,
        "next_page": normalized_page + 1 if normalized_page < total_pages else None,
    }
