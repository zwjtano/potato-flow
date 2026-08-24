#!/usr/bin/env python3
"""Privacy-preserving ledger for logical AI jobs and physical requests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from .path_policy import enforce_file_mode, ensure_directory
from .utils import get_app_subdir


_CURRENT_LEDGER: ContextVar["AiRequestLedger | None"] = ContextVar(
    "potatoflow_ai_ledger", default=None
)
_CURRENT_RUN_ID: ContextVar[str] = ContextVar("potatoflow_ai_run_id", default="")
_CURRENT_TASK_ID: ContextVar[str] = ContextVar("potatoflow_ai_task_id", default="")
_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()
_RETENTION_LOCK = threading.Lock()
_LAST_RETENTION_CHECK = 0.0


def _utc_timestamp() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def input_fingerprint(*values: Any) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(_json(value).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for block in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def response_usage(response: Any) -> dict[str, int | None]:
    usage = _field(response, "usage")
    input_tokens = _field(usage, "prompt_tokens")
    if input_tokens is None:
        input_tokens = _field(usage, "input_tokens")
    output_tokens = _field(usage, "completion_tokens")
    if output_tokens is None:
        output_tokens = _field(usage, "output_tokens")
    total_tokens = _field(usage, "total_tokens")
    details = _field(usage, "prompt_tokens_details")
    if details is None:
        details = _field(usage, "input_tokens_details")
    cached_tokens = _field(details, "cached_tokens")
    return {
        "input_tokens": int(input_tokens) if input_tokens is not None else None,
        "output_tokens": int(output_tokens) if output_tokens is not None else None,
        "total_tokens": int(total_tokens) if total_tokens is not None else None,
        "cached_tokens": int(cached_tokens) if cached_tokens is not None else None,
    }


def _pricing_table() -> dict[str, Any]:
    raw = str(os.environ.get("POTATOFLOW_AI_PRICING_JSON") or "").strip()
    path = str(os.environ.get("POTATOFLOW_AI_PRICING_PATH") or "").strip()
    try:
        value = json.loads(raw) if raw else (
            json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        )
    except (OSError, TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    models = value.get("models")
    return models if isinstance(models, dict) else value


def resolve_ai_model_pricing(provider: str, model: str) -> dict[str, Any]:
    table = _pricing_table()
    provider_key = str(provider or "").casefold().strip()
    model_key = str(model or "").casefold().strip()
    matched_key = next((
        key for key in (f"{provider_key}/{model_key}", model_key, "*")
        if key and isinstance(table.get(key), dict)
    ), "")
    if not matched_key:
        return {}
    raw = table[matched_key]
    try:
        input_rate = max(0.0, float(raw.get("input_per_million") or 0))
        output_rate = max(0.0, float(raw.get("output_per_million") or 0))
        cached_rate = max(0.0, float(
            raw.get("cached_input_per_million", input_rate)
        ))
    except (TypeError, ValueError):
        return {}
    return {
        "matched_key": matched_key,
        "currency": str(raw.get("currency") or "USD").upper(),
        "input_per_million": input_rate,
        "cached_input_per_million": cached_rate,
        "output_per_million": output_rate,
        "formula_version": "1",
    }


def estimate_ai_request_cost(
    usage: dict[str, int | None],
    pricing: dict[str, Any],
) -> float | None:
    if not pricing or pricing.get("currency") != "USD":
        return None
    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    cached_tokens = min(input_tokens, max(0, int(usage.get("cached_tokens") or 0)))
    regular_input_tokens = input_tokens - cached_tokens
    cost = (
        regular_input_tokens * float(pricing["input_per_million"])
        + cached_tokens * float(pricing["cached_input_per_million"])
        + output_tokens * float(pricing["output_per_million"])
    ) / 1_000_000
    return round(cost, 10)


def provider_request_id(response: Any) -> str:
    direct = _field(response, "id")
    if direct:
        return str(direct)
    headers = _field(response, "headers")
    if headers:
        for key in ("x-request-id", "request-id", "x-trace-id"):
            value = headers.get(key) if hasattr(headers, "get") else None
            if value:
                return str(value)
    return ""


class AiRequestLedger:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = str(os.environ.get("POTATOFLOW_AI_LEDGER_PATH") or "").strip()
        self.path = Path(path or configured or get_app_subdir("db/ai-requests.sqlite3"))

    def _connect(self) -> sqlite3.Connection:
        ensure_directory(self.path.parent, private=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        cache_key = str(self.path.resolve())
        schema_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_runs'"
        ).fetchone() is not None
        if cache_key not in _INITIALIZED_PATHS or not schema_exists:
            with _SCHEMA_LOCK:
                schema_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_runs'"
                ).fetchone() is not None
                if cache_key not in _INITIALIZED_PATHS or not schema_exists:
                    connection.execute("PRAGMA journal_mode=WAL")
                    self._ensure_schema(connection)
                    enforce_file_mode(self.path, private=True)
                    _INITIALIZED_PATHS.add(cache_key)
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back a short-lived connection, then always close it.

        ``sqlite3.Connection``'s own context manager does not close the
        connection.  That leaks a file handle on Windows and prevents ledger
        rotation, cleanup, and temporary-directory removal.
        """
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_runs (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                completed_at REAL,
                kind TEXT NOT NULL,
                scene_name TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                physical_request_count INTEGER NOT NULL DEFAULT 0,
                input_fingerprint TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                error_type TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS ai_requests (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES ai_runs(id) ON DELETE CASCADE,
                attempt INTEGER NOT NULL,
                created_at REAL NOT NULL,
                completed_at REAL,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                provider_request_id TEXT NOT NULL DEFAULT '',
                latency_ms INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cached_tokens INTEGER,
                cost_usd REAL,
                error_type TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_ai_runs_created_at
                ON ai_runs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_requests_run_id
                ON ai_requests(run_id, attempt);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_requests_run_attempt
                ON ai_requests(run_id, attempt);
            """
        )
        connection.commit()

    def begin_run(
        self,
        *,
        kind: str,
        scene_name: str,
        provider: str,
        model: str,
        fingerprint: str,
        task_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO ai_runs
                   (id, created_at, kind, scene_name, provider, model, task_id,
                    status, input_fingerprint, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                (
                    run_id,
                    _utc_timestamp(),
                    kind,
                    scene_name,
                    provider,
                    model,
                    task_id,
                    fingerprint,
                    _json(metadata),
                ),
            )
        return run_id

    def finish_run(self, run_id: str, *, status: str, error: Exception | None = None) -> None:
        if not run_id:
            return
        with self._connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM ai_requests WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            connection.execute(
                """UPDATE ai_runs
                   SET completed_at=?, status=?, physical_request_count=?,
                       error_type=?, error_message=? WHERE id=?""",
                (
                    _utc_timestamp(),
                    status,
                    int(count),
                    error.__class__.__name__ if error else "",
                    str(error)[:1000] if error else "",
                    run_id,
                ),
            )
        self._maybe_prune_expired()

    def prune_expired(
        self,
        retention_days: float,
        *,
        now: float | None = None,
        limit: int = 1000,
    ) -> int:
        days = float(retention_days or 0)
        if days <= 0:
            return 0
        cutoff = (time.time() if now is None else float(now)) - days * 86400
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT id FROM ai_runs
                   WHERE status IN ('completed', 'failed')
                     AND completed_at IS NOT NULL AND completed_at < ?
                   ORDER BY completed_at LIMIT ?""",
                (cutoff, max(1, min(int(limit), 10_000))),
            ).fetchall()
            run_ids = [str(row[0]) for row in rows]
            if run_ids:
                placeholders = ",".join("?" for _value in run_ids)
                connection.execute(
                    f"DELETE FROM ai_runs WHERE id IN ({placeholders})",
                    run_ids,
                )
        return len(run_ids)

    def _maybe_prune_expired(self) -> None:
        global _LAST_RETENTION_CHECK
        try:
            retention_days = float(
                str(os.environ.get("POTATOFLOW_AI_LEDGER_RETENTION_DAYS") or "0")
            )
        except ValueError:
            return
        if retention_days <= 0:
            return
        now = time.time()
        if now - _LAST_RETENTION_CHECK < 6 * 3600:
            return
        with _RETENTION_LOCK:
            if now - _LAST_RETENTION_CHECK < 6 * 3600:
                return
            self.prune_expired(retention_days, now=now)
            _LAST_RETENTION_CHECK = now

    def begin_request(self, run_id: str, *, mode: str, metadata: dict[str, Any] | None = None) -> tuple[str, float]:
        if not run_id:
            return "", time.monotonic()
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_requests WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            ) + 1
            connection.execute(
                """INSERT INTO ai_requests
                   (id, run_id, attempt, created_at, status, mode, metadata_json)
                   VALUES (?, ?, ?, ?, 'running', ?, ?)""",
                (request_id, run_id, attempt, _utc_timestamp(), mode, _json(metadata)),
            )
        return request_id, started

    def finish_request(
        self,
        request_id: str,
        started: float,
        *,
        response: Any = None,
        error: Exception | None = None,
    ) -> None:
        if not request_id:
            return
        usage = response_usage(response)
        with self._connection() as connection:
            row = connection.execute(
                """SELECT r.provider, r.model, q.metadata_json
                   FROM ai_requests q JOIN ai_runs r ON r.id=q.run_id
                   WHERE q.id=?""",
                (request_id,),
            ).fetchone()
            pricing = resolve_ai_model_pricing(row[0], row[1]) if row else {}
            cost_usd = estimate_ai_request_cost(usage, pricing)
            try:
                request_metadata = json.loads(row[2] or "{}") if row else {}
            except (TypeError, ValueError):
                request_metadata = {}
            if pricing:
                request_metadata["pricing_snapshot"] = pricing
            connection.execute(
                """UPDATE ai_requests SET
                   completed_at=?, status=?, provider_request_id=?, latency_ms=?,
                   input_tokens=?, output_tokens=?, total_tokens=?, cached_tokens=?,
                   cost_usd=?, error_type=?, error_message=?, metadata_json=?
                   WHERE id=?""",
                (
                    _utc_timestamp(),
                    "failed" if error else "completed",
                    provider_request_id(response)[:200],
                    max(0, round((time.monotonic() - started) * 1000)),
                    usage["input_tokens"],
                    usage["output_tokens"],
                    usage["total_tokens"],
                    usage["cached_tokens"],
                    cost_usd,
                    error.__class__.__name__ if error else "",
                    str(error)[:1000] if error else "",
                    _json(request_metadata),
                    request_id,
                ),
            )


