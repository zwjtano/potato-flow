import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


Y2A_ROOT = Path(__file__).resolve().parents[1] / "y2a-auto"
sys.path.insert(0, str(Y2A_ROOT))

from modules.live_recorder_manager import LiveRecorderManager, RecorderConfigError  # noqa: E402
import modules.live_recorder_manager as recorder_module  # noqa: E402


class RecordingFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.recordings = self.root / "recordings"
        self.artifacts = self.root / ".bridge" / "artifacts"
        self.recordings.mkdir(parents=True)
        self.artifacts.mkdir(parents=True)
        self.manager = LiveRecorderManager()
        patchers = (
            mock.patch.object(recorder_module, "RECORDINGS_DIR", self.recordings),
            mock.patch.object(self.manager, "_pipeline_state_path", return_value=self.root / ".bridge" / "state.sqlite3"),
            mock.patch.object(self.manager, "list_rooms", return_value=[]),
            mock.patch.object(self.manager, "rooms_with_status", return_value=[]),
            mock.patch.object(self.manager, "pipeline_jobs", return_value=[]),
        )
        for patcher in patchers:
            patcher.start()

    def tearDown(self):
        mock.patch.stopall()
        self.temporary.cleanup()

    def test_lists_video_xml_and_ass_from_both_roots(self):
        (self.recordings / "主播_abcdef-video.flv").write_bytes(b"video")
        (self.recordings / "主播_abcdef-video.xml").write_text("<i/>", encoding="utf-8")
        (self.artifacts / "主播_abcdef-video.ass").write_text("[Script Info]", encoding="utf-8")
        (self.recordings / "ignore.txt").write_text("ignore", encoding="utf-8")

        payload = self.manager.recording_files()

        self.assertEqual(payload["total_files"], 3)
        self.assertEqual({item["type"] for item in payload["files"]}, {"video", "xml", "ass"})
        self.assertEqual(payload["total_size_bytes"], 5 + 4 + 13)

    def test_file_manager_defines_html_escaping_before_rendering_rows(self):
        source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")

        self.assertIn("const escapeHtml =", source)
        self.assertLess(source.index("const escapeHtml ="), source.index("function renderFiles()"))

    def test_delete_rejects_traversal_and_removes_an_inactive_file(self):
        video = self.recordings / "finished.mp4"
        video.write_bytes(b"safe")
        file_id = self.manager.recording_files()["files"][0]["id"]

        deleted = self.manager.delete_recording_file(file_id)

        self.assertEqual(deleted["name"], "finished.mp4")
        self.assertFalse(video.exists())
        forged = self.manager._encode_file_id("recordings", "../secret.mp4")
        with self.assertRaises(RecorderConfigError):
            self.manager.recording_file(forged)

    def test_batch_delete_removes_selected_files_and_reports_invalid_ids(self):
        first = self.recordings / "first.mp4"
        second = self.recordings / "second.xml"
        first.write_bytes(b"video")
        second.write_text("<i/>", encoding="utf-8")
        file_ids = [item["id"] for item in self.manager.recording_files()["files"]]
        forged = self.manager._encode_file_id("recordings", "../outside.mp4")

        result = self.manager.delete_recording_files(file_ids + [forged])

        self.assertEqual(result["deleted_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_active_recording_is_locked_and_cannot_be_deleted(self):
        room_id = "abcdef123456"
        video = self.recordings / "主播_abcdef-live.flv"
        video.write_bytes(b"growing")
        os.utime(video, (time.time(), time.time()))
        self.manager.list_rooms.return_value = [{"id": room_id, "name": "主播"}]
        self.manager.rooms_with_status.return_value = [
            {"id": room_id, "name": "主播", "runtime": {"recording": True}}
        ]

        info = self.manager.recording_files()["files"][0]

        self.assertTrue(info["locked"])
        self.assertEqual(info["lock_reason"], "正在录制")
        with self.assertRaisesRegex(RecorderConfigError, "正在录制"):
            self.manager.delete_recording_file(info["id"])
        self.assertTrue(video.exists())

    def test_active_ffmpeg_part_file_is_visible_and_locked(self):
        room_id = "abcdef123456"
        video = self.recordings / "主播_abcdef-live.flv.part"
        video.write_bytes(b"growing")
        os.utime(video, (time.time(), time.time()))
        self.manager.list_rooms.return_value = [{"id": room_id, "name": "主播"}]
        self.manager.rooms_with_status.return_value = [
            {"id": room_id, "name": "主播", "runtime": {"recording": True}}
        ]

        info = self.manager.recording_files()["files"][0]

        self.assertEqual(info["type"], "video")
        self.assertEqual(info["extension"], "flv.part")
        self.assertTrue(info["locked"])
        self.assertEqual(info["lock_reason"], "正在录制")

    def test_pipeline_artifact_is_locked(self):
        ass = self.artifacts / "job" / "finished.ass"
        ass.parent.mkdir()
        ass.write_text("[Script Info]", encoding="utf-8")
        self.manager.pipeline_jobs.return_value = [{
            "status": "processing",
            "video_path": str(self.recordings / "finished.mp4"),
            "stages": [{"details": {"ass_path": str(ass)}}],
        }]

        info = self.manager.recording_files()["files"][0]

        self.assertTrue(info["locked"])
        self.assertEqual(info["lock_reason"], "流水线处理中")

    def _create_pipeline_job(self, fingerprint: str, video: Path, status: str = "failed"):
        state_path = self.root / ".bridge" / "state.sqlite3"
        with sqlite3.connect(state_path) as db:
            db.execute(
                """CREATE TABLE uploads (
                    fingerprint TEXT PRIMARY KEY,
                    video_path TEXT NOT NULL,
                    status TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE upload_stages (
                    fingerprint TEXT NOT NULL,
                    details_json TEXT
                )"""
            )
            db.execute(
                """CREATE TABLE recording_review_overrides (
                    fingerprint TEXT PRIMARY KEY,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute("INSERT INTO uploads VALUES (?, ?, ?)", (fingerprint, str(video), status))
        return state_path

    def test_delete_pipeline_job_can_keep_original_files(self):
        fingerprint = "a" * 64
        video = self.recordings / "finished.flv"
        video.write_bytes(b"video")
        state_path = self._create_pipeline_job(fingerprint, video)

        result = self.manager.delete_pipeline_job(fingerprint, delete_files=False)

        self.assertEqual(result["deleted_file_count"], 0)
        self.assertTrue(video.exists())
        with sqlite3.connect(state_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0], 0)

    def test_delete_pipeline_job_removes_related_files_and_rejects_active_job(self):
        fingerprint = "b" * 64
        video = self.recordings / "finished.flv"
        xml = video.with_suffix(".xml")
        ass = self.artifacts / fingerprint[:16] / "finished.ass"
        cover = self.artifacts / fingerprint[:16] / "cover.jpg"
        video.write_bytes(b"video")
        xml.write_text("<i/>", encoding="utf-8")
        ass.parent.mkdir(parents=True)
        ass.write_text("[Script Info]", encoding="utf-8")
        cover.write_bytes(b"cover")
        state_path = self._create_pipeline_job(fingerprint, video)
        with sqlite3.connect(state_path) as db:
            db.execute(
                "INSERT INTO upload_stages VALUES (?, ?)",
                (fingerprint, json.dumps({"ass_path": str(ass)})),
            )
            db.execute(
                "INSERT INTO recording_review_overrides VALUES (?, ?, ?)",
                (fingerprint, json.dumps({"cover_path": str(cover)}), "now"),
            )

        result = self.manager.delete_pipeline_job(fingerprint, delete_files=True)

        self.assertGreaterEqual(result["deleted_file_count"], 3)
        self.assertFalse(video.exists())
        self.assertFalse(xml.exists())
        self.assertFalse(ass.exists())
        self.assertFalse(cover.exists())

        active_fingerprint = "c" * 64
        active_video = self.recordings / "active.flv"
        active_video.write_bytes(b"active")
        with sqlite3.connect(state_path) as db:
            db.execute(
                "INSERT INTO uploads VALUES (?, ?, ?)",
                (active_fingerprint, str(active_video), "processing"),
            )
        with self.assertRaisesRegex(RecorderConfigError, "仍在处理中"):
            self.manager.delete_pipeline_job(active_fingerprint, delete_files=True)
        self.assertTrue(active_video.exists())


if __name__ == "__main__":
    unittest.main()
