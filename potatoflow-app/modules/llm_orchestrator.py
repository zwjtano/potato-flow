#!/usr/bin/env python3
"""Shared request policy for OpenAI-compatible text generation.

This module deliberately stays below prompt/business logic.  It provides:

* provider capability detection without sending OpenAI-only parameters to an
  arbitrary compatible endpoint;
* stable prompt-cache keys and explicit cache breakpoints for supported OpenAI
  models;
* a small cross-process sliding-window RPM governor.

It never stores prompts, credentials, subtitles, or model output.
"""

from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .path_policy import enforce_file_mode, ensure_directory
from .utils import get_app_subdir


_CACHE_MODES = {"auto", "explicit", "off"}
_GOVERNOR_SCHEMA_LOCK = threading.Lock()
_GOVERNOR_INITIALIZED_PATHS: set[str] = set()


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _is_official_openai_endpoint(base_url: str) -> bool:
    raw = str(base_url or "").strip()
    if not raw:
        return True
    try:
        host = (urlparse(raw).hostname or "").casefold()
    except ValueError:
        return False
    return host == "api.openai.com"


def _provider_bucket(api_key: str, base_url: str) -> str:
    endpoint = str(base_url or "https://api.openai.com/v1").strip().rstrip("/")
    credential = hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()[:16]
    return hashlib.sha256(f"{endpoint}\x00{credential}".encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class LLMRequestPolicy:
    provider_bucket: str
    prompt_cache_mode: str = "off"
    prompt_cache_ttl: str = "30m"
    prompt_cache_key_shards: int = 1
    rpm_limit: int = 0
    max_retries: int = 2
    retry_base_seconds: float = 0.75
    attempt_compatible_thinking_control: bool = True

    @property
    def prompt_cache_enabled(self) -> bool:
        return self.prompt_cache_mode != "off"


def resolve_llm_request_policy(
    config: Mapping[str, Any] | None,
    *,
    model_name: str = "",
) -> LLMRequestPolicy:
    """Resolve safe defaults for an OpenAI or compatible endpoint.

    ``auto`` only enables vendor-specific request fields for the official
    endpoint.  Compatible providers can opt in with ``explicit`` after their
    parameter support has been verified.
    """
    cfg = config or {}
    base_url = str(cfg.get("OPENAI_BASE_URL") or "").strip()
    requested_mode = str(cfg.get("OPENAI_PROMPT_CACHE_MODE") or "auto").strip().casefold()
    if requested_mode not in _CACHE_MODES:
        requested_mode = "auto"
    official = _is_official_openai_endpoint(base_url)
    model = str(model_name or cfg.get("OPENAI_MODEL_NAME") or "").strip().casefold()

    if requested_mode == "off":
        effective_mode = "off"
    elif requested_mode == "explicit":
        effective_mode = "explicit"
    elif official:
        # GPT-5.6 supports the explicit cache breakpoint contract.  Older
        # official models still benefit from a stable prompt_cache_key while
        # using the API's automatic caching behavior.
        effective_mode = "explicit" if model.startswith("gpt-5.6") else "auto"
    else:
        effective_mode = "off"

    ttl = str(cfg.get("OPENAI_PROMPT_CACHE_TTL") or "30m").strip()
    if ttl not in {"in-memory", "30m", "24h"}:
        ttl = "30m"
    rpm_limit = _bounded_int(cfg.get("OPENAI_RPM_LIMIT", 30), 30, 0, 60_000)
    max_retries = _bounded_int(cfg.get("OPENAI_REQUEST_MAX_RETRIES", 2), 2, 0, 8)
    try:
        retry_base_seconds = float(cfg.get("OPENAI_RETRY_BASE_SECONDS", 0.75) or 0.75)
    except (TypeError, ValueError):
        retry_base_seconds = 0.75
    retry_base_seconds = max(0.1, min(retry_base_seconds, 30.0))
    shards_default = max(1, (rpm_limit + 14) // 15) if rpm_limit else 1
    shards = _bounded_int(
        cfg.get("OPENAI_PROMPT_CACHE_KEY_SHARDS", shards_default),
        shards_default,
        1,
        64,
    )
    return LLMRequestPolicy(
        provider_bucket=_provider_bucket(str(cfg.get("OPENAI_API_KEY") or ""), base_url),
        prompt_cache_mode=effective_mode,
        prompt_cache_ttl=ttl,
        prompt_cache_key_shards=shards,
        rpm_limit=rpm_limit,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
        attempt_compatible_thinking_control=not official,
    )


def is_retryable_llm_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        status_code = int(status) if status is not None else 0
    except (TypeError, ValueError):
        status_code = 0
    if status_code in {408, 409, 429} or status_code >= 500:
        return True
    name = exc.__class__.__name__.casefold()
    return any(marker in name for marker in (
        "ratelimit", "timeout", "connection", "internalserver", "serviceunavailable"
    ))


def llm_retry_delay(
    exc: Exception,
    *,
    attempt: int,
    base_seconds: float,
) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    retry_after = headers.get("retry-after") if hasattr(headers, "get") else None
    try:
        if retry_after is not None:
            return max(0.0, min(float(retry_after), 120.0))
    except (TypeError, ValueError):
        pass
    exponential = float(base_seconds) * (2 ** max(0, int(attempt)))
    return min(60.0, exponential + random.uniform(0.0, exponential * 0.25))


def _stable_cache_key(
    policy: LLMRequestPolicy,
    *,
    model_name: str,
    scene_name: str,
    system_prompt: str,
    user_content: Any,
) -> str:
    stable = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:16]
    variable = hashlib.sha256(str(user_content).encode("utf-8")).digest()
    shard = int.from_bytes(variable[:4], "big") % policy.prompt_cache_key_shards
    safe_model = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in model_name)[:32]
    safe_scene = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in scene_name)[:40]
    return f"pf:{safe_model}:{safe_scene}:{stable}:s{shard}"


