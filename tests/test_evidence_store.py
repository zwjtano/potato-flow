import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.evidence_store import CandidateEvent, EvidenceRecord, EvidenceStore
from modules.transcript_document import TranscriptDocument, TranscriptSegment


def _document():
    return TranscriptDocument(
        source="cloud_asr",
        source_kind="asr",
        provider="groq",
        model="whisper-large-v3-turbo",
        recording_start_unix_ts=0,
        recording_end_unix_ts=60,
        audio_sha256="audio-hash",
        language="zh",
        segments=(
            TranscriptSegment(
                id="speech:000001", start_seconds=12.4, end_seconds=18.7,
                text="Ame 这把出了 BKB",
                source_metadata={"raw_text": "阿妹这把出了PKB"},
            ),
            TranscriptSegment(
                id="speech:000002", start_seconds=30, end_seconds=34,
                text="这波不能接，先刷一会儿",
            ),
            TranscriptSegment(
                id="speech:000003", start_seconds=40, end_seconds=44,
                text="现在比分是 2 - 1",
            ),
        ),
    )


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = EvidenceStore(Path(self.temporary.name) / "evidence.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def test_transcript_is_idempotent_searchable_and_preserves_raw_text(self):
        self.assertEqual(self.store.ingest_transcript("rec-1", _document()), 3)
        self.assertEqual(self.store.ingest_transcript("rec-1", _document()), 3)
        results = self.store.search_speech("rec-1", "BKB")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["raw_text"], "阿妹这把出了PKB")
        self.assertEqual(results[0]["normalized_text"], "Ame 这把出了 BKB")
        self.assertEqual(self.store.recording_summary("rec-1")["speech_segments"], 3)
        self.assertEqual(len(self.store.search_speech("rec-1", "2 - 1")), 1)

    def test_evidence_and_candidate_event_are_upserted(self):
        self.store.upsert_evidence(EvidenceRecord(
            evidence_id="visual:1", recording_id="rec-1", type="visual",
            start_seconds=18, end_seconds=18,
            content="Xtreme Gaming 2 - 1 Team Spirit", confidence=0.94,
            source="rapidocr",
        ))
        self.store.upsert_event(CandidateEvent(
            event_id="event:1", recording_id="rec-1",
            start_seconds=10, end_seconds=30,
            summary="主播展示比赛比分", confidence=0.9,
            evidence_refs=("visual:1",),
        ))
        summary = self.store.recording_summary("rec-1")
        self.assertEqual(summary["evidence_counts"]["visual"], 1)
        self.assertEqual(summary["candidate_events"], 1)


if __name__ == "__main__":
    unittest.main()
