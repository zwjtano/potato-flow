import sys
import time
import unittest
from pathlib import Path


Y2A_ROOT = Path(__file__).resolve().parents[1] / "y2a-auto"
sys.path.insert(0, str(Y2A_ROOT))

from modules.live_recorder_manager import LiveRecorderManager  # noqa: E402


class LiveRecorderStatusTests(unittest.TestCase):
    def setUp(self):
        self.rooms = [
            {
                "id": "aaaaaa111111",
                "name": "开播主播",
                "url": "https://www.douyu.com/100",
                "platform": "douyu",
            },
            {
                "id": "bbbbbb222222",
                "name": "离线主播",
                "url": "https://live.bilibili.com/200",
                "platform": "bilibili",
            },
        ]

    def test_maps_biliup_worker_status_per_room(self):
        payload = {
            "rooms": [
                {
                    "downloader_status": "Ok(Working)",
                    "live_streamer": {
                        "remark": "开播主播_aaaaaa",
                        "url": "https://www.douyu.com/100",
                    },
                },
                {
                    "downloader_status": "Ok(Idle)",
                    "live_streamer": {
                        "remark": "离线主播_bbbbbb",
                        "url": "https://live.bilibili.com/200",
                    },
                },
            ]
        }
        infos = [
            {
                "url": "https://www.douyu.com/100",
                "title": "真实直播标题",
                "date": int(time.time()) - 65,
            }
        ]

        rooms = LiveRecorderManager._merge_room_runtime(self.rooms, True, payload, infos)

        self.assertEqual(rooms[0]["runtime"]["state"], "recording")
        self.assertEqual(rooms[0]["runtime"]["label"], "录制中")
        self.assertGreaterEqual(rooms[0]["runtime"]["duration_seconds"], 65)
        self.assertEqual(rooms[0]["runtime"]["live_title"], "真实直播标题")
        self.assertEqual(rooms[1]["runtime"]["state"], "offline")
        self.assertEqual(rooms[1]["runtime"]["label"], "未开播")

    def test_engine_stopped_does_not_claim_room_is_monitored(self):
        rooms = LiveRecorderManager._merge_room_runtime(self.rooms, False)

        self.assertTrue(all(room["runtime"]["state"] == "stopped" for room in rooms))
        self.assertTrue(all(room["runtime"]["label"] == "引擎未启动" for room in rooms))


if __name__ == "__main__":
    unittest.main()
