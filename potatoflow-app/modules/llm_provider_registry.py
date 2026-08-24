#!/usr/bin/env python3
"""Versioned text-model provider profiles and cross-process circuit breakers."""

from __future__ import annotations

import hashlib
import contextvars
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .path_policy import enforce_file_mode, ensure_directory
from .prompt_registry import canonical_scene_name
from .utils import get_app_subdir


PROFILE_SLOTS = tuple(range(1, 5))
_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()
_LAST_PROVIDER_ROUTE: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "potatoflow_last_provider_route", default={}
)


@dataclass(frozen=True)
class LlmProviderProfile:
    profile_id: str
    api_key: str
    base_url: str
    model_name: str
    rpm_limit: int
    scenes: tuple[str, ...]
    priority: int
    legacy: bool = False

    @property
    def health_key(self) -> str:
        # Include only a digest of the credential. Replacing an exhausted key
        # should immediately create a fresh circuit without persisting secrets.
        key_digest = hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()
        raw = f"{self.profile_id}\0{self.base_url}\0{self.model_name}\0{key_digest}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def supports_scene(self, scene_name: str) -> bool:
        if not self.scenes or "*" in self.scenes:
            return True
        canonical = canonical_scene_name(scene_name)
        return scene_name in self.scenes or canonical in self.scenes

    def materialize_config(self, root: Mapping[str, Any]) -> dict[str, Any]:
        config = dict(root or {})
        config.update({
            "OPENAI_API_KEY": self.api_key,
            "OPENAI_BASE_URL": self.base_url,
            "OPENAI_MODEL_NAME": self.model_name,
            "OPENAI_RPM_LIMIT": self.rpm_limit,
            "OPENAI_PROVIDER_ROUTING_DISABLED": True,
            "OPENAI_ACTIVE_PROVIDER_PROFILE": self.profile_id,
        })
        return config


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _parse_scenes(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value or "*").replace("\n", ",").split(",")
    return tuple(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def resolve_provider_profiles(
    config: Mapping[str, Any] | None,
    *,
    scene_name: str,
) -> tuple[LlmProviderProfile, ...]:
    root = config or {}
    profiles: list[LlmProviderProfile] = []
    configured_profile_count = 0
    if not _coerce_bool(root.get("OPENAI_PROVIDER_ROUTING_DISABLED", False)):
        for slot in PROFILE_SLOTS:
            prefix = f"OPENAI_PROVIDER_{slot}_"
            if not _coerce_bool(root.get(f"{prefix}ENABLED", False)):
                continue
            api_key = str(root.get(f"{prefix}API_KEY") or "").strip()
            if not api_key:
                continue
            configured_profile_count += 1
            profile = LlmProviderProfile(
                profile_id=str(root.get(f"{prefix}ID") or f"provider-{slot}").strip(),
                api_key=api_key,
                base_url=str(root.get(f"{prefix}BASE_URL") or "").strip().rstrip("/"),
                model_name=str(root.get(f"{prefix}MODEL_NAME") or "").strip(),
                rpm_limit=max(0, int(root.get(f"{prefix}RPM_LIMIT", 30) or 30)),
                scenes=_parse_scenes(root.get(f"{prefix}SCENES", "*")),
                priority=slot,
            )
            if not scene_name or profile.supports_scene(scene_name):
                profiles.append(profile)

    legacy_key = str(root.get("OPENAI_API_KEY") or "").strip()
    legacy_fallback = _coerce_bool(root.get("OPENAI_PROVIDER_LEGACY_FALLBACK_ENABLED", True))
    # A legacy-only installation keeps working automatically. Once explicit
    # profiles exist, the operator can disable the legacy credential entirely;
    # a scene mismatch must not silently re-enable it.
    if configured_profile_count == 0 or (profiles and legacy_fallback):
        if legacy_key:
            profiles.append(LlmProviderProfile(
                profile_id="legacy-default",
                api_key=legacy_key,
                base_url=str(root.get("OPENAI_BASE_URL") or "").strip().rstrip("/"),
                model_name=str(root.get("OPENAI_MODEL_NAME") or "").strip(),
                rpm_limit=max(0, int(root.get("OPENAI_RPM_LIMIT", 30) or 30)),
                scenes=("*",),
                priority=999,
                legacy=True,
            ))
    return tuple(profiles)


class ProviderHealthStore:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = str(os.environ.get("POTATOFLOW_LLM_PROVIDER_HEALTH_PATH") or "").strip()
        self.path = Path(path or configured or get_app_subdir("db/llm-provider-health.sqlite3"))

    def _connect(self) -> sqlite3.Connection:
        ensure_directory(self.path.parent, private=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        path_key = str(self.path.resolve())
        if path_key not in _INITIALIZED_PATHS:
            with _SCHEMA_LOCK:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS llm_provider_health (
                           health_key TEXT PRIMARY KEY,
                           profile_id TEXT NOT NULL,
                           unavailable_until REAL NOT NULL,
                           reason TEXT NOT NULL DEFAULT '',
                           updated_at REAL NOT NULL
                       )"""
                )
                connection.commit()
                enforce_file_mode(self.path, private=True)
                _INITIALIZED_PATHS.add(path_key)
        return connection

    def is_available(self, profile: LlmProviderProfile, *, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT unavailable_until FROM llm_provider_health WHERE health_key=?",
                (profile.health_key,),
            ).fetchone()
        finally:
            connection.close()
        return row is None or float(row[0] or 0.0) <= current

    def mark_unavailable(
        self,
        profile: LlmProviderProfile,
        *,
        cooldown_seconds: float,
        reason: str,
        now: float | None = None,
    ) -> None:
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute(
                """INSERT INTO llm_provider_health
                   (health_key, profile_id, unavailable_until, reason, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(health_key) DO UPDATE SET
                     unavailable_until=excluded.unavailable_until,
                     reason=excluded.reason,
                     updated_at=excluded.updated_at""",
                (
                    profile.health_key,
                    profile.profile_id,
                    current + max(1.0, float(cooldown_seconds)),
                    str(reason or "")[:200],
                    current,
                ),
            )
            connection.commit()
        finally:
            connection.close()


def preferred_available_profile(
    profiles: Sequence[LlmProviderProfile],
    health_store: ProviderHealthStore,
) -> LlmProviderProfile | None:
    return next((profile for profile in profiles if health_store.is_available(profile)), None)


def is_provider_failover_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    text = str(exc or "").casefold()
    return status in {401, 402, 403, 429, 503} or any(marker in text for marker in (
        "rate_limit",
        "rate limit",
        "quota",
        "insufficient_quota",
        "capacity",
        "billing",
    ))


def record_last_provider_route(profile: LlmProviderProfile, model_name: str) -> None:
    _LAST_PROVIDER_ROUTE.set({"profile_id": profile.profile_id, "model": str(model_name or "")})


def get_last_provider_route() -> dict[str, str]:
    return dict(_LAST_PROVIDER_ROUTE.get() or {})


_DEFAULT_HEALTH_STORE = ProviderHealthStore()


def get_provider_health_store() -> ProviderHealthStore:
    return _DEFAULT_HEALTH_STORE
