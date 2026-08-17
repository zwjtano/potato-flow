import json
import sys
from pathlib import Path
from unittest import TestCase, mock


APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.bilibili_uploader import BilibiliUploader  # noqa: E402


class BilibiliChapterTests(TestCase):
    def test_save_chapters_uses_creator_center_contract(self):
        uploader = BilibiliUploader("cookies.json")
        with mock.patch.object(uploader, "_chapter_api", return_value={}) as request:
            ok, result = uploader.save_chapters(
                aid=123,
                cid=456,
                chapters=[
                    {"from": 0, "to": 60, "content": "开场"},
                    {"from": 60, "to": 180, "content": "关键团战"},
                ],
            )
        self.assertTrue(ok)
        self.assertTrue(result["saved"])
        call = request.call_args.kwargs
        self.assertEqual(call["url"], "https://member.bilibili.com/x/web/card/submit")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["data"]["type"], 2)
        self.assertEqual(json.loads(call["data"]["cards"])[1]["content"], "关键团战")

    def test_save_chapters_rejects_invalid_timeline(self):
        uploader = BilibiliUploader("cookies.json")
        ok, message = uploader.save_chapters(
            aid=123,
            cid=456,
            chapters=[
                {"from": 3, "to": 60, "content": "开场"},
                {"from": 50, "to": 80, "content": "重叠"},
            ],
        )
        self.assertFalse(ok)
        self.assertIn("00:00", message)

    def test_ai_generation_polls_then_saves(self):
        uploader = BilibiliUploader("cookies.json")
        responses = [
            {"task_id": "task", "poll_time": 200},
            {"state": 0, "chapters": []},
            {
                "state": 2,
                "chapters": [
                    {"from": 0, "to": 60, "content": "开场"},
                    {"from": 60, "to": 120, "content": "决胜"},
                ],
            },
        ]
        with (
            mock.patch.object(uploader, "_chapter_api", side_effect=responses),
            mock.patch.object(
                uploader,
                "save_chapters",
                return_value=(True, {"saved": True}),
            ) as save,
            mock.patch("modules.bilibili_uploader.time.sleep"),
        ):
            ok, result = uploader.generate_ai_chapters(aid=123, cid=456)
        self.assertTrue(ok)
        self.assertTrue(result["saved"])
        save.assert_called_once()