def get_ai_request_ledger() -> AiRequestLedger:
    return AiRequestLedger()


def summarize_task_ai_usage(task_id: str) -> dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {}
    try:
        ledger = get_ai_request_ledger()
        with ledger._connection() as connection:
            row = connection.execute(
                """SELECT
                       COUNT(DISTINCT r.id),
                       COUNT(q.id),
                       COALESCE(SUM(CASE WHEN q.status='failed' THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(q.input_tokens), 0),
                       COALESCE(SUM(q.output_tokens), 0),
                       COALESCE(SUM(q.total_tokens), 0),
                       COALESCE(SUM(q.cached_tokens), 0),
                       COALESCE(SUM(q.latency_ms), 0),
                       COALESCE(SUM(q.cost_usd), 0)
                   FROM ai_runs r
                   LEFT JOIN ai_requests q ON q.run_id=r.id
                   WHERE r.task_id=?""",
                (task_key,),
            ).fetchone()
    except Exception:
        return {}
    return {
        "logical_request_count": int(row[0] or 0),
        "physical_request_count": int(row[1] or 0),
        "failed_physical_request_count": int(row[2] or 0),
        "input_tokens": int(row[3] or 0),
        "output_tokens": int(row[4] or 0),
        "total_tokens": int(row[5] or 0),
        "cached_tokens": int(row[6] or 0),
        "physical_latency_ms": int(row[7] or 0),
        "cost_usd": round(float(row[8] or 0), 10),
    }


