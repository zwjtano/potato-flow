#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""PotatoFlow adapter for the bundled Biliup uploader."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from .bilibili_auth import load_cookie_dict
from .biliup_line_manager import SUPPORTED_LINES
from .config_manager import load_config
from .utils import get_app_root_dir, get_app_subdir


RESULT_PREFIX = "POTATOFLOW_RESULT="
PROGRESS_PREFIX = "POTATOFLOW_PROGRESS="


def _biliup_binary() -> str:
    configured = str(os.environ.get("BILIUP_BIN") or "").strip()
    candidates = [
        configured,
        os.path.join(get_app_root_dir(), "upstream-biliup", "target", "release", "biliup"),
        "biliup",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate == "biliup":
            return candidate
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("未找到容器内置 Biliup 上传程序")


def _cookie_payload(cookie_file: str) -> dict[str, Any]:
    cookies = load_cookie_dict(cookie_file)
    cookie_items = []
    for name, value in cookies.items():
        cookie_items.append({
            "name": str(name),
            "value": str(value),
            "domain": ".bilibili.com",
            "path": "/",
            "http_only": False,
            "secure": True,
        })
    try:
        mid = int(str(cookies.get("DedeUserID") or "0"))
    except ValueError:
        mid = 0
    return {
        "cookie_info": {"cookies": cookie_items},
        "sso": [],
        "token_info": {
            "access_token": "",
            "expires_in": 0,
            "mid": mid,
            "refresh_token": "",
        },
        "platform": "web",
    }


def _write_cookie_file(cookie_file: str) -> str:
    temp_dir = get_app_subdir("temp")
    os.makedirs(temp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="biliup-cookie-", suffix=".json", dir=temp_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_cookie_payload(cookie_file), handle, ensure_ascii=False)
        return path
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _compact_error(lines: deque[str]) -> str:
    useful = []
    for raw in lines:
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(raw or "")).strip()
        if not line or line.startswith((PROGRESS_PREFIX, RESULT_PREFIX)):
            continue
        useful.append(line)
    return " | ".join(useful[-8:])[-1800:] or "Biliup 投稿进程异常退出"


