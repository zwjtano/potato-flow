#!/usr/bin/env python3
"""Timestamped, searchable evidence storage with progressive disclosure APIs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .path_policy import enforce_file_mode, ensure_directory
from .utils import get_app_subdir


_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    recording_id: str
    type: str
    start_seconds: float
    end_seconds: float
    content: str
    confidence: float | None = None
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateEvent:
    event_id: str
    recording_id: str
    start_seconds: float
    end_seconds: float
    summary: str
    confidence: float
    evidence_refs: tuple[str, ...]


class EvidenceStore:
    """SQLite source of truth; large evidence is queried on demand, never prompt-dumped."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = str(os.environ.get("POTATOFLOW_EVIDENCE_DB_PATH") or "").strip()
        self.path = Path(path or configured or get_app_subdir("db/evidence.sqlite3"))

    def _connect(self) -> sqlite3.Connection:
        ensure_directory(self.path.parent, private=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        path_key = str(self.path.resolve())
        if path_key not in _INITIALIZED_PATHS:
            with _SCHEMA_LOCK:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS transcript_documents (
                        recording_id TEXT PRIMARY KEY,
                        schema_version TEXT NOT NULL,
                        audio_sha256 TEXT NOT NULL DEFAULT '',
                        provider TEXT NOT NULL DEFAULT '',
                        model TEXT NOT NULL DEFAULT '',
                        language TEXT NOT NULL DEFAULT '',
                        duration_seconds REAL NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS transcript_segments (
                        recording_id TEXT NOT NULL,
                        segment_id TEXT NOT NULL,
                        start_s REAL NOT NULL,
                        end_s REAL NOT NULL,
                        raw_text TEXT NOT NULL,
                        normalized_text TEXT NOT NULL,
                        provider TEXT NOT NULL DEFAULT '',
                        confidence REAL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        PRIMARY KEY (recording_id, segment_id)
                    );
                    CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
                        recording_id UNINDEXED,
                        segment_id UNINDEXED,
                        raw_text,
                        normalized_text,
                        tokenize='unicode61'
                    );
                    CREATE TABLE IF NOT EXISTS visual_frames (
                        frame_id TEXT PRIMARY KEY,
                        recording_id TEXT NOT NULL,
                        timestamp_s REAL NOT NULL,
                        frame_path TEXT NOT NULL,
                        frame_sha256 TEXT NOT NULL,
                        perceptual_hash TEXT NOT NULL DEFAULT '',
                        source_trigger TEXT NOT NULL DEFAULT '',
                        ocr_model_version TEXT NOT NULL DEFAULT '',
                        UNIQUE (recording_id, timestamp_s, ocr_model_version)
                    );
                    CREATE TABLE IF NOT EXISTS ocr_frame_cache (
                        frame_sha256 TEXT NOT NULL,
                        ocr_model_version TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (frame_sha256, ocr_model_version)
                    );
                    CREATE TABLE IF NOT EXISTS evidence_records (
                        evidence_id TEXT PRIMARY KEY,
                        recording_id TEXT NOT NULL,
                        type TEXT NOT NULL,
                        start_s REAL NOT NULL,
                        end_s REAL NOT NULL,
                        content TEXT NOT NULL,
                        confidence REAL,
                        source TEXT NOT NULL DEFAULT '',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_evidence_recording_time
                        ON evidence_records(recording_id, start_s, type);
                    CREATE TABLE IF NOT EXISTS evidence_events (
                        event_id TEXT PRIMARY KEY,
                        recording_id TEXT NOT NULL,
                        start_s REAL NOT NULL,
                        end_s REAL NOT NULL,
                        summary TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        evidence_refs_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_event_recording_time
                        ON evidence_events(recording_id, start_s);
                    """
                )
                connection.commit()
                enforce_file_mode(self.path, private=True)
                _INITIALIZED_PATHS.add(path_key)
        return connection

    def ingest_transcript(self, recording_id: str, document: Any) -> int:
        recording_id = str(recording_id or "").strip()
        if not recording_id:
            raise ValueError("recording_id is required")
        now = time.time()
        segments = tuple(getattr(document, "segments", ()) or ())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM transcript_fts WHERE recording_id=?", (recording_id,))
            connection.execute("DELETE FROM transcript_segments WHERE recording_id=?", (recording_id,))
            connection.execute(
                """INSERT INTO transcript_documents
                   (recording_id, schema_version, audio_sha256, provider, model,
                    language, duration_seconds, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(recording_id) DO UPDATE SET
                    schema_version=excluded.schema_version,
                    audio_sha256=excluded.audio_sha256,
                    provider=excluded.provider,
                    model=excluded.model,
                    language=excluded.language,
                    duration_seconds=excluded.duration_seconds,
                    source=excluded.source,
                    updated_at=excluded.updated_at""",
                (
                    recording_id,
                    str(getattr(document, "schema_version", "1.0")),
                    str(getattr(document, "audio_sha256", "")),
                    str(getattr(document, "provider", "")),
                    str(getattr(document, "model", "")),
                    str(getattr(document, "language", "")),
                    float(getattr(document, "duration_seconds", 0) or 0),
                    str(getattr(document, "source", "")),
                    now,
                ),
            )
            for segment in segments:
                metadata = dict(getattr(segment, "source_metadata", {}) or {})
                normalized = str(getattr(segment, "text", "") or "").strip()
                raw = str(metadata.get("raw_text") or normalized).strip()
                if not normalized:
                    continue
                values = (
                    recording_id,
                    str(getattr(segment, "id", "")),
                    float(getattr(segment, "start_seconds", 0) or 0),
                    float(getattr(segment, "end_seconds", 0) or 0),
                    raw,
                    normalized,
                    str(getattr(document, "provider", "")),
                    getattr(segment, "confidence", None),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                )
                connection.execute(
                    """INSERT INTO transcript_segments
                       (recording_id, segment_id, start_s, end_s, raw_text,
                        normalized_text, provider, confidence, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                connection.execute(
                    """INSERT INTO transcript_fts
                       (recording_id, segment_id, raw_text, normalized_text)
                       VALUES (?, ?, ?, ?)""",
                    (recording_id, values[1], raw, normalized),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return len(segments)

    def search_speech(self, recording_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(100, int(limit)))
        connection = self._connect()
        try:
            try:
                rows = connection.execute(
                    """SELECT s.segment_id, s.start_s, s.end_s, s.raw_text,
                              s.normalized_text, s.provider, s.confidence
                       FROM transcript_fts f
                       JOIN transcript_segments s
                         ON s.recording_id=f.recording_id AND s.segment_id=f.segment_id
                       WHERE f.recording_id=? AND transcript_fts MATCH ?
                       ORDER BY bm25(transcript_fts), s.start_s
                       LIMIT ?""",
                    (str(recording_id), str(query), safe_limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # User-entered punctuation can be invalid FTS syntax. A bounded
                # LIKE fallback is slower but keeps the retrieval tool safe.
                escaped_query = str(query).replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped_query}%"
                rows = connection.execute(
                    """SELECT segment_id, start_s, end_s, raw_text,
                              normalized_text, provider, confidence
                       FROM transcript_segments
                       WHERE recording_id=?
                         AND (raw_text LIKE ? ESCAPE '\\'
                              OR normalized_text LIKE ? ESCAPE '\\')
                       ORDER BY start_s LIMIT ?""",
                    (str(recording_id), pattern, pattern, safe_limit),
                ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def read_speech_segments(
        self,
        recording_id: str,
        *,
        start_seconds: float,
        end_seconds: float,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(500, int(limit)))
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT segment_id, start_s, end_s, raw_text, normalized_text,
                          provider, confidence
                   FROM transcript_segments
                   WHERE recording_id=? AND end_s>=? AND start_s<=?
                   ORDER BY start_s LIMIT ?""",
                (str(recording_id), float(start_seconds), float(end_seconds), safe_limit),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def upsert_evidence(self, evidence: EvidenceRecord) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """INSERT INTO evidence_records
                   (evidence_id, recording_id, type, start_s, end_s, content,
                    confidence, source, metadata_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(evidence_id) DO UPDATE SET
                    recording_id=excluded.recording_id, type=excluded.type,
                    start_s=excluded.start_s, end_s=excluded.end_s,
                    content=excluded.content, confidence=excluded.confidence,
                    source=excluded.source, metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at""",
                (
                    evidence.evidence_id,
                    evidence.recording_id,
                    evidence.type,
                    float(evidence.start_seconds),
                    float(evidence.end_seconds),
                    evidence.content,
                    evidence.confidence,
                    evidence.source,
                    json.dumps(evidence.metadata, ensure_ascii=False, sort_keys=True),
                    time.time(),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def find_visual_frame(self, frame_sha256: str, ocr_model_version: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT frame_id, recording_id, timestamp_s, frame_path,
                          perceptual_hash, source_trigger
                   FROM visual_frames
                   WHERE frame_sha256=? AND ocr_model_version=?""",
                (str(frame_sha256), str(ocr_model_version)),
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row is not None else None

    def get_cached_ocr(self, frame_sha256: str, ocr_model_version: str) -> list[dict[str, Any]] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT result_json FROM ocr_frame_cache
                   WHERE frame_sha256=? AND ocr_model_version=?""",
                (str(frame_sha256), str(ocr_model_version)),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        try:
            value = json.loads(str(row[0]))
        except (TypeError, json.JSONDecodeError):
            return None
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else None

    def put_cached_ocr(
        self,
        frame_sha256: str,
        ocr_model_version: str,
        items: Iterable[dict[str, Any]],
    ) -> None:
        payload = json.dumps(list(items), ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute(
                """INSERT INTO ocr_frame_cache
                   (frame_sha256, ocr_model_version, result_json, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(frame_sha256, ocr_model_version) DO UPDATE SET
                    result_json=excluded.result_json,
                    created_at=excluded.created_at""",
                (str(frame_sha256), str(ocr_model_version), payload, time.time()),
            )
            connection.commit()
        finally:
            connection.close()

    def upsert_visual_frame(
        self,
        *,
        frame_id: str,
        recording_id: str,
        timestamp_seconds: float,
        frame_path: str,
        frame_sha256: str,
        perceptual_hash: str,
        source_trigger: str,
        ocr_model_version: str,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """INSERT INTO visual_frames
                   (frame_id, recording_id, timestamp_s, frame_path, frame_sha256,
                    perceptual_hash, source_trigger, ocr_model_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(frame_id) DO UPDATE SET
                    recording_id=excluded.recording_id,
                    timestamp_s=excluded.timestamp_s,
                    frame_path=excluded.frame_path,
                    frame_sha256=excluded.frame_sha256,
                    perceptual_hash=excluded.perceptual_hash,
                    source_trigger=excluded.source_trigger,
                    ocr_model_version=excluded.ocr_model_version""",
                (
                    frame_id,
                    recording_id,
                    float(timestamp_seconds),
                    frame_path,
                    frame_sha256,
                    perceptual_hash,
                    source_trigger,
                    ocr_model_version,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def upsert_event(self, event: CandidateEvent) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """INSERT INTO evidence_events
                   (event_id, recording_id, start_s, end_s, summary, confidence,
                    evidence_refs_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_id) DO UPDATE SET
                    recording_id=excluded.recording_id, start_s=excluded.start_s,
                    end_s=excluded.end_s, summary=excluded.summary,
                    confidence=excluded.confidence,
                    evidence_refs_json=excluded.evidence_refs_json,
                    updated_at=excluded.updated_at""",
                (
                    event.event_id,
                    event.recording_id,
                    float(event.start_seconds),
                    float(event.end_seconds),
                    event.summary,
                    float(event.confidence),
                    json.dumps(event.evidence_refs, ensure_ascii=False),
                    time.time(),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def recording_summary(self, recording_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            transcript = connection.execute(
                """SELECT duration_seconds, provider, model,
                          (SELECT COUNT(*) FROM transcript_segments s
                           WHERE s.recording_id=d.recording_id) AS speech_segments
                   FROM transcript_documents d WHERE recording_id=?""",
                (str(recording_id),),
            ).fetchone()
            counts = connection.execute(
                """SELECT type, COUNT(*) AS count FROM evidence_records
                   WHERE recording_id=? GROUP BY type""",
                (str(recording_id),),
            ).fetchall()
            events = connection.execute(
                "SELECT COUNT(*) FROM evidence_events WHERE recording_id=?",
                (str(recording_id),),
            ).fetchone()[0]
        finally:
            connection.close()
        summary = dict(transcript) if transcript is not None else {
            "duration_seconds": 0,
            "provider": "",
            "model": "",
            "speech_segments": 0,
        }
        summary["evidence_counts"] = {str(row["type"]): int(row["count"]) for row in counts}
        summary["candidate_events"] = int(events)
        return summary


_DEFAULT_STORE = EvidenceStore()


def get_evidence_store() -> EvidenceStore:
    return _DEFAULT_STORE
