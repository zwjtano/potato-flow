#!/usr/bin/env python3
"""Normalized transcript assets shared by platform captions and ASR providers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .path_policy import atomic_write_text


TRANSCRIPT_SCHEMA_VERSION = "1.0"
LEGACY_TRANSCRIPT_SCHEMA_VERSIONS = frozenset({"0.9"})


@dataclass(frozen=True)
class TranscriptWord:
    start_seconds: float
    end_seconds: float
    text: str
    confidence: float | None = None


@dataclass(frozen=True)
class TranscriptSegment:
    id: str
    start_seconds: float
    end_seconds: float
    text: str
    speaker_id: str = ""
    speaker_name: str = ""
    speaker_confidence: float | None = None
    confidence: float | None = None
    words: tuple[TranscriptWord, ...] = ()
    source_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def time(self) -> float:
        """Compatibility timestamp for existing timeline selection helpers."""
        return float(self.start_seconds)

    @property
    def end_time(self) -> float:
        return float(self.end_seconds)


@dataclass(frozen=True)
class TranscriptDocument:
    source: str
    source_kind: str
    provider: str
    model: str
    recording_start_unix_ts: float
    recording_end_unix_ts: float
    segments: tuple[TranscriptSegment, ...]
    audio_sha256: str = ""
    language: str = ""
    schema_version: str = TRANSCRIPT_SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != TRANSCRIPT_SCHEMA_VERSION:
            raise ValueError(f"unsupported transcript schema: {self.schema_version}")
        if self.recording_end_unix_ts < self.recording_start_unix_ts:
            raise ValueError("recording end precedes recording start")
        previous_start = -1.0
        for segment in self.segments:
            if not segment.id or not segment.text.strip():
                raise ValueError("transcript segments require id and text")
            if segment.start_seconds < 0 or segment.end_seconds < segment.start_seconds:
                raise ValueError("invalid transcript segment timeframe")
            if segment.start_seconds < previous_start:
                raise ValueError("transcript segments must be time ordered")
            previous_start = segment.start_seconds

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.recording_end_unix_ts - self.recording_start_unix_ts)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["segments"] = [asdict(segment) for segment in self.segments]
        return value


def persist_transcript_document(
    document: TranscriptDocument,
    path: Path,
    *,
    private: bool = False,
) -> None:
    """Atomically persist an immutable transcript snapshot for retries/audits."""
    atomic_write_text(
        path,
        json.dumps(document.to_dict(), ensure_ascii=False, indent=2) + "\n",
        private=private,
    )


def transcript_document_from_dict(value: dict[str, Any]) -> TranscriptDocument:
    value = migrate_transcript_document_dict(value)
    segments = []
    for item in value.get("segments", []):
        if not isinstance(item, dict):
            continue
        words = tuple(
            TranscriptWord(**word)
            for word in item.get("words", [])
            if isinstance(word, dict)
        )
        segments.append(TranscriptSegment(
            id=str(item.get("id") or ""),
            start_seconds=float(item.get("start_seconds") or 0),
            end_seconds=float(item.get("end_seconds") or 0),
            text=str(item.get("text") or ""),
            speaker_id=str(item.get("speaker_id") or ""),
            speaker_name=str(item.get("speaker_name") or ""),
            speaker_confidence=item.get("speaker_confidence"),
            confidence=item.get("confidence"),
            words=words,
            source_metadata=dict(item.get("source_metadata") or {}),
        ))
    return TranscriptDocument(
        source=str(value.get("source") or ""),
        source_kind=str(value.get("source_kind") or ""),
        provider=str(value.get("provider") or ""),
        model=str(value.get("model") or ""),
        recording_start_unix_ts=float(value.get("recording_start_unix_ts") or 0),
        recording_end_unix_ts=float(value.get("recording_end_unix_ts") or 0),
        segments=tuple(segments),
        audio_sha256=str(value.get("audio_sha256") or ""),
        language=str(value.get("language") or ""),
        schema_version=str(value.get("schema_version") or TRANSCRIPT_SCHEMA_VERSION),
        metadata=dict(value.get("metadata") or {}),
    )


def migrate_transcript_document_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Upgrade the pre-v1 draft field aliases without mutating the input."""
    migrated = dict(value)
    version = str(migrated.get("schema_version") or TRANSCRIPT_SCHEMA_VERSION)
    if version == TRANSCRIPT_SCHEMA_VERSION:
        return migrated
    if version not in LEGACY_TRANSCRIPT_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported transcript schema: {version}")
    segments = []
    for item in migrated.get("segments", []):
        if not isinstance(item, dict):
            continue
        segment = dict(item)
        segment.setdefault("start_seconds", segment.pop("start", segment.get("time", 0)))
        segment.setdefault("end_seconds", segment.pop("end", segment.get("end_time", 0)))
        words = []
        for raw_word in segment.get("words", []):
            if not isinstance(raw_word, dict):
                continue
            word = dict(raw_word)
            word.setdefault("start_seconds", word.pop("start", 0))
            word.setdefault("end_seconds", word.pop("end", 0))
            words.append(word)
        segment["words"] = words
        segments.append(segment)
    migrated["segments"] = segments
    migrated["schema_version"] = TRANSCRIPT_SCHEMA_VERSION
    metadata = dict(migrated.get("metadata") or {})
    metadata.setdefault("migrated_from_schema", version)
    migrated["metadata"] = metadata
    return migrated


