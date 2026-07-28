#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Helpers for passing an optional Cookie file to biliup's Douyin recorder."""

from __future__ import annotations

import json
import re
from pathlib import Path


DOUYIN_REQUIRED_COOKIE_NAMES = (
    "__ac_nonce",
    "__ac_signature",
    "sessionid",
)


def _is_douyin_domain(value: object) -> bool:
    domain = str(value or "").strip().lstrip(".").lower()
    return not domain or domain == "douyin.com" or domain.endswith(".douyin.com")


def _selected_cookie_header(pairs: list[tuple[str, str]]) -> str:
    selected: dict[str, str] = {}
    for raw_name, raw_value in pairs:
        name = str(raw_name or "").strip()
        value = str(raw_value or "").strip()
        if name in DOUYIN_REQUIRED_COOKIE_NAMES and value:
            selected[name] = value
    return "; ".join(
        f"{name}={selected[name]}"
        for name in DOUYIN_REQUIRED_COOKIE_NAMES
        if name in selected
    )


def normalize_douyin_cookie(content: str | bytes) -> str:
    """Convert an export to biliup's minimal required Douyin Cookie header."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="ignore")
    content = str(content or "").lstrip("\ufeff").strip()
    if not content:
        return ""

    if content.startswith("# Netscape HTTP Cookie File") or "\t" in content:
        pairs: list[tuple[str, str]] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 7:
                continue
            if not _is_douyin_domain(fields[0]):
                continue
            name = fields[-2].strip()
            value = fields[-1].strip()
            if name and value:
                pairs.append((name, value))
        return _selected_cookie_header(pairs)

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        pairs = []
        for item in re.split(r"[;\r\n]+", content):
            name, separator, value = item.strip().partition("=")
            if separator:
                pairs.append((name, value))
        return _selected_cookie_header(pairs)
    items = payload.get("cookies", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return ""
    pairs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not _is_douyin_domain(item.get("domain")):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name and value:
            pairs.append((name, value))
    return _selected_cookie_header(pairs)


def missing_douyin_cookie_names(content: str | bytes) -> tuple[str, ...]:
    """Return required biliup Cookie names missing from an import."""
    normalized = normalize_douyin_cookie(content)
    present = {
        item.partition("=")[0].strip()
        for item in normalized.split(";")
        if "=" in item
    }
    return tuple(
        name for name in DOUYIN_REQUIRED_COOKIE_NAMES if name not in present
    )


def load_douyin_cookie(cookie_file: str | Path) -> str:
    """Load a Cookie file as the value of ``user.douyin_cookie``."""
    path = Path(cookie_file)
    if not path.is_file():
        return ""
    try:
        return normalize_douyin_cookie(path.read_bytes())
    except OSError:
        return ""
