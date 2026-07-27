from __future__ import annotations

import json
from typing import Any

from .models import (
    EVENT_LOGIN_LOCKED,
    EVENT_LOGIN_SUCCESS,
    EVENT_COOKIE_INVALID,
    EVENT_QR_LOGIN_FAILED,
    EVENT_QR_LOGIN_SUCCESS,
    EVENT_RECORDING_STARTED,
    EVENT_RECORDING_STOPPED,
    EVENT_TASK_ADDED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    NotificationEvent,
    NotificationMessage,
)


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _truncate(text: str, limit: int = 240) -> str:
    clean = _as_text(text)
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)] + "…"


def _upload_target_label(upload_target: Any) -> str:
    return "bilibili"


def _task_title(payload: dict[str, Any]) -> str:
    for key in ("video_title_translated", "video_title_original", "title"):
        value = _as_text(payload.get(key))
        if value:
            return value
    return "未命名任务"


def _task_platform_result(payload: dict[str, Any]) -> str:
    if payload.get("bilibili_uploaded"):
        return "bilibili"
    return _upload_target_label(payload.get("upload_target"))


def _pretty_error_text(value: Any) -> str:
    text = _truncate(_as_text(value), 300)
    return text or "未提供错误详情"


def _markdown_lines(*lines: str) -> str:
    return "\n".join(line for line in lines if _as_text(line))


def _kv(label: str, value: Any) -> str:
    """格式化一个键值行：**标签** 值"""
    text = _as_text(value)
    if not text:
        return ""
    return f"**{label}：**{text}"


def _section_block(*kv_lines: str) -> str:
    """将多行键值组装成引用块。"""
    return "\n".join(f"> {line}" for line in kv_lines if _as_text(line))


