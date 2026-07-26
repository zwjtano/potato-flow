#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Helpers for passing an optional Cookie file to biliup's Douyin recorder."""

from __future__ import annotations

import json
from pathlib import Path


def load_douyin_cookie(cookie_file: str | Path) -> str:
    """Load JSON or plain-text Cookies as the value of ``user.douyin_cookie``."""
    path = Path(cookie_file)
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8")
        payload = json.loads(content)
    except (OSError, json.JSONDecodeError):
        try:
            return path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            return ""
    items = payload.get("cookies", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return ""
    pairs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name and value:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)
