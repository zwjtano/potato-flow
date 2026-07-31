#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Manage the bundled biliup recorder from the unified Y2A web application."""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from .path_policy import atomic_write_text, ensure_directory, safe_path_component
from .task_lifecycle import recording_task_capabilities

APP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = APP_ROOT.parent
CONFIG_DIR = APP_ROOT / "config"
RECORDINGS_DIR = WORKSPACE_ROOT / "docker-data" / "recordings"
ROOMS_PATH = CONFIG_DIR / "live_recorders.json"
BILIUP_CONFIG_PATH = CONFIG_DIR / "biliup.generated.yaml"
BRIDGE_CONFIG_PATH = WORKSPACE_ROOT / "bridge.config.json"
BRIDGE_CONFIG_EXAMPLE = WORKSPACE_ROOT / "bridge.config.example.json"
LOG_PATH = APP_ROOT / "logs" / "biliup-recorder.log"
PID_PATH = APP_ROOT / "temp" / "biliup-recorder.pid"
STATUS_PATH = APP_ROOT / "temp" / "biliup-recorder-status.json"
CONTROL_PATH = APP_ROOT / "temp" / "biliup-recorder-control.json"
RELOAD_PATH = APP_ROOT / "temp" / "biliup-recorder-reload.json"
RECORDER_RUNTIME_DIR = APP_ROOT / "temp" / "recorder-engine"
ROOM_REFERENCE_DIR = WORKSPACE_ROOT / ".bridge" / "room-references"
FFMPEG_DIR = APP_ROOT / "ffmpeg" / "darwin_arm64"
RECORDING_FILE_SUFFIXES = {
    ".mp4": "video", ".flv": "video", ".mkv": "video", ".webm": "video",
    ".ts": "video", ".m2ts": "video", ".mov": "video",
    ".xml": "xml", ".ass": "ass",
}
DEFAULT_RECORDING_TITLE_TEMPLATE = "{streamer}｜{ai_topic}｜{date}"
DEFAULT_RECORDING_DESCRIPTION_TEMPLATE = "{recording_intro}"
LEGACY_RECORDING_TITLE_TEMPLATES = {
    "",
    "{stem}",
    "【直播回放】{streamer}｜{ai_topic}｜{date}",
    "{streamer}｜{ai_topic}｜{date}｜【直播回放】",
}
LEGACY_RECORDING_DESCRIPTION_TEMPLATES = {
    "",
    "{stem}",
    "直播录播：{stem}",
}
DEFAULT_RECORDING_SEGMENT_MINUTES = 60
AUTO_UPLOAD_RETRY_DELAY_SECONDS = 5 * 60
AUTO_UPLOAD_RETRY_MAX_RETRIES = 3
RECORDING_NOTIFICATION_POLL_SECONDS = 2
RECORDING_STAGE_LABELS = {
    "detect": "监控开播",
    "record": "自动录制",
    "ass": "生成 ASS",
    "ai": "生成 AI 简介",
    "xml_identity": "主播英雄识别",
    "live_stats": "直播数据整理",
    "cover": "生成录制文件封面",
    "cover_16x9": "生成 16:9 个人空间封面",
    "cover_4x3": "生成 4:3 首页推荐封面",
    "remux": "FLV 转 MP4",
    "verify": "验证内嵌封面",
    "cleanup": "清理原 FLV",
    "upload": "投稿 B站",
}

logger = logging.getLogger("live_recorder_manager")


class RecorderConfigError(ValueError):
    pass


def recordings_dir(value: Any = None) -> Path:
    """Resolve the configured recording directory."""
    raw = value
    if raw is None:
        try:
            from .config_manager import load_config

            raw = load_config().get("RECORDINGS_PATH", "docker-data/recordings")
        except Exception:
            raw = "docker-data/recordings"
    text = str(raw or "docker-data/recordings").strip()
    if "\x00" in text:
        raise RecorderConfigError("录播目录包含非法字符")
    if text in {
        ".",
        "recordings",
        "./recordings",
        "docker-data/recordings",
        "./docker-data/recordings",
    }:
        docker_recordings = Path("/data/recordings")
        if docker_recordings.is_dir():
            return docker_recordings
        return Path(RECORDINGS_DIR)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = WORKSPACE_ROOT / path
    return path.resolve(strict=False)


def validate_recordings_dir(value: Any) -> Path:
    path = recordings_dir(value)
    try:
        ensure_directory(path)
        if not path.is_dir():
            raise OSError("目标不是文件夹")
    except OSError as exc:
        raise RecorderConfigError(f"无法使用录播目录“{path}”：{exc}") from exc
    return path


def _atomic_json(path: Path, value: Any) -> None:
    try:
        private = path.resolve(strict=False).is_relative_to(CONFIG_DIR.resolve(strict=False))
    except (OSError, ValueError):
        private = path.name.endswith("cookies.json")
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        private=private,
    )


def _yaml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _slug(value: str) -> str:
    return safe_path_component(value)


def _room_file_marker(room: dict[str, Any]) -> str:
    """Stable, reader-facing marker used by folders and generated filenames."""
    return _slug(str(room.get("name") or "直播间"))


def _task_display_name(value: Any) -> str:
    """Return a compact streamer token that remains readable in task IDs."""
    compact = "".join(char for char in str(value or "") if char.isalnum())
    return compact[:12].upper() or "LIVE"


def _task_display_date(video_path: Any, created_at: Any) -> str:
    """Prefer the recording filename's local date, then fall back to the DB date."""
    filename_match = re.search(r"(?<!\d)(20\d{6})(?:[_-]\d{2})", Path(str(video_path or "")).name)
    if filename_match:
        return filename_match.group(1)[4:]
    created_match = re.match(r"20\d{2}-(\d{2})-(\d{2})", str(created_at or ""))
    if created_match:
        return "".join(created_match.groups())
    return datetime.now().strftime("%m%d")


def _task_display_platform(value: Any) -> str:
    return {
        "bilibili": "BL",
        "douyin": "DY",
        "douyu": "DYU",
        "youtube": "YT",
    }.get(str(value or "").strip().lower(), "LIVE")


def _recording_file_type(path: Path) -> str | None:
    """Classify finalized recordings and FFmpeg's actively-written *.part files."""
    suffix = path.suffix.lower()
    if suffix == ".part":
        suffix = path.with_suffix("").suffix.lower()
    return RECORDING_FILE_SUFFIXES.get(suffix)


def _workspace_runtime_path(value: Any, default: str) -> str:
    """Convert repository-relative paths into paths valid in the active runtime."""
    raw = str(value or default).strip()
    path = Path(raw).expanduser()
    if path.is_absolute():
        return str(path)
    parts = path.parts
    if parts and parts[0] == "y2a-auto":
        return str(APP_ROOT.joinpath(*parts[1:]))
    return str(WORKSPACE_ROOT / path)


