import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.asr_free_quota import FreeAsrQuotaLedger
from modules.asr_api_client import AsrApiClient, AsrConfig
from modules.asr_provider_router import FreeAsrCandidate, FreeAsrProviderRouter
from modules.speech_recognition import create_speech_recognizer_from_config
from modules.subtitle_pipeline_types import (
    AsrSegmentTiming,
    AsrTranscriptionResult,
    DetectedSpeechWindow,
)


class FreeAsrQuotaLedgerTests(unittest.TestCase):
    def test_reservation_is_atomic_and_respects_hour_and_day_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = FreeAsrQuotaLedger(Path(temporary) / "quota.sqlite3")
            self.assertTrue(ledger.reserve(
                "groq", 40, daily_limit_seconds=100, hourly_limit_seconds=60, now=1000
            ))
            self.assertFalse(ledger.reserve(
                "groq", 30, daily_limit_seconds=100, hourly_limit_seconds=60, now=1001
            ))
            self.assertTrue(ledger.reserve(
                "groq", 30, daily_limit_seconds=100, hourly_limit_seconds=60, now=5000
            ))
            self.assertFalse(ledger.reserve(
                "groq", 31, daily_limit_seconds=100, hourly_limit_seconds=60, now=5001
            ))
            self.assertEqual(ledger.usage("groq", now=5001)["day_seconds"], 70.0)


class FreeAsrProviderRouterTests(unittest.TestCase):
    @staticmethod
    def _client(provider: str, result: AsrTranscriptionResult):
        client = SimpleNamespace(
            config=SimpleNamespace(
                max_workers=1,
                language="zh",
                model_name=f"{provider}-model",
            ),
            set_language_hint=Mock(),
            transcribe_window=Mock(return_value=result),
            _probe_wav_duration=Mock(return_value=10.0),
        )
        return client

    def test_quota_or_provider_failure_automatically_uses_next_provider(self):
        window = DetectedSpeechWindow(0, 10, 0, 10)
        first = self._client("groq", AsrTranscriptionResult(
            provider="groq",
            response_format="",
            timestamp_mode="none",
            failure_token="asr_quota_exhausted",
        ))
        second = self._client("cloudflare", AsrTranscriptionResult(
            provider="cloudflare",
            response_format="json",
            timestamp_mode="segment",
            segments=[AsrSegmentTiming(0, 10, "字幕")],
        ))
        with tempfile.TemporaryDirectory() as temporary:
            router = FreeAsrProviderRouter(
                [
                    FreeAsrCandidate("groq", first, 100, 100),
                    FreeAsrCandidate("cloudflare", second, 100, 0),
                ],
                quota_ledger=FreeAsrQuotaLedger(Path(temporary) / "quota.sqlite3"),
            )
            result = router.transcribe_window("unused.wav", window=window)

        self.assertTrue(result.ok)
        first.transcribe_window.assert_called_once()
        second.transcribe_window.assert_called_once()
        self.assertEqual(result.metadata["provider_attempts"], ["groq", "cloudflare"])
        self.assertEqual(router.used_providers, {"cloudflare"})

    def test_all_exhausted_returns_pending_without_calling_provider(self):
        window = DetectedSpeechWindow(0, 10, 0, 10)
        client = self._client("groq", AsrTranscriptionResult(
            provider="groq", response_format="", timestamp_mode="none"
        ))
        with tempfile.TemporaryDirectory() as temporary:
            ledger = FreeAsrQuotaLedger(Path(temporary) / "quota.sqlite3")
            self.assertTrue(ledger.reserve(
                "groq", 100, daily_limit_seconds=100, hourly_limit_seconds=100, now=time.time()
            ))
            router = FreeAsrProviderRouter(
                [FreeAsrCandidate("groq", client, 100, 100)],
                quota_ledger=ledger,
            )
            result = router.transcribe_window("unused.wav", window=window)

        self.assertEqual(result.failure_token, "pending_free_quota")
        client.transcribe_window.assert_not_called()


class FreeAsrIntegrationTests(unittest.TestCase):
    def test_factory_builds_distinct_provider_profiles_without_key_rotation(self):
        recognizer = create_speech_recognizer_from_config({
            "SPEECH_RECOGNITION_ENABLED": True,
            "SPEECH_RECOGNITION_PROVIDER": "free_pool",
            "GROQ_API_KEY": "groq-secret",
            "CLOUDFLARE_API_TOKEN": "cloudflare-secret",
            "CLOUDFLARE_ACCOUNT_ID": "account",
        }, "task")
        self.assertIsNotNone(recognizer)
        self.assertEqual(
            [candidate.provider_id for candidate in recognizer._asr.candidates],
            ["groq", "cloudflare"],
        )
        self.assertEqual(recognizer.config.provider, "free_pool")

    def test_factory_refuses_empty_free_pool(self):
        self.assertIsNone(create_speech_recognizer_from_config({
            "SPEECH_RECOGNITION_ENABLED": True,
            "SPEECH_RECOGNITION_PROVIDER": "free_pool",
        }))

    def test_cloudflare_adapter_normalizes_timed_segments(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "success": True,
            "result": {
                "text": "测试字幕",
                "segments": [{"start": 0.0, "end": 1.5, "text": "测试字幕"}],
            },
        }
        client = AsrApiClient(AsrConfig(
            provider="cloudflare",
            api_key="secret",
            base_url="https://api.cloudflare.test/run/model",
            model_name="@cf/openai/whisper-large-v3-turbo",
            cloudflare_account_id="account",
        ))
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "clip.wav"
            audio.write_bytes(b"RIFF-test")
            with patch("modules.asr_api_client.requests.post", return_value=response) as post:
                result = client.transcribe_window(
                    str(audio),
                    window=DetectedSpeechWindow(0, 1.5, 0, 1.5),
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "cloudflare")
        self.assertEqual(result.segments[0].text, "测试字幕")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer secret")


if __name__ == "__main__":
    unittest.main()
