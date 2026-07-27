#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Shared proxy configuration for YouTube and Telegram network requests."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def legacy_proxy_values(config: dict[str, Any] | None) -> tuple[str, str, str]:
    """Return the first usable proxy from the pre-unification config keys."""
    values = dict(config or {})
    youtube_proxy = ("YOUTUBE_PROXY_URL", "YOUTUBE_PROXY_USERNAME", "YOUTUBE_PROXY_PASSWORD")
    youtube_api_proxy = (
        "YOUTUBE_API_PROXY_URL",
        "YOUTUBE_API_PROXY_USERNAME",
        "YOUTUBE_API_PROXY_PASSWORD",
    )
    telegram_proxy = ("NOTIFY_TELEGRAM_PROXY_URL", "", "")
    legacy_candidates = []
    if bool(values.get("YOUTUBE_PROXY_ENABLED")):
        legacy_candidates.append(youtube_proxy)
    if bool(values.get("YOUTUBE_API_PROXY_ENABLED")):
        legacy_candidates.append(youtube_api_proxy)
    legacy_candidates.extend((telegram_proxy, youtube_proxy, youtube_api_proxy))
    seen = set()
    for url_key, username_key, password_key in legacy_candidates:
        if url_key in seen:
            continue
        seen.add(url_key)
        url = str(values.get(url_key) or "").strip()
        if url:
            username = str(values.get(username_key) or "").strip() if username_key else ""
            password = str(values.get(password_key) or "").strip() if password_key else ""
            return url, username, password
    return "", "", ""


def common_proxy_values(config: dict[str, Any] | None) -> tuple[str, str, str]:
    """Return the common proxy values, with fallback only for unmigrated config."""
    values = dict(config or {})
    if "NETWORK_PROXY_URL" in values:
        return (
            str(values.get("NETWORK_PROXY_URL") or "").strip(),
            str(values.get("NETWORK_PROXY_USERNAME") or "").strip(),
            str(values.get("NETWORK_PROXY_PASSWORD") or "").strip(),
        )
    return legacy_proxy_values(values)


def build_common_proxy_url(config: dict[str, Any] | None) -> str:
    """Build and validate the common proxy URL, including optional auth."""
    proxy_url, username, password = common_proxy_values(config)
    if not proxy_url:
        return ""
    if "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"
    parsed = urlsplit(proxy_url)
    if parsed.scheme.lower() not in SUPPORTED_PROXY_SCHEMES or not parsed.hostname:
        raise ValueError("通用代理地址无效，请填写 HTTP、HTTPS 或 SOCKS5 代理地址")
    if username and password and parsed.username is None:
        protocol, rest = proxy_url.split("://", 1)
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}"
        proxy_url = f"{protocol}://{auth}@{rest}"
    return proxy_url