def detect_platform(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host in {"live.bilibili.com", "b23.tv"}:
        return "bilibili"
    if host == "douyu.com" or host.endswith(".douyu.com"):
        return "douyu"
    if host == "douyin.com" or host.endswith(".douyin.com"):
        return "douyin"
    raise RecorderConfigError("只支持哔哩哔哩、斗鱼和抖音直播间 URL")


def extract_supported_room_url(value: str) -> str:
    """Extract the first supported live-room URL from a URL or share message."""
    text = str(value or "").strip()
    if not text:
        raise RecorderConfigError("请输入直播间链接或平台分享文案")

    candidates = [text]
    candidates.extend(
        match.group(0).rstrip("，。！？；：、,.!?;:)]}）】》>\"'")
        for match in re.finditer(r"https?://[^\s<>\"']+", text, flags=re.IGNORECASE)
    )
    for candidate in candidates:
        try:
            detect_platform(candidate)
        except RecorderConfigError:
            continue
        return candidate
    raise RecorderConfigError(
        "分享文案中没有找到支持的直播间链接；"
        "请粘贴哔哩哔哩、斗鱼或抖音直播间链接"
    )


def extract_douyin_share_name(value: str) -> str:
    """Extract the visible streamer name from Douyin's standard share copy."""
    match = re.search(r"【([^【】\r\n]{1,80})】\s*正在直播", str(value or ""))
    return match.group(1).strip() if match else ""


def _open_url(
    url: str,
    *,
    referer: str = "",
    cookie: str = "",
    timeout: int = 12,
) -> tuple[bytes, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
    }
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            return response.read(), response.geturl()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RecorderConfigError(f"读取直播间信息失败：{exc}") from exc


def _resolve_redirect_url(
    url: str,
    *,
    referer: str = "",
    cookie: str = "",
    timeout: int = 15,
) -> str:
    """Follow a short URL without downloading the final response body."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
    }
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    last_error: Exception | None = None
    for method in ("HEAD", "GET"):
        try:
            with urlopen(
                Request(url, headers=headers, method=method),
                timeout=timeout,
            ) as response:
                return response.geturl()
        except HTTPError as exc:
            final_url = exc.geturl()
            if final_url and final_url != url:
                return final_url
            last_error = exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
    raise RecorderConfigError(f"解析直播间短链接失败：{last_error}")


def _response_json(
    url: str,
    *,
    referer: str = "",
    cookie: str = "",
    timeout: int = 12,
) -> dict[str, Any]:
    body, _ = _open_url(url, referer=referer, cookie=cookie, timeout=timeout)
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecorderConfigError(f"解析平台直播间信息失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise RecorderConfigError("平台返回的直播间信息格式无效")
    return payload


def _post_form_json(
    url: str,
    data: dict[str, Any],
    *,
    referer: str = "",
    cookie: str = "",
    timeout: int = 12,
) -> dict[str, Any]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
        ),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    request = Request(
        url,
        data=urlencode(data).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RecorderConfigError(f"读取直播间信息失败：{exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecorderConfigError(f"解析平台直播间信息失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise RecorderConfigError("平台返回的直播间信息格式无效")
    return payload


def _resolve_douyu_real_room_id(room_ref: str) -> str:
    """Resolve vanity/named Douyu room references through the mobile page."""
    try:
        body, _ = _open_url(f"https://m.douyu.com/{room_ref}")
    except RecorderConfigError as exc:
        raise RecorderConfigError(f"解析斗鱼真实房间号失败：{exc}") from exc
    text = body.decode("utf-8", errors="replace")
    match = re.search(
        r'"roomInfo"\s*:\s*\{.*?"rid"\s*:\s*"?(\d+)',
        text,
        flags=re.DOTALL,
    )
    if not match:
        match = re.search(r'"rid"\s*:\s*"?(\d+)', text)
    if not match:
        raise RecorderConfigError("无法从斗鱼链接识别真实房间号")
    return match.group(1)


def _douyin_cookie_path() -> Path:
    """Resolve the optional biliup Cookie file without importing Flask."""
    try:
        config = json.loads((CONFIG_DIR / "config.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        config = {}
    raw = str(config.get("DOUYIN_COOKIES_PATH") or "cookies/douyin_cookies.json")
    path = Path(raw).expanduser()
    return path if path.is_absolute() else APP_ROOT / path


def _douyin_cookie_header() -> str:
    from .douyin_auth import load_douyin_cookie

    return load_douyin_cookie(_douyin_cookie_path())


def _bilibili_cookie_path() -> Path:
    """Resolve the Bilibili account Cookie shared by upload and recording."""
    try:
        config = json.loads((CONFIG_DIR / "config.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        config = {}
    raw = str(config.get("BILIBILI_COOKIES_PATH") or "cookies/bili_cookies.json")
    path = Path(raw).expanduser()
    return path if path.is_absolute() else APP_ROOT / path


def _sync_bilibili_recorder_cookie() -> Path | None:
    """Normalize the app Cookie into biliup's cookie_info file format."""
    destination = CONFIG_DIR / "biliup.bilibili.cookies.json"
    source = _bilibili_cookie_path()
    try:
        content = source.read_text(encoding="utf-8").strip()
        cookies: dict[str, str] = {}
        if content.startswith("# Netscape HTTP Cookie File") or "\t" in content:
            for line in content.splitlines():
                parts = line.strip().split("\t")
                if len(parts) >= 7 and not line.lstrip().startswith("#"):
                    cookies[str(parts[5])] = str(parts[6])
        else:
            payload = json.loads(content)
            cookie_list: Any = payload
            if isinstance(payload, dict):
                cookie_list = payload.get("cookies")
                cookie_info = payload.get("cookie_info")
                if isinstance(cookie_info, dict):
                    cookie_list = cookie_info.get("cookies")
                if not isinstance(cookie_list, list):
                    cookies.update({
                        str(name): str(value)
                        for name, value in payload.items()
                        if isinstance(value, (str, int, float))
                    })
            if isinstance(cookie_list, list):
                for item in cookie_list:
                    if not isinstance(item, dict):
                        continue
                    name, value = item.get("name"), item.get("value")
                    if name is not None and value is not None:
                        cookies[str(name)] = str(value)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        destination.unlink(missing_ok=True)
        return None
    cookie_items = [
        {"name": str(name), "value": str(value)}
        for name, value in cookies.items()
        if str(name).strip() and str(value)
    ]
    if not cookie_items:
        destination.unlink(missing_ok=True)
        return None
    _atomic_json(destination, {"cookie_info": {"cookies": cookie_items}})
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination


def _decode_json_string(value: str) -> str:
    try:
        return str(json.loads(f'"{value}"'))
    except (json.JSONDecodeError, TypeError):
        return html.unescape(value.replace(r"\/", "/"))


def _first_json_text(text: str, keys: tuple[str, ...]) -> str:
    for key in keys:
        match = re.search(
            rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"',
            text,
        )
        if match:
            value = _decode_json_string(match.group(1)).strip()
            if value:
                return value
    return ""


def _douyin_relation_sec_uid(query: str) -> str:
    """Return the target streamer ID embedded in a Douyin live share URL."""
    values = parse_qs(query)
    outer_sec_uid = str((values.get("sec_user_id") or [""])[0]).strip()
    try:
        extra_params = json.loads(
            str((values.get("extra_params") or ["{}"])[0])
        )
        live_params = json.loads(
            str(extra_params.get("live_common_share_params") or "{}")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return outer_sec_uid
    return str(
        live_params.get("sec_relation_user_id") or outer_sec_uid
    ).strip()


def _douyin_target_user_info(text: str, sec_uid: str) -> dict[str, Any]:
    """Select the requested profile instead of the signed-in viewer profile."""
    if not sec_uid:
        return {}
    render_match = re.search(
        r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not render_match:
        return {}
    try:
        payload = json.loads(unquote(render_match.group(1)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    app = payload.get("app") if isinstance(payload, dict) else None
    user = app.get("user") if isinstance(app, dict) else None
    info = user.get("info") if isinstance(user, dict) else None
    if not isinstance(info, dict):
        return {}
    returned_sec_uid = str(
        info.get("secUid") or info.get("sec_uid") or ""
    ).strip()
    return info if returned_sec_uid == sec_uid else {}


def _douyin_avatar_url(user: dict[str, Any]) -> str:
    for key in ("avatar_thumb", "avatar_medium", "avatar_larger"):
        avatar = user.get(key)
        if not isinstance(avatar, dict):
            continue
        urls = avatar.get("url_list")
        if isinstance(urls, list) and urls:
            value = str(urls[0] or "").strip()
            if value:
                return value
    return str(
        user.get("avatarUrl") or user.get("avatar300Url") or ""
    ).strip()


def _resolve_douyin_reflow_metadata(
    room_id: str,
    request_sec_uid: str,
    cookie: str,
) -> dict[str, str]:
    """Read the authoritative room owner from Douyin's live reflow API."""
    params = {
        "room_id": room_id,
        "sec_user_id": request_sec_uid,
        "type_id": "0",
        "live_id": "1",
        "version_code": "99.99.99",
        "app_id": "1128",
        "aid": "6383",
    }
    payload = _response_json(
        "https://webcast.amemv.com/webcast/room/reflow/info/?"
        + urlencode(params),
        referer="https://live.douyin.com/",
        cookie=cookie,
        timeout=20,
    )
    data = payload.get("data")
    room = data.get("room") if isinstance(data, dict) else None
    owner = room.get("owner") if isinstance(room, dict) else None
    if not isinstance(room, dict) or not isinstance(owner, dict):
        return {}
    owner_sec_uid = str(
        owner.get("sec_uid") or owner.get("secUid") or ""
    ).strip()
    web_rid = str(
        owner.get("web_rid") or owner.get("webRid") or ""
    ).strip()
    canonical_url = (
        f"https://live.douyin.com/{web_rid}"
        if web_rid
        else (
            f"https://www.douyin.com/user/{quote(owner_sec_uid, safe='')}"
            if owner_sec_uid
            else ""
        )
    )
    return {
        "room_id": str(
            room.get("id_str") or room.get("id") or room_id
        ).strip(),
        "web_rid": web_rid,
        "sec_uid": owner_sec_uid,
        "name": str(owner.get("nickname") or "").strip(),
        "avatar_url": _douyin_avatar_url(owner),
        "live_title": str(room.get("title") or "").strip(),
        "url": canonical_url,
    }


def _resolve_douyin_metadata(url: str) -> dict[str, str]:
    """Resolve direct/short Douyin live URLs from the official room HTML."""
    cookie = _douyin_cookie_header()
    redirect_room_id = ""
    sec_uid = ""
    parsed_source = urlparse(url)
    if (parsed_source.hostname or "").lower() == "v.douyin.com":
        redirect_url = _resolve_redirect_url(
            url,
            referer="https://www.douyin.com/",
            cookie=cookie,
        )
        parsed_redirect = urlparse(redirect_url)
        redirect_host = (parsed_redirect.hostname or "").lower()
        if redirect_host == "webcast.amemv.com":
            room_match = re.search(r"/reflow/(\d+)", parsed_redirect.path)
            redirect_room_id = room_match.group(1) if room_match else ""
            redirect_values = parse_qs(parsed_redirect.query)
            request_sec_uid = str(
                (redirect_values.get("sec_user_id") or [""])[0]
            ).strip()
            if redirect_room_id:
                try:
                    reflow_metadata = _resolve_douyin_reflow_metadata(
                        redirect_room_id,
                        request_sec_uid,
                        cookie,
                    )
                except RecorderConfigError:
                    logger.warning(
                        "抖音直播回流接口未返回主播资料，回退到用户页解析"
                    )
                else:
                    if (
                        reflow_metadata.get("name")
                        and reflow_metadata.get("sec_uid")
                        and reflow_metadata.get("url")
                    ):
                        return reflow_metadata
            sec_uid = _douyin_relation_sec_uid(parsed_redirect.query)
            if not sec_uid:
                raise RecorderConfigError("抖音分享链接中没有有效的用户 ID")
            url = f"https://www.douyin.com/user/{quote(sec_uid, safe='')}"
        else:
            url = redirect_url

    body, final_url = _open_url(
        url,
        referer="https://live.douyin.com/",
        cookie=cookie,
        timeout=20,
    )
    final_platform = detect_platform(final_url)
    if final_platform != "douyin":
        raise RecorderConfigError("抖音短链接没有指向抖音直播间")
    parsed = urlparse(final_url)
    room_ref = (
        redirect_room_id
        or parsed.path.strip("/").split("/", 1)[0]
    )
    text = html.unescape(body.decode("utf-8", errors="replace"))
    render_match = re.search(
        r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if render_match:
        text = f"{text}\n{unquote(render_match.group(1))}"

    target_user = _douyin_target_user_info(text, sec_uid)
    if target_user:
        room_data = target_user.get("roomData")
        room_data = room_data if isinstance(room_data, dict) else {}
        web_rid = str(
            room_data.get("webRid")
            or room_data.get("web_rid")
            or ""
        ).strip()
        room_id = str(
            redirect_room_id
            or room_data.get("roomId")
            or room_data.get("room_id")
            or ""
        ).strip()
        name = str(target_user.get("nickname") or "").strip()
        title = str(
            room_data.get("title") or room_data.get("roomTitle") or ""
        ).strip()
        avatar_url = str(
            target_user.get("avatarUrl")
            or target_user.get("avatar300Url")
            or ""
        ).strip()
    else:
        web_rid = _first_json_text(text, ("web_rid", "webRid")) or room_ref
        room_id = (
            redirect_room_id
            or _first_json_text(text, ("roomId", "room_id", "id_str"))
            or web_rid
        )
        sec_uid = sec_uid or _first_json_text(
            text,
            ("sec_uid", "secUid", "sec_user_id"),
        )
        name = _first_json_text(text, ("nickname", "nick_name", "user_name"))
        title = _first_json_text(text, ("title", "room_title"))
        avatar_url = ""
        avatar_match = re.search(
            r'"avatar_(?:thumb|medium|large)"\s*:\s*\{.*?'
            r'"url_list"\s*:\s*\[\s*"((?:\\.|[^"\\])*)"',
            text,
            flags=re.DOTALL,
        )
        if avatar_match:
            avatar_url = _decode_json_string(avatar_match.group(1))
        if not avatar_url:
            avatar_url = _first_json_text(text, ("avatar", "avatarUrl"))
    if not title:
        title_match = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
            text,
            flags=re.IGNORECASE,
        )
        if title_match:
            title = html.unescape(title_match.group(1)).strip()
    if not room_id and not sec_uid:
        raise RecorderConfigError("抖音直播间链接中没有有效房间号或用户 ID")
    canonical_url = (
        f"https://live.douyin.com/{web_rid}"
        if web_rid
        else f"https://www.douyin.com/user/{quote(sec_uid, safe='')}"
    )
    return {
        "room_id": room_id,
        "web_rid": web_rid,
        "sec_uid": sec_uid,
        "name": name,
        "avatar_url": avatar_url,
        "live_title": title,
        "url": canonical_url,
    }


class LiveRecorderManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._reload_thread: threading.Thread | None = None
        self._orphan_recovery_thread: threading.Thread | None = None
        self._upload_retry_thread: threading.Thread | None = None
        self._recording_notification_thread: threading.Thread | None = None
        self._recording_notification_lock = threading.Lock()
        self._recording_notification_states: dict[str, bool] = {}
        self._recording_notification_details: dict[str, dict[str, Any]] = {}
        if os.environ.pop("POTATO_FLOW_CONTAINER_START", "") == "1":
            self.recover_interrupted_pipeline_jobs()
            self._ensure_upload_retry_thread()

    @property
    def binary_path(self) -> Path:
        override = os.environ.get("BILIUP_BIN", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        release = WORKSPACE_ROOT / "upstream-biliup" / "target" / "release" / "biliup"
        debug = WORKSPACE_ROOT / "upstream-biliup" / "target" / "debug" / "biliup"
        return release if release.is_file() else debug

    def list_rooms(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(ROOMS_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        rooms = data if isinstance(data, list) else []
        for room in rooms:
            room.setdefault("segment_enabled", True)
            room.setdefault("segment_minutes", DEFAULT_RECORDING_SEGMENT_MINUTES)
            room.setdefault("multipart_enabled", False)
            room.setdefault("record_only", False)
            room.setdefault("bilibili_account_id", "")
        return rooms

    @staticmethod
    def recording_prompt_defaults() -> dict[str, str]:
        """Return the built-in prompts displayed by each room editor."""
        if str(WORKSPACE_ROOT) not in sys.path:
            sys.path.insert(0, str(WORKSPACE_ROOT))
        try:
            import bridge
        except ModuleNotFoundError as exc:
            logger.warning("AI 投稿模块不完整，录制页暂时使用内置提示词摘要：%s", exc)
            return {
                "title": "根据本段直播内容提炼核心主题，生成准确、简洁的中文标题。",
                "description": "根据本段弹幕与直播内容生成完整中文简介，保持客观且不虚构。",
                "cover": "根据本段核心内容生成清晰醒目的横向视频封面；DOTA2 内容应遵循游戏原设。",
            }

        return {
            "title": bridge.DEFAULT_RECORDING_TITLE_AI_PROMPT,
            "description": bridge.DEFAULT_RECORDING_DESCRIPTION_AI_PROMPT,
            "cover": bridge.DEFAULT_RECORDING_COVER_AI_PROMPT,
        }

    def save_room_prompts(
        self,
        room_id: str,
        *,
        title_prompt: str = "",
        description_prompt: str = "",
        cover_prompt: str = "",
        cover_reference_file: Any = None,
        cover_reference_suffix: str = "",
        restore_cover_reference: bool = False,
    ) -> dict[str, Any]:
        """Save per-room AI settings and an optional custom character reference."""
        values = {
            "ai_title_prompt": str(title_prompt or "").strip(),
            "ai_description_prompt": str(description_prompt or "").strip(),
            "ai_cover_prompt": str(cover_prompt or "").strip(),
        }
        for value in values.values():
            if len(value) > 6000:
                raise RecorderConfigError("单个自定义提示词不能超过 6000 个字符")
        with self._lock:
            rooms = self.list_rooms()
            room = next((item for item in rooms if item.get("id") == room_id), None)
            if room is None:
                raise RecorderConfigError("没有找到该直播间")
            previous_reference = str(room.get("cover_reference_file") or "").strip()
            if restore_cover_reference:
                if previous_reference:
                    (ROOM_REFERENCE_DIR / Path(previous_reference).name).unlink(missing_ok=True)
                room.pop("cover_reference_file", None)
            elif cover_reference_file and str(
                getattr(cover_reference_file, "filename", "") or ""
            ).strip():
                suffix = str(cover_reference_suffix or "").lower()
                if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                    raise RecorderConfigError("人物底稿只支持 JPG、PNG 或 WEBP")
                ensure_directory(ROOM_REFERENCE_DIR, private=True)
                safe_room_id = re.sub(r"[^0-9A-Za-z_-]+", "-", room_id).strip("-")
                destination = ROOM_REFERENCE_DIR / f"{safe_room_id}{suffix}"
                temporary = ROOM_REFERENCE_DIR / f".{safe_room_id}.upload{suffix}"
                cover_reference_file.save(temporary)
                if (
                    not temporary.is_file()
                    or temporary.stat().st_size <= 0
                    or temporary.stat().st_size > 10 * 1024 * 1024
                ):
                    temporary.unlink(missing_ok=True)
                    raise RecorderConfigError("人物底稿保存失败或文件超过 10 MB")
                temporary.replace(destination)
                destination.chmod(0o600)
                if previous_reference and previous_reference != destination.name:
                    (ROOM_REFERENCE_DIR / Path(previous_reference).name).unlink(missing_ok=True)
                room["cover_reference_file"] = destination.name
            room.update(values)
            _atomic_json(ROOMS_PATH, rooms)
            self._sync_bridge_profiles(rooms)
            return dict(room)

    def room_cover_reference(self, room_id: str) -> tuple[Path | None, str]:
        """Resolve the effective local reference, falling back to bundled artwork."""
        room = next(
            (item for item in self.list_rooms() if item.get("id") == room_id),
            None,
        )
        if room is None:
            raise RecorderConfigError("没有找到该直播间")
        custom_name = Path(str(room.get("cover_reference_file") or "")).name
        if custom_name:
            custom_path = ROOM_REFERENCE_DIR / custom_name
            if custom_path.is_file():
                return custom_path, "custom"
        if str(WORKSPACE_ROOT) not in sys.path:
            sys.path.insert(0, str(WORKSPACE_ROOT))
        try:
            import bridge
        except ModuleNotFoundError as exc:
            logger.warning("无法加载 AI 封面底稿模块，直播录制页回退到主播头像：%s", exc)
            return None, "avatar"

        dedicated = bridge.recording_cover_reference(str(room.get("name") or ""))
        if dedicated:
            return dedicated[1], "built_in"
        return None, "avatar"

    def _worker_status_payload(self, pid: int) -> dict[str, Any]:
        try:
            payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            if int(payload.get("pid") or 0) != pid:
                return {}
            if time.time() - float(payload.get("updated_at") or 0) > 5:
                return {}
            return payload
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return {}

    @staticmethod
    def _worker_status(value: Any) -> str:
        status = str(value or "").strip()
        match = re.search(r"\b(Working|Pending|Idle|Pause)\b", status)
        return match.group(1) if match else "Unknown"

    @classmethod
    def _merge_room_runtime(
        cls,
        rooms: list[dict[str, Any]],
        engine_running: bool,
        status_payload: dict[str, Any] | None = None,
        stream_infos: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        workers = status_payload.get("rooms", []) if isinstance(status_payload, dict) else []
        workers_by_url: dict[str, dict[str, Any]] = {}
        workers_by_remark: dict[str, dict[str, Any]] = {}
        for worker in workers if isinstance(workers, list) else []:
            if not isinstance(worker, dict):
                continue
            live_streamer = worker.get("live_streamer") or {}
            if isinstance(live_streamer, dict):
                workers_by_url[str(live_streamer.get("url") or "")] = worker
                workers_by_remark[str(live_streamer.get("remark") or "")] = worker

        latest_info_by_url: dict[str, dict[str, Any]] = {}
        for info in stream_infos or []:
            if not isinstance(info, dict):
                continue
            url = str(info.get("url") or "")
            previous = latest_info_by_url.get(url)
            if previous is None or int(info.get("date") or 0) > int(previous.get("date") or 0):
                latest_info_by_url[url] = info

        enriched: list[dict[str, Any]] = []
        for source_room in rooms:
            room = dict(source_room)
            manual_enabled = bool(room.get("enabled", True))
            room_url = str(room.get("url") or "")
            parsed_room_url = urlparse(room_url)
            room["display_url"] = parsed_room_url._replace(query="", fragment="").geturl()
            room["display_room_id"] = parsed_room_url.path.rstrip("/").rsplit("/", 1)[-1] or "—"
            room["uid"] = cls.room_uid(room)
            remark = f"{_slug(str(room.get('name') or ''))}_{str(room.get('id') or '')[:6]}"
            worker = workers_by_remark.get(remark) or workers_by_url.get(room_url)
            raw_status = cls._worker_status(worker.get("downloader_status")) if worker else "Unknown"

            if not engine_running:
                state, label = "stopped", "引擎未启动"
                primary, secondary = "等待启动引擎", "启动后自动检测开播"
            elif not manual_enabled:
                state, label = "paused", "已手动停止"
                primary, secondary = "录制已停止", "点击开始录制后恢复直播检测"
            elif raw_status == "Working":
                state, label = "recording", "录制中"
                primary, secondary = "正在录制", "已检测开播，正在写入录播文件"
            elif raw_status == "Pending":
                state, label = "checking", "检测中"
                primary, secondary = "正在检测直播", "正在请求平台直播状态"
            elif raw_status == "Idle":
                state, label = "offline", "未开播"
                primary, secondary = "当前未开播", "每 30 秒自动检测一次"
            elif raw_status == "Pause":
                state, label = "paused", "已暂停"
                primary, secondary = "直播间已暂停", "恢复后继续检测开播"
            else:
                state, label = "unknown", "状态未知"
                primary, secondary = "暂时无法读取状态", "内部录制 worker 尚未同步该房间"

            duration_seconds = 0
            live_title = ""
            started_at = ""
            info = latest_info_by_url.get(room_url) if state == "recording" else None
            if info:
                timestamp = int(info.get("date") or 0)
                live_title = str(info.get("title") or "")
                if timestamp > 0:
                    started = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                    started_at = started.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                    duration_seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))

            room["runtime"] = {
                "state": state,
                "label": label,
                "primary": primary,
                "secondary": secondary,
                "raw_status": raw_status,
                "recording": state == "recording",
                "live": state == "recording",
                "manual_enabled": manual_enabled,
                "duration_seconds": duration_seconds,
                "started_at": started_at,
                "live_title": live_title,
                "current_file": "",
                "current_file_size_bytes": 0,
                "segment_time": cls._room_segment_time(room),
                "segment_enabled": bool(room.get("segment_enabled", True)),
                "segment_minutes": cls._room_segment_minutes(room),
                "record_only": bool(room.get("record_only", False)),
                "multipart_enabled": bool(
                    not room.get("record_only", False)
                    and room.get("segment_enabled", True)
                    and room.get("multipart_enabled", False)
                ),
            }
            enriched.append(room)
        return enriched

    @staticmethod
    def room_uid(room: dict[str, Any]) -> str:
        """Return the public room identifier used in live-recording URLs."""
        platform_room_id = str(room.get("platform_room_id") or "").strip()
        if platform_room_id:
            return platform_room_id
        room_url = str(room.get("url") or "").strip()
        if room_url:
            parsed = urlparse(room_url)
            url_room_id = parsed.path.rstrip("/").rsplit("/", 1)[-1].strip()
            if url_room_id:
                return url_room_id
        return ""

    @staticmethod
    def _attach_current_recording_files(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach the most recently written video to each actively recording room."""
        active_markers = {
            _room_file_marker(room): room
            for room in rooms
            if room.get("runtime", {}).get("recording")
        }
        root = recordings_dir()
        if not active_markers or not root.is_dir():
            return rooms

        latest_by_marker: dict[str, tuple[float, Path, int]] = {}
        for path in root.rglob("*"):
            if not path.is_file() or _recording_file_type(path) != "video":
                continue
            marker = next((value for value in active_markers if value in path.name), "")
            if not marker:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            previous = latest_by_marker.get(marker)
            if previous is None or stat.st_mtime > previous[0]:
                latest_by_marker[marker] = (stat.st_mtime, path, stat.st_size)

        for marker, room in active_markers.items():
            current = latest_by_marker.get(marker)
            if current is None:
                continue
            _, path, size_bytes = current
            room["runtime"]["current_file"] = path.name
            room["runtime"]["current_file_size_bytes"] = size_bytes
        return rooms

    def rooms_with_status(self) -> list[dict[str, Any]]:
        rooms = self.list_rooms()
        pid = self._pid()
        if pid is None:
            return self._merge_room_runtime(rooms, False)
        status_payload = self._worker_status_payload(pid)
        return self._attach_current_recording_files(
            self._merge_room_runtime(
                rooms,
                True,
                status_payload,
                status_payload.get("stream_infos", []),
            )
        )

    def live_status_payload(self) -> dict[str, Any]:
        status = self.status()
        rooms = self.rooms_with_status()
        return {
            "running": status["running"],
            "pid": status["pid"],
            "rooms": [
                {
                    "id": room.get("id"),
                    **room["runtime"],
                }
                for room in rooms
            ],
        }

    @staticmethod
    def _recording_notification_payload(
        room: dict[str, Any],
        *,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        runtime = room.get("runtime") if isinstance(room.get("runtime"), dict) else {}
        previous = dict(details or {})
        room_url = str(room.get("url") or previous.get("room_url") or "").strip()
        try:
            platform = detect_platform(room_url)
        except RecorderConfigError:
            platform = str(previous.get("platform") or "直播平台")
        duration_seconds = int(runtime.get("duration_seconds") or 0)
        started_monotonic = float(previous.get("started_monotonic") or 0)
        if duration_seconds <= 0 and started_monotonic > 0:
            duration_seconds = max(0, int(time.monotonic() - started_monotonic))
        duration_text = ""
        if duration_seconds > 0:
            hours, remainder = divmod(duration_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            duration_text = (
                f"{hours}小时{minutes}分{seconds}秒"
                if hours
                else f"{minutes}分{seconds}秒"
            )
        return {
            "room_id": str(room.get("id") or previous.get("room_id") or ""),
            "streamer": str(room.get("name") or previous.get("streamer") or "未知主播"),
            "platform": platform,
            "room_url": room_url,
            "live_title": str(
                runtime.get("live_title")
                or previous.get("live_title")
                or room.get("live_title")
                or ""
            ),
            "started_at": str(runtime.get("started_at") or previous.get("started_at") or ""),
            "current_file": str(runtime.get("current_file") or previous.get("current_file") or ""),
            "duration_seconds": duration_seconds,
            "duration_text": duration_text,
            "started_monotonic": started_monotonic or time.monotonic(),
        }

    def _reconcile_recording_notifications(self, rooms: list[dict[str, Any]]) -> None:
        from .notifications import (
            EVENT_RECORDING_STARTED,
            EVENT_RECORDING_STOPPED,
            NotificationEvent,
            emit_notification_event,
        )

        events: list[NotificationEvent] = []
        current_ids: set[str] = set()
        with self._recording_notification_lock:
            for room in rooms:
                room_id = str(room.get("id") or "").strip()
                if not room_id:
                    continue
                current_ids.add(room_id)
                runtime = room.get("runtime") if isinstance(room.get("runtime"), dict) else {}
                recording = bool(runtime.get("recording"))
                previous_recording = bool(self._recording_notification_states.get(room_id, False))
                previous_details = self._recording_notification_details.get(room_id, {})
                if recording and not previous_recording:
                    payload = self._recording_notification_payload(room)
                    self._recording_notification_details[room_id] = payload
                    events.append(NotificationEvent(EVENT_RECORDING_STARTED, payload))
                elif recording:
                    self._recording_notification_details[room_id] = (
                        self._recording_notification_payload(room, details=previous_details)
                    )
                elif previous_recording:
                    payload = self._recording_notification_payload(
                        room,
                        details=previous_details,
                    )
                    events.append(NotificationEvent(EVENT_RECORDING_STOPPED, payload))
                    self._recording_notification_details.pop(room_id, None)
                self._recording_notification_states[room_id] = recording

            for room_id in set(self._recording_notification_states) - current_ids:
                if self._recording_notification_states.get(room_id):
                    details = self._recording_notification_details.get(room_id, {})
                    payload = self._recording_notification_payload({}, details=details)
                    events.append(NotificationEvent(EVENT_RECORDING_STOPPED, payload))
                self._recording_notification_states.pop(room_id, None)
                self._recording_notification_details.pop(room_id, None)

        for event in events:
            emit_notification_event(event)

    def _recording_notification_snapshot(self) -> list[dict[str, Any]]:
        rooms = self.list_rooms()
        pid = self._pid()
        if pid is None:
            return self._merge_room_runtime(rooms, False)
        status_payload = self._worker_status_payload(pid)
        return self._merge_room_runtime(
            rooms,
            True,
            status_payload,
            status_payload.get("stream_infos", []),
        )

    def _ensure_recording_notification_thread(self) -> None:
        if (
            self._recording_notification_thread is not None
            and self._recording_notification_thread.is_alive()
        ):
            return

        def monitor() -> None:
            while True:
                try:
                    self._reconcile_recording_notifications(
                        self._recording_notification_snapshot()
                    )
                except Exception:
                    logger.exception("同步录制开始/停止通知失败")
                time.sleep(RECORDING_NOTIFICATION_POLL_SECONDS)

        self._recording_notification_thread = threading.Thread(
            target=monitor,
            daemon=True,
            name="potato-recording-notifications",
        )
        self._recording_notification_thread.start()

    def resolve_room(self, url: str) -> dict[str, Any]:
        """Resolve a supported room URL into canonical streamer metadata."""
        raw_input = str(url or "")
        url = extract_supported_room_url(raw_input)
        platform = detect_platform(url)
        if platform == "bilibili":
            parsed = urlparse(url)
            if (parsed.hostname or "").lower() == "b23.tv":
                try:
                    _, url = _open_url(url)
                except RecorderConfigError as exc:
                    raise RecorderConfigError(f"解析 B站短链接失败：{exc}") from exc
                if detect_platform(url) != "bilibili":
                    raise RecorderConfigError("B站短链接没有指向直播间")
            room_match = re.search(r"/(\d+)", urlparse(url).path)
            if not room_match:
                raise RecorderConfigError("B站直播间链接中没有有效房间号")
            room = _response_json(
                "https://api.live.bilibili.com/room/v1/Room/get_info"
                f"?room_id={room_match.group(1)}",
                referer="https://live.bilibili.com/",
            )
            if room.get("code") != 0 or not isinstance(room.get("data"), dict):
                raise RecorderConfigError(
                    f"B站直播间识别失败：{room.get('message') or room.get('msg') or '房间不存在'}"
                )
            room_data = room["data"]
            real_room_id = str(room_data.get("room_id") or "").strip()
            uid = str(room_data.get("uid") or "").strip()
            if not real_room_id or not uid:
                raise RecorderConfigError("B站直播间没有有效的房间号或主播 UID")
            master = _response_json(
                f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={uid}",
                referer=f"https://live.bilibili.com/{real_room_id}",
            )
            master_data = master.get("data") if master.get("code") == 0 else None
            info = master_data.get("info") if isinstance(master_data, dict) else None
            name = str(info.get("uname") or "").strip() if isinstance(info, dict) else ""
            avatar_url = str(info.get("face") or "").strip() if isinstance(info, dict) else ""
            if not name:
                raise RecorderConfigError("B站没有返回主播名称，请稍后重试")
            return {
                "platform": "bilibili",
                "platform_name": "哔哩哔哩",
                "room_id": real_room_id,
                "name": name,
                "avatar_url": avatar_url,
                "url": f"https://live.bilibili.com/{real_room_id}",
                "live_title": str(room_data.get("title") or "").strip(),
            }

        if platform == "douyin":
            resolved = _resolve_douyin_metadata(url)
            if not resolved.get("name"):
                resolved["name"] = extract_douyin_share_name(raw_input)
            if not resolved.get("name"):
                raise RecorderConfigError(
                    "抖音没有返回真实主播资料；请更新抖音 Cookie 后重试"
                )
            return {
                "platform": "douyin",
                "platform_name": "抖音",
                **resolved,
            }

        parsed = urlparse(url)
        room_ref = parsed.path.strip("/").split("/", 1)[0]
        if not room_ref:
            raise RecorderConfigError("斗鱼直播间链接中没有有效房间号")
        room_id = room_ref
        if not room_id.isdigit():
            room_id = _resolve_douyu_real_room_id(room_ref)
        try:
            payload = _response_json(
                f"https://www.douyu.com/betard/{room_id}",
                referer="https://www.douyu.com/",
            )
        except RecorderConfigError:
            # Numeric Douyu vanity IDs look exactly like ordinary room IDs.
            # If the direct API lookup fails, resolve the mobile page's rid and
            # retry with the platform's internal room ID.
            if not room_ref.isdigit():
                raise
            real_room_id = _resolve_douyu_real_room_id(room_ref)
            if real_room_id == room_id:
                raise
            room_id = real_room_id
            payload = _response_json(
                f"https://www.douyu.com/betard/{room_id}",
                referer="https://www.douyu.com/",
            )
        room_data = payload.get("room")
        if not isinstance(room_data, dict):
            raise RecorderConfigError("斗鱼直播间不存在或暂时无法访问")
        real_room_id = str(room_data.get("room_id") or room_id).strip()
        name = str(room_data.get("owner_name") or room_data.get("nickname") or "").strip()
        avatar = room_data.get("avatar")
        avatar_url = str(room_data.get("owner_avatar") or "").strip()
        if not avatar_url and isinstance(avatar, dict):
            avatar_url = str(avatar.get("big") or avatar.get("middle") or avatar.get("small") or "").strip()
        if not name:
            raise RecorderConfigError("斗鱼没有返回主播名称，请稍后重试")
        return {
            "platform": "douyu",
            "platform_name": "斗鱼",
            "room_id": real_room_id,
            "name": name,
            "avatar_url": avatar_url,
            "url": f"https://www.douyu.com/{real_room_id}",
            "live_title": str(room_data.get("room_name") or "").strip(),
        }

    def _search_douyu_rooms(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        direct_match = re.fullmatch(
            r"(?:https?://(?:www\.|m\.)?douyu\.com/)?(\d+)/?",
            keyword.strip(),
        )
        if direct_match:
            room_ref = direct_match.group(1)
            resolved = self.resolve_room(f"https://www.douyu.com/{room_ref}")
            resolved["is_live"] = None
            resolved["category_name"] = "斗鱼直播"
            return [resolved]

        payload = _post_form_json(
            "https://m.douyu.com/api/search/anchor",
            {
                "did": "00000003333",
                "limit": max(1, min(20, limit)),
                "offset": 0,
                "sk": keyword,
            },
            referer="https://m.douyu.com/search",
        )
        data = payload.get("data")
        candidates = data.get("list") if isinstance(data, dict) else None
        if not isinstance(candidates, list):
            candidates = []
        rooms: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            room_id = str(candidate.get("roomId") or "").strip()
            name = str(candidate.get("nickname") or "").strip()
            if not room_id or not name or room_id in seen:
                continue
            seen.add(room_id)
            avatar_url = str(candidate.get("avatar") or "").strip()
            if avatar_url.startswith("//"):
                avatar_url = f"https:{avatar_url}"
            rooms.append({
                "platform": "douyu",
                "platform_name": "斗鱼",
                "room_id": room_id,
                "name": name,
                "avatar_url": avatar_url,
                "url": f"https://www.douyu.com/{room_id}",
                "live_title": str(candidate.get("roomName") or "").strip(),
                "is_live": bool(candidate.get("isLive")),
                "category_name": str(candidate.get("cateName") or "").strip(),
            })
            if len(rooms) >= max(1, min(20, limit)):
                break
        return rooms

    def _search_bilibili_rooms(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        from .bilibili_auth import load_cookie_dict

        cookie_path = _bilibili_cookie_path()
        if not cookie_path.is_file():
            raise RecorderConfigError("未上传 B站 Cookie")
        cookies = load_cookie_dict(str(cookie_path))
        cookie_header = "; ".join(
            f"{name}={value}" for name, value in cookies.items() if name and value
        )
        payload = _response_json(
            "https://api.bilibili.com/x/web-interface/search/type?"
            + urlencode({
                "search_type": "bili_user",
                "keyword": keyword,
                "page": 1,
                "page_size": max(1, min(20, limit)),
            }),
            referer="https://search.bilibili.com/",
            cookie=cookie_header,
        )
        if int(payload.get("code") or 0) != 0:
            raise RecorderConfigError(
                f"B站搜索失败：{payload.get('message') or payload.get('code')}"
            )
        data = payload.get("data")
        candidates = data.get("result") if isinstance(data, dict) else None
        if not isinstance(candidates, list):
            candidates = []
        rooms: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            mid = str(candidate.get("mid") or "").strip()
            if not mid:
                continue
            room_payload = _response_json(
                "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?"
                + urlencode({"mid": mid}),
                referer=f"https://space.bilibili.com/{mid}",
                cookie=cookie_header,
            )
            room_data = room_payload.get("data")
            room_data = room_data if isinstance(room_data, dict) else {}
            room_id = str(
                room_data.get("roomid") or candidate.get("room_id") or ""
            ).strip()
            if not room_id or room_id == "0" or room_id in seen:
                continue
            seen.add(room_id)
            name = html.unescape(
                re.sub(r"<[^>]+>", "", str(candidate.get("uname") or "").strip())
            )
            avatar_url = str(
                candidate.get("upic") or candidate.get("face") or ""
            ).strip()
            if avatar_url.startswith("//"):
                avatar_url = f"https:{avatar_url}"
            rooms.append({
                "platform": "bilibili",
                "platform_name": "B站",
                "room_id": room_id,
                "name": name or f"B站用户 {mid}",
                "avatar_url": avatar_url,
                "url": f"https://live.bilibili.com/{room_id}",
                "live_title": str(candidate.get("usign") or "").strip(),
                "is_live": bool(
                    int(room_data.get("liveStatus") or candidate.get("is_live") or 0)
                ),
                "category_name": "哔哩哔哩直播",
            })
            if len(rooms) >= max(1, min(20, limit)):
                break
        return rooms

    def search_rooms_with_diagnostics(
        self,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search supported platforms and expose per-platform diagnostics."""
        keyword = str(query or "").strip()
        if len(keyword) < 2:
            raise RecorderConfigError("请输入至少 2 个字符的主播名字")
        if len(keyword) > 80:
            raise RecorderConfigError("主播名字不能超过 80 个字符")

        rooms: list[dict[str, Any]] = []
        platforms: list[dict[str, Any]] = []
        for platform, label, searcher in (
            ("bilibili", "B站", self._search_bilibili_rooms),
            ("douyu", "斗鱼", self._search_douyu_rooms),
        ):
            try:
                found = searcher(keyword, limit)
                rooms.extend(found)
                platforms.append({
                    "platform": platform,
                    "label": label,
                    "ok": bool(found),
                    "count": len(found),
                    "message": (
                        f"找到 {len(found)} 个候选"
                        if found
                        else f"{label}接口没有返回匹配直播间"
                    ),
                })
            except Exception as exc:
                platforms.append({
                    "platform": platform,
                    "label": label,
                    "ok": False,
                    "count": 0,
                    "message": str(exc),
                })

        douyin_cookie = _douyin_cookie_header()
        platforms.append({
            "platform": "douyin",
            "label": "抖音",
            "ok": False,
            "count": 0,
            "message": (
                "已检测到 Cookie；昵称搜索还需要抖音动态签名，本版本暂不返回候选"
                if douyin_cookie
                else "未上传抖音 Cookie，无法测试昵称搜索"
            ),
        })
        return {
            "rooms": rooms[:max(1, min(20, limit))],
            "platforms": platforms,
        }

    def search_rooms(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.search_rooms_with_diagnostics(query, limit)["rooms"]

    @staticmethod
    def _normalize_new_room_recording_settings(
        *,
        segment_enabled: bool,
        segment_minutes: Any,
        multipart_enabled: bool,
        record_only: bool,
        bilibili_account_id: str = "",
    ) -> dict[str, Any]:
        try:
            minutes = int(segment_minutes)
        except (TypeError, ValueError) as exc:
            raise RecorderConfigError("分段时长必须是整数分钟") from exc
        if segment_enabled and not 1 <= minutes <= 1440:
            raise RecorderConfigError("分段时长必须在 1 到 1440 分钟之间")
        return {
            "segment_enabled": bool(segment_enabled),
            "segment_minutes": max(1, min(1440, minutes)),
            "multipart_enabled": bool(
                multipart_enabled and segment_enabled and not record_only
            ),
            "record_only": bool(record_only),
            "bilibili_account_id": str(bilibili_account_id or "").strip(),
        }

    def add_room_from_url(
        self,
        url: str,
        *,
        segment_enabled: bool | None = None,
        segment_minutes: Any = None,
        multipart_enabled: bool | None = None,
        record_only: bool | None = None,
        bilibili_account_id: str | None = None,
    ) -> dict[str, Any]:
        resolved = self.resolve_room(url)
        settings_provided = any(
            value is not None
            for value in (
                segment_enabled,
                segment_minutes,
                multipart_enabled,
                record_only,
                bilibili_account_id,
            )
        )
        recording_settings = (
            self._normalize_new_room_recording_settings(
                segment_enabled=True if segment_enabled is None else segment_enabled,
                segment_minutes=(
                    DEFAULT_RECORDING_SEGMENT_MINUTES
                    if segment_minutes is None
                    else segment_minutes
                ),
                multipart_enabled=(
                    False if multipart_enabled is None else multipart_enabled
                ),
                record_only=False if record_only is None else record_only,
                bilibili_account_id=bilibili_account_id or "",
            )
            if settings_provided
            else {}
        )
        with self._lock:
            rooms = self.list_rooms()
            existing = next(
                (
                    room for room in rooms
                    if room.get("platform") == resolved["platform"]
                    and (
                        str(room.get("platform_room_id") or "") == resolved["room_id"]
                        or (
                            bool(resolved.get("sec_uid"))
                            and str(room.get("platform_user_id") or "")
                            == str(resolved.get("sec_uid"))
                        )
                        or str(room.get("url") or "").rstrip("/") == resolved["url"]
                    )
                ),
                None,
            )
            if existing is None:
                existing = {
                    "id": uuid.uuid4().hex,
                    "enabled": True,
                    **(
                        recording_settings
                        or {
                            "segment_enabled": True,
                            "segment_minutes": DEFAULT_RECORDING_SEGMENT_MINUTES,
                            "multipart_enabled": False,
                            "record_only": False,
                        }
                    ),
                }
                rooms.append(existing)
            existing.update({
                "name": resolved["name"],
                "url": resolved["url"],
                "platform": resolved["platform"],
                "platform_room_id": resolved["room_id"],
                "platform_user_id": str(
                    resolved.get("sec_uid")
                    or existing.get("platform_user_id")
                    or ""
                ),
                "avatar_url": resolved["avatar_url"],
                **recording_settings,
            })
            _atomic_json(ROOMS_PATH, rooms)
            self.sync_configs(rooms)
            self._write_control_state(rooms)
            return dict(existing)

    def add_room_from_url_and_reload(
        self,
        url: str,
        **recording_settings: Any,
    ) -> tuple[dict[str, Any], str]:
        """Resolve, save and reload a room without interrupting active recordings."""
        with self._lock:
            was_running = self._pid() is not None
            room = self.add_room_from_url(url, **recording_settings)
            if not was_running:
                return room, "saved"
            if any(item.get("runtime", {}).get("recording") for item in self.rooms_with_status()):
                _atomic_json(RELOAD_PATH, {"requested_at": time.time()})
                self._ensure_reload_thread()
                return room, "pending"
            self.stop()
            self.start()
            return room, "reloaded"

    def save_room(self, name: str, url: str, room_id: str | None = None) -> dict[str, Any]:
        name = name.strip()
        url = url.strip()
        if not name:
            raise RecorderConfigError("直播间名称不能为空")
        platform = detect_platform(url)
        with self._lock:
            rooms = self.list_rooms()
            existing = next((room for room in rooms if room.get("id") == room_id), None)
            if existing is None:
                existing = {"id": uuid.uuid4().hex, "enabled": True}
                rooms.append(existing)
            existing.update({"name": name, "url": url, "platform": platform})
            _atomic_json(ROOMS_PATH, rooms)
            self.sync_configs(rooms)
            self._write_control_state(rooms)
            return dict(existing)

    def save_room_and_reload(self, name: str, url: str) -> tuple[dict[str, Any], str]:
        """Save a room and make a running worker load it without truncating recordings."""
        with self._lock:
            was_running = self._pid() is not None
            room = self.save_room(name, url)
            if not was_running:
                return room, "saved"
            if any(item.get("runtime", {}).get("recording") for item in self.rooms_with_status()):
                _atomic_json(RELOAD_PATH, {"requested_at": time.time()})
                self._ensure_reload_thread()
                return room, "pending"
            self.stop()
            self.start()
            return room, "reloaded"

    def _ensure_reload_thread(self) -> None:
        if self._reload_thread is not None and self._reload_thread.is_alive():
            return
        self._reload_thread = threading.Thread(
            target=self._reload_when_recordings_finish,
            name="biliup-recorder-config-reload",
            daemon=True,
        )
        self._reload_thread.start()

    def _reload_when_recordings_finish(self) -> None:
        while RELOAD_PATH.exists():
            time.sleep(1)
            with self._lock:
                if self._pid() is None:
                    RELOAD_PATH.unlink(missing_ok=True)
                    return
                try:
                    reload_request = json.loads(RELOAD_PATH.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    reload_request = {}
                force_boundary = bool(reload_request.get("force_segment_boundary"))
                if (
                    not force_boundary
                    and any(item.get("runtime", {}).get("recording") for item in self.rooms_with_status())
                ):
                    continue
                try:
                    self.stop()
                    self.start()
                except RecorderConfigError:
                    return
                RELOAD_PATH.unlink(missing_ok=True)
                return

    def delete_room(self, room_id: str) -> bool:
        with self._lock:
            rooms = self.list_rooms()
            filtered = [room for room in rooms if room.get("id") != room_id]
            if len(filtered) == len(rooms):
                return False
            _atomic_json(ROOMS_PATH, filtered)
            self.sync_configs(filtered)
            self._write_control_state(filtered)
            return True

    def delete_room_and_reload(self, room_id: str) -> str:
        """Delete one room and safely reload a running recorder when possible."""
        with self._lock:
            rooms = self.list_rooms()
            if not any(room.get("id") == room_id for room in rooms):
                return "missing"
            was_running = self._pid() is not None
            runtime_rooms = self.rooms_with_status() if was_running else []
            target = next((room for room in runtime_rooms if room.get("id") == room_id), None)
            if target and target.get("runtime", {}).get("recording"):
                raise RecorderConfigError("这个直播间正在录制，请先停止该直播间并等待文件安全收尾后再删除")
            other_recording = any(
                room.get("id") != room_id and room.get("runtime", {}).get("recording")
                for room in runtime_rooms
            )
            self.delete_room(room_id)
            if not was_running:
                return "deleted"
            if not self.list_rooms():
                self.stop()
                return "stopped"
            if other_recording:
                _atomic_json(RELOAD_PATH, {"requested_at": time.time()})
                self._ensure_reload_thread()
                return "pending"
            self.stop()
            self.start()
            return "reloaded"

    def _write_control_state(self, rooms: list[dict[str, Any]] | None = None) -> None:
        rooms = rooms if rooms is not None else self.list_rooms()
        _atomic_json(
            CONTROL_PATH,
            {
                "updated_at": time.time(),
                "rooms": {
                    str(room.get("url") or ""): bool(room.get("enabled", True))
                    for room in rooms
                    if room.get("url")
                },
            },
        )

    def _clear_stale_multipart_session(self, session_key: str) -> bool:
        """Detach an earlier failed broadcast before a manual recording restarts."""
        state_path = self._pipeline_state_path()
        if not state_path.is_file():
            return False
        try:
            with sqlite3.connect(state_path, timeout=5) as db:
                cursor = db.execute(
                    "DELETE FROM multipart_sessions WHERE session_key=?",
                    (session_key,),
                )
            return cursor.rowcount > 0
        except sqlite3.Error:
            return False

    @staticmethod
    def _room_segment_minutes(room: dict[str, Any]) -> int:
        try:
            return max(1, min(1440, int(room.get("segment_minutes") or DEFAULT_RECORDING_SEGMENT_MINUTES)))
        except (TypeError, ValueError):
            return DEFAULT_RECORDING_SEGMENT_MINUTES

    @classmethod
    def _room_segment_time(cls, room: dict[str, Any]) -> str | None:
        if not bool(room.get("segment_enabled", True)):
            return None
        minutes = cls._room_segment_minutes(room)
        hours, remainder = divmod(minutes, 60)
        return f"{hours:02d}:{remainder:02d}:00"

    def room_multipart_enabled(self, room: dict[str, Any] | str) -> bool:
        if isinstance(room, str):
            room = next((item for item in self.list_rooms() if item.get("id") == room), {})
        return bool(
            not room.get("record_only", False)
            and room.get("segment_enabled", True)
            and room.get("multipart_enabled", False)
        )

    def save_room_recording_settings(
        self,
        room_id: str,
        *,
        segment_enabled: bool,
        segment_minutes: Any,
        multipart_enabled: bool,
        record_only: bool = False,
        bilibili_account_id: str = "",
    ) -> tuple[dict[str, Any], str]:
        """Save per-room segmentation/upload mode and safely rotate active files."""
        try:
            minutes = int(segment_minutes)
        except (TypeError, ValueError) as exc:
            raise RecorderConfigError("分段时长必须是整数分钟") from exc
        if segment_enabled and not 1 <= minutes <= 1440:
            raise RecorderConfigError("分段时长必须在 1 到 1440 分钟之间")
        minutes = max(1, min(1440, minutes or DEFAULT_RECORDING_SEGMENT_MINUTES))

        with self._lock:
            rooms = self.list_rooms()
            room = next((item for item in rooms if item.get("id") == room_id), None)
            if room is None:
                raise RecorderConfigError("没有找到该直播间")
            runtime_rooms = self.rooms_with_status() if self._pid() is not None else []
            target_runtime = next((item for item in runtime_rooms if item.get("id") == room_id), {})
            target_recording = bool(target_runtime.get("runtime", {}).get("recording"))

            room.update({
                "segment_enabled": bool(segment_enabled),
                "segment_minutes": minutes,
                "multipart_enabled": bool(multipart_enabled and segment_enabled),
                "record_only": bool(record_only),
                "bilibili_account_id": str(bilibili_account_id or "").strip(),
            })
            if (room["record_only"] or not room["multipart_enabled"]) and not target_recording:
                self._clear_stale_multipart_session(room_id)
            _atomic_json(ROOMS_PATH, rooms)
            self.sync_configs(rooms)
            self._write_control_state(rooms)

            if self._pid() is None:
                return dict(room), "saved"
            _atomic_json(RELOAD_PATH, {
                "requested_at": time.time(),
                "room_id": room_id,
                "force_segment_boundary": target_recording,
            })
            self._ensure_reload_thread()
            return dict(room), "pending" if target_recording else "queued"

    def set_room_recording(self, room_id: str, enabled: bool) -> dict[str, Any]:
        """Enable or gracefully pause one room without stopping the whole engine."""
        with self._lock:
            rooms = self.list_rooms()
            room = next((item for item in rooms if item.get("id") == room_id), None)
            if room is None:
                raise RecorderConfigError("没有找到该直播间")
            was_enabled = bool(room.get("enabled", True))
            if enabled and not was_enabled:
                self._clear_stale_multipart_session(str(room["id"]))
            room["enabled"] = bool(enabled)
            _atomic_json(ROOMS_PATH, rooms)
            self._write_control_state(rooms)
            if enabled and self._pid() is None:
                self.start()
            return dict(room)

    def sync_configs(self, rooms: list[dict[str, Any]] | None = None) -> None:
        rooms = rooms if rooms is not None else self.list_rooms()
        root = validate_recordings_dir(None)
        ensure_directory(CONFIG_DIR)
        # biliup creates its own data/data.sqlite3 relative to cwd. Keep that
        # third-party runtime state outside the user-facing recording tree.
        ensure_directory(RECORDER_RUNTIME_DIR)
        ensure_directory(LOG_PATH.parent)
        ensure_directory(PID_PATH.parent)

        lines = [
            "# 由统一管理后台自动生成，请勿手动编辑。",
            "downloader: ffmpeg",
            # 固定按时长切分；是否合并到同一个 BVID 由投稿模式设置决定。
            "file_size: null",
            "segment_time: null",
            # 手动录制允许随时停止；不能让 biliup 把短录播当作碎片删除，
            # 否则视频不会进入 segment_processor / ASS 流程。
            "filtering_threshold: 0",
            f"filename_prefix: {_yaml_string(str(root / '{streamer}_{title}_%Y-%m-%d_%H-%M'))}",
            "uploader: Noop",
            "delay: 30",
            "event_loop_interval: 30",
            "checker_sleep: 10",
            f"pool1_size: {max(3, len(rooms) + 1)}",
            "pool2_size: 1",
            "bilibili_danmaku: true",
            # 25000 是 biliup 的最高 B站画质请求值；原画仍取决于账号权限和直播源。
            "bili_qn: 25000",
            "douyu_danmaku: true",
            "douyin_danmaku: true",
        ]
        user_lines = []
        bilibili_cookie_file = _sync_bilibili_recorder_cookie()
        if bilibili_cookie_file:
            user_lines.append(
                f"  bili_cookie_file: {_yaml_string(str(bilibili_cookie_file))}"
            )
        douyin_cookie = _douyin_cookie_header()
        if douyin_cookie:
            user_lines.append(f"  douyin_cookie: {_yaml_string(douyin_cookie)}")
        if user_lines:
            lines.append("user:")
            lines.extend(user_lines)
        if not rooms:
            lines.append("streamers: {}")
        else:
            lines.append("streamers:")
        for room in rooms:
            key = f"{_slug(str(room['name']))}_{str(room['id'])[:6]}"
            file_marker = _room_file_marker(room)
            filename_prefix = str(
                root
                / file_marker
                / f"{file_marker}_{{title}}_{{live_start}}"
                / f"{file_marker}_{{title}}_%Y-%m-%d_%H-%M"
            )
            session_key = str(room["id"])
            segment_time = self._room_segment_time(room)
            record_only = bool(room.get("record_only", False))
            multipart_enabled = self.room_multipart_enabled(room)
            bridge_base = [
                _yaml_string(str(APP_ROOT / ".venv" / "bin" / "python")),
                _yaml_string(str(WORKSPACE_ROOT / "bridge.py")),
                "--config",
                _yaml_string(str(BRIDGE_CONFIG_PATH)),
            ]
            segment_args = [*bridge_base, "record-only", "--room-id", _yaml_string(session_key)]
            if not record_only:
                segment_args = [*bridge_base, "ingest"]
                if multipart_enabled:
                    segment_args.extend(["--session-key", _yaml_string(session_key)])
            segment_command = " ".join(segment_args)
            room_lines = [
                f"  {_yaml_string(key)}:",
                "    url:",
                f"      - {_yaml_string(str(room['url']))}",
                "    uploader: Noop",
                f"    filename_prefix: {_yaml_string(filename_prefix)}",
                "    override:",
                f"      segment_time: {_yaml_string(segment_time) if segment_time else 'null'}",
                "      file_size: null",
                "    segment_processor:",
                f"      - run: {_yaml_string(segment_command)}",
            ]
            if multipart_enabled:
                finalize_command = " ".join([
                    *bridge_base,
                    "finalize-session",
                    "--session-key",
                    _yaml_string(session_key),
                ])
                room_lines.extend([
                    "    postprocessor:",
                    f"      - run: {_yaml_string(finalize_command)}",
                ])
            lines.extend(room_lines)
        atomic_write_text(BILIUP_CONFIG_PATH, "\n".join(lines) + "\n", private=True)
        self._sync_bridge_profiles(rooms)

    def refresh_credentials(self) -> str:
        """Regenerate recorder config and safely reload a running worker."""
        with self._lock:
            self.sync_configs()
            if self._pid() is None:
                return "saved"
            if any(
                item.get("runtime", {}).get("recording")
                for item in self.rooms_with_status()
            ):
                _atomic_json(RELOAD_PATH, {"requested_at": time.time()})
                self._ensure_reload_thread()
                return "pending"
            self.stop()
            self.start()
            return "reloaded"

    def _sync_bridge_profiles(self, rooms: list[dict[str, Any]]) -> None:
        if BRIDGE_CONFIG_PATH.exists():
            try:
                config = json.loads(BRIDGE_CONFIG_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RecorderConfigError(f"桥接配置不是有效 JSON：{exc}") from exc
        elif BRIDGE_CONFIG_EXAMPLE.exists():
            config = json.loads(BRIDGE_CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        else:
            config = {}
        config["y2a_root"] = str(APP_ROOT)
        config["bilibili_cookies"] = _workspace_runtime_path(
            config.get("bilibili_cookies"),
            "y2a-auto/cookies/bili_cookies.json",
        )
        config["danmaku_fonts_dir"] = _workspace_runtime_path(
            config.get("danmaku_fonts_dir"),
            "y2a-auto/fonts",
        )
        if str(config.get("title_template") or "").strip() in LEGACY_RECORDING_TITLE_TEMPLATES:
            config["title_template"] = DEFAULT_RECORDING_TITLE_TEMPLATE
        if (
            str(config.get("description_template") or "").strip()
            in LEGACY_RECORDING_DESCRIPTION_TEMPLATES
        ):
            config["description_template"] = DEFAULT_RECORDING_DESCRIPTION_TEMPLATE
        config.setdefault("post_description_comment", True)
        config.setdefault("pin_description_comment", True)
        if (FFMPEG_DIR / "ffmpeg").is_file():
            config["ffmpeg"] = str(FFMPEG_DIR / "ffmpeg")
        if (FFMPEG_DIR / "ffprobe").is_file():
            config["ffprobe"] = str(FFMPEG_DIR / "ffprobe")
        from .bilibili_accounts import resolve_account, resolve_cookie_path
        from .config_manager import load_config

        app_config = load_config()
        config["douyu_stats_enabled"] = bool(
            app_config.get("DOUYU_STATS_ENABLED", True)
        )
        config["douyu_stats_append_description"] = bool(
            app_config.get("DOUYU_STATS_APPEND_DESCRIPTION", True)
        )
        config["douyu_stats_cover_context_enabled"] = bool(
            app_config.get("DOUYU_STATS_COVER_CONTEXT_ENABLED", True)
        )
        profiles = []
        for room in rooms:
            account = resolve_account(
                app_config,
                room.get("bilibili_account_id"),
            )
            profile = {
                "match": f"*{_room_file_marker(room)}*",
                "source_url": room["url"],
                "streamer_name": str(room["name"]),
                "streamer_avatar_url": str(room.get("avatar_url") or ""),
                "tags": [str(room["name"]), "直播录播"],
                "ai_title_prompt": str(room.get("ai_title_prompt") or ""),
                "ai_description_prompt": str(room.get("ai_description_prompt") or ""),
                "ai_cover_prompt": str(room.get("ai_cover_prompt") or ""),
                "bilibili_account_id": str(account["id"]),
                "bilibili_account_name": str(account["name"]),
                "bilibili_cookies": str(resolve_cookie_path(account.get("cookies_path"))),
            }
            custom_reference_name = Path(
                str(room.get("cover_reference_file") or "")
            ).name
            custom_reference_path = ROOM_REFERENCE_DIR / custom_reference_name
            if custom_reference_name and custom_reference_path.is_file():
                profile["cover_reference_path"] = str(custom_reference_path)
            profiles.append(profile)
        config["profiles"] = profiles
        _atomic_json(BRIDGE_CONFIG_PATH, config)

    def _pid(self) -> int | None:
        if self._process is not None and self._process.poll() is None:
            return self._process.pid
        try:
            pid = int(PID_PATH.read_text(encoding="utf-8").strip())
            payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            status_pid = int(payload.get("pid") or 0)
            heartbeat_fresh = time.time() - float(payload.get("updated_at") or 0) <= 5
            if status_pid != pid or not heartbeat_fresh:
                raise ProcessLookupError
            try:
                os.kill(pid, 0)
            except PermissionError:
                return pid
            return pid
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            PID_PATH.unlink(missing_ok=True)
        return None

    def status(self) -> dict[str, Any]:
        pid = self._pid()
        return {
            "running": pid is not None,
            "pid": pid,
            "binary_ready": self.binary_path.is_file() and os.access(self.binary_path, os.X_OK),
            "binary_path": str(self.binary_path),
            "config_path": str(BILIUP_CONFIG_PATH),
            "recordings_path": str(recordings_dir()),
        }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._pid() is not None:
                self._ensure_orphan_recovery_thread()
                self._ensure_upload_retry_thread()
                self._ensure_recording_notification_thread()
                return self.status()
            if not self.list_rooms():
                raise RecorderConfigError("请先添加至少一个直播间")
            binary = self.binary_path
            if not binary.is_file():
                raise RecorderConfigError("录制引擎尚未构建，请先安装 Rust 并构建 biliup")
            self.sync_configs()
            self._write_control_state()
            STATUS_PATH.unlink(missing_ok=True)
            self._log_handle = LOG_PATH.open("a", encoding="utf-8")
            process_env = os.environ.copy()
            if FFMPEG_DIR.is_dir():
                process_env["PATH"] = f"{FFMPEG_DIR}{os.pathsep}{process_env.get('PATH', '')}"
            self._process = subprocess.Popen(
                [
                    str(binary),
                    "recorder",
                    "--config",
                    str(BILIUP_CONFIG_PATH),
                    "--status-file",
                    str(STATUS_PATH),
                ],
                cwd=RECORDER_RUNTIME_DIR,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
                env=process_env,
            )
            atomic_write_text(PID_PATH, str(self._process.pid))
            time.sleep(0.25)
            if self._process.poll() is not None:
                exit_code = self._process.returncode
                self._process = None
                PID_PATH.unlink(missing_ok=True)
                raise RecorderConfigError(
                    f"录制 worker 启动失败（退出码 {exit_code}），请检查录制日志并重新构建 biliup"
                )
            self._ensure_orphan_recovery_thread()
            self._ensure_upload_retry_thread()
            self._ensure_recording_notification_thread()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            pid = self._pid()
            if pid is not None:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                if self._process is not None:
                    try:
                        self._process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(pid, signal.SIGKILL)
            self._process = None
            if self._log_handle is not None:
                self._log_handle.close()
                self._log_handle = None
            PID_PATH.unlink(missing_ok=True)
            STATUS_PATH.unlink(missing_ok=True)
            self._reconcile_recording_notifications(
                self._merge_room_runtime(self.list_rooms(), False)
            )
            return self.status()

    def tail_log(self, lines: int = 120) -> str:
        try:
            content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return "尚无录制日志。"
        tail = "\n".join(content[-max(1, min(lines, 500)):])
        # 直播源经常把签名、令牌放在查询参数中，后台排错日志不应直接暴露它们。
        return re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]+", r"\1?[已隐藏]", tail)

    def _pipeline_state_path(self) -> Path:
        try:
            # bridge.load_config resolves symlinks before resolving state_db.
            # Docker keeps bridge.config.json in /data, so use the same real
            # parent here or stale multipart sessions are cleared in the wrong
            # database (/data/bridge instead of /data/.bridge).
            config_path = BRIDGE_CONFIG_PATH.expanduser().resolve()
            config = json.loads(config_path.read_text(encoding="utf-8"))
            configured = Path(str(config.get("state_db") or ".bridge/state.sqlite3")).expanduser()
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            config_path = BRIDGE_CONFIG_PATH.expanduser().resolve()
            configured = Path(".bridge/state.sqlite3")
        return configured.resolve() if configured.is_absolute() else (config_path.parent / configured).resolve()

    def recover_interrupted_pipeline_jobs(self) -> int:
        """Turn container-interrupted bridge jobs into visible retryable failures."""
        state_path = self._pipeline_state_path()
        if not state_path.is_file():
            return 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        reason = "服务重启中断了当前处理，请点击重试继续"
        try:
            with sqlite3.connect(state_path, timeout=5) as db:
                interrupted_fingerprints = {
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT fingerprint FROM upload_stages WHERE status IN ('running', 'queued')"
                    ).fetchall()
                }
                inconsistent_fingerprints = {
                    row[0]
                    for row in db.execute(
                        """SELECT DISTINCT uploads.fingerprint
                           FROM uploads
                           JOIN upload_stages
                             ON upload_stages.fingerprint = uploads.fingerprint
                           WHERE uploads.status IN ('processing', 'video_uploaded')
                             AND upload_stages.status = 'failed'"""
                    ).fetchall()
                }
                fingerprints = sorted(interrupted_fingerprints | inconsistent_fingerprints)
                if not fingerprints:
                    return 0
                placeholders = ",".join("?" for _ in fingerprints)
                if interrupted_fingerprints:
                    interrupted = sorted(interrupted_fingerprints)
                    interrupted_placeholders = ",".join("?" for _ in interrupted)
                    db.execute(
                        f"""UPDATE upload_stages
                            SET status='failed', error=?, finished_at=?, updated_at=?
                            WHERE status IN ('running', 'queued')
                              AND fingerprint IN ({interrupted_placeholders})""",
                        (reason, now, now, *interrupted),
                    )
                db.execute(
                    f"""UPDATE uploads
                        SET status='failed', error=?, updated_at=?
                        WHERE status IN ('processing', 'video_uploaded')
                          AND fingerprint IN ({placeholders})""",
                    (reason, now, *fingerprints),
                )
            return len(fingerprints)
        except sqlite3.Error:
            return 0

    def _orphan_recording_candidates(
        self,
        minimum_age_seconds: float = 120,
    ) -> list[tuple[Path, str]]:
        """Find finalized videos that have never been claimed by the bridge."""
        state_path = self._pipeline_state_path()
        known_paths: set[Path] = set()
        if state_path.is_file():
            try:
                with sqlite3.connect(state_path, timeout=5) as db:
                    known_paths = {
                        Path(str(row[0])).expanduser().resolve()
                        for row in db.execute("SELECT video_path FROM uploads").fetchall()
                    }
                    try:
                        known_paths.update(
                            Path(str(row[0])).expanduser().resolve()
                            for row in db.execute(
                                "SELECT video_path FROM recording_exclusions"
                            ).fetchall()
                        )
                    except sqlite3.Error:
                        pass
            except sqlite3.Error:
                return []

        room_markers = [
            (
                str(room.get("id") or ""),
                _room_file_marker(room),
            )
            for room in self.list_rooms()
            if not room.get("record_only", False)
        ]
        video_suffixes = {
            suffix for suffix, kind in RECORDING_FILE_SUFFIXES.items() if kind == "video"
        }
        cutoff = time.time() - max(0, minimum_age_seconds)
        candidates: list[tuple[Path, str]] = []
        root = recordings_dir()
        if not root.is_dir():
            return candidates
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in video_suffixes:
                continue
            resolved = path.resolve()
            if resolved in known_paths:
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            room_id = next(
                (
                    room_id
                    for room_id, marker in room_markers
                    if room_id and marker and marker in path.name
                ),
                "",
            )
            if room_id:
                candidates.append((resolved, room_id))
        candidates.sort(key=lambda item: item[0].stat().st_mtime)
        return candidates

    def recover_orphan_recordings(self, minimum_age_seconds: float = 120) -> int:
        """Feed missed segments back into independent or multipart upload flows."""
        candidates = self._orphan_recording_candidates(minimum_age_seconds)
        if not candidates:
            return 0
        log_path = APP_ROOT / "logs" / "orphan-recording-recovery.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        recovered = 0
        with log_path.open("a", encoding="utf-8") as log_handle:
            for video, room_id in candidates:
                command = [
                    sys.executable,
                    str(WORKSPACE_ROOT / "bridge.py"),
                    "--config",
                    str(BRIDGE_CONFIG_PATH),
                    "ingest",
                ]
                if self.room_multipart_enabled(room_id):
                    command.extend(["--session-key", room_id])
                command.append(str(video))
                result = subprocess.run(
                    command,
                    cwd=WORKSPACE_ROOT,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                if result.returncode == 0:
                    recovered += 1
        return recovered

    def _ensure_orphan_recovery_thread(self) -> None:
        if self._orphan_recovery_thread is not None and self._orphan_recovery_thread.is_alive():
            return

        def worker() -> None:
            # Let the normal segment hook claim freshly finalized files first.
            time.sleep(30)
            while True:
                try:
                    self.recover_orphan_recordings()
                except Exception:
                    pass
                time.sleep(300)

        self._orphan_recovery_thread = threading.Thread(
            target=worker,
            name="potato-orphan-recording-recovery",
            daemon=True,
        )
        self._orphan_recovery_thread.start()

    @staticmethod
    def _state_datetime(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def retry_due_upload_jobs(self) -> int:
        """Restart Bilibili upload failures once their five-minute delay expires."""
        retried = 0
        now = datetime.now(timezone.utc)
        for job in self.pipeline_jobs(100):
            if not job.get("auto_retry_scheduled"):
                continue
            retry_at = self._state_datetime(job.get("auto_retry_at"))
            if retry_at is None or retry_at > now:
                continue
            try:
                if self.retry_pipeline_job(str(job["id"]), automatic=True):
                    retried += 1
            except (OSError, RecorderConfigError):
                # retry_pipeline_job records launch failures back into the task.
                continue
        return retried

    def _ensure_upload_retry_thread(self) -> None:
        if self._upload_retry_thread is not None and self._upload_retry_thread.is_alive():
            return

        def worker() -> None:
            # Allow the application and recorder state database to finish starting first.
            time.sleep(15)
            while True:
                try:
                    self.retry_due_upload_jobs()
                except Exception:
                    pass
                time.sleep(15)

        self._upload_retry_thread = threading.Thread(
            target=worker,
            name="potato-bilibili-upload-retry",
            daemon=True,
        )
        self._upload_retry_thread.start()

    def _recording_file_roots(self) -> dict[str, Path]:
        return {
            "recordings": recordings_dir().resolve(),
            "artifacts": (self._pipeline_state_path().parent / "artifacts").resolve(),
        }

    @staticmethod
    def _encode_file_id(source: str, relative_path: str) -> str:
        raw = json.dumps({"source": source, "path": relative_path}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def _resolve_recording_file(self, file_id: str) -> tuple[Path, str, str]:
        try:
            padded = file_id + "=" * (-len(file_id) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            source = str(payload["source"])
            relative_path = str(payload["path"])
        except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecorderConfigError("文件编号无效") from exc
        root = self._recording_file_roots().get(source)
        if root is None or not relative_path or Path(relative_path).is_absolute():
            raise RecorderConfigError("文件编号无效")
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RecorderConfigError("文件路径超出录播目录") from exc
        if not candidate.is_file() or _recording_file_type(candidate) is None:
            raise RecorderConfigError("文件不存在或不属于可管理的录播文件")
        return candidate, source, relative_path

    def _recording_locks(self) -> tuple[set[Path], list[str]]:
        processing_files: set[Path] = set()
        for job in self.pipeline_jobs(100):
            if job.get("status") in {"processing", "video_uploaded"}:
                candidate_paths = [job.get("video_path")]
                for stage in job.get("stages") or []:
                    details = stage.get("details") if isinstance(stage, dict) else None
                    if not isinstance(details, dict):
                        continue
                    candidate_paths.extend(
                        value for key, value in details.items()
                        if key.endswith("_path") or key in {"danmaku_xml", "ass_path"}
                    )
                for value in candidate_paths:
                    if isinstance(value, str) and value:
                        processing_files.add(Path(value).resolve())
        active_markers = [
            _room_file_marker(room)
            for room in self.rooms_with_status()
            if room.get("runtime", {}).get("recording")
        ]
        return processing_files, active_markers

    def _recording_file_info(
        self,
        path: Path,
        source: str,
        relative_path: str,
        processing_files: set[Path],
        active_markers: list[str],
    ) -> dict[str, Any]:
        stat = path.stat()
        room_markers = [
            (str(room.get("id") or ""), _room_file_marker(room))
            for room in self.list_rooms()
        ]
        room_id = next((room_id for room_id, marker in room_markers if marker and marker in path.name), None)
        recording_active = (
            source == "recordings"
            and time.time() - stat.st_mtime < 120
            and any(marker in path.name for marker in active_markers)
        )
        pipeline_active = path.resolve() in processing_files
        file_type = _recording_file_type(path)
        has_cover = (
            file_type == "video"
            and any(path.with_suffix(suffix).is_file() for suffix in (".jpg", ".jpeg", ".png", ".webp"))
        )
        return {
            "id": self._encode_file_id(source, relative_path),
            "name": path.name,
            "relative_path": relative_path,
            "source": source,
            "type": file_type,
            "has_cover": has_cover,
            "extension": (
                f"{path.with_suffix('').suffix.lower().lstrip('.')}.part"
                if path.suffix.lower() == ".part"
                else path.suffix.lower().lstrip(".")
            ),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            "modified_timestamp": stat.st_mtime,
            "room_id": room_id,
            "locked": recording_active or pipeline_active,
            "lock_reason": "正在录制" if recording_active else ("流水线处理中" if pipeline_active else ""),
        }

    def recording_files(self, limit: int = 500) -> dict[str, Any]:
        processing_files, active_markers = self._recording_locks()
        files: list[dict[str, Any]] = []
        for source, root in self._recording_file_roots().items():
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or _recording_file_type(path) is None:
                    continue
                relative_path = path.relative_to(root).as_posix()
                try:
                    files.append(self._recording_file_info(
                        path, source, relative_path, processing_files, active_markers
                    ))
                except OSError:
                    continue
        files.sort(key=lambda item: item["modified_timestamp"], reverse=True)
        total_files = len(files)
        total_size = sum(item["size_bytes"] for item in files)
        limited = files[:max(1, min(limit, 2000))]
        return {
            "files": limited,
            "total_files": total_files,
            "total_size_bytes": total_size,
            "truncated": len(limited) < total_files,
        }

    def recording_file(self, file_id: str) -> tuple[Path, dict[str, Any]]:
        path, source, relative_path = self._resolve_recording_file(file_id)
        processing_files, active_markers = self._recording_locks()
        return path, self._recording_file_info(
            path, source, relative_path, processing_files, active_markers
        )

    def recording_cover(self, file_id: str) -> Path:
        video, info = self.recording_file(file_id)
        if info["type"] != "video":
            raise RecorderConfigError("该文件不是录播视频")
        for suffix in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = video.with_suffix(suffix)
            if candidate.is_file():
                return candidate
        raise RecorderConfigError("该录播视频还没有同名封面")

    def delete_recording_file(self, file_id: str) -> dict[str, Any]:
        with self._lock:
            path, info = self.recording_file(file_id)
            if info["locked"]:
                raise RecorderConfigError(f"文件{info['lock_reason']}，暂时不能删除")
            try:
                path.unlink()
            except FileNotFoundError as exc:
                raise RecorderConfigError("文件已经不存在") from exc
            return info

    def delete_recording_files(self, file_ids: list[str]) -> dict[str, Any]:
        if not isinstance(file_ids, list) or not file_ids:
            raise RecorderConfigError("请选择要删除的文件")
        if len(file_ids) > 500:
            raise RecorderConfigError("单次最多删除 500 个文件")
        deleted: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_id in file_ids:
            file_id = str(raw_id or "").strip()
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            try:
                deleted.append(self.delete_recording_file(file_id))
            except RecorderConfigError as exc:
                failed.append({"id": file_id, "error": str(exc)})
        return {
            "deleted": deleted,
            "failed": failed,
            "deleted_count": len(deleted),
            "failed_count": len(failed),
        }

    @staticmethod
    def _decode_json(value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(value) if value else {}
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _ensure_pipeline_display_ids(
        db: sqlite3.Connection,
        room_markers: list[dict[str, str]],
    ) -> dict[str, str]:
        """Persist stable, reader-facing IDs without replacing internal fingerprints."""
        db.execute(
            """CREATE TABLE IF NOT EXISTS recording_display_ids (
                fingerprint TEXT PRIMARY KEY,
                display_id TEXT NOT NULL UNIQUE,
                assigned_at TEXT NOT NULL
            )"""
        )
        assigned = {
            str(row["fingerprint"]): str(row["display_id"])
            for row in db.execute(
                "SELECT fingerprint, display_id FROM recording_display_ids"
            ).fetchall()
        }
        used_ids = set(assigned.values())
        upload_rows = db.execute(
            """SELECT fingerprint, video_path, platform, created_at
               FROM uploads
               ORDER BY created_at, fingerprint"""
        ).fetchall()
        for row in upload_rows:
            fingerprint = str(row["fingerprint"])
            if fingerprint in assigned:
                continue
            video_name = Path(str(row["video_path"] or "")).name
            matched_room = next(
                (
                    item
                    for item in room_markers
                    if item["marker"] and item["marker"] in video_name
                ),
                None,
            )
            platform = (
                matched_room.get("platform")
                if matched_room
                else ("bilibili" if row["platform"] == "bilibili" else "")
            )
            room_name = matched_room.get("name") if matched_room else "LIVE"
            base = "-".join(
                (
                    _task_display_platform(platform),
                    _task_display_name(room_name),
                    _task_display_date(row["video_path"], row["created_at"]),
                )
            )
            sequence = 1
            display_id = f"{base}-{sequence:03d}"
            while display_id in used_ids:
                sequence += 1
                display_id = f"{base}-{sequence:03d}"
            db.execute(
                """INSERT OR IGNORE INTO recording_display_ids
                   (fingerprint, display_id, assigned_at)
                   VALUES (?, ?, ?)""",
                (
                    fingerprint,
                    display_id,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            persisted = db.execute(
                "SELECT display_id FROM recording_display_ids WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if persisted:
                assigned[fingerprint] = str(persisted["display_id"])
                used_ids.add(str(persisted["display_id"]))
        db.commit()
        return assigned

    def pipeline_jobs(self, limit: int = 30, room_id: str | None = None) -> list[dict[str, Any]]:
        state_path = self._pipeline_state_path()
        if not state_path.is_file():
            return []
        room_markers = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "直播间"),
                "avatar_url": str(item.get("avatar_url") or ""),
                "platform": str(item.get("platform") or ""),
                "marker": _room_file_marker(item),
            }
            for item in self.list_rooms()
        ]
        try:
            with sqlite3.connect(state_path, timeout=5) as db:
                db.row_factory = sqlite3.Row
                display_ids = self._ensure_pipeline_display_ids(db, room_markers)
                uploads = db.execute(
                    "SELECT * FROM uploads ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 100)),)
                ).fetchall()
                stage_rows = db.execute(
                    "SELECT * FROM upload_stages ORDER BY updated_at"
                ).fetchall()
                db.execute(
                    """CREATE TABLE IF NOT EXISTS recording_review_overrides (
                        fingerprint TEXT PRIMARY KEY,
                        metadata_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )"""
                )
                override_rows = db.execute(
                    "SELECT fingerprint, metadata_json, updated_at FROM recording_review_overrides"
                ).fetchall()
        except sqlite3.Error:
            return []
        overrides = {
            row["fingerprint"]: {
                **self._decode_json(row["metadata_json"]),
                "updated_at": row["updated_at"],
            }
            for row in override_rows
        }
        stages_by_job: dict[str, list[dict[str, Any]]] = {}
        for row in stage_rows:
            stages_by_job.setdefault(row["fingerprint"], []).append({
                "key": row["stage"], "status": row["status"],
                "details": self._decode_json(row["details_json"]), "error": row["error"],
                "started_at": row["started_at"], "finished_at": row["finished_at"],
                "updated_at": row["updated_at"],
            })
        queued_upload_ids = [
            row["fingerprint"]
            for row in sorted(
                (
                    row for row in stage_rows
                    if row["stage"] == "upload" and row["status"] == "queued"
                ),
                key=lambda row: str(row["updated_at"] or ""),
            )
        ]
        queued_upload_positions = {
            fingerprint: index
            for index, fingerprint in enumerate(queued_upload_ids, 1)
        }
        room_marker = None
        if room_id:
            room = next((item for item in self.list_rooms() if item.get("id") == room_id), None)
            if room:
                room_marker = _room_file_marker(room)
        jobs = []
        allowed_cover_root = self._recording_file_roots()["artifacts"].resolve()
        allowed_recordings_root = self._recording_file_roots()["recordings"].resolve()
        stage_orders = {
            "record_only": ("record", "ass", "cover", "remux", "verify", "cleanup"),
            "bilibili": (
                "detect", "record", "ass", "live_stats", "xml_identity", "ai",
                "cover", "cover_16x9", "cover_4x3", "upload", "cleanup",
            ),
        }
        from .bilibili_accounts import resolve_account
        from .config_manager import load_config

        account_config = load_config()
        for row in uploads:
            result = self._decode_json(row["result_json"])
            video_path = str(
                result.get("final_video_path")
                or result.get("video_path")
                or row["video_path"]
            )
            if room_marker and room_marker not in Path(video_path).name:
                continue
            matched_room = next(
                (item for item in room_markers if item["marker"] and item["marker"] in Path(video_path).name),
                None,
            )
            stages = stages_by_job.get(row["fingerprint"], [])
            order = stage_orders.get(str(row["platform"]), stage_orders["bilibili"])
            order_index = {key: index for index, key in enumerate(order)}
            stages.sort(key=lambda item: order_index.get(str(item.get("key")), len(order)))
            upload_stage = next((item for item in stages if item["key"] == "upload"), {})
            ai_stage = next((item for item in stages if item["key"] == "ai"), {})
            cover_stage = next(
                (item for item in stages if item["key"] == "cover_16x9"),
                next((item for item in stages if item["key"] == "cover"), {}),
            )
            cover43_stage = next(
                (item for item in stages if item["key"] == "cover_4x3"),
                {},
            )
            upload_details = upload_stage.get("details") if isinstance(upload_stage, dict) else {}
            ai_details = ai_stage.get("details") if isinstance(ai_stage, dict) else {}
            cover_details = cover_stage.get("details") if isinstance(cover_stage, dict) else {}
            upload_details = upload_details if isinstance(upload_details, dict) else {}
            ai_details = ai_details if isinstance(ai_details, dict) else {}
            cover_details = cover_details if isinstance(cover_details, dict) else {}
            cover43_details = (
                cover43_stage.get("details") if isinstance(cover43_stage, dict) else {}
            )
            cover43_details = cover43_details if isinstance(cover43_details, dict) else {}
            duration_seconds = 0
            duration_sources = [result]
            duration_sources.extend(
                stage.get("details")
                for stage in stages
                if isinstance(stage.get("details"), dict)
            )
            for duration_source in duration_sources:
                if not isinstance(duration_source, dict):
                    continue
                raw_duration = (
                    duration_source.get("video_duration_seconds")
                    or duration_source.get("duration_seconds")
                )
                try:
                    duration_seconds = max(0, int(round(float(raw_duration or 0))))
                except (TypeError, ValueError):
                    duration_seconds = 0
                if duration_seconds > 0:
                    break
            if duration_seconds >= 3600:
                hours, remainder = divmod(duration_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                duration_text = f"{hours}:{minutes:02d}:{seconds:02d}"
            elif duration_seconds > 0:
                minutes, seconds = divmod(duration_seconds, 60)
                duration_text = f"{minutes}:{seconds:02d}"
            else:
                duration_text = ""
            review = overrides.get(row["fingerprint"], {})
            cover_candidate = str(
                review.get("cover_path")
                or cover_details.get("ai_cover_path")
                or cover_details.get("cover_used_for_upload")
                or result.get("cover_path")
                or ""
            ).strip()
            cover_path = Path(cover_candidate).resolve() if cover_candidate else None
            local_cover_available = bool(
                cover_path
                and cover_path.is_file()
                and cover_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                and (
                    cover_path == allowed_cover_root
                    or allowed_cover_root in cover_path.parents
                    or cover_path == allowed_recordings_root
                    or allowed_recordings_root in cover_path.parents
                )
            )
            cover43_candidate = str(
                review.get("cover43_path")
                or cover43_details.get("ai_cover_4x3_path")
                or cover43_details.get("cover43_used_for_upload")
                or cover_details.get("ai_cover_4x3_path")
                or cover_details.get("cover43_used_for_upload")
                or result.get("cover43_path")
                or ""
            ).strip()
            cover43_path = Path(cover43_candidate).resolve() if cover43_candidate else None
            local_cover43_available = bool(
                cover43_path
                and cover43_path.is_file()
                and cover43_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                and (
                    cover43_path == allowed_cover_root
                    or allowed_cover_root in cover43_path.parents
                    or cover43_path == allowed_recordings_root
                    or allowed_recordings_root in cover43_path.parents
                )
            )
            bilibili_result = result.get("bilibili")
            if not isinstance(bilibili_result, dict):
                bilibili_result = upload_details.get("bilibili")
            bilibili_result = bilibili_result if isinstance(bilibili_result, dict) else {}
            bvid = str(bilibili_result.get("bvid") or "").strip()
            bilibili_cover_url = str(bilibili_result.get("cover_url") or "").strip()
            bilibili_cover43_url = str(bilibili_result.get("cover43_url") or "").strip()
            cover_route_available = local_cover_available or bool(bvid)
            cover_available = (
                cover_route_available
                or urlparse(bilibili_cover_url).scheme in {"http", "https"}
            )
            title = str(
                review.get("title")
                or upload_details.get("title")
                or ai_details.get("title")
                or Path(video_path).stem
            )
            description = str(
                review.get("description")
                or upload_details.get("description")
                or ai_details.get("description")
                or ""
            )
            tags = review.get("tags")
            if not isinstance(tags, list):
                tags = upload_details.get("tags") or ai_details.get("final_tags") or []
            partition_id = str(
                review.get("partition_id")
                or upload_details.get("partition_id")
                or ai_details.get("selected_partition_id")
                or ""
            )
            selected_account = resolve_account(
                account_config,
                result.get("bilibili_account_id")
                or upload_details.get("bilibili_account_id")
                or (matched_room or {}).get("bilibili_account_id"),
            )
            completed_stages = sum(
                1 for stage in stages
                if stage.get("status") in {"completed", "skipped", "warning"}
            )
            failed_stage = next((stage.get("key") for stage in stages if stage.get("status") == "failed"), None)
            job_status = str(row["status"] or "")
            if failed_stage and job_status in {"processing", "video_uploaded"}:
                # A restart can happen after the stage is persisted as failed but
                # before the parent upload row is updated. Never present that
                # inconsistent state as an active task.
                job_status = "failed"
            active_stage = next(
                (
                    stage.get("key")
                    for stage in stages
                    if stage.get("status") in {"running", "queued"}
                ),
                None,
            )
            upload_queued = upload_stage.get("status") == "queued"
            stage_label = RECORDING_STAGE_LABELS.get(
                str(failed_stage or active_stage or ""),
                str(failed_stage or active_stage or "处理任务"),
            )
            if failed_stage:
                progress_label = f"{stage_label}失败"
            elif upload_queued:
                progress_label = (
                    f"等待投稿队列（第 "
                    f"{queued_upload_positions.get(row['fingerprint'], 1)} 位）"
                )
            elif active_stage:
                progress_label = f"正在{stage_label}"
            elif job_status == "paused":
                progress_label = "投稿已暂停"
            elif job_status == "completed":
                progress_label = "全部处理完成"
            else:
                progress_label = "正在处理任务"
            attempts = int(row["attempts"] or 0)
            automatic_retries_used = max(0, attempts - 1)
            auto_retry_scheduled = bool(
                job_status == "failed"
                and row["platform"] == "bilibili"
                and failed_stage == "upload"
                and automatic_retries_used < AUTO_UPLOAD_RETRY_MAX_RETRIES
            )
            retry_base = self._state_datetime(row["updated_at"])
            auto_retry_at = (
                datetime.fromtimestamp(
                    retry_base.timestamp() + AUTO_UPLOAD_RETRY_DELAY_SECONDS,
                    tz=timezone.utc,
                ).isoformat(timespec="seconds")
                if auto_retry_scheduled and retry_base
                else None
            )
            auto_retry_remaining_seconds = (
                max(
                    0,
                    int(
                        (
                            self._state_datetime(auto_retry_at)
                            - datetime.now(timezone.utc)
                        ).total_seconds()
                    ),
                )
                if auto_retry_at
                else None
            )
            upload_progress = upload_details.get("upload_progress")
            upload_progress = upload_progress if isinstance(upload_progress, dict) else None
            upload_progress_text = ""
            if upload_progress:
                uploaded = float(upload_progress.get("uploaded_bytes") or 0)
                total_bytes = float(upload_progress.get("total_bytes") or 0)
                speed = float(
                    upload_progress.get("speed_bytes_per_second")
                    or upload_progress.get("speed_bytes_per_sec")
                    or 0
                )
                eta = upload_progress.get("eta_seconds")
                peak_speed = float(
                    upload_progress.get("peak_speed_bytes_per_second")
                    or upload_details.get("peak_speed_bytes_per_second")
                    or speed
                    or 0
                )
                if upload_stage.get("status") == "running" and total_bytes > 0 and speed > 0 and eta is not None:
                    upload_progress_text = (
                        f"已经上传：{uploaded / 1024 / 1024:.1f}MB/{total_bytes / 1024 / 1024:.1f}MB　"
                        f"当前速度：{speed / 1024 / 1024:.1f}MB/s　"
                        f"最高速度：{peak_speed / 1024 / 1024:.1f}MB/s　"
                        f"剩余时间：{float(eta):.1f}秒"
                    )
                elif peak_speed > 0:
                    upload_progress_text = f"最高上传速度：{peak_speed / 1024 / 1024:.1f}MB/s"
            capabilities = recording_task_capabilities(job_status)
            jobs.append({
                "id": row["fingerprint"],
                "display_id": display_ids.get(row["fingerprint"], row["fingerprint"][:12]),
                "short_id": row["fingerprint"][:12],
                "video_path": video_path, "video_name": Path(video_path).name,
                "title": title,
                "duration_seconds": duration_seconds,
                "duration_text": duration_text,
                "description": description,
                "tags": tags,
                "partition_id": partition_id,
                "review_override": review,
                "platform": row["platform"], "status": job_status,
                "record_only": row["platform"] == "record_only",
                "attempts": attempts, "result": result, "error": row["error"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "room_id": matched_room["id"] if matched_room else None,
                "room_name": matched_room["name"] if matched_room else "未匹配直播间",
                "room_avatar_url": matched_room["avatar_url"] if matched_room else "",
                "bilibili_account_id": selected_account["id"],
                "bilibili_account_name": selected_account["name"],
                "bilibili_account_uid": selected_account.get("bilibili_uid", ""),
                "bilibili_account_avatar_url": selected_account.get("avatar_url", ""),
                "source": "recording",
                "bvid": bvid,
                "bilibili_url": str(bilibili_result.get("url") or ""),
                "bilibili_cover_url": bilibili_cover_url,
                "bilibili_cover43_url": bilibili_cover43_url,
                "cover_available": cover_available,
                "local_cover_available": local_cover_available,
                "local_cover43_available": local_cover43_available,
                "cover43_available": (
                    local_cover43_available
                    or urlparse(bilibili_cover43_url).scheme in {"http", "https"}
                ),
                "cover_route_available": cover_route_available,
                "cover_updated_at": str(
                    review.get("updated_at")
                    or cover43_stage.get("updated_at")
                    or cover_stage.get("updated_at")
                    or row["updated_at"]
                    or ""
                ),
                "completed_stages": completed_stages,
                "total_stages": len(stages) or len(order),
                "progress_label": progress_label,
                "failed_stage": failed_stage,
                "active_stage": active_stage,
                "upload_queued": upload_queued,
                "upload_queue_position": queued_upload_positions.get(row["fingerprint"]),
                "upload_progress": upload_progress,
                "upload_progress_text": upload_progress_text,
                "auto_retry_scheduled": auto_retry_scheduled,
                "auto_retry_at": auto_retry_at,
                "auto_retry_remaining_seconds": auto_retry_remaining_seconds,
                "auto_retry_number": attempts if auto_retry_scheduled else None,
                "auto_retry_max_retries": AUTO_UPLOAD_RETRY_MAX_RETRIES,
                "auto_retry_exhausted": bool(
                    job_status == "failed"
                    and row["platform"] == "bilibili"
                    and failed_stage == "upload"
                    and automatic_retries_used >= AUTO_UPLOAD_RETRY_MAX_RETRIES
                ),
                **capabilities,
                "stages": stages,
            })
        return jobs

    def save_pipeline_review(
        self,
        fingerprint: str,
        *,
        title: str,
        description: str,
        tags: list[str],
        partition_id: str,
        cover_file: Any = None,
        cover43_file: Any = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RecorderConfigError("任务编号无效")
        job = self.pipeline_job(fingerprint)
        if not job:
            raise RecorderConfigError("没有找到该录播任务")
        clean_title = str(title or "").strip()
        clean_description = str(description or "").strip()
        clean_partition = str(partition_id or "").strip()
        clean_tags = []
        for tag in tags:
            value = str(tag or "").strip()[:20]
            if value and value not in clean_tags:
                clean_tags.append(value)
        clean_tags = clean_tags[:6]
        if not clean_title:
            raise RecorderConfigError("标题不能为空")
        if len(clean_title) > 80:
            raise RecorderConfigError("B站标题不能超过 80 个字符")
        if len(clean_description) > 2000:
            raise RecorderConfigError("B站简介不能超过 2000 个字符")
        if not clean_partition.isdigit():
            raise RecorderConfigError("请选择有效的 B站分区")

        previous = job.get("review_override")
        previous = previous if isinstance(previous, dict) else {}
        cover_path = str(previous.get("cover_path") or "").strip()
        cover43_path = str(previous.get("cover43_path") or "").strip()

        def save_cover(upload: Any, stem: str) -> str:
            if not upload or not str(getattr(upload, "filename", "") or "").strip():
                return ""
            suffix = Path(str(upload.filename)).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                raise RecorderConfigError("封面只支持 JPG、PNG 或 WEBP")
            artifact_dir = self._recording_file_roots()["artifacts"] / fingerprint[:16]
            artifact_dir.mkdir(parents=True, exist_ok=True)
            destination = artifact_dir / f"{stem}{suffix}"
            upload.save(destination)
            if not destination.is_file() or destination.stat().st_size <= 0:
                raise RecorderConfigError("封面保存失败")
            return str(destination)

        cover_path = save_cover(cover_file, "manual-review-cover-16x9") or cover_path
        cover43_path = save_cover(cover43_file, "manual-review-cover-4x3") or cover43_path

        now = datetime.now(timezone.utc).isoformat()
        metadata = {
            **previous,
            "title": clean_title,
            "description": clean_description,
            "tags": clean_tags,
            "partition_id": clean_partition,
            "cover_path": cover_path or None,
            "cover43_path": cover43_path or None,
            "pending_published_update": job.get("status") == "completed",
            "updated_at": now,
        }
        self._store_pipeline_review_override(fingerprint, metadata)
        return metadata

    def _store_pipeline_review_override(
        self,
        fingerprint: str,
        metadata: dict[str, Any],
    ) -> None:
        """Persist a recording review/preview without changing the Bilibili archive."""
        state_path = self._pipeline_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        now = str(metadata.get("updated_at") or datetime.now(timezone.utc).isoformat())
        with sqlite3.connect(state_path, timeout=30) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS recording_review_overrides (
                    fingerprint TEXT PRIMARY KEY,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """INSERT INTO recording_review_overrides
                   (fingerprint, metadata_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                     metadata_json=excluded.metadata_json,
                     updated_at=excluded.updated_at""",
                (fingerprint, json.dumps(metadata, ensure_ascii=False), now),
            )

    def regenerate_published_metadata(
        self,
        fingerprint: str,
        fields: set[str],
    ) -> dict[str, Any]:
        """Generate a preview for an already-uploaded recording archive."""
        allowed_fields = {"title", "description", "tags", "cover"}
        selected = {str(field).strip().lower() for field in fields} & allowed_fields
        if not selected:
            raise RecorderConfigError("请选择要重新生成的标题、简介或封面")
        job = self.pipeline_job(fingerprint)
        if not job:
            raise RecorderConfigError("没有找到该录播任务")
        if job.get("status") != "completed" or not job.get("bvid"):
            raise RecorderConfigError("只有已成功上传到 B站的录播任务可以重新生成稿件信息")

        try:
            if str(WORKSPACE_ROOT) not in sys.path:
                sys.path.insert(0, str(WORKSPACE_ROOT))
            import bridge
            from .ai_enhancer import (
                _request_json_object,
                generate_video_tags,
                get_openai_client,
            )
            from .config_manager import load_config

            app_config = load_config()
            if selected & {"title", "description", "tags"} and not app_config.get("OPENAI_API_KEY"):
                raise RecorderConfigError("未配置全局 AI API Key，无法重新生成标题、简介或标签")
            if (
                "cover" in selected
                and not (
                    app_config.get("OPENAI_IMAGE_API_KEY")
                    or app_config.get("OPENAI_API_KEY")
                )
            ):
                raise RecorderConfigError("未配置图片或全局 AI API Key，无法重新生成封面")
            bridge_config = bridge.load_config(BRIDGE_CONFIG_PATH)
            video_path = Path(str(job.get("video_path") or "recording.flv"))
            bridge_config = bridge.effective_config(bridge_config, video_path)
            values = bridge.recording_metadata_values(video_path, bridge_config)
            current_title = str(job.get("title") or "").strip()
            current_description = str(job.get("description") or "").strip()
            ai_stage = next(
                (stage for stage in job.get("stages", []) if stage.get("key") == "ai"),
                {},
            )
            ai_details = ai_stage.get("details") if isinstance(ai_stage, dict) else {}
            ai_details = ai_details if isinstance(ai_details, dict) else {}
            context = {
                "streamer": job.get("room_name") or values.get("streamer") or "主播",
                "recorded_at": values.get("date") or "",
                "current_title": current_title,
                "current_description": current_description,
                "previous_topic": ai_details.get("title_topic") or "",
                "part_title": ai_details.get("part_title") or "",
                "part_description": ai_details.get("part_description") or "",
                "live_title": values.get("live_title") or "",
            }
            title_prompt = str(
                bridge_config.get("ai_title_prompt")
                or bridge.DEFAULT_RECORDING_TITLE_AI_PROMPT
            ).strip()
            description_prompt = str(
                bridge_config.get("ai_description_prompt")
                or bridge.DEFAULT_RECORDING_DESCRIPTION_AI_PROMPT
            ).strip()
            system_prompt = f"""
你是哔哩哔哩直播录播编辑。根据给出的现有稿件信息重新拟定核心标题和简介。
只能使用输入中已经出现的事实、对局内容和观众反应，不得虚构主播说过的话、比赛结果或英雄。
title_topic 是自然、有信息量的中文核心主题，不含主播名、日期、时间和“直播回放”，最多18个中文字符。
description 是可直接用于B站投稿的完整中文简介，保留有价值的事件脉络和观众反应，不出现文件名、任务编号或内部路径，不超过1800字。
本直播间的标题要求：{title_prompt}
本直播间的简介要求：{description_prompt}
返回 JSON：{{"title_topic":"...","description":"..."}}。
""".strip()
            generated: dict[str, Any] = {}
            if selected & {"title", "description"}:
                generated_result = _request_json_object(
                    client=get_openai_client(app_config),
                    model_name=str(app_config.get("OPENAI_MODEL_NAME") or "gpt-4o-mini"),
                    system_prompt=system_prompt,
                    payload=context,
                    max_tokens=1100,
                    temperature=0.35,
                    thinking_enabled=bool(app_config.get("OPENAI_THINKING_ENABLED", False)),
                    logger_obj=None,
                    scene_name="recording_published_metadata_regenerate",
                )
                generated = generated_result if isinstance(generated_result, dict) else {}
            title_topic = re.sub(
                r"[\r\n｜|]+",
                " ",
                str(
                    generated.get("title_topic")
                    or ai_details.get("title_topic")
                    or ""
                ).strip(),
            )[:28].strip()
            generated_description = str(generated.get("description") or "").strip()[:1800]

            title = current_title
            description = current_description
            if "title" in selected:
                if not title_topic:
                    raise RecorderConfigError("AI 没有返回可用的标题主题")
                title, _, _ = bridge.render_metadata(
                    video_path,
                    bridge_config,
                    ai_topic=title_topic,
                )
            if "description" in selected:
                if not generated_description:
                    raise RecorderConfigError("AI 没有返回可用的简介")
                description = generated_description

            tags = list(job.get("tags") or [])
            if "tags" in selected:
                tags = [
                    str(tag).strip()
                    for tag in generate_video_tags(
                        title,
                        description,
                        openai_config=app_config,
                        task_id=None,
                    )
                    if str(tag).strip()
                ][:6]
                if not tags:
                    raise RecorderConfigError("AI 没有返回可用的视频标签")

            previous = job.get("review_override")
            previous = previous if isinstance(previous, dict) else {}
            cover_path = str(previous.get("cover_path") or "").strip()
            cover43_path = str(previous.get("cover43_path") or "").strip()
            cover_details: dict[str, Any] = {}
            if "cover" in selected:
                artifact_dir = self._recording_file_roots()["artifacts"] / fingerprint[:16]
                errors: list[str] = []
                variants = (
                    ("16x9", (1920, 1080), artifact_dir / "ai_cover_16x9.jpg"),
                    ("4x3", (1600, 1200), artifact_dir / "ai_cover_4x3.jpg"),
                )
                for variant, target_size, output_path in variants:
                    try:
                        generated_path, variant_details = (
                            bridge.generate_recording_cover_with_ai(
                                title=title,
                                ai_topic=title_topic or str(ai_details.get("title_topic") or ""),
                                description=description,
                                streamer=str(job.get("room_name") or values.get("streamer") or ""),
                                cfg=bridge_config,
                                work_dir=artifact_dir,
                                recording_dir=video_path.parent,
                                target_size=target_size,
                                output_path=output_path,
                            )
                        )
                        cover_details[variant] = variant_details
                        if generated_path:
                            if variant == "16x9":
                                cover_path = str(generated_path)
                            else:
                                cover43_path = str(generated_path)
                        else:
                            errors.append(f"{variant} 未生成")
                    except Exception as exc:
                        cover_details[variant] = {"error": str(exc)}
                        errors.append(f"{variant}: {exc}")
                if errors and not cover_path and not cover43_path:
                    raise RecorderConfigError("两种 AI 封面均生成失败：" + "；".join(errors))
                if errors:
                    cover_details["errors"] = errors

            now = datetime.now(timezone.utc).isoformat()
            metadata = {
                **previous,
                "title": title,
                "description": description,
                "tags": tags,
                "partition_id": str(job.get("partition_id") or ""),
                "cover_path": cover_path or None,
                "cover43_path": cover43_path or None,
                "ai_regenerated_fields": sorted(selected),
                "ai_regenerated_at": now,
                "ai_title_topic": title_topic or None,
                "ai_cover_details": cover_details or previous.get("ai_cover_details"),
                "pending_published_update": True,
                "updated_at": now,
            }
            self._store_pipeline_review_override(fingerprint, metadata)
            return metadata
        except RecorderConfigError:
            raise
        except Exception as exc:
            raise RecorderConfigError(f"AI 重新生成失败：{exc}") from exc

    def update_published_metadata(self, fingerprint: str) -> dict[str, Any]:
        """Apply the reviewed preview to Bilibili while preserving every page."""
        job = self.pipeline_job(fingerprint)
        if not job:
            raise RecorderConfigError("没有找到该录播任务")
        if job.get("status") != "completed" or not job.get("bvid"):
            raise RecorderConfigError("只有已成功上传的 B站稿件可以更新")
        review = job.get("review_override")
        review = review if isinstance(review, dict) else {}
        title = str(review.get("title") or job.get("title") or "").strip()
        description = str(review.get("description") or job.get("description") or "").strip()
        cover_path = str(review.get("cover_path") or "").strip()
        cover43_path = str(review.get("cover43_path") or "").strip()
        bilibili_result = job.get("result", {}).get("bilibili")
        if not isinstance(bilibili_result, dict):
            raise RecorderConfigError("任务中缺少原始 B站投稿结果，无法安全更新")

        try:
            if str(APP_ROOT) not in sys.path:
                sys.path.insert(0, str(APP_ROOT))
            from .bilibili_uploader import BilibiliUploader

            from .bilibili_accounts import resolve_account, resolve_cookie_path
            from .config_manager import load_config

            account = resolve_account(
                load_config(),
                (job.get("result") or {}).get("bilibili_account_id")
                or job.get("bilibili_account_id"),
            )
            cookie_file = str(resolve_cookie_path(account.get("cookies_path")))
            reviewed_tags = review.get("tags")
            tags = (
                [str(tag).strip() for tag in reviewed_tags if str(tag).strip()][:6]
                if isinstance(reviewed_tags, list)
                else list(job.get("tags") or [])[:6]
            )
            partition_id = str(
                review.get("partition_id") or job.get("partition_id") or ""
            ).strip()
            uploader = BilibiliUploader(cookie_file=cookie_file)
            ok, updated_result = uploader.update_uploaded_metadata(
                result=bilibili_result,
                title=title,
                description=description,
                tags=tags,
                partition_id=partition_id,
                cover_file_path=cover_path if cover_path and Path(cover_path).is_file() else "",
                cover43_file_path=(
                    cover43_path if cover43_path and Path(cover43_path).is_file() else ""
                ),
            )
            if not ok or not isinstance(updated_result, dict):
                raise RecorderConfigError(str(updated_result))

            whole_result = dict(job.get("result") or {})
            whole_result["bilibili"] = updated_result
            now = datetime.now(timezone.utc).isoformat()
            state_path = self._pipeline_state_path()
            with sqlite3.connect(state_path, timeout=30) as db:
                db.execute(
                    "UPDATE uploads SET result_json=?, updated_at=? WHERE fingerprint=?",
                    (json.dumps(whole_result, ensure_ascii=False), now, fingerprint),
                )
            metadata = {
                **review,
                "title": title,
                "description": description,
                "tags": tags,
                "partition_id": partition_id,
                "cover_path": cover_path or None,
                "cover43_path": cover43_path or None,
                "pending_published_update": False,
                "published_updated_at": now,
                "published_update_result": {
                    "bvid": updated_result.get("bvid"),
                    "aid": updated_result.get("aid"),
                    "part_count": updated_result.get("part_count"),
                },
                "updated_at": now,
            }
            self._store_pipeline_review_override(fingerprint, metadata)
            return metadata
        except RecorderConfigError:
            raise
        except Exception as exc:
            raise RecorderConfigError(f"更新 B站稿件失败：{exc}") from exc

    def pipeline_job(self, fingerprint: str) -> dict[str, Any] | None:
        return next((job for job in self.pipeline_jobs(100) if job["id"] == fingerprint), None)

    @staticmethod
    def _pipeline_process_cmdline(pid: int) -> str:
        try:
            return (
                Path(f"/proc/{pid}/cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", errors="replace")
            )
        except OSError:
            return ""

    def _pipeline_worker_pid(self, job: dict[str, Any]) -> int:
        """Resolve a bridge worker PID, including tasks created by older releases."""
        candidates: list[Any] = []
        result = job.get("result")
        if isinstance(result, dict):
            candidates.append(result.get("worker_pid"))
        for stage in job.get("stages") or []:
            details = stage.get("details") if isinstance(stage, dict) else None
            if isinstance(details, dict):
                candidates.append(details.get("worker_pid"))

        expected_path = str(job.get("video_path") or "")
        expected_name = Path(expected_path).name

        def command_for(pid: int) -> str:
            return self._pipeline_process_cmdline(pid)

        def matches(pid: int, *, require_video: bool) -> bool:
            cmdline = self._pipeline_process_cmdline(pid)
            return bool(
                "bridge.py" in cmdline
                and (
                    not require_video
                    or not expected_name
                    or expected_path in cmdline
                    or expected_name in cmdline
                )
            )

        for value in candidates:
            try:
                pid = int(value or 0)
            except (TypeError, ValueError):
                continue
            if pid > 1 and matches(pid, require_video=False):
                return pid

        proc_root = Path("/proc")
        try:
            process_dirs = list(proc_root.iterdir())
        except OSError:
            return 0
        bridge_pids: list[int] = []
        for process_dir in process_dirs:
            if not process_dir.name.isdigit():
                continue
            pid = int(process_dir.name)
            if pid <= 1 or "bridge.py" not in command_for(pid):
                continue
            bridge_pids.append(pid)
            if matches(pid, require_video=True):
                return pid
        # Older releases received the video path through stdin, so it is not
        # visible in /proc/<pid>/cmdline. A single live bridge process is still
        # unambiguous and safe to stop.
        return bridge_pids[0] if len(bridge_pids) == 1 else 0

    @staticmethod
    def _pipeline_descendant_pids(pid: int) -> list[int]:
        """Return Linux child processes so legacy non-group workers stop cleanly."""
        descendants: list[int] = []
        pending = [pid]
        seen = {pid}
        while pending:
            parent = pending.pop()
            children_path = Path(f"/proc/{parent}/task/{parent}/children")
            try:
                children = [
                    int(value)
                    for value in children_path.read_text(
                        encoding="utf-8",
                        errors="ignore",
                    ).split()
                    if value.isdigit()
                ]
            except OSError:
                children = []
            for child in children:
                if child <= 1 or child in seen:
                    continue
                seen.add(child)
                descendants.append(child)
                pending.append(child)
        return descendants

    def _terminate_pipeline_worker(self, job: dict[str, Any]) -> int:
        pid = self._pipeline_worker_pid(job)
        if pid <= 1:
            return 0
        try:
            process_group = os.getpgid(pid)
        except ProcessLookupError:
            return 0
        except PermissionError as exc:
            raise RecorderConfigError(f"没有权限读取任务进程：{exc}") from exc

        try:
            if process_group == pid:
                os.killpg(process_group, signal.SIGTERM)
            else:
                for child_pid in reversed(self._pipeline_descendant_pids(pid)):
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RecorderConfigError(f"没有权限暂停任务进程：{exc}") from exc

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not self._pipeline_process_cmdline(pid):
                return pid
            time.sleep(0.05)
        try:
            if process_group == pid:
                os.killpg(process_group, signal.SIGKILL)
            else:
                for child_pid in reversed(self._pipeline_descendant_pids(pid)):
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RecorderConfigError(f"没有权限强制停止任务进程：{exc}") from exc
        return pid

    def pause_pipeline_job(self, fingerprint: str) -> bool:
        """Stop any active bridge stage and preserve every source artifact."""
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RecorderConfigError("任务编号无效")
        job = self.pipeline_job(fingerprint)
        if not job:
            raise RecorderConfigError("没有找到该录播任务")
        if not recording_task_capabilities(job.get("status")).get("pausable"):
            raise RecorderConfigError("只有正在处理的任务可以暂停")

        self._terminate_pipeline_worker(job)

        state_path = self._pipeline_state_path()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, sqlite3.connect(state_path, timeout=30) as db:
            current = db.execute(
                "SELECT status FROM uploads WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if not current:
                raise RecorderConfigError("没有找到该录播任务")
            if current[0] == "completed":
                raise RecorderConfigError("任务已经完成，无法暂停")
            db.execute(
                """UPDATE upload_stages
                   SET status='paused', error=NULL, finished_at=?, updated_at=?
                   WHERE fingerprint=? AND status IN ('queued', 'running')""",
                (now, now, fingerprint),
            )
            db.execute(
                """UPDATE uploads
                   SET status='paused', error=NULL, updated_at=?
                   WHERE fingerprint=? AND status IN ('processing', 'video_uploaded')""",
                (now, fingerprint),
            )
        return True

    def delete_pipeline_job(self, fingerprint: str, delete_files: bool = False) -> dict[str, Any]:
        """Stop and delete one recording pipeline job and, optionally, its files."""
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RecorderConfigError("任务编号无效")
        state_path = self._pipeline_state_path()
        if not state_path.is_file():
            raise RecorderConfigError("没有找到该录播任务")

        with sqlite3.connect(state_path, timeout=30) as db:
            status_row = db.execute(
                "SELECT status FROM uploads WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
        if not status_row:
            raise RecorderConfigError("没有找到该录播任务")
        if status_row[0] in {"processing", "video_uploaded"}:
            job = self.pipeline_job(fingerprint)
            if not job:
                raise RecorderConfigError("任务进程尚未停止，无法安全删除")
            if job.get("status") in {"processing", "video_uploaded"}:
                self.pause_pipeline_job(fingerprint)

        with self._lock, sqlite3.connect(state_path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM uploads WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if not row:
                raise RecorderConfigError("没有找到该录播任务")
            if row["status"] in {"processing", "video_uploaded"}:
                failed_stage = db.execute(
                    """SELECT 1 FROM upload_stages
                       WHERE fingerprint=? AND status='failed' LIMIT 1""",
                    (fingerprint,),
                ).fetchone()
                if not failed_stage:
                    raise RecorderConfigError("任务进程尚未停止，请稍后重试删除")

            stage_rows = db.execute(
                "SELECT details_json FROM upload_stages WHERE fingerprint=?",
                (fingerprint,),
            ).fetchall()
            tables = {
                item[0]
                for item in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            review = {}
            if "recording_review_overrides" in tables:
                review_row = db.execute(
                    "SELECT metadata_json FROM recording_review_overrides WHERE fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                review = self._decode_json(review_row["metadata_json"]) if review_row else {}
                db.execute(
                    "DELETE FROM recording_review_overrides WHERE fingerprint=?",
                    (fingerprint,),
                )
            db.execute("DELETE FROM upload_stages WHERE fingerprint=?", (fingerprint,))
            db.execute("DELETE FROM uploads WHERE fingerprint=?", (fingerprint,))

        deleted_files: list[str] = []
        if delete_files:
            candidates: set[Path] = {Path(str(row["video_path"]))}
            video_path = Path(str(row["video_path"]))
            candidates.update(video_path.with_suffix(suffix) for suffix in (".xml", ".ass"))
            for stage_row in stage_rows:
                details = self._decode_json(stage_row["details_json"])
                for key, value in details.items():
                    if isinstance(value, str) and (
                        key.endswith("_path") or key in {"danmaku_xml", "ass_path", "cover"}
                    ):
                        candidates.add(Path(value))
            manual_cover = str(review.get("cover_path") or "").strip()
            if manual_cover:
                candidates.add(Path(manual_cover))
            manual_cover43 = str(review.get("cover43_path") or "").strip()
            if manual_cover43:
                candidates.add(Path(manual_cover43))

            roots = tuple(self._recording_file_roots().values())
            for candidate in candidates:
                resolved = candidate.expanduser().resolve()
                if not any(
                    resolved == root or root in resolved.parents
                    for root in roots
                ):
                    continue
                try:
                    if resolved.is_file():
                        resolved.unlink()
                        deleted_files.append(str(resolved))
                except OSError:
                    continue

            artifact_dir = self._recording_file_roots()["artifacts"] / fingerprint[:16]
            if artifact_dir.is_dir():
                shutil.rmtree(artifact_dir, ignore_errors=True)

        log_path = APP_ROOT / "logs" / f"pipeline-{fingerprint[:12]}.log"
        try:
            log_path.unlink()
        except FileNotFoundError:
            pass
        return {
            "fingerprint": fingerprint,
            "deleted_files": deleted_files,
            "deleted_file_count": len(deleted_files),
        }

    def pipeline_cover(self, fingerprint: str, variant: str = "16x9") -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RecorderConfigError("任务编号无效")
        if variant not in {"16x9", "4x3"}:
            raise RecorderConfigError("封面类型无效")
        job = self.pipeline_job(fingerprint)
        if not job:
            raise RecorderConfigError("没有找到该录播任务")
        review = job.get("review_override")
        review = review if isinstance(review, dict) else {}
        stage_key = "cover_4x3" if variant == "4x3" else "cover_16x9"
        cover_stage = next(
            (stage for stage in job.get("stages", []) if stage.get("key") == stage_key),
            next(
                (stage for stage in job.get("stages", []) if stage.get("key") == "cover"),
                {},
            ),
        )
        details = cover_stage.get("details") if isinstance(cover_stage, dict) else {}
        details = details if isinstance(details, dict) else {}

        cache_dir = self._recording_file_roots()["artifacts"] / "task-covers"

        def image_suffix(data: bytes) -> str:
            if data.startswith(b"\xff\xd8\xff"):
                return ".jpg"
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                return ".png"
            if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                return ".webp"
            return ""

        if variant == "4x3":
            candidate = str(
                review.get("cover43_path")
                or details.get("ai_cover_4x3_path")
                or details.get("cover43_used_for_upload")
                or ""
            ).strip()
        else:
            candidate = str(
                review.get("cover_path")
                or details.get("ai_cover_16x9_path")
                or details.get("ai_cover_path")
                or details.get("cover_used_for_upload")
                or ""
            ).strip()
        allowed_roots = tuple(self._recording_file_roots().values())
        if candidate:
            path = Path(candidate).resolve()
            if not any(path == root or root in path.parents for root in allowed_roots):
                raise RecorderConfigError("封面路径不在允许的录播产物目录中")
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                return path

        bvid = str(job.get("bvid") or "").strip()
        if variant == "4x3":
            cover43_url = str(job.get("bilibili_cover43_url") or "").strip()
            if urlparse(cover43_url).scheme not in {"http", "https"}:
                raise RecorderConfigError("该任务暂无可预览的 4:3 首页推荐封面")
            image_data, _ = _open_url(
                cover43_url,
                referer=f"https://www.bilibili.com/video/{bvid}",
                timeout=10,
            )
            detected_suffix = image_suffix(image_data[:16])
            if len(image_data) < 64 or not detected_suffix:
                raise RecorderConfigError("B 站返回的 4:3 封面内容无效")
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = (cache_dir / f"{fingerprint}-4x3{detected_suffix}").resolve()
            cache_path.write_bytes(image_data)
            return cache_path
        if not bvid:
            raise RecorderConfigError("该任务暂无可预览的封面")

        for suffix in (".jpg", ".jpeg", ".png", ".webp"):
            cached = (cache_dir / f"{fingerprint}{suffix}").resolve()
            if not cached.is_file() or cached.stat().st_size <= 0:
                continue
            detected_suffix = image_suffix(cached.read_bytes()[:16])
            if not detected_suffix:
                continue
            if detected_suffix != suffix and not (
                suffix == ".jpeg" and detected_suffix == ".jpg"
            ):
                corrected = cached.with_suffix(detected_suffix)
                cached.replace(corrected)
                return corrected
            return cached

        referer = f"https://www.bilibili.com/video/{bvid}"
        cover_url = str(job.get("bilibili_cover_url") or "").strip()
        if urlparse(cover_url).scheme not in {"http", "https"}:
            payload = _response_json(
                "https://api.bilibili.com/x/web-interface/view?"
                + urlencode({"bvid": bvid}),
                referer=referer,
                timeout=10,
            )
            data = payload.get("data")
            data = data if isinstance(data, dict) else {}
            cover_url = str(data.get("pic") or "").strip()
        if urlparse(cover_url).scheme not in {"http", "https"}:
            raise RecorderConfigError("B 站未返回该任务的封面")

        image_data, _ = _open_url(cover_url, referer=referer, timeout=10)
        detected_suffix = image_suffix(image_data[:16])
        if len(image_data) < 64 or not detected_suffix:
            raise RecorderConfigError("B 站返回的封面内容无效")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = (cache_dir / f"{fingerprint}{detected_suffix}").resolve()
        temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temp_path.write_bytes(image_data)
        temp_path.replace(cache_path)
        return cache_path

    def retry_pipeline_job(self, fingerprint: str, *, automatic: bool = False) -> bool:
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RecorderConfigError("任务编号无效")
        job = self.pipeline_job(fingerprint)
        if not job:
            raise RecorderConfigError("没有找到该录播任务")
        if not recording_task_capabilities(job.get("status")).get("retryable"):
            raise RecorderConfigError("只有失败、试运行或已暂停任务可以重试")
        video = Path(job["video_path"])
        if not video.is_file():
            raise RecorderConfigError("原始录播文件已不存在，无法重试")
        log_path = APP_ROOT / "logs" / f"pipeline-{fingerprint[:12]}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(WORKSPACE_ROOT / "bridge.py"),
            "--config",
            str(BRIDGE_CONFIG_PATH),
        ]
        if job.get("record_only"):
            room_id = str(job.get("result", {}).get("room_id") or job.get("room_id") or "")
            if not room_id:
                raise RecorderConfigError("仅录制任务缺少直播间编号，无法重试")
            command.extend(["record-only", "--room-id", room_id, str(video)])
        else:
            command.extend(["ingest", "--retry", str(video)])

        claimed = False
        state_path = self._pipeline_state_path()
        if automatic:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                with sqlite3.connect(state_path, timeout=5) as db:
                    cursor = db.execute(
                        """UPDATE uploads SET status='processing', updated_at=?
                           WHERE fingerprint=? AND status='failed'""",
                        (now, fingerprint),
                    )
                    claimed = cursor.rowcount == 1
            except sqlite3.Error as exc:
                raise RecorderConfigError(f"无法锁定自动重试任务：{exc}") from exc
            if not claimed:
                return False

        try:
            with log_path.open("a", encoding="utf-8") as log_handle:
                if automatic:
                    log_handle.write(
                        f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                        f"投稿失败已等待 5 分钟，开始第 {job['attempts']}/{AUTO_UPLOAD_RETRY_MAX_RETRIES} 次自动重试。\n"
                    )
                    log_handle.flush()
                subprocess.Popen(
                    command,
                    cwd=WORKSPACE_ROOT, stdout=log_handle, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
        except OSError as exc:
            if claimed:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                with sqlite3.connect(state_path, timeout=5) as db:
                    db.execute(
                        """UPDATE uploads SET status='failed', error=?, updated_at=?
                           WHERE fingerprint=? AND status='processing'""",
                        (f"自动重试启动失败：{exc}", now, fingerprint),
                    )
            raise
        return True

    def pipeline_log(self, fingerprint: str, lines: int = 200) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            return "任务编号无效。"
        path = APP_ROOT / "logs" / f"pipeline-{fingerprint[:12]}.log"
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return "该任务暂无独立重试日志；阶段错误与产物信息可在详情中查看。"
        return "\n".join(content[-max(1, min(lines, 500)):])


live_recorder_manager = LiveRecorderManager()