@contextmanager
def ai_task_context(task_id: str) -> Iterator[None]:
    token = _CURRENT_TASK_ID.set(str(task_id or "").strip())
    try:
        yield
    finally:
        _CURRENT_TASK_ID.reset(token)


def begin_current_physical_request(
    *,
    mode: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[AiRequestLedger | None, str, float]:
    ledger = _CURRENT_LEDGER.get()
    run_id = _CURRENT_RUN_ID.get()
    if not ledger or not run_id:
        return None, "", time.monotonic()
    try:
        request_id, started = ledger.begin_request(
            run_id,
            mode=mode,
            metadata=metadata,
        )
        return ledger, request_id, started
    except Exception:
        return None, "", time.monotonic()


def finish_current_physical_request(
    handle: tuple[AiRequestLedger | None, str, float],
    *,
    response: Any = None,
    error: Exception | None = None,
) -> None:
    ledger, request_id, started = handle
    if not ledger or not request_id:
        return
    try:
        ledger.finish_request(
            request_id,
            started,
            response=response,
            error=error,
        )
    except Exception:
        pass


@contextmanager
def logical_ai_request(
    *,
    kind: str,
    scene_name: str,
    provider: str,
    model: str,
    fingerprint: str,
    task_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> Iterator[tuple[AiRequestLedger | None, str]]:
    """Yield a ledger/run pair and never let observability break production."""
    ledger: AiRequestLedger | None = None
    run_id = ""
    effective_task_id = str(task_id or _CURRENT_TASK_ID.get() or "").strip()
    try:
        ledger = get_ai_request_ledger()
        run_id = ledger.begin_run(
            kind=kind,
            scene_name=scene_name,
            provider=provider,
            model=model,
            fingerprint=fingerprint,
            task_id=effective_task_id,
            metadata=metadata,
        )
    except Exception:
        ledger = None
        run_id = ""
    ledger_token = _CURRENT_LEDGER.set(ledger)
    run_token = _CURRENT_RUN_ID.set(run_id)
    try:
        yield ledger, run_id
    except Exception as exc:
        if ledger and run_id:
            try:
                ledger.finish_run(run_id, status="failed", error=exc)
            except Exception:
                pass
        raise
    else:
        if ledger and run_id:
            try:
                ledger.finish_run(run_id, status="completed")
            except Exception:
                pass
    finally:
        _CURRENT_RUN_ID.reset(run_token)
        _CURRENT_LEDGER.reset(ledger_token)
