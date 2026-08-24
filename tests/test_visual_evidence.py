import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.evidence_store import EvidenceStore
from modules.visual_evidence import generate_candidate_times, run_visual_evidence_pipeline


class VisualEvidenceTests(unittest.TestCase):
    def test_triggered_candidates_are_retained_when_periodic_budget_is_full(self):
        values = generate_candidate_times(
            3600,
            interval_seconds=30,
            triggered_times=[(3500, "danmaku_peak")],
            max_frames=5,
        )
        self.assertTrue(any(item.trigger == "danmaku_peak" for item in values))
        self.assertLessEqual(len(values), 5)

    def test_pipeline_deduplicates_and_reuses_ocr_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "video.mp4"
            video.write_bytes(b"fake")
            store = EvidenceStore(root / "evidence.sqlite3")
            engine_calls = []

            def fake_extract(_ffmpeg, _video, _timestamp, output):
                Image.new("RGB", (64, 32), "white").save(output)

            class FakeEngine:
                def __call__(self, path):
                    engine_calls.append(path)
                    return SimpleNamespace(
                        txts=("Xtreme Gaming", "!"),
                        scores=(0.95, 0.99),
                        boxes=(
                            [[0, 0], [20, 0], [20, 10], [0, 10]],
                            [[0, 0], [2, 0], [2, 2], [0, 2]],
                        ),
                    )

            config = {
                "VISUAL_EVIDENCE_SAMPLE_INTERVAL_SECONDS": 30,
                "VISUAL_EVIDENCE_MAX_FRAMES": 3,
                "VISUAL_EVIDENCE_OCR_CONFIDENCE": 0.6,
                "VISUAL_EVIDENCE_OCR_MODEL_VERSION": "test-ocr-v1",
            }
            with patch("modules.visual_evidence._extract_frame", side_effect=fake_extract), patch(
                "modules.visual_evidence.get_app_subdir", return_value=root / "frames"
            ):
                first = run_visual_evidence_pipeline(
                    video,
                    recording_id="rec-1",
                    duration_seconds=60,
                    config=config,
                    store=store,
                    ocr_engine=FakeEngine(),
                )
                second = run_visual_evidence_pipeline(
                    video,
                    recording_id="rec-1",
                    duration_seconds=60,
                    config=config,
                    store=store,
                    ocr_engine=FakeEngine(),
                )
            self.assertEqual(first.extracted, 3)
            self.assertEqual(first.deduplicated, 2)
            self.assertEqual(first.evidence_count, 1)
            self.assertEqual(second.cache_hits, 1)
            self.assertEqual(len(engine_calls), 1)
            self.assertEqual(store.recording_summary("rec-1")["evidence_counts"]["visual"], 1)


if __name__ == "__main__":
    unittest.main()
