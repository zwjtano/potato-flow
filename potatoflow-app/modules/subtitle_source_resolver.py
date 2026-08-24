#!/usr/bin/env python3
"""Resolve the best available subtitle source into TranscriptDocument v1."""

from __future__ import annotations

import re
import json
import subprocess
from pathlib import Path

from .douyu_stats_formatter import (
    get_ai_captions_for_recording,
    recording_timeframe,
)
from .transcript_document import (
    TranscriptDocument,
    TranscriptSegment,
    load_transcript_document,
    persist_transcript_document,
    with_transcript_quality,
)


_SRT_TIMING = re.compile(
    r"(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{1,3})"
)


def _subtitle_seconds(value: str) -> float:
    parts = str(value).replace(",", ".").split(":")
    if len(parts) == 2:
        hours = 0
        minutes, remainder = parts
    elif len(parts) == 3:
        hours, minutes, remainder = parts
    else:
        raise ValueError(f"invalid subtitle timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(remainder)


def _sidecar_candidates(video_dir: Path, video_path: str | Path | None) -> list[Path]:
    if not video_path:
        return []
    video = Path(video_path)
    candidates = [
        video.with_suffix(".srt"),
        video.with_name(f"{video.stem}_AI字幕.srt"),
        video.with_name(f"{video.stem}.ai.srt"),
        video.with_suffix(".zh.srt"),
        video.with_suffix(".zh-CN.srt"),
        video.with_suffix(".vtt"),
        video.with_name(f"{video.stem}_AI字幕.vtt"),
        video.with_name(f"{video.stem}.ai.vtt"),
        video.with_suffix(".zh.vtt"),
        video.with_suffix(".zh-CN.vtt"),
    ]
    return list(dict.fromkeys(path for path in candidates if path.is_file()))


def transcript_document_path(
    video_dir: str | Path,
    video_path: str | Path | None = None,
) -> Path:
    root = Path(video_dir)
    if video_path:
        return root / ".potato-flow" / f"{Path(video_path).stem}.transcript.json"
    return root / ".potato-flow" / "transcript.json"


def _segments_from_srt(text: str, *, source_reference: str) -> tuple[TranscriptSegment, ...]:
    segments: list[TranscriptSegment] = []
    for block_index, block in enumerate(re.split(r"\r?\n\s*\r?\n", text), start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next(
            (index for index, line in enumerate(lines) if _SRT_TIMING.search(line)),
            None,
        )
        if timing_index is None:
            continue
        match = _SRT_TIMING.search(lines[timing_index])
        if match is None:
            continue
        body = re.sub(r"<[^>]+>", "", " ".join(lines[timing_index + 1:])).strip()
        if not body:
            continue
        start_seconds = _subtitle_seconds(match.group("start"))
        end_seconds = _subtitle_seconds(match.group("end"))
        if end_seconds < start_seconds:
            continue
        segments.append(TranscriptSegment(
            id=f"subtitle:{block_index}:{round(start_seconds * 1000)}",
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            text=body,
            source_metadata={"source_reference": source_reference},
        ))
    return tuple(segments)


def _document_from_srt(
    subtitle_path: Path,
    *,
    recording_start: float,
    recording_end: float,
) -> TranscriptDocument | None:
    text = subtitle_path.read_text(encoding="utf-8-sig", errors="replace")
    segments = _segments_from_srt(text, source_reference=str(subtitle_path))
    if not segments:
        return None
    is_vtt = subtitle_path.suffix.casefold() == ".vtt"
    return TranscriptDocument(
        source="sidecar_vtt" if is_vtt else "sidecar_srt",
        source_kind="sidecar_subtitle",
        provider="local",
        model="webvtt" if is_vtt else "srt",
        recording_start_unix_ts=recording_start,
        recording_end_unix_ts=max(
            recording_end,
            recording_start + max(segment.end_seconds for segment in segments),
        ),
        segments=segments,
        metadata={
            "subtitle_path": str(subtitle_path),
            "segment_count": len(segments),
        },
    )


def _embedded_srt(
    video_path: Path,
    *,
    ffprobe: str,
    ffmpeg: str,
) -> tuple[str, dict] | None:
    if not video_path.is_file():
        return None
    probe = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "s",
            "-show_entries", "stream=index,codec_name:stream_tags=language,title",
            "-of", "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if probe.returncode != 0:
        return None
    try:
        streams = json.loads(probe.stdout or "{}").get("streams", [])
    except (TypeError, ValueError):
        return None
    streams = [stream for stream in streams if isinstance(stream, dict)]
    if not streams:
        return None

    def preference(stream: dict) -> tuple[int, int]:
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        language = str(tags.get("language") or "").casefold()
        title = str(tags.get("title") or "").casefold()
        chinese = language in {"chi", "zho", "zh", "zh-cn", "zh-hans"}
        manual = not any(marker in title for marker in ("auto", "自动", "asr"))
        return (int(chinese) * 2 + int(manual), -int(stream.get("index") or 0))

    selected = max(streams, key=preference)
    stream_index = int(selected.get("index") or 0)
    extracted = subprocess.run(
        [
            ffmpeg,
            "-v", "error",
            "-i", str(video_path),
            "-map", f"0:{stream_index}",
            "-f", "srt",
            "pipe:1",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if extracted.returncode != 0 or not str(extracted.stdout or "").strip():
        return None
    return str(extracted.stdout), selected


def _document_from_embedded_subtitle(
    video_path: Path,
    *,
    recording_start: float,
    recording_end: float,
    ffprobe: str,
    ffmpeg: str,
) -> TranscriptDocument | None:
    try:
        extracted = _embedded_srt(video_path, ffprobe=ffprobe, ffmpeg=ffmpeg)
    except (OSError, subprocess.SubprocessError):
        return None
    if extracted is None:
        return None
    srt_text, stream = extracted
    segments = _segments_from_srt(
        srt_text,
        source_reference=f"{video_path}#stream:{int(stream.get('index') or 0)}",
    )
    if not segments:
        return None
    return TranscriptDocument(
        source="embedded_subtitle",
        source_kind="embedded_subtitle",
        provider="media_container",
        model=str(stream.get("codec_name") or "subtitle_stream"),
        recording_start_unix_ts=recording_start,
        recording_end_unix_ts=max(
            recording_end,
            recording_start + max(segment.end_seconds for segment in segments),
        ),
        segments=segments,
        language=str((stream.get("tags") or {}).get("language") or ""),
        metadata={
            "video_path": str(video_path),
            "stream_index": int(stream.get("index") or 0),
            "segment_count": len(segments),
        },
    )


def _document_from_douyu_result(
    result: object,
    *,
    video_dir: str | Path,
) -> TranscriptDocument | None:
    if not isinstance(result, dict):
        return None
    captions = result.get("items")
    if not isinstance(captions, list) or not captions:
        return None
    try:
        start_ts = float(result.get("recording_start_unix_ts") or 0)
        end_ts = float(result.get("recording_end_unix_ts") or start_ts)
        segments = tuple(
            TranscriptSegment(
                id=f"douyu-caption:{item['idx']}:{round(float(item['time']) * 1000)}",
                start_seconds=float(item["time"]),
                end_seconds=float(item["end_time"]),
                text=str(item["text"]),
                source_metadata={
                    "caption_idx": str(item["idx"]),
                    "stream_pts_seconds": item.get("stream_pts_seconds"),
                },
            )
            for item in captions
            if isinstance(item, dict)
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not segments:
        return None
    return TranscriptDocument(
        source="douyu_asr_caption",
        source_kind="platform_auto_caption",
        provider="douyu",
        model="asr_caption",
        recording_start_unix_ts=start_ts,
        recording_end_unix_ts=end_ts,
        segments=segments,
        language="zh",
        metadata={
            "recording_directory": str(Path(video_dir)),
            "segment_count": len(segments),
            "source_last_end_seconds": max(segment.end_seconds for segment in segments),
        },
    )


def _merge_douyu_documents(
    cached: TranscriptDocument,
    current: TranscriptDocument,
) -> TranscriptDocument:
    """Merge a possibly pruned live snapshot without losing older captions."""
    segments_by_id = {segment.id: segment for segment in cached.segments}
    segments_by_id.update({segment.id: segment for segment in current.segments})
    segments = tuple(sorted(
        segments_by_id.values(),
        key=lambda segment: (segment.start_seconds, segment.id),
    ))
    return TranscriptDocument(
        source=current.source,
        source_kind=current.source_kind,
        provider=current.provider,
        model=current.model,
        recording_start_unix_ts=min(
            cached.recording_start_unix_ts,
            current.recording_start_unix_ts,
        ),
        recording_end_unix_ts=max(
            cached.recording_end_unix_ts,
            current.recording_end_unix_ts,
        ),
        segments=segments,
        language=current.language or cached.language,
        metadata={
            **cached.metadata,
            **current.metadata,
            "segment_count": len(segments),
            "source_last_end_seconds": max(
                segment.end_seconds for segment in segments
            ),
        },
    )


def resolve_transcript_document(
    video_dir: str | Path,
    *,
    video_path: str | Path | None = None,
    duration_seconds: float | None = None,
    persist: bool = True,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
) -> TranscriptDocument | None:
    """Resolve sidecar/platform subtitles; return None for legacy/ASR fallback."""
    root = Path(video_dir)
    output = transcript_document_path(root, video_path)
    recording_start, recording_end = recording_timeframe(str(root))
    if duration_seconds and duration_seconds > 0:
        recording_end = recording_start + float(duration_seconds)
    for subtitle_path in _sidecar_candidates(root, video_path):
        try:
            document = _document_from_srt(
                subtitle_path,
                recording_start=recording_start,
                recording_end=recording_end,
            )
        except (OSError, ValueError):
            document = None
        if document is not None:
            document = with_transcript_quality(document)
            if persist:
                persist_transcript_document(document, output)
            return document

    cached = load_transcript_document(output)
    if cached is not None and cached.segments:
        quality_was_missing = "quality" not in cached.metadata
        cached = with_transcript_quality(cached)
        if persist and quality_was_missing:
            persist_transcript_document(cached, output)
        if cached.source != "douyu_asr_caption":
            return cached
        # Douyu revises and appends captions during a recording. Refresh a
        # platform snapshot when newer source data is available, but retain the
        # persisted copy when the live stats file has disappeared or is broken.
        try:
            current_result = get_ai_captions_for_recording(
                str(video_dir),
                duration_seconds=duration_seconds,
                include_timeframe=True,
            )
        except Exception:
            return cached
        current = _document_from_douyu_result(
            current_result,
            video_dir=video_dir,
        )
        if current is None:
            return cached
        current = with_transcript_quality(current)
        merged = _merge_douyu_documents(cached, current)
        merged = with_transcript_quality(merged)
        if merged.segments == cached.segments:
            return cached
        if persist:
            persist_transcript_document(merged, output)
        return merged

    if video_path:
        document = _document_from_embedded_subtitle(
            Path(video_path),
            recording_start=recording_start,
            recording_end=recording_end,
            ffprobe=ffprobe,
            ffmpeg=ffmpeg,
        )
        if document is not None:
            document = with_transcript_quality(document)
            if persist:
                persist_transcript_document(document, output)
            return document

    try:
        result = get_ai_captions_for_recording(
            str(video_dir),
            duration_seconds=duration_seconds,
            include_timeframe=True,
        )
    except Exception:
        return None
    document = _document_from_douyu_result(result, video_dir=video_dir)
    if document is None:
        return None
    document = with_transcript_quality(document)
    if persist:
        persist_transcript_document(document, output)
    return document
