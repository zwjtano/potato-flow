import json
import sys
from pathlib import Path
from unittest import TestCase, mock


APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.bilibili_uploader import BilibiliUploader  # noqa: E402
from modules.live_recorder_manager import LiveRecorderManager, RecorderConfigError  # noqa: E402


class BilibiliChapterTests(TestCase):
    def test_room_chapter_modes_are_normalized(self):
        for mode in ("auto", "timeline", "bilibili_ai", "off"):
            self.assertEqual(LiveRecorderManager._normalize_bilibili_chapter_mode(mode), mode)
        with self.assertRaises(RecorderConfigError):
            LiveRecorderManager._normalize_bilibili_chapter_mode("unknown")

    def test_verified_timeline_becomes_contiguous_chapters(self):
        chapters = BilibiliUploader.chapters_from_timeline_lines(
            ["03:15 第一波团战", "18:40 肉山争夺", "01:02:03 决胜团"],
            duration_seconds=7200,
        )
        self.assertEqual(chapters[0], {"from": 0, "to": 1120, "content": "第一波团战"})
        self.assertEqual(chapters[-1]["to"], 7200)

    def test_verified_timeline_obeys_bilibili_limit_and_video_duration(self):
        lines = [f"{minute:02d}:00 事件{minute}" for minute in range(12)]
        chapters = BilibiliUploader.chapters_from_timeline_lines(
            lines, duration_seconds=665, chapter_limit=10
        )
        self.assertEqual(len(chapters), 10)
        self.assertEqual(chapters[-1]["to"], 665)
        self.assertLess(chapters[-1]["from"], chapters[-1]["to"])

    def test_preferred_generation_uses_timeline_before_ai(self):
        uploader = BilibiliUploader("cookies.json")
        with (
            mock.patch.object(uploader, "save_chapters", return_value=(True, {"saved": True, "chapters": [{}, {}]})) as save,
            mock.patch.object(uploader, "generate_ai_chapters") as ai,
        ):
            ok, result = uploader.generate_preferred_chapters(
                aid=1, cid=2, timeline_lines=["00:10 开场", "10:00 团战"], duration_seconds=900
            )
        self.assertTrue(ok)
        self.assertEqual(result["source"], "verified_timeline")
        save.assert_called_once()
        ai.assert_not_called()

    def test_preferred_generation_falls_back_to_bilibili_ai(self):
        uploader = BilibiliUploader("cookies.json")
        with mock.patch.object(
            uploader, "generate_ai_chapters", return_value=(True, {"saved": True, "chapters": [{}, {}]})
        ) as ai:
            ok, result = uploader.generate_preferred_chapters(
                aid=1, cid=2, timeline_lines=["00:10 只有一个点"], mode="auto"
            )
        self.assertTrue(ok)
        self.assertEqual(result["source"], "bilibili_ai")
        ai.assert_called_once()

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
