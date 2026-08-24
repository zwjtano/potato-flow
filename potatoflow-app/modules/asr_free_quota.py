#!/usr/bin/env python3
"""Conservative cross-process quota ledger for explicitly free ASR routes."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .path_policy import enforce_file_mode, ensure_directory
from .utils import get_app_subdir


_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()


class FreeAsrQuotaLedger:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = str(os.environ.get("POTATOFLOW_ASR_QUOTA_PATH") or "").strip()
        self.path = Path(path or configured or get_app_subdir("db/asr-free-quota.sqlite3"))

    def _connect(self) -> sqlite3.Connection:
        ensure_directory(self.path.parent, private=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        cache_key = str(self.path.resolve())
        if cache_key not in _INITIALIZED_PATHS:
            with _SCHEMA_LOCK:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS asr_quota_reservations (
                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                           provider_id TEXT NOT NULL,
                           reserved_at REAL NOT NULL,
                           audio_seconds REAL NOT NULL,
                           task_id TEXT NOT NULL DEFAULT ''
                       )"""
                )
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS idx_asr_quota_provider_time
                       ON asr_quota_reservations(provider_id, reserved_at)"""
                )
                connection.commit()
                enforce_file_mode(self.path, private=True)
                _INITIALIZED_PATHS.add(cache_key)
        return connection

    def reserve(
        self,
        provider_id: str,
        audio_seconds: float,
        *,
        daily_limit_seconds: float,
        hourly_limit_seconds: float = 0,
        rpm_limit: int = 0,
        task_id: str = "",
        now: float | None = None,
    ) -> bool:
        """Atomically reserve quota without ever exceeding configured limits."""
        seconds = max(0.01, float(audio_seconds or 0.0))
        daily_limit = max(0.0, float(daily_limit_seconds or 0.0))
        hourly_limit = max(0.0, float(hourly_limit_seconds or 0.0))
        request_limit = max(0, int(rpm_limit or 0))
        if not provider_id or daily_limit <= 0:
            return False
        current = time.time() if now is None else float(now)
        day_start = current - 86_400.0
        hour_start = current - 3_600.0
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM asr_quota_reservations WHERE reserved_at < ?",
                (day_start,),
            )
            daily_used = float(connection.execute(
                """SELECT COALESCE(SUM(audio_seconds), 0)
                   FROM asr_quota_reservations
                   WHERE provider_id=? AND reserved_at>=?""",
                (provider_id, day_start),
            ).fetchone()[0] or 0.0)
            hourly_used = 0.0
            if hourly_limit > 0:
                hourly_used = float(connection.execute(
                    """SELECT COALESCE(SUM(audio_seconds), 0)
                       FROM asr_quota_reservations
                       WHERE provider_id=? AND reserved_at>=?""",
                    (provider_id, hour_start),
                ).fetchone()[0] or 0.0)
            recent_requests = 0
            if request_limit > 0:
                recent_requests = int(connection.execute(
                    """SELECT COUNT(*) FROM asr_quota_reservations
                       WHERE provider_id=? AND reserved_at>=?""",
                    (provider_id, current - 60.0),
                ).fetchone()[0] or 0)
            allowed = (
                daily_used + seconds <= daily_limit
                and (hourly_limit <= 0 or hourly_used + seconds <= hourly_limit)
                and (request_limit <= 0 or recent_requests < request_limit)
            )
            if allowed:
                connection.execute(
                    """INSERT INTO asr_quota_reservations
                       (provider_id, reserved_at, audio_seconds, task_id)
                       VALUES (?, ?, ?, ?)""",
                    (provider_id, current, seconds, str(task_id or "")),
                )
            connection.commit()
            return allowed
        finally:
            connection.close()

    def usage(self, provider_id: str, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT
                     COALESCE(SUM(CASE WHEN reserved_at>=? THEN audio_seconds ELSE 0 END), 0),
                     COALESCE(SUM(CASE WHEN reserved_at>=? THEN audio_seconds ELSE 0 END), 0)
                   FROM asr_quota_reservations WHERE provider_id=?""",
                (current - 3_600.0, current - 86_400.0, provider_id),
            ).fetchone()
        finally:
            connection.close()
        return {"hour_seconds": float(row[0] or 0.0), "day_seconds": float(row[1] or 0.0)}


_DEFAULT_LEDGER = FreeAsrQuotaLedger()


def get_free_asr_quota_ledger() -> FreeAsrQuotaLedger:
    return _DEFAULT_LEDGER
