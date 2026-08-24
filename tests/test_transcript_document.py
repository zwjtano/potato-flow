import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.subtitle_source_resolver import resolve_transcript_document
from modules.transcript_document import (
    TranscriptDocument,
    TranscriptSegment,
    export_transcript_srt,
    transcript_document_from_cues,
    transcript_document_from_dict,
    transcript_document_to_srt,
    transcript_quality_assessment,
)
from modules.transcript_document import transcript_document_from_asr_result
from modules.subtitle_pipeline_types import (
    AsrSegmentTiming,
    AsrTranscriptionResult,
    AsrWordTiming,
)


class TranscriptDocumentTests(unittest.TestCase):
    def test_rejects_unsorted_segments(self):
        with self.assertRaisesRegex(ValueError, "time ordered"):
            TranscriptDocument(
                source="test",
                source_kind="platform_auto_caption",
                provider="test",
                model="test",
                recording_start_unix_ts=100,
                recording_end_unix_ts=120,
                segments=(
                    TranscriptSegment("two", 10, 11, "第二句"),
                    TranscriptSegment("one", 1, 2, "第一句"),
                ),
            )

    def test_resolver_normalizes_and_persists_douyu_captions(self):
        source = {
            "recording_start_unix_ts": 100.0,
            "recording_end_unix_ts": 120.0,
            "items": [
                {
                    "idx": "7",
                    "time": 1.5,
                    "end_time": 3.0,
                    "text": "平台 AI 字幕",
                    "stream_pts_seconds": 11.0,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "modules.subtitle_source_resolver.get_ai_captions_for_recording",
            return_value=source,
        ):
            document = resolve_transcript_document(temporary)
            output = Path(temporary) / ".potato-flow" / "transcript.json"
            persisted = json.loads(output.read_text(encoding="utf-8"))

        self.assertIsNotNone(document)
        self.assertEqual(document.source, "douyu_asr_caption")
        self.assertEqual(document.segments[0].text, "平台 AI 字幕")
        self.assertEqual(document.segments[0].time, 1.5)
        self.assertEqual(persisted["schema_version"], "1.0")
        self.assertEqual(persisted["segments"][0]["id"], "douyu-caption:7:1500")

    def test_resolver_returns_none_without_platform_captions(self):
        with patch(
            "modules.subtitle_source_resolver.get_ai_captions_for_recording",
            return_value={
                "recording_start_unix_ts": 100.0,
                "recording_end_unix_ts": 120.0,
                "items": [],
            },
        ):
            self.assertIsNone(resolve_transcript_document("/missing", persist=False))

    def test_sidecar_srt_has_priority_over_platform_auto_captions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "主播_2026-08-24_10-00-00"
            root.mkdir()
            video = root / "recording.flv"
            video.write_bytes(b"video")
            video.with_suffix(".srt").write_text(
                "1\n00:00:01,000 --> 00:00:03,500\n人工字幕第一句\n\n"
                "2\n00:00:05,000 --> 00:00:07,000\n人工字幕第二句\n",
                encoding="utf-8",
            )
            with patch(
                "modules.subtitle_source_resolver.get_ai_captions_for_recording"
            ) as platform_captions:
                document = resolve_transcript_document(
                    root,
                    video_path=video,
                    duration_seconds=10,
                )

            persisted = json.loads(
                (root / ".potato-flow" / "recording.transcript.json").read_text(
                    encoding="utf-8"
                )
            )

        platform_captions.assert_not_called()
        self.assertEqual(document.source, "sidecar_srt")
        self.assertEqual(
            [segment.text for segment in document.segments],
            ["人工字幕第一句", "人工字幕第二句"],
        )
        self.assertEqual(persisted["source_kind"], "sidecar_subtitle")

    def test_webvtt_sidecar_supports_timestamp_without_hour(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "recording.flv"
            video.write_bytes(b"video")
            video.with_suffix(".vtt").write_text(
                "WEBVTT\n\nintro\n00:01.250 --> 00:03.500 align:start\n"
                "第一条 VTT 字幕\n\n00:05.000 --> 00:06.250\n第二条字幕\n",
                encoding="utf-8",
            )
            with patch(
                "modules.subtitle_source_resolver.get_ai_captions_for_recording"
            ) as platform_captions:
                document = resolve_transcript_document(
                    root,
                    video_path=video,
                    duration_seconds=10,
                )

        platform_captions.assert_not_called()
        self.assertEqual(document.source, "sidecar_vtt")
        self.assertEqual(document.model, "webvtt")
        self.assertEqual(document.segments[0].start_seconds, 1.25)
        self.assertEqual(
            [segment.text for segment in document.segments],
            ["第一条 VTT 字幕", "第二条字幕"],
        )

    def test_resolver_recovers_persisted_transcript_without_source_snapshot(self):
        source = {
            "recording_start_unix_ts": 100.0,
            "recording_end_unix_ts": 120.0,
            "items": [{
                "idx": "8",
                "time": 2.0,
                "end_time": 3.0,
                "text": "已持久化字幕",
                "stream_pts_seconds": None,
            }],
        }
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "modules.subtitle_source_resolver.get_ai_captions_for_recording",
                return_value=source,
            ):
                first = resolve_transcript_document(temporary)
            with patch(
                "modules.subtitle_source_resolver.get_ai_captions_for_recording",
                side_effect=AssertionError("cache should be used"),
            ):
                recovered = resolve_transcript_document(temporary)

        self.assertEqual(first.to_dict(), recovered.to_dict())

    def test_resolver_refreshes_partial_douyu_caption_cache(self):
        first_source = {
            "recording_start_unix_ts": 100.0,
            "recording_end_unix_ts": 120.0,
            "items": [{
                "idx": "1", "time": 1.0, "end_time": 2.0,
                "text": "第一句", "stream_pts_seconds": 1.0,
            }],
        }
        updated_source = {
            "recording_start_unix_ts": 100.0,
            "recording_end_unix_ts": 120.0,
            # The daemon may prune an older item while appending a new one.
            # Refresh must merge this snapshot instead of shrinking the cache.
            "items": [{
                "idx": "2", "time": 4.0, "end_time": 5.0,
                "text": "后来补到的第二句", "stream_pts_seconds": 4.0,
            }],
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "modules.subtitle_source_resolver.get_ai_captions_for_recording",
            side_effect=[first_source, updated_source],
        ):
            first = resolve_transcript_document(temporary)
            refreshed = resolve_transcript_document(temporary)
            persisted = json.loads(
                (Path(temporary) / ".potato-flow" / "transcript.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(len(first.segments), 1)
        self.assertEqual([item.text for item in refreshed.segments], [
            "第一句", "后来补到的第二句",
        ])
        self.assertEqual(persisted["metadata"]["segment_count"], 2)
        self.assertEqual(persisted["metadata"]["source_last_end_seconds"], 5.0)

    def test_unreadable_sidecar_falls_back_to_douyu_captions(self):
        source = {
            "recording_start_unix_ts": 100.0,
            "recording_end_unix_ts": 120.0,
            "items": [{
                "idx": "3", "time": 1.0, "end_time": 2.0,
                "text": "斗鱼字幕仍可用", "stream_pts_seconds": None,
            }],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "recording.flv"
            video.write_bytes(b"video")
            video.with_suffix(".srt").write_text("placeholder", encoding="utf-8")
            with patch(
                "modules.subtitle_source_resolver._document_from_srt",
                side_effect=OSError("permission denied"),
            ), patch(
                "modules.subtitle_source_resolver._document_from_embedded_subtitle",
                return_value=None,
            ), patch(
                "modules.subtitle_source_resolver.get_ai_captions_for_recording",
                return_value=source,
            ):
                document = resolve_transcript_document(root, video_path=video)

        self.assertEqual(document.source, "douyu_asr_caption")
        self.assertEqual(document.segments[0].text, "斗鱼字幕仍可用")

    def test_platform_caption_read_failure_returns_none_for_legacy_fallback(self):
        with patch(
            "modules.subtitle_source_resolver.get_ai_captions_for_recording",
            side_effect=OSError("stats snapshot replaced"),
        ):
            document = resolve_transcript_document("/missing", persist=False)

        self.assertIsNone(document)

    def test_each_video_uses_an_independent_transcript_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_video = root / "part-1.flv"
            second_video = root / "part-2.flv"
            first_video.write_bytes(b"one")
            second_video.write_bytes(b"two")
            sources = [
                {
                    "recording_start_unix_ts": 100.0,
                    "recording_end_unix_ts": 110.0,
                    "items": [{"idx": "1", "time": 1, "end_time": 2, "text": "第一段"}],
                },
                {
                    "recording_start_unix_ts": 200.0,
                    "recording_end_unix_ts": 210.0,
                    "items": [{"idx": "2", "time": 1, "end_time": 2, "text": "第二段"}],
                },
            ]
            with patch(
                "modules.subtitle_source_resolver.get_ai_captions_for_recording",
                side_effect=sources,
            ):
                first = resolve_transcript_document(root, video_path=first_video)
                second = resolve_transcript_document(root, video_path=second_video)

            outputs = sorted(path.name for path in (root / ".potato-flow").glob("*.json"))

        self.assertEqual(first.segments[0].text, "第一段")
        self.assertEqual(second.segments[0].text, "第二段")
        self.assertEqual(outputs, ["part-1.transcript.json", "part-2.transcript.json"])

    def test_embedded_subtitle_is_extracted_before_platform_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "主播_2026-08-24_11-00-00"
            root.mkdir()
            video = root / "recording.mkv"
            video.write_bytes(b"container")
            probe = type("Result", (), {
                "returncode": 0,
                "stdout": json.dumps({"streams": [
                    {"index": 2, "codec_name": "subrip", "tags": {"language": "eng"}},
                    {"index": 3, "codec_name": "subrip", "tags": {"language": "zho", "title": "中文字幕"}},
                ]}),
            })()
            extract = type("Result", (), {
                "returncode": 0,
                "stdout": "1\n00:00:02,000 --> 00:00:04,000\n内嵌中文字幕\n",
            })()
            with patch(
                "modules.subtitle_source_resolver.subprocess.run",
                side_effect=[probe, extract],
            ) as run, patch(
                "modules.subtitle_source_resolver.get_ai_captions_for_recording"
            ) as platform_captions:
                document = resolve_transcript_document(
                    root,
                    video_path=video,
                    duration_seconds=10,
                )

        platform_captions.assert_not_called()
        self.assertEqual(document.source, "embedded_subtitle")
        self.assertEqual(document.language, "zho")
        self.assertEqual(document.segments[0].text, "内嵌中文字幕")
        self.assertIn("0:3", run.call_args_list[1].args[0])

    def test_existing_asr_result_normalizes_to_transcript_document(self):
        result = AsrTranscriptionResult(
            provider="voxtral",
            response_format="verbose_json",
            timestamp_mode="word",
            language="zh",
            segments=[AsrSegmentTiming(
                start_s=1.0,
                end_s=3.0,
                text="主播说测试",
                confidence=0.9,
                words=[AsrWordTiming(1.0, 1.5, "主播")],
                metadata={
                    "speaker_id": "speaker_0",
                    "speaker_name": "主播名",
                    "speaker_confidence": 0.95,
                },
            )],
        )
        document = transcript_document_from_asr_result(
            result,
            model="voxtral-test",
            audio_sha256="b" * 64,
            recording_start_unix_ts=100,
        )

        self.assertEqual(document.provider, "voxtral")
        self.assertEqual(document.recording_end_unix_ts, 103)
        self.assertEqual(document.segments[0].speaker_name, "主播名")
        self.assertEqual(document.segments[0].words[0].text, "主播")

    def test_transcript_document_exports_standard_srt_atomically(self):
        document = TranscriptDocument(
            source="douyu_asr_caption",
            source_kind="platform_auto_caption",
            provider="douyu",
            model="asr_caption",
            recording_start_unix_ts=100,
            recording_end_unix_ts=200,
            segments=(
                TranscriptSegment("one", 1.234, 3.5, "第一句"),
                TranscriptSegment("two", 65, 66.001, "第二句\n仍是同一段"),
            ),
        )
        expected = (
            "1\n00:00:01,234 --> 00:00:03,500\n第一句\n\n"
            "2\n00:01:05,000 --> 00:01:06,001\n第二句\n仍是同一段\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "captions.srt"
            export_transcript_srt(document, path)
            encoded = path.read_bytes()

        self.assertEqual(transcript_document_to_srt(document), expected)
        self.assertTrue(encoded.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(encoded.decode("utf-8-sig"), expected)

    def test_legacy_draft_schema_migrates_to_v1(self):
        document = transcript_document_from_dict({
            "schema_version": "0.9",
            "source": "legacy_asr",
            "source_kind": "asr",
            "provider": "legacy",
            "model": "draft",
            "recording_start_unix_ts": 0,
            "recording_end_unix_ts": 10,
            "segments": [{
                "id": "legacy:1",
                "start": 1.0,
                "end": 2.0,
                "text": "旧格式字幕",
                "words": [{"start": 1.0, "end": 1.5, "text": "旧格式"}],
            }],
        })

        self.assertEqual(document.schema_version, "1.0")
        self.assertEqual(document.segments[0].start_seconds, 1.0)
        self.assertEqual(document.segments[0].words[0].end_seconds, 1.5)
        self.assertEqual(document.metadata["migrated_from_schema"], "0.9")

    def test_future_transcript_schema_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported transcript schema"):
            transcript_document_from_dict({"schema_version": "2.0"})

    def test_finalized_asr_cues_normalize_to_transcript_document(self):
        document = transcript_document_from_cues(
            [
                {"start": 1.0, "end": 2.5, "text": "第一条识别结果"},
                {"start": 4.0, "end": 5.0, "text": "第二条识别结果"},
            ],
            provider="voxtral",
            model="voxtral-mini",
            audio_sha256="c" * 64,
            duration_seconds=10,
            language="zh",
        )

        self.assertEqual(document.source, "cloud_asr")
        self.assertEqual(document.provider, "voxtral")
        self.assertEqual(document.audio_sha256, "c" * 64)
        self.assertEqual(document.segments[0].id, "asr-cue:1:1000")
        self.assertEqual(document.metadata["normalized_from"], "finalized_cues")

    def test_quality_assessment_respects_source_without_reordering_content(self):
        segments = (TranscriptSegment("one", 1, 2, "同一份文字"),)
        manual = TranscriptDocument(
            "sidecar_srt", "sidecar_subtitle", "local", "srt", 0, 10, segments
        )
        platform = TranscriptDocument(
            "douyu_asr_caption", "platform_auto_caption", "douyu",
            "asr_caption", 0, 10, segments,
        )

        manual_quality = transcript_quality_assessment(manual)
        platform_quality = transcript_quality_assessment(platform)

        self.assertGreater(manual_quality["score"], platform_quality["score"])
        self.assertEqual(manual_quality["tier"], "high")
        self.assertEqual(platform_quality["tier"], "medium")

    def test_resolver_persists_quality_diagnostics(self):
        source = {
            "recording_start_unix_ts": 100.0,
            "recording_end_unix_ts": 120.0,
            "items": [{
                "idx": "9", "time": 1.0, "end_time": 2.0,
                "text": "带质量评分的字幕", "stream_pts_seconds": None,
            }],
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "modules.subtitle_source_resolver.get_ai_captions_for_recording",
            return_value=source,
        ):
            document = resolve_transcript_document(temporary)
            persisted = json.loads(
                (Path(temporary) / ".potato-flow" / "transcript.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(document.metadata["quality"]["tier"], "medium")
        self.assertEqual(
            persisted["metadata"]["quality"], document.metadata["quality"]
        )


if __name__ == "__main__":
    unittest.main()
