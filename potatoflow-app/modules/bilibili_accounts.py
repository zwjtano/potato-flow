"""Bilibili upload account registry with legacy single-account compatibility."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from .utils import get_app_root_dir, get_app_subdir


LEGACY_ACCOUNT_ID = "default"
LEGACY_ACCOUNT_NAME = "默认账号"
ACCOUNTS_CONFIG_KEY = "BILIBILI_ACCOUNTS"
DEFAULT_ACCOUNT_CONFIG_KEY = "BILIBILI_DEFAULT_ACCOUNT_ID"


def _clean_account_id(value: Any) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", str(value or "").strip()).strip("-")
    return normalized[:64]


def normalize_accounts(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    config = config if isinstance(config, dict) else {}
    raw_accounts = config.get(ACCOUNTS_CONFIG_KEY)
    raw_accounts = raw_accounts if isinstance(raw_accounts, list) else []
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_accounts:
        if not isinstance(raw, dict):
            continue
        account_id = _clean_account_id(raw.get("id"))
        cookie_path = str(raw.get("cookies_path") or "").strip()
        if not account_id or not cookie_path or account_id in seen:
            continue
        seen.add(account_id)
        accounts.append({
            "id": account_id,
            "name": str(
                raw.get("bilibili_name")
                or raw.get("name")
                or account_id
            ).strip()[:80] or account_id,
            "cookies_path": cookie_path,
            "bilibili_name": str(raw.get("bilibili_name") or "").strip(),
            "bilibili_uid": str(raw.get("bilibili_uid") or "").strip(),
            "avatar_url": str(raw.get("avatar_url") or "").strip(),
        })

    legacy_path = str(
        config.get("BILIBILI_COOKIES_PATH") or "cookies/bili_cookies.json"
    ).strip()
    accounts.insert(0, {
        "id": LEGACY_ACCOUNT_ID,
        "name": str(config.get("BILIBILI_ACCOUNT_NAME") or LEGACY_ACCOUNT_NAME),
        "cookies_path": legacy_path,
        "bilibili_name": str(config.get("BILIBILI_ACCOUNT_NAME") or ""),
        "bilibili_uid": str(config.get("BILIBILI_ACCOUNT_UID") or ""),
        "avatar_url": str(config.get("BILIBILI_ACCOUNT_AVATAR_URL") or ""),
        "legacy": True,
    })
    for account in accounts:
        if not account.get("bilibili_uid"):
            account["bilibili_uid"] = account_uid(account.get("cookies_path"))
    return accounts


def resolve_cookie_path(path_value: Any) -> Path:
    raw = Path(str(path_value or "").strip()).expanduser()
    if raw.is_absolute():
        return raw
    app_root = Path(get_app_root_dir())
    candidates = (app_root / raw, app_root.parent / raw)
    return next((path for path in candidates if path.is_file()), candidates[0])


def account_uid(path_value: Any) -> str:
    try:
        from .bilibili_auth import load_cookie_dict

        cookies = load_cookie_dict(str(resolve_cookie_path(path_value)))
        return str(
            cookies.get("DedeUserID")
            or cookies.get("dedeuserid")
            or ""
        ).strip()
    except (FileNotFoundError, OSError, ValueError):
        return ""


def default_account_id(config: dict[str, Any] | None) -> str:
    accounts = normalize_accounts(config)
    requested = _clean_account_id(
        (config or {}).get(DEFAULT_ACCOUNT_CONFIG_KEY)
        if isinstance(config, dict)
        else ""
    )
    ids = {account["id"] for account in accounts}
    return requested if requested in ids else LEGACY_ACCOUNT_ID


def resolve_account(
    config: dict[str, Any] | None,
    account_id: Any = None,
) -> dict[str, Any]:
    accounts = normalize_accounts(config)
    requested = _clean_account_id(account_id) or default_account_id(config)
    selected = next(
        (account for account in accounts if account["id"] == requested),
        None,
    )
    if selected is None:
        fallback_id = default_account_id(config)
        selected = next(
            (account for account in accounts if account["id"] == fallback_id),
            accounts[0],
        )
    return dict(selected)


def create_account_record(name: str, filename: str = "") -> dict[str, str]:
    clean_name = str(name or "").strip()
    if not clean_name:
        clean_name = "待识别账号"
    if len(clean_name) > 80:
        raise ValueError("账号名称不能超过 80 个字符")
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in {".json", ".txt"}:
        suffix = ".json"
    account_id = f"bili-{uuid.uuid4().hex[:12]}"
    relative_path = f"cookies/bilibili_accounts/{account_id}{suffix}"
    return {
        "id": account_id,
        "name": clean_name,
        "cookies_path": relative_path,
    }


def account_cookie_destination(account: dict[str, Any]) -> Path:
    filename = Path(str(account.get("cookies_path") or "")).name
    if not filename:
        raise ValueError("账号 Cookie 路径无效")
    directory = Path(get_app_subdir("cookies")) / "bilibili_accounts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def serialize_custom_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "id": str(account["id"]),
            "name": str(account["name"]),
            "cookies_path": str(account["cookies_path"]),
            "bilibili_name": str(account.get("bilibili_name") or ""),
            "bilibili_uid": str(account.get("bilibili_uid") or ""),
            "avatar_url": str(account.get("avatar_url") or ""),
        }
        for account in accounts
        if str(account.get("id") or "") != LEGACY_ACCOUNT_ID
    ]
