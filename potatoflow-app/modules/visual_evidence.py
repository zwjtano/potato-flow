#!/usr/bin/env python3
"""Bounded FFmpeg frame sampling and local RapidOCR visual evidence."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from .evidence_store import EvidenceRecord, EvidenceStore, get_evidence_store
from .path_policy import ensure_directory
from .utils import get_app_subdir


@dataclass(frozen=True)
class CandidateTime:
    timestamp_seconds: float
    trigger: str


@dataclass(frozen=True)
class VisualEvidenceRun:
    candidates: int
    extracted: int
    deduplicated: int
    cache_hits: int
    ocr_positive_frames: int
    evidence_count: int


def generate_candidate_times(
    duration_seconds: float,
    *,
    interval_seconds: float = 30,
    triggered_times: Iterable[tuple[float, str]] = (),
    max_frames: int = 180,
) -> tuple[CandidateTime, ...]:
    duration = max(0.0, float(duration_seconds))
    interval = max(5.0, float(interval_seconds))
    candidates: dict[int, CandidateTime] = {}
    timestamp = 0.0
    while timestamp <= duration and len(candidates) < max_frames:
        millisecond = round(timestamp * 1000)
        candidates[millisecond] = CandidateTime(timestamp, "periodic")
        timestamp += interval
    for event_time, trigger in triggered_times:
        for offset in (-1.0, 0.0, 1.0, 2.0):
            value = min(duration, max(0.0, float(event_time) + offset))
            millisecond = round(value * 1000)
            candidates[millisecond] = CandidateTime(value, str(trigger or "event"))
    safe_max = max(1, int(max_frames))
    triggered = sorted(
        (item for item in candidates.values() if item.trigger != "periodic"),
        key=lambda item: item.timestamp_seconds,
    )
    periodic = sorted(
        (item for item in candidates.values() if item.trigger == "periodic"),
        key=lambda item: item.timestamp_seconds,
    )
    selected = triggered[:safe_max]
    if len(selected) < safe_max:
        selected.extend(periodic[:safe_max - len(selected)])
    return tuple(sorted(selected, key=lambda item: item.timestamp_seconds))


def difference_hash(path: Path) -> str:
    with Image.open(path) as image:
        resized = image.convert("L").resize((9, 8))
        pixel_values = getattr(resized, "get_flattened_data", resized.getdata)()
        pixels = list(pixel_values)
    bits = []
    for row in range(8):
        base = row * 9
        bits.extend(pixels[base + column] > pixels[base + column + 1] for column in range(8))
    value = sum((1 << index) for index, bit in enumerate(bits) if bit)
    return f"{value:016x}"


def hamming_distance(first: str, second: str) -> int:
    return (int(first, 16) ^ int(second, 16)).bit_count()


def _clean_ocr_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= 1 or not any(character.isalnum() or "\u4e00" <= character <= "\u9fff" for character in text):
        return ""
    return text


def _box_to_list(box: Any) -> list[list[float]]:
    if hasattr(box, "tolist"):
        box = box.tolist()
    try:
        return [[round(float(x), 2), round(float(y), 2)] for x, y in box]
    except (TypeError, ValueError):
        return []


def _extract_frame(ffmpeg: str, video_path: Path, timestamp: float, output_path: Path) -> None:
    completed = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{timestamp:.3f}", "-i", str(video_path),
            "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", str(output_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if completed.returncode != 0 or not output_path.is_file():
        raise RuntimeError(completed.stderr.strip()[-500:] or "FFmpeg frame extraction failed")


def run_visual_evidence_pipeline(
    video_path: str | Path,
    *,
    recording_id: str,
    duration_seconds: float,
    config: dict[str, Any],
    triggered_times: Iterable[tuple[float, str]] = (),
    store: EvidenceStore | None = None,
    ocr_engine: Any | None = None,
) -> VisualEvidenceRun:
    """Run sequentially after recording; frame/model hashes make retries idempotent."""
    video = Path(video_path)
    if not video.is_file():
        raise FileNotFoundError(video)
    evidence_store = store or get_evidence_store()
    model_version = str(
        config.get("VISUAL_EVIDENCE_OCR_MODEL_VERSION") or "rapidocr-3.9.2-default"
    )
    candidates = generate_candidate_times(
        duration_seconds,
        interval_seconds=float(config.get("VISUAL_EVIDENCE_SAMPLE_INTERVAL_SECONDS", 30) or 30),
        triggered_times=triggered_times,
        max_frames=int(config.get("VISUAL_EVIDENCE_MAX_FRAMES", 180) or 180),
    )
    if ocr_engine is None:
        from rapidocr import RapidOCR

        ocr_engine = RapidOCR()
    output_dir = ensure_directory(
        get_app_subdir(f"evidence/frames/{hashlib.sha256(recording_id.encode('utf-8')).hexdigest()[:20]}"),
        private=True,
    )
    confidence_threshold = max(0.0, min(1.0, float(config.get("VISUAL_EVIDENCE_OCR_CONFIDENCE", 0.60) or 0.60)))
    seen_hashes: list[str] = []
    extracted = deduplicated = cache_hits = positive = evidence_count = 0
    for index, candidate in enumerate(candidates, start=1):
        frame_path = output_dir / f"frame-{round(candidate.timestamp_seconds * 1000):010d}.jpg"
        _extract_frame(str(config.get("ffmpeg") or "ffmpeg"), video, candidate.timestamp_seconds, frame_path)
        extracted += 1
        raw = frame_path.read_bytes()
        frame_sha256 = hashlib.sha256(raw).hexdigest()
        dhash = difference_hash(frame_path)
        if any(hamming_distance(dhash, previous) <= 2 for previous in seen_hashes):
            deduplicated += 1
            frame_path.unlink(missing_ok=True)
            continue
        seen_hashes.append(dhash)
        accepted = evidence_store.get_cached_ocr(frame_sha256, model_version)
        if accepted is not None:
            cache_hits += 1
        frame_id = f"frame:{recording_id}:{round(candidate.timestamp_seconds * 1000)}"
        if accepted is None:
            result = ocr_engine(str(frame_path))
            texts = tuple(getattr(result, "txts", ()) or ())
            scores = tuple(getattr(result, "scores", ()) or ())
            boxes = tuple(getattr(result, "boxes", ()) or ())
            accepted = []
            for text_value, score_value, box_value in zip(texts, scores, boxes):
                text = _clean_ocr_text(text_value)
                score = float(score_value or 0)
                if not text or score < confidence_threshold:
                    continue
                accepted.append({"text": text, "confidence": score, "bbox": _box_to_list(box_value)})
            evidence_store.put_cached_ocr(frame_sha256, model_version, accepted)
        evidence_store.upsert_visual_frame(
            frame_id=frame_id,
            recording_id=recording_id,
            timestamp_seconds=candidate.timestamp_seconds,
            frame_path=str(frame_path),
            frame_sha256=frame_sha256,
            perceptual_hash=dhash,
            source_trigger=candidate.trigger,
            ocr_model_version=model_version,
        )
        if not accepted:
            continue
        positive += 1
        evidence_id = f"visual:{recording_id}:{round(candidate.timestamp_seconds * 1000)}"
        evidence_store.upsert_evidence(EvidenceRecord(
            evidence_id=evidence_id,
            recording_id=recording_id,
            type="visual",
            start_seconds=candidate.timestamp_seconds,
            end_seconds=candidate.timestamp_seconds,
            content=" | ".join(item["text"] for item in accepted),
            confidence=sum(item["confidence"] for item in accepted) / len(accepted),
            source="rapidocr",
            metadata={
                "frame_id": frame_id,
                "trigger": candidate.trigger,
                "texts": accepted,
                "ocr_model_version": model_version,
            },
        ))
        evidence_count += 1
    persisted_visual_count = int(
        evidence_store.recording_summary(recording_id)
        .get("evidence_counts", {})
        .get("visual", evidence_count)
    )
    return VisualEvidenceRun(
        candidates=len(candidates),
        extracted=extracted,
        deduplicated=deduplicated,
        cache_hits=cache_hits,
        ocr_positive_frames=positive,
        evidence_count=persisted_visual_count,
    )
