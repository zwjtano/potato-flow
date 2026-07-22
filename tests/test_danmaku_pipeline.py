import tempfile
import unittest
from pathlib import Path

from danmaku_pipeline import (
    build_ass,
    format_comments_for_ai,
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
            text = ass_path.read_text(encoding="utf-8-sig")
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


if __name__ == "__main__":
    unittest.main()