def apply_prompt_cache_policy(
    create_kwargs: dict[str, Any],
    policy: LLMRequestPolicy | None,
    *,
    model_name: str,
    scene_name: str,
    system_prompt: str,
    user_content: Any,
) -> dict[str, Any]:
    """Mutate and return Chat Completions kwargs with cache-safe fields."""
    if not policy or not policy.prompt_cache_enabled:
        return create_kwargs
    cache_key = _stable_cache_key(
        policy,
        model_name=model_name,
        scene_name=scene_name,
        system_prompt=system_prompt,
        user_content=user_content,
    )
    create_kwargs["prompt_cache_key"] = cache_key
    if policy.prompt_cache_mode == "explicit":
        create_kwargs["messages"][0]["content"] = [{
            "type": "text",
            "text": system_prompt,
            "prompt_cache_breakpoint": {"mode": "explicit"},
        }]
        create_kwargs["prompt_cache_options"] = {
            "mode": "explicit",
            "ttl": policy.prompt_cache_ttl,
        }
    return create_kwargs


class AiRequestGovernor:
    """Cross-process sliding-window request limiter backed by SQLite."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = str(os.environ.get("POTATOFLOW_AI_GOVERNOR_PATH") or "").strip()
        self.path = Path(path or configured or get_app_subdir("db/ai-governor.sqlite3"))

    def _connect(self) -> sqlite3.Connection:
        ensure_directory(self.path.parent, private=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        cache_key = str(self.path.resolve())
        schema_exists = connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='ai_rate_events'"""
        ).fetchone() is not None
        if cache_key not in _GOVERNOR_INITIALIZED_PATHS or not schema_exists:
            with _GOVERNOR_SCHEMA_LOCK:
                schema_exists = connection.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type='table' AND name='ai_rate_events'"""
                ).fetchone() is not None
                if cache_key not in _GOVERNOR_INITIALIZED_PATHS or not schema_exists:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS ai_rate_events (
                               bucket TEXT NOT NULL,
                               created_at REAL NOT NULL
                           )"""
                    )
                    connection.execute(
                        """CREATE INDEX IF NOT EXISTS idx_ai_rate_events_bucket_time
                           ON ai_rate_events(bucket, created_at)"""
                    )
                    connection.commit()
                    enforce_file_mode(self.path, private=True)
                    _GOVERNOR_INITIALIZED_PATHS.add(cache_key)
        return connection

    def acquire(self, bucket: str, rpm_limit: int) -> float:
        """Wait for a request slot and return seconds spent waiting."""
        limit = max(0, int(rpm_limit or 0))
        if not bucket or limit <= 0:
            return 0.0
        started = time.monotonic()
        while True:
            now = time.time()
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cutoff = now - 60.0
                connection.execute(
                    "DELETE FROM ai_rate_events WHERE created_at <= ?", (cutoff,)
                )
                rows = connection.execute(
                    """SELECT created_at FROM ai_rate_events
                       WHERE bucket=? ORDER BY created_at ASC""",
                    (bucket,),
                ).fetchall()
                if len(rows) < limit:
                    connection.execute(
                        "INSERT INTO ai_rate_events(bucket, created_at) VALUES (?, ?)",
                        (bucket, now),
                    )
                    connection.commit()
                    return max(0.0, time.monotonic() - started)
                wait_seconds = max(0.01, float(rows[0][0]) + 60.0 - now + 0.01)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            time.sleep(min(wait_seconds, 60.0))


_DEFAULT_GOVERNOR = AiRequestGovernor()


def acquire_request_slot(policy: LLMRequestPolicy | None) -> float:
    if not policy:
        return 0.0
    return _DEFAULT_GOVERNOR.acquire(policy.provider_bucket, policy.rpm_limit)
