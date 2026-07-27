#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Server-wide Biliup upload-line probing and selection.

The 10 MiB probe is deliberately only run from the settings action. Normal
uploads read BILIBILI_UPLOAD_LINE and never benchmark the network again.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

import requests

from .config_manager import load_config, save_config
from .utils import get_app_subdir


PROBE_URL = "https://member.bilibili.com/preupload?r=probe"
PROBE_SIZE = 10 * 1024 * 1024
SUPPORTED_LINES = {
    "bldsa", "cnbldsa", "andsa", "atdsa",
    "bda2", "cnbd", "anbd", "atbd",
    "tx", "cntx", "antx", "attx",
    "txa", "alia",
}
LINE_LABELS = {
    "bldsa": "B站 DSA",
    "cnbldsa": "B站 DSA（中国）",
    "andsa": "B站 DSA（海外）",
    "atdsa": "B站 DSA（海外）",
    "bda2": "百度云",
    "cnbd": "百度云（中国）",
    "anbd": "百度云（海外）",
    "atbd": "百度云（海外）",
    "tx": "腾讯云",
    "cntx": "腾讯云（中国）",
    "antx": "腾讯云（海外）",
    "attx": "腾讯云（海外）",
    "txa": "腾讯云（海外）",
    "alia": "阿里云（海外）",
}
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://member.bilibili.com/platform/upload/video/frame",
}


def _cache_path() -> str:
    return os.path.join(get_app_subdir("config"), "biliup_line_probe.json")


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".biliup-lines-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_probe_state() -> dict[str, Any]:
    config = load_config()
    cached: dict[str, Any] = {}
    try:
        with open(_cache_path(), "r", encoding="utf-8") as handle:
            value = json.load(handle)
            if isinstance(value, dict):
                cached = value
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    cached["selected_line"] = str(config.get("BILIBILI_UPLOAD_LINE") or "").strip().lower()
    cached["engine"] = "biliup"
    cached.setdefault("results", [])
    cached["supported_lines"] = [
        {"value": key, "label": LINE_LABELS.get(key, key)}
        for key in sorted(SUPPORTED_LINES)
    ]
    return cached


def select_upload_line(line: str) -> dict[str, Any]:
    normalized = str(line or "").strip().lower()
    if normalized not in SUPPORTED_LINES:
        raise ValueError("该线路不受当前 Biliup 上传器支持")
    config = load_config()
    config["BILIBILI_UPLOAD_ENGINE"] = "biliup"
    config["BILIBILI_UPLOAD_LINE"] = normalized
    if not save_config(config):
        raise RuntimeError("投稿线路保存失败")
    state = load_probe_state()
    state["selected_line"] = normalized
    return state


def _normalize_probe_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def _measure_line(item: dict[str, Any], method: str, payload: bytes) -> dict[str, Any]:
    query = str(item.get("query") or "").strip()
    line = ""
    for part in query.split("&"):
        if part.startswith("upcdn="):
            line = part.split("=", 1)[1].strip().lower()
            break
    result = {
        "line": line or query or "unknown",
        "label": LINE_LABELS.get(line, line or query or "未知线路"),
        "supported": line in SUPPORTED_LINES,
        "probe_url": _normalize_probe_url(item.get("probe_url")),
        "ok": False,
    }
    if not result["probe_url"]:
        result["error"] = "未返回测速地址"
        return result
    started = time.monotonic()
    try:
        if method == "get":
            response = requests.get(result["probe_url"], headers=REQUEST_HEADERS, timeout=(8, 30))
            transferred = len(response.content or b"")
        else:
            response = requests.post(
                result["probe_url"],
                data=payload,
                headers={**REQUEST_HEADERS, "Content-Type": "application/octet-stream"},
                timeout=(8, 60),
            )
            transferred = len(payload)
        response.raise_for_status()
        elapsed = max(time.monotonic() - started, 0.001)
        result.update({
            "ok": True,
            "elapsed_ms": round(elapsed * 1000),
            "speed_mbps": round((transferred / 1024 / 1024) / elapsed, 2),
        })
    except Exception as exc:
        result["elapsed_ms"] = round(max(time.monotonic() - started, 0.0) * 1000)
        result["error"] = str(exc).splitlines()[0][:240]
    return result


def probe_and_select() -> dict[str, Any]:
    response = requests.get(PROBE_URL, headers=REQUEST_HEADERS, timeout=(8, 30))
    response.raise_for_status()
    data = response.json()
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, list) or not lines:
        raise RuntimeError("B站没有返回可测速的投稿线路")
    probe_meta = data.get("probe") if isinstance(data.get("probe"), dict) else {}
    method = "get" if probe_meta.get("get") is not None else "post"
    payload = b"\0" * PROBE_SIZE if method == "post" else b""
    results: list[dict[str, Any]] = []
    # Probe strictly one line at a time. Concurrent uploads compete for the
    # same server bandwidth and make both speed measurements misleading.
    for item in lines:
        if isinstance(item, dict):
            results.append(_measure_line(item, method, payload))
    results.sort(key=lambda item: (
        not bool(item.get("ok")),
        not bool(item.get("supported")),
        int(item.get("elapsed_ms") or 10**9),
    ))
    candidates = [item for item in results if item.get("ok") and item.get("supported")]
    if not candidates:
        raise RuntimeError("测速完成，但没有找到当前 Biliup 支持且可用的线路")
    selected = str(candidates[0]["line"])
    config = load_config()
    config["BILIBILI_UPLOAD_ENGINE"] = "biliup"
    config["BILIBILI_UPLOAD_LINE"] = selected
    if not save_config(config):
        raise RuntimeError("测速成功，但线路配置保存失败")
    state = {
        "engine": "biliup",
        "selected_line": selected,
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "probe_size_bytes": PROBE_SIZE if method == "post" else 0,
        "probe_method": method.upper(),
        "results": results,
    }
    _write_json_atomic(_cache_path(), state)
    return load_probe_state()
