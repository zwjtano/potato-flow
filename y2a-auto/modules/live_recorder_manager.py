#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Manage the bundled biliup recorder from the unified Y2A web application."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


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
FFMPEG_DIR = APP_ROOT / "ffmpeg" / "darwin_arm64"


class RecorderConfigError(ValueError):
    pass


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _yaml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", value.strip())
    return cleaned.strip("_") or "直播间"


def detect_platform(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host in {"live.bilibili.com", "b23.tv"}:
        return "bilibili"
    if host == "douyu.com" or host.endswith(".douyu.com"):
        return "douyu"
    raise RecorderConfigError("只支持哔哩哔哩直播间和斗鱼直播间 URL")


class LiveRecorderManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._log_handle = None

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
            room_url = str(room.get("url") or "")
            remark = f"{_slug(str(room.get('name') or ''))}_{str(room.get('id') or '')[:6]}"
            worker = workers_by_remark.get(remark) or workers_by_url.get(room_url)
            raw_status = cls._worker_status(worker.get("downloader_status")) if worker else "Unknown"

            if not engine_running:
                state, label = "stopped", "引擎未启动"
                primary, secondary = "等待启动引擎", "启动后自动检测开播"
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
            return dict(existing)

    def delete_room(self, room_id: str) -> bool:
        with self._lock:
            rooms = self.list_rooms()
            filtered = [room for room in rooms if room.get("id") != room_id]
            if len(filtered) == len(rooms):
                return False
            _atomic_json(ROOMS_PATH, filtered)
            self.sync_configs(filtered)
            return True

    def sync_configs(self, rooms: list[dict[str, Any]] | None = None) -> None:
        rooms = rooms if rooms is not None else self.list_rooms()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        (RECORDINGS_DIR / "data").mkdir(parents=True, exist_ok=True)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)

        bridge_command = " ".join(
            [
                _yaml_string(str(APP_ROOT / ".venv" / "bin" / "python")),
                _yaml_string(str(WORKSPACE_ROOT / "bridge.py")),
                "--config",
                _yaml_string(str(BRIDGE_CONFIG_PATH)),
                "ingest",
            ]
        )
        lines = [
            "# 由统一管理后台自动生成，请勿手动编辑。",
            "downloader: ffmpeg",
            "file_size: 2621440000",
            "filtering_threshold: 20",
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
            lines.extend(
                [
                    f"  {_yaml_string(key)}:",
                    "    url:",
                    f"      - {_yaml_string(str(room['url']))}",
                    "    uploader: Noop",
                    "    postprocessor:",
                    f"      - run: {_yaml_string(bridge_command)}",
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
        if (FFMPEG_DIR / "ffmpeg").is_file():
            config["ffmpeg"] = str(FFMPEG_DIR / "ffmpeg")
        if (FFMPEG_DIR / "ffprobe").is_file():
            config["ffprobe"] = str(FFMPEG_DIR / "ffprobe")
        config["profiles"] = [
            {
                "match": f"*{_slug(str(room['name']))}_{str(room['id'])[:6]}*",
                "source_url": room["url"],
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
            os.kill(pid, 0)
            return pid
        except PermissionError:
            # 在容器或受限运行环境中，同一服务的子进程可能不允许发送
            # signal 0；这代表无法探测，不代表进程不存在。
            return pid
        except (FileNotFoundError, ValueError, ProcessLookupError):
            PID_PATH.unlink(missing_ok=True)
        try:
            payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            status_pid = int(payload.get("pid") or 0)
            if status_pid and time.time() - float(payload.get("updated_at") or 0) <= 5:
                try:
                    os.kill(status_pid, 0)
                except PermissionError:
                    return status_pid
                except ProcessLookupError:
                    pass
                else:
                    PID_PATH.write_text(str(status_pid), encoding="utf-8")
                    return status_pid
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
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
                return self.status()
            if not self.list_rooms():
                raise RecorderConfigError("请先添加至少一个直播间")
            binary = self.binary_path
            if not binary.is_file():
                raise RecorderConfigError("录制引擎尚未构建，请先安装 Rust 并构建 biliup")
            self.sync_configs()
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


live_recorder_manager = LiveRecorderManager()
