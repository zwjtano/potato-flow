"""Unified read-model helpers for the mixed upload task queue."""

from __future__ import annotations

from math import ceil
from typing import Iterable


QUEUE_FILTERS = ("all", "active", "queued", "review", "failed", "paused", "completed")
SOURCE_FILTERS = ("all", "recording", "youtube")

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