def load_transcript_document(path: Path) -> TranscriptDocument | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        return transcript_document_from_dict(value)
    except (OSError, ValueError, TypeError):
        return None


def _srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def transcript_document_to_srt(document: TranscriptDocument) -> str:
    blocks = []
    for index, segment in enumerate(document.segments, start=1):
        text = str(segment.text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            continue
        blocks.append(
            f"{index}\n{_srt_timestamp(segment.start_seconds)} --> "
            f"{_srt_timestamp(segment.end_seconds)}\n{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def export_transcript_srt(
    document: TranscriptDocument,
    path: Path,
    *,
    private: bool = False,
) -> None:
    """Atomically export a UTF-8 BOM SRT for players and manual review."""
    atomic_write_text(
        path,
        transcript_document_to_srt(document),
        encoding="utf-8-sig",
        private=private,
        newline="",
    )


def transcript_evidence(document: TranscriptDocument) -> list[dict[str, Any]]:
    """Return the compact evidence shape consumed by metadata generation."""
    return [
        {
            "id": segment.id,
            "time": float(segment.start_seconds),
            "end_time": float(segment.end_seconds),
            "text": segment.text,
            "speaker_id": segment.speaker_id,
            "speaker_name": segment.speaker_name,
            "confidence": segment.confidence,
        }
        for segment in document.segments
    ]


def transcript_text(document: TranscriptDocument) -> str:
    return "\n".join(segment.text for segment in document.segments)


def transcript_quality_assessment(document: TranscriptDocument) -> dict[str, Any]:
    """Return source-aware diagnostics without changing resolver priority."""
    source_bases = {
        "sidecar_srt": 0.92,
        "sidecar_vtt": 0.92,
        "embedded_subtitle": 0.84,
        "douyu_asr_caption": 0.74,
        "cloud_asr": 0.70,
    }
    base = float(source_bases.get(document.source, 0.60))
    segment_count = len(document.segments)
    character_count = sum(len(segment.text.strip()) for segment in document.segments)
    span = (
        max(segment.end_seconds for segment in document.segments)
        - min(segment.start_seconds for segment in document.segments)
        if document.segments else 0.0
    )
    duration = document.duration_seconds
    span_ratio = min(1.0, span / duration) if duration > 0 else 0.0
    confidences = [
        float(segment.confidence)
        for segment in document.segments
        if segment.confidence is not None
    ]
    average_confidence = (
        sum(confidences) / len(confidences) if confidences else None
    )
    score = base
    score += min(0.04, segment_count / 500)
    score += min(0.03, character_count / 50_000)
    if average_confidence is not None:
        score += max(-0.10, min(0.05, (average_confidence - 0.70) * 0.25))
    score = round(max(0.0, min(1.0, score)), 4)
    return {
        "score": score,
        "tier": "high" if score >= 0.85 else "medium" if score >= 0.65 else "low",
        "source_base_score": base,
        "segment_count": segment_count,
        "character_count": character_count,
        "timeline_span_ratio": round(span_ratio, 4),
        "average_confidence": (
            round(average_confidence, 4) if average_confidence is not None else None
        ),
    }


def with_transcript_quality(document: TranscriptDocument) -> TranscriptDocument:
    assessment = transcript_quality_assessment(document)
    if document.metadata.get("quality") == assessment:
        return document
    return replace(
        document,
        metadata={**document.metadata, "quality": assessment},
    )


def transcript_document_from_asr_result(
    result: Any,
    *,
    model: str,
    audio_sha256: str,
    recording_start_unix_ts: float = 0.0,
    recording_end_unix_ts: float | None = None,
    source: str = "cloud_asr",
) -> TranscriptDocument:
    """Normalize the existing Whisper/Voxtral result without importing it."""
    normalized_segments: list[TranscriptSegment] = []
    for index, segment in enumerate(getattr(result, "segments", ()) or (), start=1):
        metadata = dict(getattr(segment, "metadata", {}) or {})
        words = tuple(
            TranscriptWord(
                start_seconds=float(getattr(word, "start_s", 0) or 0),
                end_seconds=float(getattr(word, "end_s", 0) or 0),
                text=str(getattr(word, "text", "") or ""),
                confidence=getattr(word, "confidence", None),
            )
            for word in (getattr(segment, "words", ()) or ())
            if str(getattr(word, "text", "") or "").strip()
        )
        start_seconds = float(getattr(segment, "start_s", 0) or 0)
        end_seconds = float(getattr(segment, "end_s", start_seconds) or start_seconds)
        normalized_segments.append(TranscriptSegment(
            id=str(metadata.get("evidence_id") or f"speech:{index:06d}"),
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            text=str(getattr(segment, "text", "") or "").strip(),
            speaker_id=str(metadata.get("speaker_id") or metadata.get("speaker") or ""),
            speaker_name=str(metadata.get("speaker_name") or ""),
            speaker_confidence=metadata.get("speaker_confidence"),
            confidence=getattr(segment, "confidence", None),
            words=words,
            source_metadata=metadata,
        ))
    normalized_segments = [
        segment for segment in normalized_segments if segment.text
    ]
    inferred_end = max(
        (segment.end_seconds for segment in normalized_segments),
        default=0.0,
    )
    recording_end = (
        float(recording_end_unix_ts)
        if recording_end_unix_ts is not None
        else float(recording_start_unix_ts) + inferred_end
    )
    return TranscriptDocument(
        source=source,
        source_kind="asr",
        provider=str(getattr(result, "provider", "") or "unknown"),
        model=str(model or ""),
        recording_start_unix_ts=float(recording_start_unix_ts),
        recording_end_unix_ts=recording_end,
        segments=tuple(normalized_segments),
        audio_sha256=str(audio_sha256 or ""),
        language=str(getattr(result, "language", "") or ""),
        metadata={
            "response_format": str(getattr(result, "response_format", "") or ""),
            "timestamp_mode": str(getattr(result, "timestamp_mode", "") or ""),
            "failure_token": str(getattr(result, "failure_token", "") or ""),
            **dict(getattr(result, "metadata", {}) or {}),
        },
    )


def transcript_document_from_cues(
    cues: list[Any],
    *,
    provider: str,
    model: str,
    audio_sha256: str,
    duration_seconds: float,
    language: str = "",
) -> TranscriptDocument:
    """Normalize finalized subtitle cues produced by the current ASR pipeline."""
    segments = []
    for index, cue in enumerate(cues, start=1):
        if isinstance(cue, dict):
            start = cue.get("start", cue.get("start_seconds", 0))
            end = cue.get("end", cue.get("end_seconds", start))
            text = cue.get("text", "")
        else:
            start = getattr(cue, "start_s", getattr(cue, "start_seconds", 0))
            end = getattr(cue, "end_s", getattr(cue, "end_seconds", start))
            text = getattr(cue, "text", "")
        clean_text = str(text or "").strip()
        if not clean_text:
            continue
        start_seconds = max(0.0, float(start or 0))
        end_seconds = max(start_seconds, float(end or start_seconds))
        segments.append(TranscriptSegment(
            id=f"asr-cue:{index}:{round(start_seconds * 1000)}",
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            text=clean_text,
        ))
    return TranscriptDocument(
        source="cloud_asr",
        source_kind="asr",
        provider=str(provider or "unknown"),
        model=str(model or "unknown"),
        recording_start_unix_ts=0.0,
        recording_end_unix_ts=max(
            float(duration_seconds or 0),
            max((segment.end_seconds for segment in segments), default=0.0),
        ),
        segments=tuple(segments),
        audio_sha256=str(audio_sha256 or ""),
        language=str(language or ""),
        metadata={
            "segment_count": len(segments),
            "normalized_from": "finalized_cues",
        },
    )
