#!/usr/bin/env python3
"""Capability-first registry, policy matching, and shared provider circuits."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from .path_policy import enforce_file_mode, ensure_directory
from .utils import get_app_subdir


class ProviderKind(StrEnum):
    LLM = "llm"
    ASR = "asr"
    IMAGE = "image"


@dataclass(frozen=True)
class ProviderRequest:
    kind: ProviderKind
    required_capabilities: frozenset[str] = frozenset()
    preferred_capabilities: frozenset[str] = frozenset()
    scene_name: str = ""
    constraints: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderRegistration:
    provider_id: str
    kind: ProviderKind
    adapter: str
    capabilities: frozenset[str]
    priority: int
    enabled: bool = True
    scenes: tuple[str, ...] = ("*",)
    billing_mode: str = "api_key"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def supports_scene(self, scene_name: str) -> bool:
        return not scene_name or not self.scenes or "*" in self.scenes or scene_name in self.scenes


class ProviderRegistry:
    def __init__(self, registrations: Iterable[ProviderRegistration] = ()):
        self._registrations: list[ProviderRegistration] = list(registrations)

    def register(self, registration: ProviderRegistration) -> None:
        self._registrations.append(registration)

    def all(self, kind: ProviderKind | None = None) -> tuple[ProviderRegistration, ...]:
        values = self._registrations
        if kind is not None:
            values = [item for item in values if item.kind == kind]
        return tuple(sorted(values, key=lambda item: item.priority))

    def match(self, request: ProviderRequest) -> tuple[ProviderRegistration, ...]:
        billing_mode = str(request.constraints.get("billing_mode") or "").strip()
        matches = []
        for item in self.all(request.kind):
            if not item.enabled or not item.supports_scene(request.scene_name):
                continue
            if not request.required_capabilities.issubset(item.capabilities):
                continue
            if billing_mode and billing_mode != item.billing_mode:
                continue
            preferred_score = len(request.preferred_capabilities & item.capabilities)
            matches.append((item.priority, -preferred_score, item))
        return tuple(item for _priority, _score, item in sorted(matches, key=lambda value: value[:2]))


class CircuitProfile(Protocol):
    profile_id: str
    health_key: str


_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()


class ProviderCircuitStore:
    """Private SQLite circuit state shared by every PotatoFlow process."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = str(os.environ.get("POTATOFLOW_PROVIDER_CIRCUIT_PATH") or "").strip()
        self.path = Path(path or configured or get_app_subdir("db/provider-circuits.sqlite3"))

    def _connect(self) -> sqlite3.Connection:
        ensure_directory(self.path.parent, private=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        path_key = str(self.path.resolve())
        if path_key not in _INITIALIZED_PATHS:
            with _SCHEMA_LOCK:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS provider_circuits (
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

    def is_available(self, profile: CircuitProfile, *, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT unavailable_until FROM provider_circuits WHERE health_key=?",
                (profile.health_key,),
            ).fetchone()
        finally:
            connection.close()
        return row is None or float(row[0] or 0.0) <= current

    def mark_unavailable(
        self,
        profile: CircuitProfile,
        *,
        cooldown_seconds: float,
        reason: str,
        now: float | None = None,
    ) -> None:
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute(
                """INSERT INTO provider_circuits
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


class ProviderRouter:
    """Return eligible providers; execution/retry remains owned by the adapter layer."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        health_check: Callable[[ProviderRegistration], bool] | None = None,
        quota_check: Callable[[ProviderRegistration, ProviderRequest], bool] | None = None,
    ):
        self.registry = registry
        self.health_check = health_check
        self.quota_check = quota_check

    def route(self, request: ProviderRequest) -> tuple[ProviderRegistration, ...]:
        values = []
        for item in self.registry.match(request):
            if self.health_check is not None and not self.health_check(item):
                continue
            if self.quota_check is not None and not self.quota_check(item, request):
                continue
            values.append(item)
        return tuple(values)


def build_provider_registry(config: Mapping[str, Any]) -> ProviderRegistry:
    """Expose the effective LLM/ASR/Image control-plane inventory."""
    from .image_provider_registry import resolve_image_provider_profiles
    from .llm_provider_registry import resolve_provider_profiles

    registry = ProviderRegistry()
    for profile in resolve_provider_profiles(config, scene_name=""):
        registry.register(ProviderRegistration(
            provider_id=profile.profile_id,
            kind=ProviderKind.LLM,
            adapter="openai-compatible",
            capabilities=frozenset({"llm.chat", "llm.structured_output"}),
            priority=profile.priority,
            scenes=profile.scenes,
            metadata={"model": profile.model_name, "rpm_limit": profile.rpm_limit},
        ))
    for profile in resolve_image_provider_profiles(config, scene_name=""):
        capabilities = {"image.generate"}
        if profile.supports_edit:
            capabilities.add("image.edit")
        registry.register(ProviderRegistration(
            provider_id=profile.profile_id,
            kind=ProviderKind.IMAGE,
            adapter=profile.adapter,
            capabilities=frozenset(capabilities),
            priority=profile.priority,
            scenes=profile.scenes,
            metadata={"model": profile.model_name},
        ))
    if str(config.get("GROQ_API_KEY") or "").strip():
        registry.register(ProviderRegistration(
            provider_id="groq",
            kind=ProviderKind.ASR,
            adapter="openai-compatible-audio",
            capabilities=frozenset({"asr.transcribe", "asr.segment_timestamp", "asr.word_timestamp"}),
            priority=1,
            billing_mode="free_only",
            metadata={"model": str(config.get("GROQ_ASR_MODEL_NAME") or "whisper-large-v3-turbo")},
        ))
    if str(config.get("CLOUDFLARE_API_TOKEN") or "").strip() and str(config.get("CLOUDFLARE_ACCOUNT_ID") or "").strip():
        registry.register(ProviderRegistration(
            provider_id="cloudflare",
            kind=ProviderKind.ASR,
            adapter="cloudflare-workers-ai",
            capabilities=frozenset({"asr.transcribe", "asr.segment_timestamp"}),
            priority=2,
            billing_mode="free_only",
            metadata={"model": str(config.get("CLOUDFLARE_ASR_MODEL_NAME") or "@cf/openai/whisper-large-v3-turbo")},
        ))
    return registry
