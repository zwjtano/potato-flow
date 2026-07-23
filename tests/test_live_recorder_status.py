import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


Y2A_ROOT = Path(__file__).resolve().parents[1] / "y2a-auto"
sys.path.insert(0, str(Y2A_ROOT))

from modules.live_recorder_manager import LiveRecorderManager  # noqa: E402
import modules.live_recorder_manager as recorder_module  # noqa: E402


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

    def test_resolves_bilibili_streamer_name_avatar_and_real_room_id(self):
        manager = LiveRecorderManager()
        room_response = {
            "code": 0,
            "data": {
                "uid": 123,
                "room_id": 456,
                "title": "今晚歌回",
            },
        }
        master_response = {
            "code": 0,
            "data": {
                "info": {
                    "uname": "自动识别主播",
                    "face": "https://i0.hdslb.com/avatar.jpg",
                },
            },
        }
        with mock.patch.object(
            recorder_module,
            "_response_json",
            side_effect=[room_response, master_response],
        ):
            room = manager.resolve_room("https://live.bilibili.com/100")

        self.assertEqual(room["platform"], "bilibili")
        self.assertEqual(room["room_id"], "456")
        self.assertEqual(room["name"], "自动识别主播")
        self.assertEqual(room["avatar_url"], "https://i0.hdslb.com/avatar.jpg")
        self.assertEqual(room["url"], "https://live.bilibili.com/456")

    def test_resolves_douyu_streamer_name_and_avatar(self):
        manager = LiveRecorderManager()
        response = {
            "room": {
                "room_id": 9999,
                "room_name": "陪伴每一天",
                "owner_name": "yyfyyf",
                "owner_avatar": "https://apic.douyucdn.cn/avatar.jpg",
            },
        }
        with mock.patch.object(recorder_module, "_response_json", return_value=response):
            room = manager.resolve_room("https://www.douyu.com/9999")

        self.assertEqual(room["platform"], "douyu")
        self.assertEqual(room["room_id"], "9999")
        self.assertEqual(room["name"], "yyfyyf")
        self.assertEqual(room["avatar_url"], "https://apic.douyucdn.cn/avatar.jpg")

    def test_readding_legacy_room_updates_profile_without_duplicate(self):
        manager = LiveRecorderManager()
        legacy_room = {
            "id": "legacy-room",
            "name": "旧名称",
            "url": "https://www.douyu.com/9999",
            "platform": "douyu",
        }
        resolved = {
            "platform": "douyu",
            "platform_name": "斗鱼",
            "room_id": "9999",
            "name": "新名称",
            "avatar_url": "https://apic.douyucdn.cn/new.jpg",
            "url": "https://www.douyu.com/9999",
            "live_title": "直播标题",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            rooms_path = Path(temp_dir) / "rooms.json"
            rooms_path.write_text(json.dumps([legacy_room]), encoding="utf-8")
            with mock.patch.object(recorder_module, "ROOMS_PATH", rooms_path), mock.patch.object(
                manager, "resolve_room", return_value=resolved
            ), mock.patch.object(manager, "sync_configs"), mock.patch.object(
                manager, "_write_control_state"
            ):
                room = manager.add_room_from_url(resolved["url"])
            saved = json.loads(rooms_path.read_text(encoding="utf-8"))

        self.assertEqual(len(saved), 1)
        self.assertEqual(room["id"], "legacy-room")
        self.assertEqual(room["name"], "新名称")
        self.assertEqual(room["platform_room_id"], "9999")
        self.assertEqual(room["avatar_url"], "https://apic.douyucdn.cn/new.jpg")

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

    def test_manually_stopped_room_overrides_stale_worker_status(self):
        rooms = [dict(self.rooms[0], enabled=False)]
        payload = {
            "rooms": [
                {
                    "downloader_status": "Working",
                    "live_streamer": {"url": rooms[0]["url"], "remark": "开播主播_aaaaaa"},
                }
            ]
        }

        merged = LiveRecorderManager._merge_room_runtime(rooms, True, payload)

        self.assertEqual(merged[0]["runtime"]["state"], "paused")
        self.assertEqual(merged[0]["runtime"]["label"], "已手动停止")
        self.assertFalse(merged[0]["runtime"]["manual_enabled"])

    def test_stopping_one_room_persists_control_without_stopping_engine(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            rooms_path = Path(temp_dir) / "rooms.json"
            control_path = Path(temp_dir) / "control.json"
            rooms_path.write_text(json.dumps(self.rooms), encoding="utf-8")
            with mock.patch.object(recorder_module, "ROOMS_PATH", rooms_path), mock.patch.object(
                recorder_module, "CONTROL_PATH", control_path
            ), mock.patch.object(manager, "_pid", return_value=4321):
                room = manager.set_room_recording("aaaaaa111111", False)

            saved_rooms = json.loads(rooms_path.read_text(encoding="utf-8"))
            controls = json.loads(control_path.read_text(encoding="utf-8"))

        self.assertFalse(room["enabled"])
        self.assertFalse(saved_rooms[0]["enabled"])
        self.assertFalse(controls["rooms"]["https://www.douyu.com/100"])
        self.assertTrue(controls["rooms"]["https://live.bilibili.com/200"])

    def test_add_room_reloads_running_idle_worker(self):
        manager = LiveRecorderManager()
        new_room = {"id": "cccccc333333", "name": "新主播"}
        with mock.patch.object(manager, "_pid", return_value=4321), mock.patch.object(
            manager, "save_room", return_value=new_room
        ), mock.patch.object(
            manager,
            "rooms_with_status",
            return_value=[{"runtime": {"recording": False}}],
        ), mock.patch.object(manager, "stop") as stop, mock.patch.object(manager, "start") as start:
            room, state = manager.save_room_and_reload("新主播", "https://www.douyu.com/300")

        self.assertEqual(room, new_room)
        self.assertEqual(state, "reloaded")
        stop.assert_called_once_with()
        start.assert_called_once_with()

    def test_add_room_defers_reload_while_another_room_is_recording(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            reload_path = Path(temp_dir) / "reload.json"
            with mock.patch.object(recorder_module, "RELOAD_PATH", reload_path), mock.patch.object(
                manager, "_pid", return_value=4321
            ), mock.patch.object(
                manager, "save_room", return_value={"id": "cccccc333333"}
            ), mock.patch.object(
                manager,
                "rooms_with_status",
                return_value=[{"runtime": {"recording": True}}],
            ), mock.patch.object(manager, "_ensure_reload_thread") as ensure_thread:
                _, state = manager.save_room_and_reload("新主播", "https://www.douyu.com/300")
                marker_exists = reload_path.exists()

        self.assertEqual(state, "pending")
        self.assertTrue(marker_exists)
        ensure_thread.assert_called_once_with()

    def test_bridge_profiles_receive_streamer_name_and_default_title_template(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.config.json"
            config_path.write_text(
                json.dumps({"title_template": "{stem}"}, ensure_ascii=False),
                encoding="utf-8",
            )
            with mock.patch.object(recorder_module, "BRIDGE_CONFIG_PATH", config_path):
                manager._sync_bridge_profiles(self.rooms)
            config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(
            config["title_template"],
            "【直播回放】{streamer}｜{ai_topic}｜{date}",
        )
        self.assertEqual(config["profiles"][0]["streamer_name"], "开播主播")

    def test_headless_status_file_drives_room_state(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "pid": 4321,
                        "updated_at": time.time(),
                        "rooms": [
                            {
                                "downloader_status": "Working",
                                "live_streamer": {
                                    "remark": "开播主播_aaaaaa",
                                    "url": "https://www.douyu.com/100",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(recorder_module, "STATUS_PATH", status_path), mock.patch.object(
                manager,
                "_pid",
                return_value=4321,
            ), mock.patch.object(manager, "list_rooms", return_value=self.rooms):
                rooms = manager.rooms_with_status()

        self.assertEqual(rooms[0]["runtime"]["state"], "recording")
        self.assertEqual(rooms[1]["runtime"]["state"], "unknown")

    def test_manager_does_not_depend_on_legacy_http_port(self):
        source = (Y2A_ROOT / "modules" / "live_recorder_manager.py").read_text(encoding="utf-8")

        self.assertNotIn("19159", source)
        self.assertNotIn("BILIUP_API_BASE", source)
        self.assertIn('"recorder"', source)
        self.assertIn('"--status-file"', source)

    def test_pipeline_log_stays_open_across_status_refreshes(self):
        source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")

        self.assertIn("const jobLogStates = new Map()", source)
        self.assertIn("logState.open = event.currentTarget.open", source)
        self.assertIn("${logState.open ? 'open' : ''}", source)
        self.assertIn("loadJobLog(job.id, logPre, true)", source)
        self.assertIn("logState.stickToBottom", source)

    def test_add_room_form_only_requires_room_url(self):
        source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")

        self.assertNotIn('name="name"', source)
        self.assertIn("粘贴链接，自动识别主播名称和头像", source)
        self.assertIn("/live-recording/rooms/resolve", source)
        self.assertIn("room.avatar_url", source)


if __name__ == "__main__":
    unittest.main()
