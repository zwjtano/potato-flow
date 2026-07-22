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
