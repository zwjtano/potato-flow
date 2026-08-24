#!/usr/bin/env python3
"""Private cross-process cache for validated structured model results."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .ai_request_ledger import input_fingerprint
from .path_policy import enforce_file_mode, ensure_directory
from .utils import get_app_subdir


_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()
_VALID_MODES = {"auto", "off", "refresh", "read-only"}


def resolve_result_cache_mode(config: Mapping[str, Any] | None) -> str:
    cfg = config or {}
    if bool(cfg.get("OPENAI_RESULT_CACHE_BYPASS", False)):
        return "off"
    mode = str(cfg.get("OPENAI_RESULT_CACHE_MODE") or "auto").strip().casefold()
    return mode if mode in _VALID_MODES else "auto"


def resolve_result_cache_ttl_seconds(config: Mapping[str, Any] | None) -> float:
    try:
        hours = float((config or {}).get("OPENAI_RESULT_CACHE_TTL_HOURS", 168) or 168)
    except (TypeError, ValueError):
        hours = 168.0
    return max(1.0, min(hours, 24 * 365)) * 3600.0


def resolve_result_cache_wait_seconds(config: Mapping[str, Any] | None) -> float:
    try:
        seconds = float((config or {}).get("OPENAI_RESULT_CACHE_WAIT_SECONDS", 120) or 120)
    except (TypeError, ValueError):
        seconds = 120.0
    return max(0.1, min(seconds, 300.0))


def build_result_cache_key(
    *,
    scene_name: str,
    prompt_version: str,
    provider_bucket: str,
    model_name: str,
    system_prompt: str,
    user_content: Any,
    temperature: float,
    max_tokens: int | None,
    thinking_enabled: bool,
) -> str:
    """Hash every semantic and provider input without persisting raw input."""
    return input_fingerprint(
        "ai-result-cache-v1",
        scene_name,
        prompt_version,
        provider_bucket,
        model_name,
        system_prompt,
        user_content,
        round(float(temperature), 4),
        max_tokens,
        bool(thinking_enabled),
        "json_object",
    )


class AiResultCache:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = str(os.environ.get("POTATOFLOW_AI_RESULT_CACHE_PATH") or "").strip()
        self.path = Path(path or configured or get_app_subdir("db/ai-results.sqlite3"))

    def _connect(self) -> sqlite3.Connection:
        ensure_directory(self.path.parent, private=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        cache_key = str(self.path.resolve())
        schema_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_result_cache'"
        ).fetchone() is not None
        if cache_key not in _INITIALIZED_PATHS or not schema_exists:
            with _SCHEMA_LOCK:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS ai_result_cache (
                           cache_key TEXT PRIMARY KEY,
                           scene_name TEXT NOT NULL,
                           prompt_version TEXT NOT NULL,
                           model TEXT NOT NULL,
                           result_json TEXT NOT NULL,
                           created_at REAL NOT NULL,
                           expires_at REAL NOT NULL,
                           last_accessed_at REAL NOT NULL,
                           hit_count INTEGER NOT NULL DEFAULT 0
                       )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS ai_result_fill_leases (
                           cache_key TEXT PRIMARY KEY,
                           owner_token TEXT NOT NULL,
                           expires_at REAL NOT NULL
                       )"""
                )
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS idx_ai_result_cache_expiry
                       ON ai_result_cache(expires_at)"""
                )
                connection.commit()
                enforce_file_mode(self.path, private=True)
                _INITIALIZED_PATHS.add(cache_key)
        return connection

    def acquire_fill_lease(
        self,
        cache_key: str,
        *,
        lease_seconds: float = 600,
        now: float | None = None,
    ) -> str:
        """Elect one process to fill a missing key; return its private lease token."""
        if not cache_key:
            return ""
        current = time.time() if now is None else float(now)
        owner_token = uuid.uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM ai_result_fill_leases WHERE expires_at <= ?", (current,)
            )
            cursor = connection.execute(
                """INSERT OR IGNORE INTO ai_result_fill_leases
                   (cache_key, owner_token, expires_at) VALUES (?, ?, ?)""",
                (cache_key, owner_token, current + max(1.0, float(lease_seconds))),
            )
            connection.commit()
            return owner_token if cursor.rowcount == 1 else ""
        finally:
            connection.close()

    def release_fill_lease(self, cache_key: str, owner_token: str) -> None:
        if not cache_key or not owner_token:
            return
        connection = self._connect()
        try:
            connection.execute(
                """DELETE FROM ai_result_fill_leases
                   WHERE cache_key=? AND owner_token=?""",
                (cache_key, owner_token),
            )
            connection.commit()
        finally:
            connection.close()

    def get(self, cache_key: str, *, now: float | None = None) -> dict[str, Any] | None:
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM ai_result_cache WHERE expires_at <= ?", (current,))
            row = connection.execute(
                "SELECT result_json FROM ai_result_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """UPDATE ai_result_cache
                   SET last_accessed_at=?, hit_count=hit_count+1 WHERE cache_key=?""",
                (current, cache_key),
            )
            connection.commit()
        finally:
            connection.close()
        try:
            parsed = json.loads(str(row[0]))
        except (TypeError, ValueError):
            connection = self._connect()
            try:
                connection.execute("DELETE FROM ai_result_cache WHERE cache_key=?", (cache_key,))
                connection.commit()
            finally:
                connection.close()
            return None
        return parsed if isinstance(parsed, dict) else None

    def put(
        self,
        cache_key: str,
        result: dict[str, Any],
        *,
        scene_name: str,
        prompt_version: str,
        model_name: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> bool:
        if not cache_key or not isinstance(result, dict) or not result:
            return False
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            return False
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute(
                """INSERT INTO ai_result_cache
                   (cache_key, scene_name, prompt_version, model, result_json,
                    created_at, expires_at, last_accessed_at, hit_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     result_json=excluded.result_json,
                     created_at=excluded.created_at,
                     expires_at=excluded.expires_at,
                     last_accessed_at=excluded.last_accessed_at,
                     hit_count=0""",
                (
                    cache_key,
                    scene_name,
                    prompt_version,
                    model_name,
                    encoded,
                    current,
                    current + max(1.0, float(ttl_seconds)),
                    current,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return True


_DEFAULT_RESULT_CACHE = AiResultCache()


def get_ai_result_cache() -> AiResultCache:
    return _DEFAULT_RESULT_CACHE
