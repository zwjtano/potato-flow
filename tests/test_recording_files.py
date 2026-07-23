import os
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


if __name__ == "__main__":
    unittest.main()
