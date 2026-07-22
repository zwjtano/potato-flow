import unittest
from unittest.mock import patch

from bilibili_danmaku_importer import BilibiliDanmakuImporter
from danmaku_pipeline import DanmakuComment


class FakeImporter(BilibiliDanmakuImporter):
    def __init__(self):
        pass

    def wait_for_cid(self, bvid, *, wait_seconds=300, interval=8.0):
        return 12345

    def _post_one(self, bvid, cid, comment):
        return True, ""


class BilibiliDanmakuImporterTests(unittest.TestCase):
    @patch("bilibili_danmaku_importer.time.sleep", return_value=None)
    def test_import_reports_native_cid_and_counts(self, _sleep):
        comments = [DanmakuComment(float(index), f"弹幕{index}") for index in range(10)]
        result = FakeImporter().import_comments(
            "BV1test", comments, max_comments=5, interval_seconds=0.2
        )
        self.assertEqual(result.cid, 12345)
        self.assertEqual(result.imported, 5)
        self.assertEqual(result.skipped, 5)

    @patch("bilibili_danmaku_importer.time.sleep", return_value=None)
    def test_zero_limit_imports_every_comment_without_deduplication(self, _sleep):
        comments = [DanmakuComment(float(index), "重复反应") for index in range(12)]
        result = FakeImporter().import_comments(
            "BV1test", comments, max_comments=0, interval_seconds=0.2
        )
        self.assertEqual(result.requested, 12)
        self.assertEqual(result.imported, 12)
        self.assertEqual(result.skipped, 0)


if __name__ == "__main__":
    unittest.main()
