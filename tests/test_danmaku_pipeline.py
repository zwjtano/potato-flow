import tempfile
import unittest
from pathlib import Path

from danmaku_pipeline import (
    build_ass,
    format_comments_for_ai,
    inspect_biliup_xml,
    parse_biliup_xml,
    select_summary_comments,
)


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<i>
  <d p="1.250,1,25,16711680,0,0,1,0">第一条弹幕</d>
  <d p="2.500,5,25,16777215,0,0,2,0">顶部弹幕</d>
  <d p="3.750,4,25,65280,0,0,3,0">底部弹幕</d>
</i>
"""


class DanmakuPipelineTests(unittest.TestCase):
    def test_parse_and_build_ass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            xml_path = root / "clip.xml"
            ass_path = root / "clip.ass"
            xml_path.write_text(SAMPLE_XML, encoding="utf-8")
            comments = parse_biliup_xml(xml_path)
            self.assertEqual(len(comments), 3)
            self.assertEqual(comments[0].text, "第一条弹幕")
            build_ass(comments, ass_path, width=1280, height=720)
            self.assertTrue(ass_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            text = ass_path.read_text(encoding="utf-8-sig")
            self.assertIn("Title: 简体中文弹幕", text)
            self.assertIn("Original Script: PotatoFlow", text)
            self.assertIn("PlayResX: 1280", text)
            self.assertIn("\\move(", text)
            self.assertIn("\\an8", text)
            self.assertIn("底部弹幕", text)

    def test_ai_sampling_deduplicates_spam(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clip.xml"
            repeated = "".join(
                f'<d p="{index},1,25,16777215,0,0,1,0">同一条</d>' for index in range(20)
            )
            path.write_text(f"<i>{repeated}</i>", encoding="utf-8")
            selected = select_summary_comments(parse_biliup_xml(path), 20)
            self.assertEqual(len(selected), 2)
            self.assertNotIn("uid", format_comments_for_ai(selected).lower())

    def test_inspect_xml_reports_raw_valid_invalid_and_timeline_counts(self):
        path = self._write_xml(
            '<d p="10,1,25,16777215,0,0,1,0">第一条</d>'
            '<d p="invalid">坏节点</d>'
            '<d p="75.5,1,25,16777215,0,0,2,0">第二条</d>'
        )

        details = inspect_biliup_xml(path)

        self.assertEqual(details["danmaku_xml_entries"], 3)
        self.assertEqual(details["danmaku_count"], 2)
        self.assertEqual(details["danmaku_invalid_count"], 1)
        self.assertEqual(details["danmaku_first_second"], 10.0)
        self.assertEqual(details["danmaku_last_second"], 75.5)
        self.assertEqual(details["danmaku_timeline_span_seconds"], 65.5)

    def test_ai_comment_timestamps_use_bilibili_chapter_format(self):
        comments = parse_biliup_xml(
            self._write_xml(
                '<d p="65,1,25,16777215,0,0,1,0">一分五秒</d>'
                '<d p="3661,1,25,16777215,0,0,2,0">一小时一分一秒</d>'
            )
        )
        formatted = format_comments_for_ai(comments)
        self.assertIn("[01:05] 一分五秒", formatted)
        self.assertIn("[01:01:01] 一小时一分一秒", formatted)

    def _write_xml(self, body: str) -> Path:
        temp = tempfile.NamedTemporaryFile(suffix=".xml", delete=False)
        temp.close()
        path = Path(temp.name)
        path.write_text(f"<i>{body}</i>", encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        return path


if __name__ == "__main__":
    unittest.main()
