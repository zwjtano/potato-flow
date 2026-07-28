"""Canonical task lifecycle rules shared by Web, workers and maintenance jobs."""

from __future__ import annotations

from typing import Any, Mapping


YOUTUBE_TERMINAL_STATUSES = frozenset({"completed", "failed"})
YOUTUBE_ACTIVE_STATUSES = frozenset({
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
})
YOUTUBE_PAUSABLE_STATUSES = YOUTUBE_ACTIVE_STATUSES | frozenset({"pending", "ready_for_upload"})
YOUTUBE_RETRYABLE_STATUSES = frozenset({"failed", "paused"})

RECORDING_TERMINAL_STATUSES = frozenset({"completed", "failed", "dry_run"})
RECORDING_PAUSABLE_STATUSES = frozenset({"processing", "video_uploaded"})
RECORDING_RETRYABLE_STATUSES = frozenset({"failed", "dry_run", "paused"})


def youtube_task_capabilities(status: str | None) -> dict[str, bool]:
    normalized = str(status or "").strip()
    return {
        "terminal": normalized in YOUTUBE_TERMINAL_STATUSES,
        "active": normalized in YOUTUBE_ACTIVE_STATUSES,
        "pausable": normalized in YOUTUBE_PAUSABLE_STATUSES,
        "retryable": normalized in YOUTUBE_RETRYABLE_STATUSES,
        "paused": normalized == "paused",
        # Explicit deletion is supported for every persisted state. Active
        # workers must be stopped before their record or files are removed.
        "deletable": bool(normalized),
    }


def recording_task_capabilities(status: str | None) -> dict[str, bool]:
    normalized = str(status or "").strip()
    return {
        "terminal": normalized in RECORDING_TERMINAL_STATUSES,
        "active": normalized in RECORDING_PAUSABLE_STATUSES,
        "pausable": normalized in RECORDING_PAUSABLE_STATUSES,
        "retryable": normalized in RECORDING_RETRYABLE_STATUSES,
        "paused": normalized == "paused",
        "deletable": bool(normalized),
    }


def youtube_upload_succeeded(task: Mapping[str, Any] | None) -> bool:
    """Return true only when a durable Bilibili upload response is present."""
    if not task:
        return False
    return bool(task.get("bilibili_upload_response"))


def can_automatically_cleanup_youtube_download(
    task: Mapping[str, Any] | None,
) -> bool:
    """Age-based cleanup is allowed only after a successful terminal upload."""
    if not task:
        return True  # orphan directory; the age threshold still applies
    return (
        str(task.get("status") or "") == "completed"
        and youtube_upload_succeeded(task)
    )
