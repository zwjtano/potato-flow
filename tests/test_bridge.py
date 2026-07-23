import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge


class BridgeTests(unittest.TestCase):
    def test_profile_override_and_metadata(self):
        base = {
            "title_template": "{stem}",
            "description_template": "file={name}",
            "tags": ["default"],
            "profiles": [{"match": "*alice*", "tags": ["alice"], "source_url": "https://x"}],
        }
        video = Path("2026-alice-live.mp4")
        cfg = bridge.effective_config(base, video)
        title, description, tags = bridge.render_metadata(video, cfg)
        self.assertEqual(title, "2026-alice-live")
        self.assertEqual(description, "file=2026-alice-live.mp4")
        self.assertEqual(tags, ["alice"])
        self.assertEqual(cfg["source_url"], "https://x")

    def test_default_recording_title_uses_streamer_ai_topic_and_date(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "妮可罗宾_45ecd12026-07-23_09-45-06_中韩流行.flv"
            video.write_bytes(b"video")
            title, _, _ = bridge.render_metadata(
                video,
                {
                    "title_template": bridge.DEFAULT_TITLE_TEMPLATE,
                    "streamer_name": "妮可罗宾",
                },
                ai_topic="中韩流行歌单·点歌闲聊",
            )
        self.assertEqual(title, "【直播回放】妮可罗宾｜中韩流行歌单·点歌闲聊｜2026-07-23")

    def test_default_recording_title_falls_back_to_live_title(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "主播_abcdef2026-07-23_09-45-06_深夜歌回.flv"
            video.write_bytes(b"video")
            title, _, _ = bridge.render_metadata(
                video,
                {"title_template": bridge.DEFAULT_TITLE_TEMPLATE},
            )
        self.assertEqual(title, "【直播回放】主播｜深夜歌回｜2026-07-23")

    def test_input_keeps_xml_and_pairs_by_stem(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.mp4"
            xml = root / "clip.xml"
            video.write_bytes(b"video")
            xml.write_text("<i/>", encoding="utf-8")
            paths = bridge.input_paths([str(video), str(xml)], include_stdin=False)
            self.assertEqual(bridge.find_danmaku_xml(video, paths), xml.resolve())

    def test_state_deduplicates_completed_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            store = bridge.StateStore(Path(temp) / "state.sqlite3")
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"video")
            key = bridge.fingerprint(video)
            self.assertTrue(store.claim(key, video, "bilibili"))
            store.finish(key, "completed", {"ok": True})
            self.assertFalse(store.claim(key, video, "bilibili"))

    def test_state_persists_each_inspectable_pipeline_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            store = bridge.StateStore(Path(temp) / "state.sqlite3")
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"video")
            key = bridge.fingerprint(video)
            self.assertTrue(store.claim(key, video, "bilibili"))
            store.stage(key, "ass", "running", {"danmaku_xml": "clip.xml"})
            store.stage(key, "ass", "completed", {"ass_path": "clip.ass", "danmaku_count": 12})
            with store.connect() as db:
                rows = db.execute(
                    "SELECT stage, status, details_json FROM upload_stages WHERE fingerprint=? ORDER BY stage",
                    (key,),
                ).fetchall()
            stages = {row["stage"]: row for row in rows}
            self.assertEqual(set(stages), {"detect", "record", "ass", "ai", "upload"})
            self.assertEqual(stages["record"]["status"], "completed")
            self.assertEqual(stages["ass"]["status"], "completed")
            self.assertEqual(json.loads(stages["ass"]["details_json"])["danmaku_count"], 12)

    def test_retry_preserves_uploaded_bvid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.mp4"
            cover = root / "cover.jpg"
            cookie = root / "cookie.json"
            video.write_bytes(b"video")
            cover.write_bytes(b"cover")
            cookie.write_text("[]", encoding="utf-8")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "bilibili_cookies": str(cookie),
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            key = bridge.fingerprint(video)
            store.claim(key, video, "bilibili")
            store.finish(key, "failed", {"bilibili": {"bvid": "BV1existing"}}, "dm failed")

            class MustNotUpload:
                def __init__(self, **_kwargs):
                    raise AssertionError("retry must not instantiate uploader")

            with patch.object(bridge, "import_y2a", return_value=(MustNotUpload, None)):
                self.assertTrue(bridge.upload_one(video, cfg, store, retry=True))
            result = store.results(key)
            self.assertEqual(result["bilibili"]["bvid"], "BV1existing")
            self.assertFalse(video.exists())
            self.assertEqual(result["source_cleanup"]["deleted"], [str(video.resolve())])

    def test_cleanup_after_upload_removes_video_xml_and_transcoded_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "clip.flv"
            xml = root / "clip.xml"
            upload_video = root / "artifacts" / "clip.mp4"
            upload_video.parent.mkdir()
            for path in (video, xml, upload_video):
                path.write_bytes(b"data")

            result = bridge.cleanup_uploaded_recording(video, xml, upload_video)

            self.assertEqual(result["failed"], [])
            self.assertEqual(len(result["deleted"]), 3)
            self.assertTrue(all(not path.exists() for path in (video, xml, upload_video)))

    def test_live_segments_append_to_one_bilibili_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cover = root / "cover.jpg"
            cookie = root / "cookie.json"
            first = root / "主播_abcdef2026-07-23_09-00-00_直播.flv"
            second = root / "主播_abcdef2026-07-23_10-00-00_直播.flv"
            cover.write_bytes(b"cover")
            cookie.write_text("[]", encoding="utf-8")
            first.write_bytes(b"part-one")
            second.write_bytes(b"part-two")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "bilibili_cookies": str(cookie),
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
                "danmaku_enabled": False,
                "ai_danmaku_summary_enabled": False,
                "delete_recording_after_upload": False,
            }
            calls = []

            class FakeUploader:
                def __init__(self, **_kwargs):
                    pass

                def upload_video(self, **kwargs):
                    calls.append(kwargs)
                    existing = kwargs.get("existing_submission")
                    part_count = int((existing or {}).get("part_count") or 0) + 1
                    parts = list((existing or {}).get("uploaded_parts") or [])
                    parts.append({"filename": f"part-{part_count}", "title": f"P{part_count}"})
                    return True, {
                        "bvid": "BV1multipart",
                        "aid": 123,
                        "url": "https://www.bilibili.com/video/BV1multipart",
                        "part_count": part_count,
                        "uploaded_parts": parts,
                        "cover_url": "https://example.com/cover.jpg",
                    }

            store = bridge.StateStore(root / "state.sqlite3")
            with patch.object(bridge, "import_y2a", return_value=(FakeUploader, None)):
                self.assertTrue(bridge.upload_one(first, cfg, store, session_key="room-1"))
                self.assertTrue(bridge.upload_one(second, cfg, store, session_key="room-1"))

            self.assertIsNone(calls[0]["existing_submission"])
            self.assertEqual(calls[0]["page_titles"], ["P1 09:00:00"])
            self.assertEqual(calls[1]["existing_submission"]["bvid"], "BV1multipart")
            self.assertEqual(calls[1]["page_titles"], ["P2 10:00:00"])
            session = store.multipart_session("room-1")
            self.assertEqual(session["bilibili"]["part_count"], 2)
            self.assertTrue(store.close_multipart_session("room-1"))
            self.assertEqual(store.multipart_session("room-1"), {})

    def test_load_config_rejects_non_object(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps([]), encoding="utf-8")
            with self.assertRaises(ValueError):
                bridge.load_config(path)

    def test_dry_run_validates_without_importing_y2a(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "alice.mp4"
            xml = root / "alice.xml"
            cover = root / "cover.jpg"
            video.write_bytes(b"video")
            xml.write_text(
                '<i><d p="1.0,1,25,16777215,0,0,1,0">测试弹幕</d></i>',
                encoding="utf-8",
            )
            cover.write_bytes(b"cover")
            cfg = {
                "_config_dir": str(root),
                "source_url": "https://example.com/live",
                "bilibili_partition_id": "171",
                "cover_path": str(cover),
                "stable_checks": 1,
                "stable_interval_seconds": 0.01,
            }
            store = bridge.StateStore(root / "state.sqlite3")
            self.assertTrue(bridge.upload_one(video, cfg, store, dry_run=True, danmaku_xml=xml))
            row = store.recent(1)[0]
            self.assertEqual(row["status"], "dry_run")
            result = json.loads(row["result_json"])
            self.assertEqual(result["danmaku_count"], 1)
            self.assertTrue(Path(result["ass_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
