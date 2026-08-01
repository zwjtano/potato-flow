#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import time
import traceback
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, List, Optional, Tuple, Union

from .bili_sdk import video_uploader
from .bili_sdk.exceptions import ArgsException, ResponseCodeException
from .bili_sdk.utils.network import Api, HEADERS

from .bilibili_runtime import configure_bilibili_runtime
from .bilibili_auth import load_credential_from_file, validate_credential_remote
from .biliup_uploader import upload_with_biliup
from .config_manager import load_config
from .cover_preflight import (
    BILIBILI_COVER43_SIZE,
    CoverPreflightError,
    prepare_bilibili_cover,
)
from .notifications import notify_cookie_invalid
from .utils import get_app_subdir
from .upload_queue import bilibili_upload_slot, default_bilibili_upload_lock

BILIBILI_TITLE_LIMIT = 80
BILIBILI_DESCRIPTION_LIMIT = 2000


def setup_task_logger(task_id):
    log_dir = get_app_subdir("logs")
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, f"task_{task_id}.log")
    logger = logging.getLogger(f"bilibili_uploader_{task_id}")

    if not logger.handlers:
        logger.setLevel(logging.INFO)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10485760, backupCount=5, encoding="utf-8"
        )
        file_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        logger.propagate = False

    return logger


def _compact_text(text: str, max_len: int) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..." if max_len > 3 else text[:max_len]


def _normalize_multiline_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    last_blank = True

    for raw_line in normalized.split("\n"):
        line = re.sub(r"[^\S\n]+", " ", raw_line).strip()
        if not line:
            if not last_blank and lines:
                lines.append("")
            last_blank = True
            continue
        lines.append(line)
        last_blank = False

    while lines and not lines[-1]:
        lines.pop()

    return "\n".join(lines)


def _truncate_multiline_text(text: str, max_len: int) -> str:
    normalized = _normalize_multiline_text(text)
    if len(normalized) <= max_len:
        return normalized
    if max_len <= 0:
        return ""
    if max_len <= 3:
        return normalized[:max_len]
    return normalized[: max_len - 3].rstrip() + "..."


def _remove_redundant_original_url(text: str, original_url: str) -> str:
    normalized = _normalize_multiline_text(text)
    visible_url = str(original_url or "").strip()
    if not normalized or not visible_url:
        return normalized

    cleaned_lines = []
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            cleaned_lines.append("")
            continue
        if line == visible_url:
            continue
        line = line.replace(visible_url, "").strip()
        if line:
            cleaned_lines.append(line)

    return _normalize_multiline_text("\n".join(cleaned_lines))


def format_bilibili_description(
    base_desc: str,
    original_url: str = "",
    original_uploader: str = "",
    original_upload_date: str = "",
    append_repost_notice: bool = True,
    max_len: int = BILIBILI_DESCRIPTION_LIMIT,
) -> str:
    summary = _remove_redundant_original_url(base_desc, original_url)
    is_repost = bool(original_url or original_uploader or original_upload_date)
    if not is_repost or not append_repost_notice:
        return _truncate_multiline_text(summary, max_len)

    notice_parts = ["本视频转载自YouTube"]
    if original_upload_date:
        notice_parts.append(f"原始上传时间：{original_upload_date}")
    if original_uploader:
        notice_parts.append(f"UP主：{original_uploader}")
    repost_notice = "，".join(notice_parts)

    if not summary:
        return _truncate_multiline_text(repost_notice, max_len)

    remain_len = max(0, max_len - len(repost_notice) - 2)
    summary = _truncate_multiline_text(summary, remain_len)
    if not summary:
        return _truncate_multiline_text(repost_notice, max_len)
    return f"{repost_notice}\n\n{summary}"


def _extract_response_code_from_exception(exc: Exception) -> Optional[int]:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.isdigit():
        return int(code)

    info = getattr(exc, "raw", None)
    if isinstance(info, dict):
        raw_code = info.get("code")
        if isinstance(raw_code, int):
            return raw_code
        if isinstance(raw_code, str) and raw_code.isdigit():
            return int(raw_code)

    match = re.search(r"错误代码[:：]\s*(\d+)", str(exc))
    if match:
        return int(match.group(1))
    return None


def _compact_exception_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _format_bilibili_exception(exc: Exception) -> str:
    code = _extract_response_code_from_exception(exc)
    message = _compact_exception_text(getattr(exc, "msg", "") or str(exc))

    raw = getattr(exc, "raw", None)
    if isinstance(raw, dict):
        raw_msg = _compact_exception_text(str(raw.get("message", "") or ""))
        if raw_msg and raw_msg not in message:
            message = f"{message} | 接口消息: {raw_msg}" if message else raw_msg

    if code is not None and message:
        return f"接口返回错误代码：{code}，信息：{message}"
    if code is not None:
        return f"接口返回错误代码：{code}"
    return message or "未知错误"


def _is_bilibili_http_406(exc: Exception) -> bool:
    code = _extract_response_code_from_exception(exc)
    text = _compact_exception_text(str(exc))
    return code == 406 or "状态码：406" in text or "status code: 406" in text.lower()


def _bilibili_406_hint() -> str:
    return (
        "bilibili上传被 preupload 接口返回 406 拒绝。"
        "这通常是 B 站风控导致，可能与 Cookie/buvid 状态、服务器 IP 环境或网络指纹有关。"
        "已启用 curl_cffi 浏览器指纹伪装；如仍失败，请重新扫码登录或更换网络环境后重试。"
    )