def verify_biliup_cookie(cookie_file: str) -> tuple[bool, str]:
    """Validate the converted browser Cookie through Biliup's read-only list API."""
    generated_cookie = _write_cookie_file(cookie_file)
    try:
        completed = subprocess.run(
            [
                _biliup_binary(),
                "--user-cookie", generated_cookie,
                "list",
                "--max-pages", "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=os.environ.copy(),
        )
        if completed.returncode == 0:
            return True, "Biliup 已通过网页 Cookie 读取稿件列表"
        return False, _compact_error(deque(completed.stdout.splitlines(), maxlen=80))
    except Exception as exc:
        return False, str(exc).splitlines()[0][:1800]
    finally:
        try:
            os.unlink(generated_cookie)
        except OSError:
            pass


def upload_with_biliup(
    *,
    cookie_file: str,
    video_paths: list[str],
    cover_file: str,
    title: str,
    description: str,
    tags: list[str],
    partition_id: int,
    page_titles: Optional[list[str]] = None,
    existing_submission: Optional[dict[str, Any]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    progress_detail_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> tuple[bool, dict[str, Any] | str]:
    config = load_config()
    line = str(config.get("BILIBILI_UPLOAD_LINE") or "bldsa").strip().lower()
    if line not in SUPPORTED_LINES:
        return False, f"全局投稿线路 {line or '空'} 不受支持，请在系统设置重新测速"
    try:
        limit = min(12, max(1, int(config.get("BILIBILI_UPLOAD_LIMIT") or 3)))
    except (TypeError, ValueError):
        limit = 3
    binary = _biliup_binary()
    generated_cookie = _write_cookie_file(cookie_file)
    appending = bool(isinstance(existing_submission, dict) and existing_submission.get("bvid"))
    command = [
        binary,
        "--user-cookie", generated_cookie,
    ]
    if appending:
        command.extend([
            "append",
            "--submit", "web",
            "--vid", str(existing_submission["bvid"]),
            "--line", line,
            "--limit", str(limit),
            *video_paths,
        ])
    else:
        command.extend([
            "upload",
            "--submit", "web",
            "--line", line,
            "--limit", str(limit),
            *video_paths,
            "--copyright", "1",
            "--tid", str(int(partition_id)),
            "--cover", str(cover_file),
            "--title", str(title),
            "--desc", str(description),
            "--tag", ",".join(str(item).strip() for item in tags if str(item).strip()),
        ])
    environment = os.environ.copy()
    environment["POTATOFLOW_MACHINE_OUTPUT"] = "1"
    if page_titles:
        environment["POTATOFLOW_PAGE_TITLE"] = str(page_titles[0] or "").strip()[:80]

    recent_lines: deque[str] = deque(maxlen=80)
    result_payload: dict[str, Any] | None = None
    started_at = time.monotonic()
    samples: deque[tuple[float, int]] = deque([(started_at, 0)], maxlen=20)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
        )
        assert process.stdout is not None
        with process.stdout:
            for raw_line in process.stdout:
                line_text = raw_line.rstrip("\r\n")
                recent_lines.append(line_text)
                if line_text.startswith(PROGRESS_PREFIX):
                    try:
                        progress = json.loads(line_text[len(PROGRESS_PREFIX):])
                        now = time.monotonic()
                        uploaded = max(0, int(progress.get("uploaded_bytes") or 0))
                        total = max(0, int(progress.get("total_bytes") or 0))
                        samples.append((now, uploaded))
                        while len(samples) > 2 and now - samples[0][0] > 15:
                            samples.popleft()
                        duration = max(now - samples[0][0], 0.001)
                        speed = max(0.0, (uploaded - samples[0][1]) / duration)
                        remaining = max(0, total - uploaded)
                        detail = {
                            "uploaded_bytes": uploaded,
                            "total_bytes": total,
                            "speed_bytes_per_sec": speed,
                            "eta_seconds": remaining / speed if speed > 0 else None,
                            "percent": float(progress.get("percent") or 0),
                            "line": line,
                            "engine": "biliup",
                        }
                        if progress_callback:
                            progress_callback(f"{detail['percent']:.1f}%")
                        if progress_detail_callback:
                            progress_detail_callback(detail)
                    except Exception:
                        pass
                elif line_text.startswith(RESULT_PREFIX):
                    try:
                        decoded = json.loads(line_text[len(RESULT_PREFIX):])
                        if isinstance(decoded, dict):
                            result_payload = decoded
                    except json.JSONDecodeError:
                        pass
                elif log_callback and line_text.strip():
                    log_callback(line_text.strip())
        return_code = process.wait()
        if return_code != 0:
            return False, _compact_error(recent_lines)
        if not isinstance(result_payload, dict):
            return False, "Biliup 投稿已结束，但没有返回可识别的稿件结果"

        response_data = result_payload.get("data")
        response_data = response_data if isinstance(response_data, dict) else {}
        bvid = response_data.get("bvid") or result_payload.get("bvid")
        aid = response_data.get("aid") or result_payload.get("aid")
        if appending:
            bvid = bvid or existing_submission.get("bvid")
            aid = aid or existing_submission.get("aid")
        if not bvid and not aid:
            return False, f"Biliup 返回中未找到 BVID/AID：{result_payload}"
        return True, {
            "bvid": bvid,
            "aid": aid,
            "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
            "part_count": int(existing_submission.get("part_count") or 0) + len(video_paths)
            if appending else len(video_paths),
            "uploaded_parts": [],
            "cover_url": str(existing_submission.get("cover_url") or "") if appending else "",
            "upload_engine": "biliup",
            "upload_line": line,
            "raw_response": result_payload,
        }
    except Exception as exc:
        return False, str(exc).splitlines()[0][:1800]
    finally:
        try:
            os.unlink(generated_cookie)
        except OSError:
            pass