def build_notification_message(event: NotificationEvent) -> NotificationMessage:
    payload = event.as_payload()
    event_type = event.event_type

    if event_type == EVENT_TASK_ADDED:
        task_kind = _as_text(payload.get("task_kind"))
        if task_kind in {"recording_upload", "record_only"}:
            is_record_only = task_kind == "record_only"
            streamer = _as_text(payload.get("streamer")) or "未知主播"
            video_file = _truncate(
                _as_text(payload.get("video_file"))
                or _as_text(payload.get("video_path")),
                220,
            )
            target = "仅本地处理，不投稿" if is_record_only else "哔哩哔哩"
            icon = "💾" if is_record_only else "📹"
            kind_label = "仅录制任务" if is_record_only else "录播投稿任务"
            title = f"PotatoFlow {icon} {kind_label}已添加"
            summary = f"{streamer} | {video_file}"
            body = _section_block(
                _kv("任务类型", kind_label),
                _kv("主播", streamer),
                _kv("录播文件", video_file),
                _kv("处理目标", target),
                _kv("任务 ID", f"`{_as_text(payload.get('task_id'))}`"),
                _kv("直播间", _truncate(_as_text(payload.get("source_url")), 500)),
                _kv("时间", _as_text(payload.get("occurred_at"))),
            )
            markdown = _markdown_lines(f"**{icon} {kind_label}已添加**", "", body)
            return NotificationMessage(title=title, summary=summary, markdown=markdown)

        title = "PotatoFlow 📋 任务已添加"
        summary = f"{payload.get('task_id', '')[:8]} | {_upload_target_label(payload.get('upload_target'))}"
        body = _section_block(
            _kv("任务 ID", f"`{_as_text(payload.get('task_id'))}`"),
            _kv("投稿目标", _upload_target_label(payload.get("upload_target"))),
            _kv("YouTube URL", _truncate(_as_text(payload.get("youtube_url")), 500)),
            _kv("时间", _as_text(payload.get("occurred_at"))),
        )
        markdown = _markdown_lines("**📋 任务已添加**", "", body)
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type == EVENT_TASK_COMPLETED:
        task_kind = _as_text(payload.get("task_kind"))
        if task_kind in {"recording_upload", "record_only"}:
            is_record_only = task_kind == "record_only"
            streamer = _as_text(payload.get("streamer")) or "未知主播"
            video_file = _truncate(
                _as_text(payload.get("video_file"))
                or _as_text(payload.get("video_path")),
                220,
            )
            target = "仅本地处理，不投稿" if is_record_only else "哔哩哔哩"
            kind_label = "仅录制任务" if is_record_only else "录播投稿任务"
            title = f"PotatoFlow ✅ {kind_label}已完成"
            summary = f"{streamer} | {video_file}"
            body = _section_block(
                _kv("任务类型", kind_label),
                _kv("主播", streamer),
                _kv("录播文件", video_file),
                _kv("处理结果", target),
                _kv("BVID", _as_text(payload.get("bvid"))),
                _kv("本地成品", _truncate(_as_text(payload.get("final_video_path")), 300)),
                _kv("任务 ID", f"`{_as_text(payload.get('task_id'))}`"),
                _kv("时间", _as_text(payload.get("occurred_at"))),
            )
            markdown = _markdown_lines(f"**✅ {kind_label}已完成**", "", body)
            return NotificationMessage(title=title, summary=summary, markdown=markdown)

        task_title = _task_title(payload)
        platform_result = _task_platform_result(payload)
        title = "PotatoFlow ✅ 任务已完成"
        summary = f"{task_title} | {platform_result}"
        body = _section_block(
            _kv("视频标题", _truncate(task_title, 120)),
            _kv("任务 ID", f"`{_as_text(payload.get('task_id'))}`"),
            _kv("投稿结果", platform_result),
            _kv("投稿目标", _upload_target_label(payload.get("upload_target"))),
            _kv("时间", _as_text(payload.get("occurred_at"))),
        )
        markdown = _markdown_lines("**✅ 任务已完成**", "", body)
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type == EVENT_TASK_FAILED:
        task_kind = _as_text(payload.get("task_kind"))
        if task_kind in {"recording_upload", "record_only"}:
            is_record_only = task_kind == "record_only"
            streamer = _as_text(payload.get("streamer")) or "未知主播"
            video_file = _truncate(
                _as_text(payload.get("video_file"))
                or _as_text(payload.get("video_path")),
                220,
            )
            error_text = _pretty_error_text(payload.get("error_message"))
            kind_label = "仅录制任务" if is_record_only else "录播投稿任务"
            title = f"PotatoFlow ❌ {kind_label}失败"
            summary = f"{streamer} | {error_text}"
            body = _section_block(
                _kv("任务类型", kind_label),
                _kv("主播", streamer),
                _kv("录播文件", video_file),
                _kv("失败阶段", _as_text(payload.get("stage"))),
                _kv("任务 ID", f"`{_as_text(payload.get('task_id'))}`"),
                _kv("时间", _as_text(payload.get("occurred_at"))),
            )
            markdown = _markdown_lines(
                f"**❌ {kind_label}失败**",
                "",
                body,
                "",
                "> **错误详情：**",
                f"> {error_text}",
            )
            return NotificationMessage(title=title, summary=summary, markdown=markdown)

        task_title = _task_title(payload)
        error_text = _pretty_error_text(payload.get("error_message"))
        title = "PotatoFlow ❌ 任务失败"
        summary = f"{task_title} | {error_text}"
        body = _section_block(
            _kv("视频标题", _truncate(task_title, 120)),
            _kv("任务 ID", f"`{_as_text(payload.get('task_id'))}`"),
            _kv("当前状态", _as_text(payload.get("status")) or "failed"),
            _kv("投稿目标", _upload_target_label(payload.get("upload_target"))),
        )
        markdown = _markdown_lines("**❌ 任务失败**", "", body, "", f"> **错误详情：**", f"> {error_text}", "", f"> {_kv('时间', _as_text(payload.get('occurred_at')))}")
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type == EVENT_LOGIN_SUCCESS:
        ip = _as_text(payload.get("ip_address")) or "unknown"
        occurred_at = _as_text(payload.get("occurred_at"))
        title = "PotatoFlow 🔐 后台登录成功"
        summary = f"{ip} | {occurred_at}"
        body = _section_block(
            _kv("来源 IP", ip),
            _kv("时间", occurred_at),
        )
        markdown = _markdown_lines("**🔐 后台登录成功**", "", body)
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type == EVENT_LOGIN_LOCKED:
        ip = _as_text(payload.get("ip_address")) or "unknown"
        failed = _as_text(payload.get("failed_attempts"))
        max_att = _as_text(payload.get("max_attempts"))
        lock_min = _as_text(payload.get("lock_minutes"))
        occurred_at = _as_text(payload.get("occurred_at"))
        title = "PotatoFlow 🚫 登录已被锁定"
        summary = f"{failed}/{max_att} | {lock_min} 分钟"
        body = _section_block(
            _kv("来源 IP", ip),
            _kv("失败次数", f"{failed}/{max_att}"),
            _kv("锁定时长", f"{lock_min} 分钟"),
            _kv("时间", occurred_at),
        )
        markdown = _markdown_lines("**🚫 登录已被锁定**", "", body)
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type in (EVENT_QR_LOGIN_SUCCESS, EVENT_QR_LOGIN_FAILED):
        platform = _as_text(payload.get("platform")) or "平台"
        is_success = event_type == EVENT_QR_LOGIN_SUCCESS
        icon = "✅" if is_success else "❌"
        status_text = "成功" if is_success else "失败"
        message = _truncate(_as_text(payload.get("message")) or ("Cookies 已保存" if is_success else "登录失败"), 300)
        occurred_at = _as_text(payload.get("occurred_at"))
        title = f"PotatoFlow {icon} {platform}扫码登录{status_text}"
        summary = f"{platform} | {message}"
        body = _section_block(
            _kv("平台", platform),
            _kv("结果", message),
            _kv("时间", occurred_at),
        )
        markdown = _markdown_lines(f"**{icon} {platform}扫码登录{status_text}**", "", body)
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type in (EVENT_RECORDING_STARTED, EVENT_RECORDING_STOPPED):
        is_started = event_type == EVENT_RECORDING_STARTED
        icon = "🔴" if is_started else "⏹️"
        status_text = "录制已开始" if is_started else "录制已停止"
        streamer = _as_text(payload.get("streamer")) or "未知主播"
        live_title = _truncate(_as_text(payload.get("live_title")), 160)
        platform = _as_text(payload.get("platform")) or "直播平台"
        occurred_at = _as_text(payload.get("occurred_at"))
        title = f"PotatoFlow {icon} {status_text}"
        summary = f"{streamer} | {live_title or platform}"
        body = _section_block(
            _kv("主播", streamer),
            _kv("平台", platform),
            _kv("直播标题", live_title),
            _kv("录制文件", _truncate(_as_text(payload.get("current_file")), 200)),
            _kv("直播间", _truncate(_as_text(payload.get("room_url")), 500)),
            _kv("录制开始", _as_text(payload.get("started_at"))),
            _kv("录制时长", _as_text(payload.get("duration_text"))),
            _kv("时间", occurred_at),
        )
        markdown = _markdown_lines(f"**{icon} {status_text}**", "", body)
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type == EVENT_COOKIE_INVALID:
        platform = _as_text(payload.get("platform")) or "平台"
        reason = _pretty_error_text(payload.get("reason"))
        occurred_at = _as_text(payload.get("occurred_at"))
        title = f"PotatoFlow 🍪 {platform} Cookie 已失效"
        summary = f"{platform} | {reason}"
        body = _section_block(
            _kv("平台", platform),
            _kv("检测位置", _as_text(payload.get("source"))),
            _kv("处理建议", "请进入“系统设置 → 账号与网络”重新登录或上传 Cookie"),
            _kv("时间", occurred_at),
        )
        markdown = _markdown_lines(
            f"**🍪 {platform} Cookie 已失效**",
            "",
            body,
            "",
            "> **原因：**",
            f"> {reason}",
        )
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    title = "PotatoFlow 💬 系统通知"
    serialized = json.dumps(payload, ensure_ascii=False)
    summary = _truncate(serialized, 180)
    body = _section_block(
        _kv("事件", event_type),
        _kv("内容", _truncate(serialized, 1500)),
    )
    markdown = _markdown_lines("**💬 系统通知**", "", body)
    return NotificationMessage(title=title, summary=summary, markdown=markdown)
