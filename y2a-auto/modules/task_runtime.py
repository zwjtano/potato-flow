"""SQLite-backed worker leases for ordinary upload tasks."""

from __future__ import annotations

import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable, Optional


DEFAULT_LEASE_SECONDS = 90
PROCESS_OWNER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class TaskLease:
    task_id: str
    owner_id: str
    acquired_at: float
    heartbeat_at: float
    lease_until: float


class TaskLeaseStore:
    """Own task execution with renewable, expiring SQLite leases."""

    def __init__(
        self,
        db_path: str,
        *,
        owner_id: str = PROCESS_OWNER_ID,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        self.db_path = str(db_path)
        self.owner_id = str(owner_id)
        self.lease_seconds = max(15, int(lease_seconds))
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def ensure_schema(self, conn: Optional[sqlite3.Connection] = None) -> None:
        owns_connection = conn is None
        active_conn = conn or self._connect()
        try:
            active_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_worker_leases (
                    task_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    lease_until REAL NOT NULL
                )
                """
            )
            active_conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_worker_leases_expiry
                ON task_worker_leases(lease_until)
                """
            )
            active_conn.commit()
        finally:
            if owns_connection:
                active_conn.close()

    def acquire(self, task_id: str) -> bool:
        if not task_id:
            return False
        now = float(self._clock())
        lease_until = now + self.lease_seconds
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_id, acquired_at, lease_until "
                "FROM task_worker_leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row and row["owner_id"] != self.owner_id and float(row["lease_until"]) > now:
                return False
            acquired_at = (
                float(row["acquired_at"])
                if row and row["owner_id"] == self.owner_id
                else now
            )
            conn.execute(
                """
                INSERT INTO task_worker_leases
                    (task_id, owner_id, acquired_at, heartbeat_at, lease_until)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    acquired_at = excluded.acquired_at,
                    heartbeat_at = excluded.heartbeat_at,
                    lease_until = excluded.lease_until
                """,
                (task_id, self.owner_id, acquired_at, now, lease_until),
            )
            return True

    def heartbeat(self, task_ids: Iterable[str]) -> int:
        normalized = sorted({str(task_id) for task_id in task_ids if task_id})
        if not normalized:
            return 0
        now = float(self._clock())
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE task_worker_leases
                SET heartbeat_at = ?, lease_until = ?
                WHERE owner_id = ? AND task_id IN ({placeholders})
                """,
                (now, now + self.lease_seconds, self.owner_id, *normalized),
            )
            return int(cursor.rowcount or 0)

    def release(self, task_id: str) -> bool:
        if not task_id:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM task_worker_leases WHERE task_id = ? AND owner_id = ?",
                (task_id, self.owner_id),
            )
            return bool(cursor.rowcount)

    def release_owner(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM task_worker_leases WHERE owner_id = ?",
                (self.owner_id,),
            )
            return int(cursor.rowcount or 0)

    def is_live(self, task_id: str) -> bool:
        if not task_id:
            return False
        now = float(self._clock())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT lease_until FROM task_worker_leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return bool(row and float(row["lease_until"]) > now)

    def get(self, task_id: str) -> Optional[TaskLease]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_worker_leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        return TaskLease(
            task_id=str(row["task_id"]),
            owner_id=str(row["owner_id"]),
            acquired_at=float(row["acquired_at"]),
            heartbeat_at=float(row["heartbeat_at"]),
            lease_until=float(row["lease_until"]),
        )
