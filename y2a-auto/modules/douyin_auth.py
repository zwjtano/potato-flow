#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Helpers for passing an optional Cookie file to biliup's Douyin recorder."""

from __future__ import annotations

import json
from pathlib import Path


def normalize_douyin_cookie(content: str | bytes) -> str:
    """Convert Get cookies.txt LOCALLY/JSON/plain text to an HTTP Cookie header."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="ignore")
    content = str(content or "").lstrip("\ufeff").strip()
    if not content:
        return ""

    if content.startswith("# Netscape HTTP Cookie File") or "\t" in content:
        pairs: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 7:
                continue
            name = fields[-2].strip()
            value = fields[-1].strip()
            if name and value:
                pairs.append(f"{name}={value}")
        return "; ".join(pairs)

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content
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


def load_douyin_cookie(cookie_file: str | Path) -> str:
    """Load a Cookie file as the value of ``user.douyin_cookie``."""
    path = Path(cookie_file)
    if not path.is_file():
        return ""
    try:
        return normalize_douyin_cookie(path.read_bytes())
    except OSError:
        return ""
