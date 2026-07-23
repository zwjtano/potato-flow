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
RECORDING_FILE_SUFFIXES = {
    ".mp4": "video", ".flv": "video", ".mkv": "video", ".webm": "video",
    ".ts": "video", ".m2ts": "video", ".mov": "video",
    ".xml": "xml", ".ass": "ass",
}


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
            parsed_room_url = urlparse(room_url)
            room["display_url"] = parsed_room_url._replace(query="", fragment="").geturl()
            room["display_room_id"] = parsed_room_url.path.rstrip("/").rsplit("/", 1)[-1] or "—"
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

    def _pipeline_state_path(self) -> Path:
        try:
            config = json.loads(BRIDGE_CONFIG_PATH.read_text(encoding="utf-8"))
            configured = Path(str(config.get("state_db") or ".bridge/state.sqlite3")).expanduser()
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            configured = Path(".bridge/state.sqlite3")
        return configured.resolve() if configured.is_absolute() else (WORKSPACE_ROOT / configured).resolve()

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
            (str(item.get("id") or ""), f"{_slug(str(item.get('name') or ''))}_{str(item.get('id') or '')[:6]}")
            for item in self.list_rooms()
        ]
        for row in uploads:
            video_path = str(row["video_path"])
            if room_marker and room_marker not in Path(video_path).name:
                continue
            result = self._decode_json(row["result_json"])
            matched_room_id = next((rid for rid, marker in room_markers if marker and marker in Path(video_path).name), None)
            jobs.append({
                "id": row["fingerprint"], "short_id": row["fingerprint"][:12],
                "video_path": video_path, "video_name": Path(video_path).name,
                "platform": row["platform"], "status": row["status"],
                "attempts": row["attempts"], "result": result, "error": row["error"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "room_id": matched_room_id,
                "stages": stages_by_job.get(row["fingerprint"], []),
            })
        return jobs

    def pipeline_job(self, fingerprint: str) -> dict[str, Any] | None:
        return next((job for job in self.pipeline_jobs(100) if job["id"] == fingerprint), None)

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
