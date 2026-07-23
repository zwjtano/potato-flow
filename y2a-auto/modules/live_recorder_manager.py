#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Manage the bundled biliup recorder from the unified Y2A web application."""

from __future__ import annotations

import base64
import json
import os
import re
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
from urllib.parse import urlparse
from urllib.request import Request, urlopen


APP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = APP_ROOT.parent
CONFIG_DIR = APP_ROOT / "config"
RECORDINGS_DIR = APP_ROOT / "recordings"
ROOMS_PATH = CONFIG_DIR / "live_recorders.json"
BILIUP_CONFIG_PATH = CONFIG_DIR / "biliup.generated.yaml"
BRIDGE_CONFIG_PATH = WORKSPACE_ROOT / "bridge.config.json"
BRIDGE_CONFIG_EXAMPLE = WORKSPACE_ROOT / "bridge.config.example.json"
LOG_PATH = APP_ROOT / "logs" / "biliup-recorder.log"
PID_PATH = APP_ROOT / "temp" / "biliup-recorder.pid"
STATUS_PATH = APP_ROOT / "temp" / "biliup-recorder-status.json"
CONTROL_PATH = APP_ROOT / "temp" / "biliup-recorder-control.json"
RELOAD_PATH = APP_ROOT / "temp" / "biliup-recorder-reload.json"
FFMPEG_DIR = APP_ROOT / "ffmpeg" / "darwin_arm64"
RECORDING_FILE_SUFFIXES = {
    ".mp4": "video", ".flv": "video", ".mkv": "video", ".webm": "video",
    ".ts": "video", ".m2ts": "video", ".mov": "video",
    ".xml": "xml", ".ass": "ass",
}
DEFAULT_RECORDING_TITLE_TEMPLATE = "【直播回放】{streamer}｜{ai_topic}｜{date}"
DEFAULT_RECORDING_SEGMENT_TIME = "01:00:00"


class RecorderConfigError(ValueError):
    pass


def _atomic_json(path: Path, value: Any) -> None:
    destination = path.resolve() if path.is_symlink() else path
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(destination)


def _yaml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", value.strip())
    return cleaned.strip("_") or "直播间"


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
    raise RecorderConfigError("只支持哔哩哔哩直播间和斗鱼直播间 URL")


def _open_url(url: str, *, referer: str = "", timeout: int = 12) -> tuple[bytes, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
    }
    if referer:
        headers["Referer"] = referer
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            return response.read(), response.geturl()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RecorderConfigError(f"读取直播间信息失败：{exc}") from exc


def _response_json(url: str, *, referer: str = "", timeout: int = 12) -> dict[str, Any]:
    body, _ = _open_url(url, referer=referer, timeout=timeout)
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


class LiveRecorderManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._reload_thread: threading.Thread | None = None
        self._orphan_recovery_thread: threading.Thread | None = None
        if os.environ.pop("POTATO_FLOW_CONTAINER_START", "") == "1":
            self.recover_interrupted_pipeline_jobs()

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
        return data if isinstance(data, list) else []

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
            }
            enriched.append(room)
        return enriched

    def rooms_with_status(self) -> list[dict[str, Any]]:
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

    def resolve_room(self, url: str) -> dict[str, Any]:
        """Resolve a supported room URL into canonical streamer metadata."""
        url = url.strip()
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

    def add_room_from_url(self, url: str) -> dict[str, Any]:
        resolved = self.resolve_room(url)
        with self._lock:
            rooms = self.list_rooms()
            existing = next(
                (
                    room for room in rooms
                    if room.get("platform") == resolved["platform"]
                    and (
                        str(room.get("platform_room_id") or "") == resolved["room_id"]
                        or str(room.get("url") or "").rstrip("/") == resolved["url"]
                    )
                ),
                None,
            )
            if existing is None:
                existing = {"id": uuid.uuid4().hex, "enabled": True}
                rooms.append(existing)
            existing.update({
                "name": resolved["name"],
                "url": resolved["url"],
                "platform": resolved["platform"],
                "platform_room_id": resolved["room_id"],
                "avatar_url": resolved["avatar_url"],
            })
            _atomic_json(ROOMS_PATH, rooms)
            self.sync_configs(rooms)
            self._write_control_state(rooms)
            return dict(existing)

    def add_room_from_url_and_reload(self, url: str) -> tuple[dict[str, Any], str]:
        """Resolve, save and reload a room without interrupting active recordings."""
        with self._lock:
            was_running = self._pid() is not None
            room = self.add_room_from_url(url)
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
            time.sleep(5)
            with self._lock:
                if self._pid() is None:
                    RELOAD_PATH.unlink(missing_ok=True)
                    return
                if any(item.get("runtime", {}).get("recording") for item in self.rooms_with_status()):
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
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        (RECORDINGS_DIR / "data").mkdir(parents=True, exist_ok=True)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# 由统一管理后台自动生成，请勿手动编辑。",
            "downloader: ffmpeg",
            # 固定按时长切分，同一场直播的各段会依次投稿为 P1、P2……。
            "file_size: null",
            f'segment_time: "{DEFAULT_RECORDING_SEGMENT_TIME}"',
            # 手动录制允许随时停止；不能让 biliup 把短录播当作碎片删除，
            # 否则视频不会进入 segment_processor / ASS 流程。
            "filtering_threshold: 0",
            'filename_prefix: "{streamer}%Y-%m-%d_%H-%M-%S_{title}"',
            "uploader: Noop",
            "delay: 30",
            "event_loop_interval: 30",
            "checker_sleep: 10",
            f"pool1_size: {max(3, len(rooms) + 1)}",
            "pool2_size: 1",
            "bilibili_danmaku: true",
            "douyu_danmaku: true",
        ]
        if not rooms:
            lines.append("streamers: {}")
        else:
            lines.append("streamers:")
        for room in rooms:
            key = f"{_slug(str(room['name']))}_{str(room['id'])[:6]}"
            session_key = str(room["id"])
            bridge_base = [
                _yaml_string(str(APP_ROOT / ".venv" / "bin" / "python")),
                _yaml_string(str(WORKSPACE_ROOT / "bridge.py")),
                "--config",
                _yaml_string(str(BRIDGE_CONFIG_PATH)),
            ]
            segment_command = " ".join([
                *bridge_base,
                "ingest",
                "--session-key",
                _yaml_string(session_key),
            ])
            finalize_command = " ".join([
                *bridge_base,
                "finalize-session",
                "--session-key",
                _yaml_string(session_key),
            ])
            lines.extend(
                [
                    f"  {_yaml_string(key)}:",
                    "    url:",
                    f"      - {_yaml_string(str(room['url']))}",
                    "    uploader: Noop",
                    "    segment_processor:",
                    f"      - run: {_yaml_string(segment_command)}",
                    "    postprocessor:",
                    f"      - run: {_yaml_string(finalize_command)}",
                ]
            )
        BILIUP_CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._sync_bridge_profiles(rooms)

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
        if str(config.get("title_template") or "").strip() in {"", "{stem}"}:
            config["title_template"] = DEFAULT_RECORDING_TITLE_TEMPLATE
        if (FFMPEG_DIR / "ffmpeg").is_file():
            config["ffmpeg"] = str(FFMPEG_DIR / "ffmpeg")
        if (FFMPEG_DIR / "ffprobe").is_file():
            config["ffprobe"] = str(FFMPEG_DIR / "ffprobe")
        config["profiles"] = [
            {
                "match": f"*{_slug(str(room['name']))}_{str(room['id'])[:6]}*",
                "source_url": room["url"],
                "streamer_name": str(room["name"]),
                "tags": [str(room["name"]), "直播录播"],
            }
            for room in rooms
        ]
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
            "recordings_path": str(RECORDINGS_DIR),
        }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._pid() is not None:
                self._ensure_orphan_recovery_thread()
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
                cwd=RECORDINGS_DIR,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
                env=process_env,
            )
            PID_PATH.write_text(str(self._process.pid), encoding="utf-8")
            time.sleep(0.25)
            if self._process.poll() is not None:
                exit_code = self._process.returncode
                self._process = None
                PID_PATH.unlink(missing_ok=True)
                raise RecorderConfigError(
                    f"录制 worker 启动失败（退出码 {exit_code}），请检查录制日志并重新构建 biliup"
                )
            self._ensure_orphan_recovery_thread()
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
                fingerprints = [
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT fingerprint FROM upload_stages WHERE status='running'"
                    ).fetchall()
                ]
                if not fingerprints:
                    return 0
                placeholders = ",".join("?" for _ in fingerprints)
                db.execute(
                    f"""UPDATE upload_stages
                        SET status='failed', error=?, finished_at=?, updated_at=?
                        WHERE status='running' AND fingerprint IN ({placeholders})""",
                    (reason, now, now, *fingerprints),
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
            except sqlite3.Error:
                return []

        room_markers = [
            (
                str(room.get("id") or ""),
                f"{_slug(str(room.get('name') or ''))}_{str(room.get('id') or '')[:6]}",
            )
            for room in self.list_rooms()
        ]
        video_suffixes = {
            suffix for suffix, kind in RECORDING_FILE_SUFFIXES.items() if kind == "video"
        }
        cutoff = time.time() - max(0, minimum_age_seconds)
        candidates: list[tuple[Path, str]] = []
        if not RECORDINGS_DIR.is_dir():
            return candidates
        for path in RECORDINGS_DIR.rglob("*"):
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
        """Sequentially feed missed segments back into their room's multipart session."""
        candidates = self._orphan_recording_candidates(minimum_age_seconds)
        if not candidates:
            return 0
        log_path = APP_ROOT / "logs" / "orphan-recording-recovery.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        recovered = 0
        with log_path.open("a", encoding="utf-8") as log_handle:
            for video, room_id in candidates:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(WORKSPACE_ROOT / "bridge.py"),
                        "--config",
                        str(BRIDGE_CONFIG_PATH),
                        "ingest",
                        "--session-key",
                        room_id,
                        str(video),
                    ],
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

    def _recording_file_roots(self) -> dict[str, Path]:
        return {
            "recordings": RECORDINGS_DIR.resolve(),
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
        if not candidate.is_file() or candidate.suffix.lower() not in RECORDING_FILE_SUFFIXES:
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
            f"{_slug(str(room.get('name') or ''))}_{str(room.get('id') or '')[:6]}"
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
            (str(room.get("id") or ""), f"{_slug(str(room.get('name') or ''))}_{str(room.get('id') or '')[:6]}")
            for room in self.list_rooms()
        ]
        room_id = next((room_id for room_id, marker in room_markers if marker and marker in path.name), None)
        recording_active = (
            source == "recordings"
            and time.time() - stat.st_mtime < 120
            and any(marker in path.name for marker in active_markers)
        )
        pipeline_active = path.resolve() in processing_files
        return {
            "id": self._encode_file_id(source, relative_path),
            "name": path.name,
            "relative_path": relative_path,
            "source": source,
            "type": RECORDING_FILE_SUFFIXES[path.suffix.lower()],
            "extension": path.suffix.lower().lstrip("."),
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
                if not path.is_file() or path.suffix.lower() not in RECORDING_FILE_SUFFIXES:
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

    def pipeline_jobs(self, limit: int = 30, room_id: str | None = None) -> list[dict[str, Any]]:
        state_path = self._pipeline_state_path()
        if not state_path.is_file():
            return []
        try:
            with sqlite3.connect(state_path, timeout=5) as db:
                db.row_factory = sqlite3.Row
                uploads = db.execute(
                    "SELECT * FROM uploads ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 100)),)
                ).fetchall()
                stage_rows = db.execute(
                    "SELECT * FROM upload_stages ORDER BY updated_at"
                ).fetchall()
        except sqlite3.Error:
            return []
        stages_by_job: dict[str, list[dict[str, Any]]] = {}
        for row in stage_rows:
            stages_by_job.setdefault(row["fingerprint"], []).append({
                "key": row["stage"], "status": row["status"],
                "details": self._decode_json(row["details_json"]), "error": row["error"],
                "started_at": row["started_at"], "finished_at": row["finished_at"],
                "updated_at": row["updated_at"],
            })
        room_marker = None
        if room_id:
            room = next((item for item in self.list_rooms() if item.get("id") == room_id), None)
            if room:
                room_marker = f"{_slug(str(room.get('name') or ''))}_{room_id[:6]}"
        jobs = []
        room_markers = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "直播间"),
                "avatar_url": str(item.get("avatar_url") or ""),
                "marker": f"{_slug(str(item.get('name') or ''))}_{str(item.get('id') or '')[:6]}",
            }
            for item in self.list_rooms()
        ]
        for row in uploads:
            video_path = str(row["video_path"])
            if room_marker and room_marker not in Path(video_path).name:
                continue
            result = self._decode_json(row["result_json"])
            matched_room = next(
                (item for item in room_markers if item["marker"] and item["marker"] in Path(video_path).name),
                None,
            )
            stages = stages_by_job.get(row["fingerprint"], [])
            upload_stage = next((item for item in stages if item["key"] == "upload"), {})
            ai_stage = next((item for item in stages if item["key"] == "ai"), {})
            upload_details = upload_stage.get("details") if isinstance(upload_stage, dict) else {}
            ai_details = ai_stage.get("details") if isinstance(ai_stage, dict) else {}
            upload_details = upload_details if isinstance(upload_details, dict) else {}
            ai_details = ai_details if isinstance(ai_details, dict) else {}
            bilibili_result = result.get("bilibili")
            if not isinstance(bilibili_result, dict):
                bilibili_result = upload_details.get("bilibili")
            bilibili_result = bilibili_result if isinstance(bilibili_result, dict) else {}
            title = str(upload_details.get("title") or ai_details.get("title") or Path(video_path).stem)
            completed_stages = sum(
                1 for stage in stages if stage.get("status") in {"completed", "skipped"}
            )
            failed_stage = next((stage.get("key") for stage in stages if stage.get("status") == "failed"), None)
            active_stage = next((stage.get("key") for stage in stages if stage.get("status") == "running"), None)
            jobs.append({
                "id": row["fingerprint"], "short_id": row["fingerprint"][:12],
                "video_path": video_path, "video_name": Path(video_path).name,
                "title": title,
                "platform": row["platform"], "status": row["status"],
                "attempts": row["attempts"], "result": result, "error": row["error"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "room_id": matched_room["id"] if matched_room else None,
                "room_name": matched_room["name"] if matched_room else "未匹配直播间",
                "room_avatar_url": matched_room["avatar_url"] if matched_room else "",
                "source": "recording",
                "bvid": str(bilibili_result.get("bvid") or ""),
                "bilibili_url": str(bilibili_result.get("url") or ""),
                "completed_stages": completed_stages,
                "total_stages": 6,
                "failed_stage": failed_stage,
                "active_stage": active_stage,
                "retryable": row["status"] in {"failed", "dry_run"},
                "stages": stages,
            })
        return jobs

    def pipeline_job(self, fingerprint: str) -> dict[str, Any] | None:
        return next((job for job in self.pipeline_jobs(100) if job["id"] == fingerprint), None)

    def pipeline_cover(self, fingerprint: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RecorderConfigError("任务编号无效")
        job = self.pipeline_job(fingerprint)
        if not job:
            raise RecorderConfigError("没有找到该录播任务")
        cover_stage = next(
            (stage for stage in job.get("stages", []) if stage.get("key") == "cover"),
            {},
        )
        details = cover_stage.get("details") if isinstance(cover_stage, dict) else {}
        details = details if isinstance(details, dict) else {}
        candidate = str(details.get("ai_cover_path") or "").strip()
        if not candidate:
            raise RecorderConfigError("该任务暂无可预览的 AI 封面")
        path = Path(candidate).resolve()
        allowed_root = self._recording_file_roots()["artifacts"]
        try:
            path.relative_to(allowed_root)
        except ValueError as exc:
            raise RecorderConfigError("封面路径不在允许的录播产物目录中") from exc
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise RecorderConfigError("录播封面文件不存在")
        return path

    def retry_pipeline_job(self, fingerprint: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RecorderConfigError("任务编号无效")
        job = self.pipeline_job(fingerprint)
        if not job:
            raise RecorderConfigError("没有找到该录播任务")
        if job["status"] not in {"failed", "dry_run"}:
            raise RecorderConfigError("只有失败或试运行任务可以重试")
        video = Path(job["video_path"])
        if not video.is_file():
            raise RecorderConfigError("原始录播文件已不存在，无法重试")
        log_path = APP_ROOT / "logs" / f"pipeline-{fingerprint[:12]}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_handle:
            subprocess.Popen(
                [sys.executable, str(WORKSPACE_ROOT / "bridge.py"), "--config", str(BRIDGE_CONFIG_PATH),
                 "ingest", "--retry", str(video)],
                cwd=WORKSPACE_ROOT, stdout=log_handle, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )

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