class _BilibiliChunkProgress:
    def __init__(self, pages=None):
        self._completed = {}
        self._totals = {}
        self._total_bytes = sum(
            max(0, int(page.get_size()))
            for page in (pages or [])
            if hasattr(page, "get_size")
        )
        self._started_at = time.monotonic()
        self._samples = deque([(self._started_at, 0)])

    def record(self, payload: Any) -> Optional[dict[str, Any]]:
        if not isinstance(payload, dict):
            return None
        page = payload.get("page")
        chunk_number = payload.get("chunk_number")
        total_chunk_count = payload.get("total_chunk_count")
        if page is None or not isinstance(chunk_number, int):
            return None
        if not isinstance(total_chunk_count, int) or total_chunk_count <= 0:
            return None

        page_key = id(page)
        self._totals[page_key] = total_chunk_count
        completed_chunks = self._completed.setdefault(page_key, {})
        if chunk_number not in completed_chunks:
            chunk_size = payload.get("chunk_size")
            if not isinstance(chunk_size, int) or chunk_size < 0:
                page_size_value = payload.get("page_size")
                if not page_size_value:
                    get_size = getattr(page, "get_size", None)
                    page_size_value = get_size() if callable(get_size) else 0
                page_size = int(page_size_value or 0)
                offset = max(0, int(payload.get("offset") or 0))
                chunk_size = max(0, min(page_size - offset, (page_size + total_chunk_count - 1) // total_chunk_count))
            completed_chunks[chunk_number] = chunk_size

        if not self._total_bytes:
            known_pages = {
                id(item): int(item.get_size())
                for item in [payload.get("page")]
                if item is not None and hasattr(item, "get_size")
            }
            self._total_bytes = sum(known_pages.values())

        completed = sum(len(chunks) for chunks in self._completed.values())
        total = sum(self._totals.values())
        if total <= 0:
            return None
        uploaded_bytes = sum(
            size for chunks in self._completed.values() for size in chunks.values()
        )
        now = time.monotonic()
        if self._samples[-1][1] != uploaded_bytes:
            self._samples.append((now, uploaded_bytes))
        while len(self._samples) > 2 and now - self._samples[0][0] > 8:
            self._samples.popleft()
        sample_time, sample_bytes = self._samples[0]
        elapsed = max(now - sample_time, 0.001)
        speed = max(0.0, (uploaded_bytes - sample_bytes) / elapsed)
        remaining = max(0, self._total_bytes - uploaded_bytes)
        eta = (remaining / speed) if speed > 0 else None
        return {
            "percent": min(95.0, completed / total * 95.0),
            "uploaded_bytes": uploaded_bytes,
            "total_bytes": self._total_bytes,
            "speed_bytes_per_second": speed,
            "eta_seconds": eta,
        }


class BilibiliUploader:
    """Bilibili uploader based on the internal SDK subset."""

    def __init__(self, cookie_file: str):
        self.cookie_file = cookie_file
        self.logger = None
        self.task_id = None

    def log(self, message: str):
        if self.logger:
            self.logger.info(message)
        else:
            print(message)

    def publish_description_comment(
        self,
        result: dict,
        description: str,
        pin: bool = True,
    ) -> dict:
        """Publish the final description as a comment and optionally pin it."""
        aid = result.get("aid") if isinstance(result, dict) else None
        bvid = str(result.get("bvid") or "") if isinstance(result, dict) else ""
        normalized = _normalize_multiline_text(description)
        message = normalized[:1000].strip()
        details = {
            "enabled": True,
            "posted": False,
            "pinned": False,
            "aid": aid,
            "bvid": bvid,
            "truncated": len(normalized) > len(message),
        }
        if not aid:
            details["error"] = "投稿结果缺少 aid，无法发布简介评论"
            return details
        if not message:
            details["error"] = "简介为空，未发布评论"
            return details

        try:
            credential = load_credential_from_file(self.cookie_file)

            async def _publish():
                reply = await Api(
                    url="https://api.bilibili.com/x/v2/reply/add",
                    method="POST",
                    verify=True,
                    credential=credential,
                ).update_data(
                    type=1,
                    oid=int(aid),
                    message=message,
                    plat=1,
                ).request()
                reply = reply if isinstance(reply, dict) else {}
                reply_info = reply.get("reply")
                if not isinstance(reply_info, dict):
                    reply_info = reply
                rpid = reply_info.get("rpid") or reply_info.get("rpid_str")
                if not rpid:
                    raise RuntimeError(f"B站发评论成功但未返回 rpid: {reply}")

                pinned = False
                pin_error = ""
                pin_attempts = 0
                if pin:
                    pin_headers = {
                        **HEADERS,
                        "Origin": "https://www.bilibili.com",
                        "Referer": (
                            f"https://www.bilibili.com/video/{bvid}/"
                            if bvid
                            else f"https://www.bilibili.com/video/av{aid}/"
                        ),
                    }
                    # A newly created reply is not always immediately visible
                    # to the pin endpoint. Retry only the non-critical pin step;
                    # never post a duplicate description comment.
                    for attempt, delay_seconds in enumerate((0, 1, 2), start=1):
                        pin_attempts = attempt
                        if delay_seconds:
                            await asyncio.sleep(delay_seconds)
                        try:
                            await Api(
                                url="https://api.bilibili.com/x/v2/reply/top",
                                method="POST",
                                verify=True,
                                credential=credential,
                            ).update_headers(**pin_headers).update_data(
                                type=1,
                                oid=int(aid),
                                rpid=int(rpid),
                                action=1,
                            ).request()
                            pinned = True
                            pin_error = ""
                            break
                        except Exception as exc:
                            pin_error = _compact_exception_text(str(exc))
                return str(rpid), pinned, pin_error, pin_attempts

            try:
                rpid, pinned, pin_error, pin_attempts = asyncio.run(_publish())
            except RuntimeError:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    rpid, pinned, pin_error, pin_attempts = pool.submit(
                        asyncio.run, _publish()
                    ).result()

            details.update({
                "posted": True,
                "pinned": pinned,
                "rpid": rpid,
                "message_length": len(message),
                "pin_attempts": pin_attempts,
            })
            if pin_error:
                details["pin_error"] = pin_error
            self.log(
                "Bilibili 简介评论发布成功"
                + ("并已置顶" if pinned else ("，但置顶失败" if pin else ""))
            )
        except Exception as exc:
            details["error"] = _compact_exception_text(str(exc))
            self.log(f"Bilibili 简介评论发布失败（不影响投稿）: {details['error']}")
        return details

    def sync_description_comment(
        self,
        result: dict,
        description: str,
    ) -> dict:
        """Update the uploader's pinned description comment, or create it once."""
        aid = result.get("aid") if isinstance(result, dict) else None
        bvid = str(result.get("bvid") or "") if isinstance(result, dict) else ""
        normalized = _normalize_multiline_text(description)
        message = normalized[:1000].strip()
        details = {
            "enabled": True,
            "posted": False,
            "updated": False,
            "pinned": False,
            "aid": aid,
            "bvid": bvid,
            "truncated": len(normalized) > len(message),
        }
        if not aid:
            details["error"] = "投稿结果缺少 aid，无法同步简介评论"
            return details
        if not message:
            details["error"] = "简介为空，未同步置顶评论"
            return details

        try:
            configure_bilibili_runtime()
            credential = load_credential_from_file(self.cookie_file)
            credential_ok, credential_msg = validate_credential_remote(credential)
            if not credential_ok:
                details["error"] = f"Bilibili登录态无效: {credential_msg}"
                return details

            async def _find_pinned_rpid() -> str:
                payload = await (
                    Api(
                        url="https://api.bilibili.com/x/v2/reply/main",
                        method="GET",
                        verify=True,
                        credential=credential,
                    )
                    .update_params(type=1, oid=int(aid), mode=3, next=0, ps=20)
                    .result
                )
                payload = payload if isinstance(payload, dict) else {}
                upper = payload.get("upper") if isinstance(payload.get("upper"), dict) else {}
                upper_top = upper.get("top")
                if isinstance(upper_top, dict):
                    rpid = upper_top.get("rpid_str") or upper_top.get("rpid")
                    if rpid:
                        return str(rpid)
                elif str(upper_top or "").isdigit():
                    return str(upper_top)

                account_mid = str(getattr(credential, "dedeuserid", "") or "")
                top_replies = payload.get("top_replies")
                for reply in top_replies if isinstance(top_replies, list) else []:
                    if not isinstance(reply, dict):
                        continue
                    member = reply.get("member") if isinstance(reply.get("member"), dict) else {}
                    control = (
                        reply.get("reply_control")
                        if isinstance(reply.get("reply_control"), dict)
                        else {}
                    )
                    is_uploader = (
                        bool(account_mid and str(member.get("mid") or "") == account_mid)
                        or bool(control.get("is_up"))
                    )
                    rpid = reply.get("rpid_str") or reply.get("rpid")
                    if is_uploader and rpid:
                        return str(rpid)
                return ""

            try:
                pinned_rpid = asyncio.run(_find_pinned_rpid())
            except RuntimeError as exc:
                if "cannot be called from a running event loop" not in str(exc):
                    raise
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pinned_rpid = pool.submit(asyncio.run, _find_pinned_rpid()).result()

            if not pinned_rpid:
                created = self.publish_description_comment(
                    {"aid": aid, "bvid": bvid},
                    message,
                    pin=True,
                )
                return {**created, "action": "created"}

            pin_headers = {
                **HEADERS,
                "Origin": "https://www.bilibili.com",
                "Referer": (
                    f"https://www.bilibili.com/video/{bvid}/"
                    if bvid
                    else f"https://www.bilibili.com/video/av{aid}/"
                ),
            }

            async def _update_and_pin() -> tuple[bool, str, int]:
                await (
                    Api(
                        url="https://api.bilibili.com/x/v2/reply/edit",
                        method="POST",
                        verify=True,
                        credential=credential,
                    )
                    .update_data(
                        type=1,
                        oid=int(aid),
                        rpid=int(pinned_rpid),
                        message=message,
                        plat=1,
                    )
                    .request()
                )
                pin_error = ""
                pin_attempts = 0
                for attempt, delay_seconds in enumerate((0, 1, 2), start=1):
                    pin_attempts = attempt
                    if delay_seconds:
                        await asyncio.sleep(delay_seconds)
                    try:
                        await (
                            Api(
                                url="https://api.bilibili.com/x/v2/reply/top",
                                method="POST",
                                verify=True,
                                credential=credential,
                            )
                            .update_headers(**pin_headers)
                            .update_data(
                                type=1,
                                oid=int(aid),
                                rpid=int(pinned_rpid),
                                action=1,
                            )
                            .request()
                        )
                        return True, "", pin_attempts
                    except Exception as exc:
                        pin_error = _compact_exception_text(str(exc))
                return False, pin_error, pin_attempts

            try:
                pinned, pin_error, pin_attempts = asyncio.run(_update_and_pin())
            except RuntimeError as exc:
                if "cannot be called from a running event loop" not in str(exc):
                    raise
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pinned, pin_error, pin_attempts = pool.submit(
                        asyncio.run, _update_and_pin()
                    ).result()

            details.update({
                "posted": True,
                "updated": True,
                "pinned": pinned,
                "rpid": pinned_rpid,
                "message_length": len(message),
                "pin_attempts": pin_attempts,
                "action": "updated",
            })
            if pin_error:
                details["pin_error"] = pin_error
            return details
        except Exception as exc:
            details["error"] = _compact_exception_text(str(exc))
            self.log(f"Bilibili 简介置顶评论同步失败: {details['error']}")
            return details

    def list_archives(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str = "pubed",
    ) -> Tuple[bool, Union[dict, str]]:
        """Return the authenticated creator account's archive list."""
        try:
            configure_bilibili_runtime()
            credential = load_credential_from_file(self.cookie_file)
            credential_ok, credential_msg = validate_credential_remote(credential)
            if not credential_ok:
                return False, f"Bilibili登录态无效: {credential_msg}"

            async def _list():
                return await (
                    Api(
                        url="https://member.bilibili.com/x/web/archives",
                        method="GET",
                        verify=True,
                        credential=credential,
                    )
                    .update_params(
                        status=str(status or "pubed"),
                        pn=max(1, int(page)),
                        ps=max(1, min(50, int(page_size))),
                        coop=1,
                        interactive=1,
                    )
                    .result
                )

            try:
                payload = asyncio.run(_list())
            except RuntimeError as exc:
                if "cannot be called from a running event loop" not in str(exc):
                    raise
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    payload = pool.submit(asyncio.run, _list()).result()
            payload = payload if isinstance(payload, dict) else {}
            rows = payload.get("arc_audits") or payload.get("archives") or []
            archives = []
            for row in rows if isinstance(rows, list) else []:
                row = row if isinstance(row, dict) else {}
                archive = row.get("Archive") or row.get("archive") or row
                if not isinstance(archive, dict):
                    continue
                bvid = str(archive.get("bvid") or "").strip()
                if not bvid:
                    continue
                archives.append({
                    "aid": archive.get("aid"),
                    "bvid": bvid,
                    "title": str(archive.get("title") or ""),
                    "cover": str(archive.get("cover") or ""),
                    "state": archive.get("state"),
                    "state_desc": str(
                        archive.get("state_descv3")
                        or archive.get("state_desc")
                        or ""
                    ),
                    "duration_seconds": int(archive.get("duration") or 0),
                    "published_at": int(
                        archive.get("ptime") or archive.get("ctime") or 0
                    ),
                    "page_count": len(row.get("Videos") or row.get("videos") or []),
                })
            page_info = payload.get("page") if isinstance(payload.get("page"), dict) else {}
            return True, {
                "archives": archives,
                "page": int(page_info.get("pn") or page or 1),
                "page_size": int(page_info.get("ps") or page_size or 20),
                "total": int(page_info.get("count") or len(archives)),
                "status": str(status or "pubed"),
            }
        except Exception as exc:
            return False, f"读取 B站稿件失败: {_compact_exception_text(str(exc))}"

    def archive_detail(self, bvid: str) -> Tuple[bool, Union[dict, str]]:
        """Read one owned archive and its exact page list."""
        clean_bvid = str(bvid or "").strip()
        if not re.fullmatch(r"(?i)BV[0-9A-Za-z]{8,20}", clean_bvid):
            return False, "BVID 格式无效"
        try:
            configure_bilibili_runtime()
            credential = load_credential_from_file(self.cookie_file)
            credential_ok, credential_msg = validate_credential_remote(credential)
            if not credential_ok:
                return False, f"Bilibili登录态无效: {credential_msg}"

            async def _read():
                return await (
                    Api(
                        url="https://member.bilibili.com/x/vupre/web/archive/view",
                        method="GET",
                        verify=True,
                        credential=credential,
                    )
                    .update_params(bvid=clean_bvid, topic_grey=1)
                    .result
                )

            try:
                current = asyncio.run(_read())
            except RuntimeError as exc:
                if "cannot be called from a running event loop" not in str(exc):
                    raise
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    current = pool.submit(asyncio.run, _read()).result()
            current = current if isinstance(current, dict) else {}
            archive = current.get("archive")
            archive = archive if isinstance(archive, dict) else current
            videos = current.get("videos")
            if not isinstance(videos, list):
                videos = archive.get("videos")
            if not isinstance(videos, list) or not videos:
                return False, "B站没有返回该稿件的分P列表"
            return True, {
                "aid": archive.get("aid"),
                "bvid": str(archive.get("bvid") or clean_bvid),
                "title": str(archive.get("title") or ""),
                "cover": str(archive.get("cover") or ""),
                "state": archive.get("state"),
                "state_desc": str(
                    archive.get("state_descv3")
                    or archive.get("state_desc")
                    or ""
                ),
                "description": str(archive.get("desc") or ""),
                "partition_id": str(archive.get("tid") or ""),
                "tags": [
                    tag.strip()
                    for tag in (
                        ",".join(
                            str(item.get("tag_name") or item.get("name") or item)
                            if isinstance(item, dict)
                            else str(item)
                            for item in (archive.get("tag") or archive.get("tags") or [])
                        )
                        if isinstance(archive.get("tag") or archive.get("tags"), list)
                        else str(archive.get("tag") or archive.get("tags") or "")
                    ).split(",")
                    if tag.strip()
                ],
                "published_at": int(
                    archive.get("ptime") or archive.get("ctime") or 0
                ),
                "pages": [
                    {
                        "page_number": index,
                        "title": str(video.get("title") or f"P{index}"),
                        "description": str(video.get("desc") or ""),
                        "cid": video.get("cid"),
                        "filename": str(video.get("filename") or ""),
                    }
                    for index, video in enumerate(videos, 1)
                    if isinstance(video, dict)
                ],
            }
        except Exception as exc:
            return False, f"读取 B站稿件失败: {_compact_exception_text(str(exc))}"

    def delete_archive(
        self,
        *,
        aid: int,
        bvid: str,
    ) -> Tuple[bool, Union[dict, str]]:
        """Permanently delete one archive already verified as owned by this account."""
        clean_bvid = str(bvid or "").strip()
        if int(aid or 0) <= 0:
            return False, "稿件缺少 aid，无法删除"
        if not re.fullmatch(r"(?i)BV[0-9A-Za-z]{8,20}", clean_bvid):
            return False, "BVID 格式无效"
        try:
            configure_bilibili_runtime()
            credential = load_credential_from_file(self.cookie_file)
            credential_ok, credential_msg = validate_credential_remote(credential)
            if not credential_ok:
                return False, f"Bilibili登录态无效: {credential_msg}"

            request_headers = {
                **HEADERS,
                "Origin": "https://member.bilibili.com",
                "Referer": "https://member.bilibili.com/platform/upload-manager/article",
            }

            async def _delete():
                return await (
                    Api(
                        url="https://member.bilibili.com/x/web/archive/delete",
                        method="POST",
                        verify=True,
                        credential=credential,
                    )
                    .update_headers(**request_headers)
                    .update_data(aid=int(aid))
                    .request()
                )

            try:
                asyncio.run(_delete())
            except RuntimeError as exc:
                if "cannot be called from a running event loop" not in str(exc):
                    raise
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(asyncio.run, _delete()).result()
            return True, {"aid": int(aid), "bvid": clean_bvid, "deleted": True}
        except Exception as exc:
            return False, f"删除 B站稿件失败: {_compact_exception_text(str(exc))}"

    def archive_comments(
        self,
        *,
        aid: int,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[bool, Union[dict, str]]:
        """Read recent root comments and visible child replies for one archive."""
        if int(aid or 0) <= 0:
            return False, "稿件缺少 aid，无法读取评论"
        try:
            configure_bilibili_runtime()
            credential = load_credential_from_file(self.cookie_file)

            async def _read():
                return await (
                    Api(
                        url="https://api.bilibili.com/x/v2/reply/main",
                        method="GET",
                        verify=True,
                        credential=credential,
                    )
                    .update_params(
                        type=1,
                        oid=int(aid),
                        mode=3,
                        next=max(0, int(page) - 1),
                        ps=max(1, min(20, int(page_size))),
                    )
                    .result
                )

            payload = asyncio.run(_read())
            payload = payload if isinstance(payload, dict) else {}
            rows = payload.get("replies") or []
            comments: list[dict[str, Any]] = []

            def append_comment(item: dict[str, Any], *, root_rpid: str = "") -> None:
                member = item.get("member") if isinstance(item.get("member"), dict) else {}
                content = item.get("content") if isinstance(item.get("content"), dict) else {}
                rpid = str(item.get("rpid_str") or item.get("rpid") or "")
                if not rpid:
                    return
                comments.append({
                    "rpid": rpid,
                    "root_rpid": root_rpid or rpid,
                    "parent_rpid": rpid,
                    "is_child": bool(root_rpid),
                    "user_name": str(member.get("uname") or "B站用户"),
                    "user_avatar": str(member.get("avatar") or ""),
                    "message": str(content.get("message") or ""),
                    "created_at": int(item.get("ctime") or 0),
                    "like_count": int(item.get("like") or 0),
                    "reply_count": int(item.get("rcount") or 0),
                })

            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                root_rpid = str(row.get("rpid_str") or row.get("rpid") or "")
                append_comment(row)
                children = row.get("replies") if isinstance(row.get("replies"), list) else []
                for child in children:
                    if isinstance(child, dict):
                        append_comment(child, root_rpid=root_rpid)
            cursor = payload.get("cursor") if isinstance(payload.get("cursor"), dict) else {}
            return True, {
                "comments": comments,
                "page": max(1, int(page)),
                "has_more": not bool(cursor.get("is_end", True)),
            }
        except Exception as exc:
            return False, f"读取 B站评论失败: {_compact_exception_text(str(exc))}"

    def reply_to_archive_comment(
        self,
        *,
        aid: int,
        root_rpid: str,
        parent_rpid: str,
        message: str,
    ) -> Tuple[bool, Union[dict, str]]:
        """Send one explicit manual reply to a selected archive comment."""
        clean_message = _normalize_multiline_text(message)[:1000].strip()
        if int(aid or 0) <= 0:
            return False, "稿件缺少 aid，无法回复"
        if not str(root_rpid or "").isdigit() or not str(parent_rpid or "").isdigit():
            return False, "回复对象无效"
        if not clean_message:
            return False, "回复内容为空"
        try:
            configure_bilibili_runtime()
            credential = load_credential_from_file(self.cookie_file)
            credential_ok, credential_msg = validate_credential_remote(credential)
            if not credential_ok:
                return False, f"Bilibili登录态无效: {credential_msg}"

            async def _reply():
                return await (
                    Api(
                        url="https://api.bilibili.com/x/v2/reply/add",
                        method="POST",
                        verify=True,
                        credential=credential,
                    )
                    .update_data(
                        type=1,
                        oid=int(aid),
                        root=int(root_rpid),
                        parent=int(parent_rpid),
                        message=clean_message,
                        plat=1,
                    )
                    .result
                )

            response = asyncio.run(_reply())
            response = response if isinstance(response, dict) else {}
            reply = response.get("reply") if isinstance(response.get("reply"), dict) else response
            return True, {
                "rpid": str(reply.get("rpid_str") or reply.get("rpid") or ""),
                "message": clean_message,
            }
        except Exception as exc:
            return False, f"回复 B站评论失败: {_compact_exception_text(str(exc))}"

    def message_overview(self) -> Tuple[bool, Union[dict, str]]:
        """Read comment/reply, private-message, like and system-notice previews."""
        try:
            configure_bilibili_runtime()
            credential = load_credential_from_file(self.cookie_file)
            credential_ok, credential_msg = validate_credential_remote(credential)
            if not credential_ok:
                return False, f"Bilibili登录态无效: {credential_msg}"

            endpoints = {
                "reply": (
                    "https://api.bilibili.com/x/msgfeed/reply",
                    {"platform": "web", "build": 0, "mobi_app": "web"},
                ),
                "private": (
                    "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions",
                    {
                        "session_type": 1,
                        "group_fold": 1,
                        "unfollow_fold": 0,
                        "sort_rule": 2,
                        "build": 0,
                        "mobi_app": "web",
                    },
                ),
                "like": (
                    "https://api.bilibili.com/x/msgfeed/like",
                    {"platform": "web", "build": 0, "mobi_app": "web"},
                ),
                "system": (
                    "https://api.bilibili.com/x/msgfeed/sys-msg",
                    {"build": 0, "mobi_app": "web"},
                ),
            }

            async def _read_one(url: str, params: dict[str, Any]):
                return await (
                    Api(
                        url=url,
                        method="GET",
                        verify=True,
                        credential=credential,
                    ).update_params(**params).result
                )

            async def _read_all():
                return await asyncio.gather(
                    *(_read_one(url, params) for url, params in endpoints.values()),
                    return_exceptions=True,
                )

            payloads = asyncio.run(_read_all())
            categories: dict[str, dict[str, Any]] = {}
            labels = {
                "reply": ("评论与回复", "https://message.bilibili.com/#/reply"),
                "private": ("私信", "https://message.bilibili.com/#/whisper"),
                "like": ("收到的赞", "https://message.bilibili.com/#/love"),
                "system": ("系统通知", "https://message.bilibili.com/#/system"),
            }

            def first_text(*values: Any) -> str:
                for value in values:
                    text = str(value or "").strip()
                    if text:
                        return text
                return ""

            for (category_key, _endpoint), payload in zip(endpoints.items(), payloads):
                label, external_url = labels[category_key]
                if isinstance(payload, Exception):
                    categories[category_key] = {
                        "label": label,
                        "external_url": external_url,
                        "unread_count": 0,
                        "items": [],
                        "error": _compact_exception_text(str(payload)),
                    }
                    continue
                data = payload if isinstance(payload, dict) else {}
                rows = (
                    data.get("items")
                    or data.get("replies")
                    or data.get("session_list")
                    or data.get("list")
                    or []
                )
                normalized_items = []
                unread_count = int(data.get("unread") or data.get("unread_count") or 0)
                for row in rows[:8] if isinstance(rows, list) else []:
                    if not isinstance(row, dict):
                        continue
                    user = row.get("user") if isinstance(row.get("user"), dict) else {}
                    member = row.get("member") if isinstance(row.get("member"), dict) else {}
                    account = row.get("account_info") if isinstance(row.get("account_info"), dict) else {}
                    item = row.get("item") if isinstance(row.get("item"), dict) else {}
                    last_message = row.get("last_msg") if isinstance(row.get("last_msg"), dict) else {}
                    raw_content = last_message.get("content")
                    if isinstance(raw_content, str) and raw_content.startswith("{"):
                        try:
                            decoded_content = json.loads(raw_content)
                            if isinstance(decoded_content, dict):
                                raw_content = decoded_content.get("content") or raw_content
                        except (TypeError, ValueError):
                            pass
                    row_unread = int(row.get("unread_count") or row.get("unread") or 0)
                    if category_key == "private":
                        unread_count += row_unread
                    normalized_items.append({
                        "user_name": first_text(
                            user.get("nickname"), user.get("uname"),
                            member.get("uname"), account.get("name"),
                            row.get("title"), label,
                        ),
                        "user_avatar": first_text(
                            user.get("avatar"), member.get("avatar"),
                            account.get("pic_url"),
                        ),
                        "text": first_text(
                            item.get("source_content"),
                            item.get("target_reply_content"),
                            item.get("title"),
                            row.get("content"), row.get("message"),
                            raw_content,
                        ),
                        "created_at": int(
                            row.get("reply_time") or row.get("like_time")
                            or row.get("ctime") or row.get("timestamp")
                            or last_message.get("timestamp") or 0
                        ),
                        "unread_count": row_unread,
                    })
                categories[category_key] = {
                    "label": label,
                    "external_url": external_url,
                    "unread_count": unread_count,
                    "items": normalized_items,
                    "error": "",
                }
            return True, {"categories": categories}
        except Exception as exc:
            return False, f"读取 B站消息失败: {_compact_exception_text(str(exc))}"

    def replace_archive_page_source(
        self,
        *,
        bvid: str,
        page_number: int,
        video_file_path: str,
        progress_detail_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        queue_status_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, Union[dict, str]]:
        """Upload a replacement source and edit exactly one page of an owned archive."""
        clean_bvid = str(bvid or "").strip()
        replacement = os.path.realpath(str(video_file_path or ""))
        if not os.path.isfile(replacement):
            return False, f"换源视频不存在: {replacement}"
        try:
            target_page = int(page_number)
        except (TypeError, ValueError):
            return False, "目标分P必须是整数"
        if target_page <= 0:
            return False, "目标分P必须从 1 开始"

        def report_queue(status: str) -> None:
            if queue_status_callback:
                try:
                    queue_status_callback(status)
                except Exception:
                    pass

        lock_path = default_bilibili_upload_lock(get_app_subdir("temp"))
        with bilibili_upload_slot(lock_path, report_queue):
            try:
                configure_bilibili_runtime()
                credential = load_credential_from_file(self.cookie_file)
                credential_ok, credential_msg = validate_credential_remote(credential)
                if not credential_ok:
                    return False, f"Bilibili登录态无效: {credential_msg}"

                async def _replace():
                    current = await (
                        Api(
                            url="https://member.bilibili.com/x/vupre/web/archive/view",
                            method="GET",
                            verify=True,
                            credential=credential,
                        )
                        .update_params(bvid=clean_bvid, topic_grey=1)
                        .result
                    )
                    current = current if isinstance(current, dict) else {}
                    archive = current.get("archive")
                    archive = archive if isinstance(archive, dict) else current
                    videos = current.get("videos")
                    if not isinstance(videos, list):
                        videos = archive.get("videos")
                    if not isinstance(videos, list) or not videos:
                        raise RuntimeError("B站没有返回原稿分P列表，已取消换源")
                    if target_page > len(videos):
                        raise RuntimeError(
                            f"目标是 P{target_page}，但该稿件只有 {len(videos)} 个分P"
                        )
                    safe_videos = []
                    for index, item in enumerate(videos, 1):
                        if not isinstance(item, dict) or not str(item.get("filename") or "").strip():
                            raise RuntimeError(
                                f"B站返回的第 {index}P 缺少 filename，已取消换源"
                            )
                        page = {
                            "title": str(item.get("title") or f"P{index}")[:80],
                            "desc": str(item.get("desc") or "")[:2000],
                            "filename": str(item["filename"]),
                        }
                        if item.get("cid") is not None:
                            page["cid"] = item["cid"]
                        safe_videos.append(page)

                    original_page = dict(safe_videos[target_page - 1])
                    page_object = video_uploader.VideoUploaderPage(
                        path=replacement,
                        title=original_page["title"],
                        description=original_page["desc"],
                    )
                    page_uploader = video_uploader.VideoUploader(
                        pages=[page_object],
                        meta={},
                        cover=video_uploader.Picture(),
                        credential=credential,
                    )
                    progress = _BilibiliChunkProgress([page_object])
                    if progress_detail_callback:
                        @page_uploader.on(video_uploader.VideoUploaderEvents.AFTER_CHUNK.value)
                        def _after_chunk(data):
                            details = progress.record(data)
                            if details:
                                progress_detail_callback(details)
                    uploaded = await page_uploader.upload_pages()
                    if len(uploaded) != 1 or not str(uploaded[0].get("filename") or ""):
                        raise RuntimeError("B站未返回新视频 filename，已取消稿件编辑")
                    replacement_page = dict(uploaded[0])
                    replacement_page["title"] = original_page["title"]
                    replacement_page["desc"] = original_page["desc"]
                    safe_videos[target_page - 1] = replacement_page

                    current_tags = archive.get("tag") or archive.get("tags") or ""
                    if isinstance(current_tags, list):
                        current_tags = ",".join(
                            str(item.get("tag_name") or item.get("name") or item)
                            if isinstance(item, dict)
                            else str(item)
                            for item in current_tags
                        )
                    resolved_aid = archive.get("aid")
                    if not resolved_aid:
                        raise RuntimeError("B站没有返回原稿 aid，已取消换源")
                    copyright_value = int(archive.get("copyright") or 1)
                    payload = {
                        "aid": int(resolved_aid),
                        "title": str(archive.get("title") or "")[:BILIBILI_TITLE_LIMIT],
                        "copyright": copyright_value,
                        "source": str(archive.get("source") or "") if copyright_value == 2 else "",
                        "cover": str(archive.get("cover") or ""),
                        "desc": str(archive.get("desc") or "")[:BILIBILI_DESCRIPTION_LIMIT],
                        "desc_format_id": int(archive.get("desc_format_id") or 0),
                        "dynamic": str(archive.get("dynamic") or ""),
                        "interactive": int(archive.get("interactive") or 0),
                        "act_reserve_create": int(archive.get("act_reserve_create") or 0),
                        "no_reprint": int(archive.get("no_reprint") or 0),
                        "open_elec": int(archive.get("open_elec") or 0),
                        "origin_state": int(archive.get("origin_state") or 0),
                        "subtitle": archive.get("subtitle") or archive.get("subtitles") or {"open": 0, "lan": ""},
                        "tag": str(current_tags),
                        "tid": int(archive.get("tid") or 0),
                        "up_close_danmu": bool(archive.get("up_close_danmu", archive.get("up_close_danmaku", False))),
                        "up_close_reply": bool(archive.get("up_close_reply", False)),
                        "up_selection_reply": bool(archive.get("up_selection_reply", False)),
                        "videos": safe_videos,
                        "csrf": credential.bili_jct,
                    }
                    cover43_url = str(archive.get("cover43") or "").strip()
                    if cover43_url:
                        payload["cover43"] = cover43_url
                    if not payload["title"] or not payload["cover"] or payload["tid"] <= 0:
                        raise RuntimeError("B站原稿核心信息不完整，已取消换源")
                    if progress_detail_callback:
                        progress_detail_callback({"percent": 99.0, "phase": "submitting"})
                    response = await (
                        Api(
                            url="https://member.bilibili.com/x/vu/web/edit",
                            method="POST",
                            verify=True,
                            credential=credential,
                            no_csrf=True,
                            json_body=True,
                        )
                        .update_params(csrf=credential.bili_jct, t=time.time() * 1000)
                        .update_data(**payload)
                        .result
                    )
                    verification = "submitted_unverified"
                    try:
                        refreshed = await (
                            Api(
                                url="https://member.bilibili.com/x/vupre/web/archive/view",
                                method="GET",
                                verify=True,
                                credential=credential,
                            )
                            .update_params(bvid=clean_bvid, topic_grey=1)
                            .result
                        )
                        refreshed_videos = (
                            refreshed.get("videos") if isinstance(refreshed, dict) else None
                        )
                        if isinstance(refreshed_videos, list) and len(refreshed_videos) >= target_page:
                            refreshed_page = refreshed_videos[target_page - 1]
                            if (
                                isinstance(refreshed_page, dict)
                                and str(refreshed_page.get("filename") or "")
                                == replacement_page["filename"]
                            ):
                                verification = "verified"
                    except Exception:
                        pass
                    return {
                        "response": response,
                        "aid": int(resolved_aid),
                        "bvid": clean_bvid,
                        "page_number": target_page,
                        "page_title": original_page["title"],
                        "old_cid": original_page.get("cid"),
                        "old_filename": original_page["filename"],
                        "new_cid": replacement_page.get("cid"),
                        "new_filename": replacement_page["filename"],
                        "source_video_path": replacement,
                        "page_count": len(safe_videos),
                        "verification": verification,
                    }

                try:
                    result = asyncio.run(_replace())
                except RuntimeError as exc:
                    if "cannot be called from a running event loop" not in str(exc):
                        raise
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        result = pool.submit(asyncio.run, _replace()).result()
                return True, result
            except Exception as exc:
                self.log(f"Bilibili换源失败: {_compact_exception_text(str(exc))}")
                self.log(traceback.format_exc())
                return False, f"Bilibili换源失败: {_compact_exception_text(str(exc))}"

    def update_uploaded_metadata(
        self,
        *,
        result: dict,
        title: str,
        description: str,
        tags: list[str] | None = None,
        partition_id: str = "",
        cover_file_path: str = "",
        cover43_file_path: str = "",
    ) -> Tuple[bool, Union[dict, str]]:
        """Update one published Bilibili archive without touching its pages."""
        bvid = str(result.get("bvid") or "").strip() if isinstance(result, dict) else ""
        aid = result.get("aid") if isinstance(result, dict) else None
        safe_title = _compact_text(title or "", BILIBILI_TITLE_LIMIT)
        safe_description = _truncate_multiline_text(
            description or "",
            BILIBILI_DESCRIPTION_LIMIT,
        )
        safe_tags: list[str] = []
        seen_tags: set[str] = set()
        if tags is not None:
            for raw_tag in tags:
                tag = _compact_text(str(raw_tag or ""), 20)
                folded = tag.casefold()
                if tag and folded not in seen_tags:
                    safe_tags.append(tag)
                    seen_tags.add(folded)
                if len(safe_tags) >= 6:
                    break
            if not safe_tags:
                return False, "视频标签为空，至少保留一个标签"
        if not bvid and not aid:
            return False, "稿件任务缺少 BVID 和 aid，无法更新"
        if not safe_title:
            return False, "标题为空，无法更新 B站稿件"
        if cover_file_path and not os.path.isfile(cover_file_path):
            return False, f"新封面文件不存在: {cover_file_path}"
        if cover43_file_path and not os.path.isfile(cover43_file_path):
            return False, f"新首页推荐封面文件不存在: {cover43_file_path}"

        try:
            configure_bilibili_runtime()
            credential = load_credential_from_file(self.cookie_file)
            credential_ok, credential_msg = validate_credential_remote(credential)
            if not credential_ok:
                notify_cookie_invalid(
                    "Bilibili",
                    credential_msg,
                    source="已投稿稿件编辑",
                )
                return False, (
                    f"Bilibili登录态无效: {credential_msg}。"
                    "请在设置页重新扫码登录后重试。"
                )

            async def _update():
                query = {"topic_grey": 1}
                if bvid:
                    query["bvid"] = bvid
                else:
                    query["aid"] = int(aid)
                current = await (
                    Api(
                        url="https://member.bilibili.com/x/vupre/web/archive/view",
                        method="GET",
                        verify=True,
                        credential=credential,
                    )
                    .update_params(**query)
                    .result
                )
                current = current if isinstance(current, dict) else {}
                archive = current.get("archive")
                archive = archive if isinstance(archive, dict) else current
                videos = current.get("videos")
                if not isinstance(videos, list):
                    videos = archive.get("videos")
                if not isinstance(videos, list) or not videos:
                    raise RuntimeError("B站没有返回原稿分P列表，已取消更新以保护现有视频")

                safe_videos = []
                for index, video in enumerate(videos, 1):
                    if not isinstance(video, dict) or not str(video.get("filename") or "").strip():
                        raise RuntimeError(
                            f"B站返回的第 {index}P 缺少 filename，已取消更新以保护现有分P"
                        )
                    page = {
                        "title": str(video.get("title") or f"P{index}")[:80],
                        "desc": str(video.get("desc") or "")[:2000],
                        "filename": str(video["filename"]),
                    }
                    if video.get("cid") is not None:
                        page["cid"] = video["cid"]
                    safe_videos.append(page)

                current_tags = archive.get("tag") or archive.get("tags") or ""
                if isinstance(current_tags, list):
                    current_tags = ",".join(
                        str(item.get("tag_name") or item.get("name") or item)
                        if isinstance(item, dict)
                        else str(item)
                        for item in current_tags
                    )
                cover_url = str(archive.get("cover") or result.get("cover_url") or "").strip()
                cover43_url = str(
                    archive.get("cover43") or result.get("cover43_url") or ""
                ).strip()
                if cover_file_path:
                    cover_url = await video_uploader.upload_cover(
                        cover_file_path,
                        credential,
                    )
                if cover43_file_path:
                    cover43_url = await video_uploader.upload_cover(
                        cover43_file_path,
                        credential,
                    )
                if not cover_url:
                    raise RuntimeError("无法取得当前或新封面地址，已取消稿件更新")

                resolved_aid = archive.get("aid") or aid
                if not resolved_aid:
                    raise RuntimeError("B站没有返回稿件 aid，无法提交更新")
                copyright_value = int(archive.get("copyright") or 1)
                payload = {
                    "aid": int(resolved_aid),
                    "title": safe_title,
                    "copyright": copyright_value,
                    "source": str(archive.get("source") or "") if copyright_value == 2 else "",
                    "cover": cover_url,
                    "desc": safe_description,
                    "desc_format_id": int(archive.get("desc_format_id") or 0),
                    "dynamic": str(archive.get("dynamic") or ""),
                    "interactive": int(archive.get("interactive") or 0),
                    "act_reserve_create": int(archive.get("act_reserve_create") or 0),
                    "no_reprint": int(archive.get("no_reprint") or 0),
                    "open_elec": int(archive.get("open_elec") or 0),
                    "origin_state": int(archive.get("origin_state") or 0),
                    "subtitle": (
                        archive.get("subtitle")
                        or archive.get("subtitles")
                        or {"open": 0, "lan": ""}
                    ),
                    "tag": ",".join(safe_tags) if tags is not None else str(current_tags),
                    "tid": (
                        int(partition_id)
                        if str(partition_id).isdigit()
                        else int(archive.get("tid") or 0)
                    ),
                    "up_close_danmu": bool(
                        archive.get("up_close_danmu", archive.get("up_close_danmaku", False))
                    ),
                    "up_close_reply": bool(archive.get("up_close_reply", False)),
                    "up_selection_reply": bool(archive.get("up_selection_reply", False)),
                    "videos": safe_videos,
                    "csrf": credential.bili_jct,
                }
                if cover43_url:
                    payload["cover43"] = cover43_url
                if payload["tid"] <= 0:
                    raise RuntimeError("B站没有返回原稿分区，已取消更新")
                response = await (
                    Api(
                        url="https://member.bilibili.com/x/vu/web/edit",
                        method="POST",
                        verify=True,
                        credential=credential,
                        no_csrf=True,
                        json_body=True,
                    )
                    .update_params(csrf=credential.bili_jct, t=time.time() * 1000)
                    .update_data(**payload)
                    .result
                )
                return response, int(resolved_aid), cover_url, cover43_url, len(safe_videos)

            try:
                response, resolved_aid, cover_url, cover43_url, page_count = asyncio.run(_update())
            except RuntimeError as exc:
                if "cannot be called from a running event loop" not in str(exc):
                    raise
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    response, resolved_aid, cover_url, cover43_url, page_count = pool.submit(
                        asyncio.run,
                        _update(),
                    ).result()

            updated = dict(result or {})
            if isinstance(response, dict):
                response_data = response.get("data")
                if isinstance(response_data, dict):
                    updated.update({
                        key: value for key, value in response_data.items()
                        if key in {"aid", "bvid"} and value
                    })
                updated["edit_response"] = response
            updated.update({
                "aid": resolved_aid,
                "bvid": bvid or updated.get("bvid"),
                "cover_url": cover_url,
                "cover43_url": cover43_url,
                "part_count": page_count,
                "metadata_updated": True,
                "tags": safe_tags if tags is not None else None,
                "partition_id": str(partition_id or ""),
            })
            self.log(f"Bilibili稿件信息更新成功: {updated.get('bvid') or resolved_aid}")
            return True, updated
        except Exception as exc:
            pretty_error = _format_bilibili_exception(exc)
            self.log(f"Bilibili稿件信息更新失败: {pretty_error}")
            self.log(traceback.format_exc())
            return False, f"Bilibili稿件信息更新失败: {pretty_error}"

    def upload_video(
        self,
        video_file_path: Union[str, List[str]],
        cover_file_path: str,
        title: str,
        description: str,
        tags: List[str],
        partition_id: Union[str, int],
        cover43_file_path: str = "",
        youtube_url: str = "",
        task_id: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        progress_detail_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        title_limit: int = BILIBILI_TITLE_LIMIT,
        description_limit: int = BILIBILI_DESCRIPTION_LIMIT,
        page_titles: Optional[List[str]] = None,
        existing_submission: Optional[dict] = None,
        is_original: bool = False,
        queue_status_callback: Optional[Callable[[str], None]] = None,
        stage_callback: Optional[Callable[[str, str, str, Optional[dict]], None]] = None,
    ) -> Tuple[bool, Union[dict, str]]:
        """Serialize every Bilibili submission across threads and bridge processes."""

        def report(status: str) -> None:
            if not queue_status_callback:
                return
            try:
                queue_status_callback(status)
            except Exception:
                pass

        lock_path = default_bilibili_upload_lock(get_app_subdir("temp"))
        with bilibili_upload_slot(lock_path, report):
            return self._upload_video_unlocked(
                video_file_path=video_file_path,
                cover_file_path=cover_file_path,
                cover43_file_path=cover43_file_path,
                title=title,
                description=description,
                tags=tags,
                partition_id=partition_id,
                youtube_url=youtube_url,
                task_id=task_id,
                progress_callback=progress_callback,
                progress_detail_callback=progress_detail_callback,
                title_limit=title_limit,
                description_limit=description_limit,
                page_titles=page_titles,
                existing_submission=existing_submission,
                is_original=is_original,
                stage_callback=stage_callback,
            )

    def _upload_video_unlocked(
        self,
        video_file_path: Union[str, List[str]],
        cover_file_path: str,
        title: str,
        description: str,
        tags: List[str],
        partition_id: Union[str, int],
        cover43_file_path: str = "",
        youtube_url: str = "",
        task_id: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        progress_detail_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        title_limit: int = BILIBILI_TITLE_LIMIT,
        description_limit: int = BILIBILI_DESCRIPTION_LIMIT,
        page_titles: Optional[List[str]] = None,
        existing_submission: Optional[dict] = None,
        is_original: bool = False,
        stage_callback: Optional[Callable[[str, str, str, Optional[dict]], None]] = None,
    ) -> Tuple[bool, Union[dict, str]]:
        self.task_id = task_id
        self.logger = setup_task_logger(task_id or "unknown")

        try:
            configure_bilibili_runtime()

            def _report_stage(
                stage: str,
                status: str,
                message: str,
                details: Optional[dict] = None,
            ) -> None:
                if not stage_callback:
                    return
                try:
                    stage_callback(stage, status, message, details)
                except Exception:
                    pass

            video_paths = (
                [str(path) for path in video_file_path]
                if isinstance(video_file_path, (list, tuple))
                else [str(video_file_path)]
            )
            if not video_paths:
                return False, "没有可上传的视频文件"
            missing_videos = [path for path in video_paths if not os.path.exists(path)]
            if missing_videos:
                return False, f"视频文件不存在: {missing_videos[0]}"
            if not os.path.exists(cover_file_path):
                return False, f"封面文件不存在: {cover_file_path}"
            if cover43_file_path and not os.path.exists(cover43_file_path):
                return False, f"首页推荐封面文件不存在: {cover43_file_path}"

            _report_stage("cover_precheck", "running", "正在转换并检查投稿封面", None)
            try:
                cover_info = prepare_bilibili_cover(cover_file_path)
                cover_file_path = cover_info["path"]
                cover43_info = None
                if cover43_file_path:
                    cover43_info = prepare_bilibili_cover(
                        cover43_file_path,
                        target_size=BILIBILI_COVER43_SIZE,
                    )
                    cover43_file_path = cover43_info["path"]
            except CoverPreflightError as exc:
                message = f"封面预检失败: {exc}"
                _report_stage("cover_precheck", "failed", message, None)
                self.log(message)
                return False, message

            precheck_details = {
                "format": cover_info["format"],
                "width": cover_info["width"],
                "height": cover_info["height"],
                "ratio": round(cover_info["ratio"], 4),
                "size_bytes": cover_info["size_bytes"],
                "source_format": cover_info["source_format"],
                "path": cover_info["path"],
            }
            if cover43_info:
                precheck_details["cover43"] = {
                    "format": cover43_info["format"],
                    "width": cover43_info["width"],
                    "height": cover43_info["height"],
                    "ratio": round(cover43_info["ratio"], 4),
                    "size_bytes": cover43_info["size_bytes"],
                    "source_format": cover43_info["source_format"],
                    "path": cover43_info["path"],
                }
            _report_stage(
                "cover_precheck",
                "completed",
                (
                    f"封面预检通过：JPEG {cover_info['width']}×{cover_info['height']}，"
                    f"{cover_info['size_bytes'] / 1024:.0f}KB"
                ),
                precheck_details,
            )
            self.log(
                f"封面预检通过：{cover_info['source_format']} -> JPEG "
                f"{cover_info['width']}x{cover_info['height']}，"
                f"{cover_info['size_bytes'] / 1024:.0f}KB"
            )

            credential = load_credential_from_file(self.cookie_file)
            credential_ok, credential_msg = validate_credential_remote(credential)
            if not credential_ok:
                notify_cookie_invalid(
                    "Bilibili",
                    credential_msg,
                    source="投稿登录态校验",
                )
                return False, f"Bilibili登录态无效: {credential_msg}。请在设置页重新扫码登录后重试上传。"

            safe_title_limit = int(title_limit or BILIBILI_TITLE_LIMIT)
            safe_desc_limit = int(description_limit or BILIBILI_DESCRIPTION_LIMIT)
            safe_title = _compact_text(title or "", safe_title_limit)
            safe_desc = _truncate_multiline_text(
                _remove_redundant_original_url(description or "", youtube_url or ""),
                safe_desc_limit,
            )
            safe_tags = [str(t).strip()[:20] for t in (tags or []) if str(t).strip()]
            safe_tags = safe_tags[:12]

            if not safe_title:
                return False, "标题为空，无法上传到bilibili"
            if not partition_id:
                return False, "分区ID为空，无法上传到bilibili"

            tid = int(partition_id)
            # YouTube/手动转载任务保持转载模式；本地直播录播可明确指定为自制。
            original = bool(is_original)
            source = None if original else (youtube_url or None)

            upload_config = load_config()
            if str(upload_config.get("BILIBILI_UPLOAD_ENGINE") or "biliup").strip().lower() == "biliup":
                self.log(
                    f"使用 Biliup 投稿，全局线路："
                    f"{str(upload_config.get('BILIBILI_UPLOAD_LINE') or 'bldsa').strip().lower()}"
                )
                if existing_submission:
                    _report_stage("cover_upload", "skipped", "追加分P沿用已有稿件封面", None)
                biliup_ok, biliup_result = upload_with_biliup(
                    cookie_file=self.cookie_file,
                    video_paths=video_paths,
                    cover_file=cover_file_path,
                    title=safe_title,
                    description=safe_desc,
                    tags=safe_tags,
                    partition_id=tid,
                    page_titles=page_titles,
                    existing_submission=existing_submission,
                    progress_callback=progress_callback,
                    progress_detail_callback=progress_detail_callback,
                    stage_callback=_report_stage,
                    log_callback=self.log,
                )
                if (
                    biliup_ok
                    and cover43_file_path
                    and not existing_submission
                    and isinstance(biliup_result, dict)
                ):
                    edit_ok, edit_result = self.update_uploaded_metadata(
                        result=biliup_result,
                        title=safe_title,
                        description=safe_desc,
                        cover43_file_path=cover43_file_path,
                    )
                    if not edit_ok:
                        self.log(f"4:3 首页推荐封面同步失败（稿件已上传）: {edit_result}")
                        biliup_result["cover43_error"] = str(edit_result)
                    elif isinstance(edit_result, dict):
                        biliup_result = edit_result
                return biliup_ok, biliup_result

            meta = video_uploader.VideoMeta(
                tid=tid,
                title=safe_title,
                desc=safe_desc,
                cover=cover_file_path,
                tags=safe_tags,
                original=original,
                source=source,
                no_reprint=False,
            )

            normalized_page_titles = [str(item or "").strip() for item in (page_titles or [])]
            pages = []
            for index, path in enumerate(video_paths):
                fallback_title = safe_title if len(video_paths) == 1 else f"P{index + 1}"
                page_title = (
                    normalized_page_titles[index]
                    if index < len(normalized_page_titles) and normalized_page_titles[index]
                    else fallback_title
                )
                pages.append(
                    video_uploader.VideoUploaderPage(
                        path=path,
                        title=page_title[:80],
                    )
                )
            uploader_kwargs = {
                "pages": pages,
                "meta": meta,
                "credential": credential,
                "cover": cover_file_path,
            }
            if cover43_file_path:
                uploader_kwargs["cover43"] = cover43_file_path
            uploader = video_uploader.VideoUploader(**uploader_kwargs)

            last_emitted_text = ""
            chunk_progress = _BilibiliChunkProgress(uploader.pages)
            page_positions = {
                id(item): index for index, item in enumerate(uploader.pages, 1)
            }
            page_count = len(uploader.pages)

            def _emit_progress(text: str):
                nonlocal last_emitted_text
                if not progress_callback:
                    return
                progress_text = str(text or "").strip()
                if not progress_text:
                    return
                if progress_text == last_emitted_text:
                    return
                last_emitted_text = progress_text
                try:
                    progress_callback(progress_text)
                except Exception:
                    pass

            def _emit_progress_detail(detail: dict[str, Any]):
                if not progress_detail_callback:
                    return
                try:
                    progress_detail_callback(dict(detail))
                except Exception:
                    pass

            def _page_label(data: Any) -> str:
                page_obj = data.get("page") if isinstance(data, dict) else None
                page_number = page_positions.get(id(page_obj), 1)
                return f"第{page_number}/{page_count}P"

            def _event_error(data: Any) -> str:
                err = data.get("err") if isinstance(data, dict) else data
                return _compact_exception_text(str(err)) or "未知错误"

            @uploader.on(video_uploader.VideoUploaderEvents.AFTER_CHUNK.value)
            def on_after_chunk(data):
                try:
                    detail = chunk_progress.record(data)
                    if detail is None:
                        _emit_progress("上传中...")
                        return
                    _emit_progress(f"{detail['percent']:.1f}%")
                    _emit_progress_detail(detail)
                except Exception:
                    pass

            @uploader.on(video_uploader.VideoUploaderEvents.CHUNK_FAILED.value)
            def on_chunk_failed(data):
                if not isinstance(data, dict):
                    self.log("Bilibili 分块上传失败")
                    return
                chunk_number = int(data.get("chunk_number", 0)) + 1
                total_chunks = data.get("total_chunk_count", "?")
                attempt = data.get("attempt", "?")
                max_attempts = data.get("max_attempts", "?")
                info = _compact_exception_text(str(data.get("info") or "未知错误"))
                if data.get("retrying"):
                    delay = data.get("retry_delay_seconds", 0)
                    self.log(
                        f"Bilibili {_page_label(data)} 分块 {chunk_number}/{total_chunks} 上传失败，"
                        f"尝试 {attempt}/{max_attempts}，{delay} 秒后重试：{info}"
                    )
                else:
                    self.log(
                        f"Bilibili {_page_label(data)} 分块 {chunk_number}/{total_chunks} 上传失败，"
                        f"已停止重试（{attempt}/{max_attempts}）：{info}"
                    )

            @uploader.on(video_uploader.VideoUploaderEvents.PRE_PAGE_SUBMIT.value)
            def on_pre_page_submit(data):
                _emit_progress("95.0%")
                self.log(f"Bilibili {_page_label(data)} 分块上传完成，正在提交分P")

            @uploader.on(video_uploader.VideoUploaderEvents.AFTER_PAGE_SUBMIT.value)
            def on_after_page_submit(data):
                self.log(f"Bilibili {_page_label(data)} 分P提交成功")

            @uploader.on(video_uploader.VideoUploaderEvents.PAGE_SUBMIT_FAILED.value)
            def on_page_submit_failed(data):
                self.log(f"Bilibili {_page_label(data)} 分P提交失败：{_event_error(data)}")

            @uploader.on(video_uploader.VideoUploaderEvents.PRE_COVER.value)
            def on_pre_cover(_data):
                _emit_progress("96.0%")
                _report_stage("cover_upload", "running", "正在上传投稿封面", None)
                self.log("开始上传Bilibili封面")

            @uploader.on(video_uploader.VideoUploaderEvents.AFTER_COVER.value)
            def on_after_cover(_data):
                _emit_progress("98.0%")
                _report_stage("cover_upload", "completed", "投稿封面上传完成", None)
                self.log("Bilibili封面上传成功")

            @uploader.on(video_uploader.VideoUploaderEvents.COVER_FAILED.value)
            def on_cover_failed(data):
                message = f"Bilibili封面上传失败：{_event_error(data)}"
                _report_stage("cover_upload", "failed", message, None)
                self.log(message)

            @uploader.on(video_uploader.VideoUploaderEvents.PRE_SUBMIT.value)
            def on_pre_submit(_data):
                _emit_progress("99.0%")
                self.log("视频和封面上传完成，正在提交Bilibili投稿")

            @uploader.on(video_uploader.VideoUploaderEvents.AFTER_SUBMIT.value)
            def on_after_submit(_data):
                self.log("Bilibili投稿接口提交成功")

            @uploader.on(video_uploader.VideoUploaderEvents.SUBMIT_FAILED.value)
            def on_submit_failed(data):
                self.log(f"Bilibili投稿提交失败：{_event_error(data)}")

            @uploader.on(video_uploader.VideoUploaderEvents.FAILED.value)
            def on_failed(data):
                err = data.get("err") if isinstance(data, dict) else data
                if isinstance(err, ResponseCodeException):
                    self.log(f"bilibili上传失败事件: {_format_bilibili_exception(err)}")
                else:
                    self.log(f"bilibili上传失败事件: {_compact_exception_text(str(err))}")

            _emit_progress("0.0%")
            appending = bool(
                isinstance(existing_submission, dict)
                and existing_submission.get("bvid")
            )
            if appending:
                _report_stage("cover_upload", "skipped", "追加分P沿用已有稿件封面", None)
            self.log("开始追加Bilibili分P" if appending else "开始上传到bilibili")

            async def _run_upload():
                if not appending:
                    return await uploader.start()

                aid = existing_submission.get("aid")
                cover_url = str(existing_submission.get("cover_url") or "")
                existing_parts = existing_submission.get("uploaded_parts")
                if not aid or not cover_url or not isinstance(existing_parts, list):
                    raise ValueError("已有稿件缺少 aid、封面地址或分P上传状态，无法安全追加分P")

                new_parts = await uploader.upload_pages()
                combined_parts = [*existing_parts, *new_parts]
                edit_kwargs = {"aid": int(aid), "cover_url": cover_url}
                existing_cover43_url = str(existing_submission.get("cover43_url") or "")
                if existing_cover43_url:
                    edit_kwargs["cover43_url"] = existing_cover43_url
                edit_result = await uploader.edit(combined_parts, **edit_kwargs)
                merged = dict(edit_result) if isinstance(edit_result, dict) else {}
                merged.setdefault("aid", aid)
                merged.setdefault("bvid", existing_submission.get("bvid"))
                merged["_uploaded_videos"] = combined_parts
                merged["_cover_url"] = cover_url
                merged["_cover43_url"] = str(existing_submission.get("cover43_url") or "")
                return merged

            try:
                result = asyncio.run(_run_upload())
            except RuntimeError:
                # 已有事件循环时，在新线程中运行
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(asyncio.run, _run_upload()).result()

            _emit_progress("100.0%")
            self.log(f"bilibili上传完成: {result}")

            if not isinstance(result, dict):
                return False, "bilibili返回结果格式异常"

            uploaded_parts = result.pop("_uploaded_videos", None)
            cover_url = result.pop("_cover_url", "")
            cover43_url = result.pop("_cover43_url", "")
            bvid = result.get("bvid")
            aid = result.get("aid")
            if not bvid and isinstance(result.get("data"), dict):
                bvid = result["data"].get("bvid")
                aid = result["data"].get("aid", aid)

            if not bvid and not aid:
                return False, f"bilibili返回中未找到 bvid/aid: {result}"

            video_url = f"https://www.bilibili.com/video/{bvid}" if bvid else ""

            return True, {
                "bvid": bvid,
                "aid": aid,
                "url": video_url,
                "part_count": len(uploaded_parts) if isinstance(uploaded_parts, list) else len(video_paths),
                "uploaded_parts": uploaded_parts if isinstance(uploaded_parts, list) else [],
                "cover_url": cover_url,
                "cover43_url": cover43_url,
            }

        except ArgsException as e:
            return False, (
                "bilibili-api 缺少网络后端依赖，请安装 httpx/aiohttp/curl_cffi。"
                f" 详细错误: {e}"
            )
        except ResponseCodeException as e:
            pretty_error = _format_bilibili_exception(e)
            if _is_bilibili_http_406(e):
                pretty_error = _bilibili_406_hint()
            self.log(f"bilibili上传异常: {pretty_error}")
            return False, f"bilibili上传异常: {pretty_error}"
        except Exception as e:
            if _is_bilibili_http_406(e):
                hint = _bilibili_406_hint()
                self.log(f"bilibili上传异常: {hint}")
                self.log(traceback.format_exc())
                return False, f"bilibili上传异常: {hint}"
            self.log(f"bilibili上传异常: {_compact_exception_text(str(e))}")
            self.log(traceback.format_exc())
            return False, f"bilibili上传异常: {_compact_exception_text(str(e))}"
