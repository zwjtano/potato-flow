#!/usr/bin/env python3
"""Scene-local ASR provider failover for the free-only subtitle fallback."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Iterable

from .asr_free_quota import FreeAsrQuotaLedger, get_free_asr_quota_ledger
from .subtitle_pipeline_types import AsrTranscriptionResult, DetectedSpeechWindow


@dataclass(frozen=True)
class FreeAsrCandidate:
    provider_id: str
    client: Any
    daily_limit_seconds: float
    hourly_limit_seconds: float = 0.0
    rpm_limit: int = 0


class FreeAsrProviderRouter:
    """Use the next configured provider when local quota or the API is exhausted."""

    def __init__(
        self,
        candidates: Iterable[FreeAsrCandidate],
        *,
        task_id: str = "",
        quota_ledger: FreeAsrQuotaLedger | None = None,
    ):
        self.candidates = tuple(candidates)
        if not self.candidates:
            raise ValueError("free ASR provider pool is empty")
        self.task_id = str(task_id or "")
        self.quota_ledger = quota_ledger or get_free_asr_quota_ledger()
        self.config = self.candidates[0].client.config
        self.client = True
        self._language_hint = ""
        self.last_provider = ""
        self.last_model = ""
        self.used_providers: set[str] = set()
        self.used_models: set[str] = set()
        self.exhausted_providers: set[str] = set()

    def set_language_hint(self, lang: str) -> None:
        self._language_hint = str(lang or "")
        for candidate in self.candidates:
            candidate.client.set_language_hint(lang)

    def detect_language_from_segments(self, *args, **kwargs) -> str:
        # Free-pool language probes would consume extra audio quota. Whisper can
        # auto-detect during the actual transcription request instead.
        return str(self.config.language or "")

    @staticmethod
    def _duration(window: DetectedSpeechWindow | None, client: Any, wav_path: str) -> float:
        if window is not None:
            return max(0.01, float(window.end_s) - float(window.start_s))
        return max(0.01, float(client._probe_wav_duration(wav_path) or 0.01))

    def transcribe_window(
        self,
        wav_path: str,
        window: DetectedSpeechWindow | None = None,
        segment_info: str | None = None,
    ) -> AsrTranscriptionResult:
        attempted: list[str] = []
        duration = self._duration(window, self.candidates[0].client, wav_path)
        for candidate in self.candidates:
            if candidate.provider_id in self.exhausted_providers:
                continue
            if not self.quota_ledger.reserve(
                candidate.provider_id,
                duration,
                daily_limit_seconds=candidate.daily_limit_seconds,
                hourly_limit_seconds=candidate.hourly_limit_seconds,
                rpm_limit=candidate.rpm_limit,
                task_id=self.task_id,
            ):
                self.exhausted_providers.add(candidate.provider_id)
                continue
            attempted.append(candidate.provider_id)
            try:
                result = candidate.client.transcribe_window(
                    wav_path,
                    window=window,
                    segment_info=segment_info,
                )
            except Exception as exc:
                result = AsrTranscriptionResult(
                    provider=candidate.provider_id,
                    response_format="",
                    timestamp_mode="none",
                    window=window,
                    failure_token="asr_failed",
                    metadata={"exception": str(exc)},
                )
            if result.ok or result.timestamp_mode == "srt":
                self.last_provider = candidate.provider_id
                self.last_model = str(candidate.client.config.model_name or "")
                self.used_providers.add(self.last_provider)
                self.used_models.add(self.last_model)
                result.metadata["provider_attempts"] = list(attempted)
                return result
            if result.failure_token == "asr_quota_exhausted":
                self.exhausted_providers.add(candidate.provider_id)

        no_remaining_provider = all(
            candidate.provider_id in self.exhausted_providers
            for candidate in self.candidates
        )
        return AsrTranscriptionResult(
            provider="free_pool",
            response_format="",
            timestamp_mode="none",
            window=window,
            failure_token="pending_free_quota" if no_remaining_provider else "asr_failed",
            metadata={"provider_attempts": attempted},
        )

    def transcribe_windows_concurrent(self, windows):
        if not windows:
            return []
        results: dict[int, AsrTranscriptionResult] = {}
        workers = max(1, int(self.config.max_workers or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self.transcribe_window,
                    wav_path,
                    window,
                    f"{window.start_s:.2f}s-{window.end_s:.2f}s",
                ): idx
                for idx, (window, wav_path) in enumerate(windows)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = AsrTranscriptionResult(
                        provider="free_pool",
                        response_format="",
                        timestamp_mode="none",
                        window=windows[index][0],
                        failure_token="asr_failed",
                        metadata={"exception": str(exc)},
                    )
        return [results[index] for index in range(len(windows))]
