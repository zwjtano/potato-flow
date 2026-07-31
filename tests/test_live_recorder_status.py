import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


Y2A_ROOT = Path(__file__).resolve().parents[1] / "y2a-auto"
sys.path.insert(0, str(Y2A_ROOT))

from modules.live_recorder_manager import LiveRecorderManager, RecorderConfigError  # noqa: E402
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

    def test_default_recordings_directory_is_project_root_recordings(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            recorder_module,
            "RECORDINGS_DIR",
            Path(temp) / "recordings",
        ), mock.patch(
            "modules.config_manager.load_config",
            return_value={"RECORDINGS_PATH": "recordings"},
        ), mock.patch.object(
            Path,
            "is_dir",
            return_value=False,
        ):
            self.assertEqual(
                recorder_module.recordings_dir(),
                Path(temp) / "recordings",
            )

    def test_default_recordings_directory_uses_docker_mount_when_available(self):
        with mock.patch(
            "modules.config_manager.load_config",
            return_value={"RECORDINGS_PATH": "recordings"},
        ), mock.patch.object(
            Path,
            "is_dir",
            autospec=True,
            side_effect=lambda path: str(path) == "/data/recordings",
        ):
            self.assertEqual(
                recorder_module.recordings_dir(),
                Path("/data/recordings"),
            )

    def test_custom_relative_recordings_directory_resolves_from_workspace(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            recorder_module,
            "WORKSPACE_ROOT",
            Path(temp),
        ), mock.patch(
            "modules.config_manager.load_config",
            return_value={"RECORDINGS_PATH": "media/live"},
        ):
            expected = (Path(temp) / "media" / "live").resolve()
            self.assertEqual(recorder_module.recordings_dir(), expected)
            self.assertEqual(
                recorder_module.validate_recordings_dir("media/live"),
                expected,
            )
            self.assertTrue(expected.is_dir())

    def test_atomic_json_writes_through_persistent_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "data"
            app_dir = root / "app"
            data_dir.mkdir()
            app_dir.mkdir()
            target = data_dir / "bridge.config.json"
            target.write_text("{}", encoding="utf-8")
            link = app_dir / "bridge.config.json"
            link.symlink_to(target)

            recorder_module._atomic_json(link, {"enabled": True})

            self.assertTrue(link.is_symlink())
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"enabled": True})
            self.assertFalse((app_dir / "bridge.config.json.tmp").exists())

    def test_room_prompt_overrides_are_saved_per_room(self):
        manager = LiveRecorderManager()
        rooms = [dict(item) for item in self.rooms]
        with mock.patch.object(manager, "list_rooms", return_value=rooms), mock.patch.object(
            manager,
            "_sync_bridge_profiles",
        ) as sync_profiles, mock.patch.object(
            recorder_module,
            "_atomic_json",
        ) as atomic_json:
            saved = manager.save_room_prompts(
                "aaaaaa111111",
                title_prompt="突出关键英雄",
                description_prompt="按时间顺序总结",
                cover_prompt="使用蓝紫色",
            )

        self.assertEqual(saved["ai_title_prompt"], "突出关键英雄")
        self.assertEqual(saved["ai_description_prompt"], "按时间顺序总结")
        self.assertEqual(saved["ai_cover_prompt"], "使用蓝紫色")
        persisted_rooms = atomic_json.call_args.args[1]
        self.assertNotIn("ai_title_prompt", persisted_rooms[1])
        sync_profiles.assert_called_once_with(persisted_rooms)

    def test_room_prompt_defaults_are_available_to_ui(self):
        defaults = LiveRecorderManager.recording_prompt_defaults()

        self.assertEqual(set(defaults), {"title", "description", "cover"})
        self.assertIn("核心主题", defaults["title"])
        self.assertIn("完整中文简介", defaults["description"])
        self.assertIn("DOTA2", defaults["cover"])

    def test_room_can_upload_and_restore_custom_cover_reference(self):
        manager = LiveRecorderManager()
        rooms = [dict(item) for item in self.rooms]
        upload = mock.Mock()
        upload.filename = "character.png"
        upload.save.side_effect = lambda path: Path(path).write_bytes(b"image-bytes")

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            recorder_module,
            "ROOM_REFERENCE_DIR",
            Path(temp),
        ), mock.patch.object(
            manager,
            "list_rooms",
            return_value=rooms,
        ), mock.patch.object(
            manager,
            "_sync_bridge_profiles",
        ), mock.patch.object(
            recorder_module,
            "_atomic_json",
        ):
            saved = manager.save_room_prompts(
                "aaaaaa111111",
                cover_reference_file=upload,
                cover_reference_suffix=".png",
            )
            reference_path, reference_kind = manager.room_cover_reference(
                "aaaaaa111111"
            )

            self.assertEqual(saved["cover_reference_file"], "aaaaaa111111.png")
            self.assertEqual(reference_kind, "custom")
            self.assertEqual(reference_path, Path(temp) / "aaaaaa111111.png")

            restored = manager.save_room_prompts(
                "aaaaaa111111",
                restore_cover_reference=True,
            )
            self.assertNotIn("cover_reference_file", restored)
            self.assertFalse((Path(temp) / "aaaaaa111111.png").exists())


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

    def test_resolves_douyin_streamer_name_avatar_and_room_id(self):
        manager = LiveRecorderManager()
        page = b"""
        <script id="RENDER_DATA" type="application/json">
        %7B%22web_rid%22%3A%22778899%22%2C%22id_str%22%3A%221234567%22%2C
        %22nickname%22%3A%22DouyinHost%22%2C%22title%22%3A%22TonightLive%22%2C
        %22avatar_thumb%22%3A%7B%22url_list%22%3A%5B%22https%3A%2F%2Fexample.com%2Favatar.jpg%22%5D%7D%7D
        </script>
        """
        with mock.patch.object(
            recorder_module,
            "_open_url",
            return_value=(page, "https://live.douyin.com/778899"),
        ), mock.patch.object(recorder_module, "_douyin_cookie_header", return_value=""):
            room = manager.resolve_room("https://live.douyin.com/778899")

        self.assertEqual(room["platform"], "douyin")
        self.assertEqual(room["platform_name"], "抖音")
        self.assertEqual(room["room_id"], "1234567")
        self.assertEqual(room["name"], "DouyinHost")
        self.assertEqual(room["avatar_url"], "https://example.com/avatar.jpg")
        self.assertEqual(room["url"], "https://live.douyin.com/778899")

    def test_resolves_douyin_url_from_full_share_message(self):
        manager = LiveRecorderManager()
        page = b"""
        <script id="RENDER_DATA" type="application/json">
        %7B%22app%22%3A%7B%22user%22%3A%7B%22info%22%3A%7B
        %22secUid%22%3A%22target-sec-user-id%22%2C
        %22nickname%22%3A%22DouyinHost%22%2C
        %22avatarUrl%22%3A%22https%3A%2F%2Fexample.com%2Favatar.jpg%22%2C
        %22roomData%22%3A%7B%22webRid%22%3A%22778899%22%7D
        %7D%7D%7D%7D
        </script>
        """
        share_message = (
            "3- #在抖音，记录美好生活#【天才青争】正在直播，来和我一起支持Ta吧。"
            "复制下方链接，打开【抖音】，直接观看直播！ "
            "https://v.douyin.com/kG8lGVITcRI/ 9@9.com :7pm"
        )
        redirect_url = (
            "https://webcast.amemv.com/douyin/webcast/reflow/1234567"
            "?sec_user_id=viewer-sec-user-id"
            "&extra_params=%7B%22live_common_share_params%22%3A"
            "%22%7B%5C%22sec_relation_user_id%5C%22%3A"
            "%5C%22target-sec-user-id%5C%22%7D%22%7D"
        )
        with mock.patch.object(
            recorder_module,
            "_resolve_redirect_url",
            return_value=redirect_url,
        ), mock.patch.object(
            recorder_module,
            "_response_json",
            return_value={
                "status_code": 0,
                "data": {
                    "room": {
                        "id_str": "1234567",
                        "title": "TonightLive",
                        "owner": {
                            "sec_uid": "target-sec-user-id",
                            "web_rid": "778899",
                            "nickname": "DouyinHost",
                            "avatar_thumb": {
                                "url_list": [
                                    "https://example.com/avatar.jpg",
                                ],
                            },
                        },
                    },
                },
            },
        ), mock.patch.object(
            recorder_module,
            "_open_url",
            return_value=(page, "https://www.douyin.com/user/target-sec-user-id"),
        ) as open_url, mock.patch.object(
            recorder_module,
            "_douyin_cookie_header",
            return_value="",
        ):
            room = manager.resolve_room(share_message)

        open_url.assert_not_called()
        self.assertEqual(room["platform"], "douyin")
        self.assertEqual(room["room_id"], "1234567")
        self.assertEqual(room["sec_uid"], "target-sec-user-id")
        self.assertEqual(room["avatar_url"], "https://example.com/avatar.jpg")
        self.assertEqual(room["url"], "https://live.douyin.com/778899")

    def test_uses_streamer_name_from_douyin_share_message_when_page_omits_it(self):
        manager = LiveRecorderManager()
        page = b"""
        <script id="RENDER_DATA" type="application/json">
        %7B%22web_rid%22%3A%22778899%22%7D
        </script>
        """
        share_message = (
            "#在抖音，记录美好生活#【天才青争】正在直播，来和我一起支持Ta吧。"
            " https://v.douyin.com/kG8lGVITcRI/"
        )
        with mock.patch.object(
            recorder_module,
            "_resolve_redirect_url",
            return_value=(
                "https://webcast.amemv.com/douyin/webcast/reflow/1234567"
                "?sec_user_id=sec-user-id"
            ),
        ), mock.patch.object(
            recorder_module,
            "_response_json",
            side_effect=RecorderConfigError("reflow unavailable"),
        ), mock.patch.object(
            recorder_module,
            "_open_url",
            return_value=(page, "https://www.douyin.com/user/sec-user-id"),
        ), mock.patch.object(
            recorder_module,
            "_douyin_cookie_header",
            return_value="",
        ):
            room = manager.resolve_room(share_message)

        self.assertEqual(room["name"], "天才青争")
        self.assertNotEqual(room["name"], f"抖音主播{room['room_id']}")

    def test_rejects_douyin_room_when_real_streamer_name_is_unavailable(self):
        manager = LiveRecorderManager()
        page = b"""
        <script id="RENDER_DATA" type="application/json">
        %7B%22web_rid%22%3A%22778899%22%7D
        </script>
        """
        with mock.patch.object(
            recorder_module,
            "_open_url",
            return_value=(page, "https://live.douyin.com/778899"),
        ), mock.patch.object(
            recorder_module,
            "_douyin_cookie_header",
            return_value="",
        ):
            with self.assertRaisesRegex(
                RecorderConfigError,
                "没有返回真实主播资料",
            ):
                manager.resolve_room("https://live.douyin.com/778899")

    def test_resolves_numeric_douyu_vanity_room_id(self):
        manager = LiveRecorderManager()
        response = {
            "room": {
                "room_id": 6558897,
                "room_name": "果小果：备战宝可梦",
                "owner_name": "果小果是个弟弟",
                "owner_avatar": "https://apic.douyucdn.cn/fruit.jpg",
            },
        }
        mobile_page = (
            b'<script>window.__DATA__={"roomInfo":{"rid":6558897,'
            b'"vipId":5556,"nickname":"fruit"}}</script>'
        )
        with mock.patch.object(
            recorder_module,
            "_response_json",
            side_effect=[RecorderConfigError("not json"), response],
        ) as response_json, mock.patch.object(
            recorder_module,
            "_open_url",
            return_value=(mobile_page, "https://m.douyu.com/5556"),
        ):
            room = manager.resolve_room("https://www.douyu.com/5556")

        self.assertEqual(room["room_id"], "6558897")
        self.assertEqual(room["name"], "果小果是个弟弟")
        self.assertEqual(room["url"], "https://www.douyu.com/6558897")
        self.assertIn("/betard/5556", response_json.call_args_list[0].args[0])
        self.assertIn("/betard/6558897", response_json.call_args_list[1].args[0])

    def test_config_uploads_each_segment_and_closes_session_after_live(self):
        manager = LiveRecorderManager()
        room = {
            **self.rooms[0],
            "segment_enabled": True,
            "segment_minutes": 60,
            "multipart_enabled": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "biliup.yaml"
            with mock.patch.object(recorder_module, "CONFIG_DIR", root / "config"), \
                    mock.patch.object(recorder_module, "RECORDINGS_DIR", root / "recordings"), \
                    mock.patch.object(recorder_module, "LOG_PATH", root / "logs" / "recorder.log"), \
                    mock.patch.object(recorder_module, "PID_PATH", root / "run" / "recorder.pid"), \
                    mock.patch.object(recorder_module, "RECORDER_RUNTIME_DIR", root / "run" / "engine"), \
                    mock.patch.object(recorder_module, "BILIUP_CONFIG_PATH", config_path), \
                    mock.patch.object(manager, "_sync_bridge_profiles"):
                manager.sync_configs([room])

            content = config_path.read_text(encoding="utf-8")
            self.assertIn("file_size: null", content)
            self.assertIn('segment_time: "01:00:00"', content)
            recordings_root = root / "recordings"
            self.assertIn(
                f'filename_prefix: "{recordings_root}/{{streamer}}_{{title}}_%Y-%m-%d_%H-%M"',
                content,
            )
            self.assertIn(
                f'filename_prefix: "{recordings_root}/开播主播/'
                '开播主播_{title}_{live_start}/开播主播_{title}_%Y-%m-%d_%H-%M"',
                content,
            )
            self.assertNotIn("aaaaaa", content.split("filename_prefix:", 2)[-1].splitlines()[0])
            self.assertNotIn("file_size: 2621440000", content)
            self.assertIn("filtering_threshold: 0", content)
            self.assertIn("segment_processor:", content)
            self.assertIn("ingest --session-key", content)
            self.assertIn("aaaaaa111111", content)
            self.assertIn("finalize-session --session-key", content)

    def test_config_enables_douyin_danmaku_and_persisted_cookie(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "biliup.yaml"
            with mock.patch.object(recorder_module, "CONFIG_DIR", root / "config"), \
                    mock.patch.object(recorder_module, "RECORDINGS_DIR", root / "recordings"), \
                    mock.patch.object(recorder_module, "LOG_PATH", root / "logs" / "recorder.log"), \
                    mock.patch.object(recorder_module, "PID_PATH", root / "run" / "recorder.pid"), \
                    mock.patch.object(recorder_module, "BILIUP_CONFIG_PATH", config_path), \
                    mock.patch.object(recorder_module, "_douyin_cookie_header", return_value="sessionid=secret"), \
                    mock.patch.object(manager, "_sync_bridge_profiles"):
                manager.sync_configs([self.rooms[0]])

            content = config_path.read_text(encoding="utf-8")
            self.assertIn("douyin_danmaku: true", content)
            self.assertIn('douyin_cookie: "sessionid=secret"', content)

    def test_config_normalizes_bilibili_cookie_for_high_quality_recording(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            cookie_path = root / "bili_cookies.json"
            config_path = root / "biliup.yaml"
            cookie_path.write_text(
                json.dumps(
                    [
                        {"name": "SESSDATA", "value": "session"},
                        {"name": "bili_jct", "value": "csrf"},
                        {"name": "DedeUserID", "value": "123"},
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.object(recorder_module, "CONFIG_DIR", config_dir), \
                    mock.patch.object(recorder_module, "RECORDINGS_DIR", root / "recordings"), \
                    mock.patch.object(recorder_module, "LOG_PATH", root / "logs" / "recorder.log"), \
                    mock.patch.object(recorder_module, "PID_PATH", root / "run" / "recorder.pid"), \
                    mock.patch.object(recorder_module, "BILIUP_CONFIG_PATH", config_path), \
                    mock.patch.object(recorder_module, "_bilibili_cookie_path", return_value=cookie_path), \
                    mock.patch.object(recorder_module, "_douyin_cookie_header", return_value=""), \
                    mock.patch.object(manager, "_sync_bridge_profiles"):
                manager.sync_configs([self.rooms[1]])

            normalized_path = config_dir / "biliup.bilibili.cookies.json"
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            content = config_path.read_text(encoding="utf-8")

        self.assertEqual(
            normalized["cookie_info"]["cookies"][0],
            {"name": "SESSDATA", "value": "session"},
        )
        self.assertIn("bili_qn: 25000", content)
        self.assertIn(f'bili_cookie_file: "{normalized_path}"', content)

    def test_config_uploads_segments_as_independent_videos_by_default(self):
        manager = LiveRecorderManager()
        room = {
            **self.rooms[0],
            "segment_enabled": True,
            "segment_minutes": 60,
            "multipart_enabled": False,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "biliup.yaml"
            with mock.patch.object(recorder_module, "CONFIG_DIR", root / "config"), \
                    mock.patch.object(recorder_module, "RECORDINGS_DIR", root / "recordings"), \
                    mock.patch.object(recorder_module, "LOG_PATH", root / "logs" / "recorder.log"), \
                    mock.patch.object(recorder_module, "PID_PATH", root / "run" / "recorder.pid"), \
                    mock.patch.object(recorder_module, "BILIUP_CONFIG_PATH", config_path), \
                    mock.patch.object(manager, "_sync_bridge_profiles"):
                manager.sync_configs([room])

            content = config_path.read_text(encoding="utf-8")
            self.assertIn("segment_processor:", content)
            self.assertIn(" ingest", content)
            self.assertNotIn("ingest --session-key", content)
            self.assertNotIn("finalize-session", content)

    def test_config_can_record_a_room_without_segmentation(self):
        manager = LiveRecorderManager()
        room = {
            **self.rooms[0],
            "segment_enabled": False,
            "segment_minutes": 60,
            "multipart_enabled": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "biliup.yaml"
            with mock.patch.object(recorder_module, "CONFIG_DIR", root / "config"), \
                    mock.patch.object(recorder_module, "RECORDINGS_DIR", root / "recordings"), \
                    mock.patch.object(recorder_module, "LOG_PATH", root / "logs" / "recorder.log"), \
                    mock.patch.object(recorder_module, "PID_PATH", root / "run" / "recorder.pid"), \
                    mock.patch.object(recorder_module, "BILIUP_CONFIG_PATH", config_path), \
                    mock.patch.object(manager, "_sync_bridge_profiles"):
                manager.sync_configs([room])

            content = config_path.read_text(encoding="utf-8")
            self.assertIn("override:\n      segment_time: null", content)
            self.assertNotIn("ingest --session-key", content)
            self.assertNotIn("finalize-session", content)

    def test_room_recording_defaults_enable_sixty_minute_segments(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            rooms_path = Path(temp_dir) / "rooms.json"
            rooms_path.write_text(json.dumps([self.rooms[0]]), encoding="utf-8")
            with mock.patch.object(recorder_module, "ROOMS_PATH", rooms_path):
                room = manager.list_rooms()[0]

        self.assertTrue(room["segment_enabled"])
        self.assertEqual(room["segment_minutes"], 60)
        self.assertFalse(room["multipart_enabled"])
        self.assertFalse(room["record_only"])

    def test_room_recording_settings_are_saved_per_room(self):
        manager = LiveRecorderManager()
        rooms = [dict(item) for item in self.rooms]
        with mock.patch.object(manager, "list_rooms", return_value=rooms), mock.patch.object(
            manager, "_pid", return_value=None
        ), mock.patch.object(manager, "sync_configs") as sync_configs, mock.patch.object(
            manager, "_write_control_state"
        ), mock.patch.object(manager, "_clear_stale_multipart_session") as clear_session, mock.patch.object(
            recorder_module, "_atomic_json"
        ) as atomic_json:
            room, state = manager.save_room_recording_settings(
                "aaaaaa111111",
                segment_enabled=True,
                segment_minutes="90",
                multipart_enabled=True,
            )

        self.assertEqual(state, "saved")
        self.assertTrue(room["segment_enabled"])
        self.assertEqual(room["segment_minutes"], 90)
        self.assertTrue(room["multipart_enabled"])
        self.assertFalse(room["record_only"])
        persisted_rooms = atomic_json.call_args_list[0].args[1]
        self.assertNotIn("segment_minutes", persisted_rooms[1])
        sync_configs.assert_called_once_with(persisted_rooms)
        clear_session.assert_not_called()

    def test_recording_setting_change_safely_rotates_an_active_segment(self):
        manager = LiveRecorderManager()
        rooms = [{
            **self.rooms[0],
            "segment_enabled": True,
            "segment_minutes": 60,
            "multipart_enabled": True,
        }]
        runtime_rooms = [{
            **rooms[0],
            "runtime": {"recording": True},
        }]
        with mock.patch.object(manager, "list_rooms", return_value=rooms), mock.patch.object(
            manager, "_pid", return_value=123
        ), mock.patch.object(
            manager, "rooms_with_status", return_value=runtime_rooms
        ), mock.patch.object(manager, "sync_configs"), mock.patch.object(
            manager, "_write_control_state"
        ), mock.patch.object(manager, "_clear_stale_multipart_session") as clear_session, mock.patch.object(
            manager, "_ensure_reload_thread"
        ) as ensure_reload, mock.patch.object(recorder_module, "_atomic_json") as atomic_json:
            room, state = manager.save_room_recording_settings(
                "aaaaaa111111",
                segment_enabled=True,
                segment_minutes="30",
                multipart_enabled=False,
            )

        self.assertEqual(state, "pending")
        self.assertFalse(room["multipart_enabled"])
        self.assertTrue(atomic_json.call_args_list[-1].args[1]["force_segment_boundary"])
        clear_session.assert_not_called()
        ensure_reload.assert_called_once()

    def test_record_only_room_keeps_files_without_upload_hooks(self):
        manager = LiveRecorderManager()
        room = {
            **self.rooms[0],
            "segment_enabled": True,
            "segment_minutes": 60,
            "multipart_enabled": True,
            "record_only": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "biliup.yaml"
            with mock.patch.object(recorder_module, "CONFIG_DIR", root / "config"), \
                    mock.patch.object(recorder_module, "RECORDINGS_DIR", root / "recordings"), \
                    mock.patch.object(recorder_module, "LOG_PATH", root / "logs" / "recorder.log"), \
                    mock.patch.object(recorder_module, "PID_PATH", root / "run" / "recorder.pid"), \
                    mock.patch.object(recorder_module, "BILIUP_CONFIG_PATH", config_path), \
                    mock.patch.object(manager, "_sync_bridge_profiles"):
                manager.sync_configs([room])

            content = config_path.read_text(encoding="utf-8")
            self.assertIn("record-only --room-id", content)
            self.assertNotIn(" ingest", content)
            self.assertNotIn("finalize-session", content)
            self.assertFalse(manager.room_multipart_enabled(room))

    def test_readding_legacy_room_updates_profile_without_duplicate(self):
        manager = LiveRecorderManager()
        legacy_room = {
            "id": "legacy-room",
            "name": "旧名称",
            "url": "https://www.douyu.com/9999",
            "platform": "douyu",
            "segment_enabled": False,
            "segment_minutes": 90,
            "multipart_enabled": False,
            "record_only": True,
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
        self.assertFalse(room["segment_enabled"])
        self.assertEqual(room["segment_minutes"], 90)
        self.assertTrue(room["record_only"])

    def test_add_room_applies_recording_settings_at_creation(self):
        manager = LiveRecorderManager()
        resolved = {
            "platform": "douyu",
            "platform_name": "斗鱼",
            "room_id": "9999",
            "name": "yyfyyf",
            "avatar_url": "https://example.com/avatar.jpg",
            "url": "https://www.douyu.com/9999",
            "live_title": "陪伴每一天",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            rooms_path = Path(temp_dir) / "rooms.json"
            with mock.patch.object(recorder_module, "ROOMS_PATH", rooms_path), mock.patch.object(
                manager, "resolve_room", return_value=resolved
            ), mock.patch.object(manager, "sync_configs"), mock.patch.object(
                manager, "_write_control_state"
            ):
                room = manager.add_room_from_url(
                    resolved["url"],
                    segment_enabled=True,
                    segment_minutes=45,
                    multipart_enabled=True,
                    record_only=True,
                )

        self.assertTrue(room["segment_enabled"])
        self.assertEqual(room["segment_minutes"], 45)
        self.assertTrue(room["record_only"])
        self.assertFalse(room["multipart_enabled"])

    def test_search_rooms_returns_douyu_candidates(self):
        manager = LiveRecorderManager()
        payload = {
            "data": {
                "list": [{
                    "roomId": 9999,
                    "nickname": "yyfyyf",
                    "avatar": "//example.com/avatar.jpg",
                    "roomName": "陪伴每一天",
                    "cateName": "DOTA2",
                    "isLive": 1,
                }]
            }
        }
        with mock.patch.object(
            recorder_module,
            "_post_form_json",
            return_value=payload,
        ), mock.patch.object(
            manager,
            "_search_bilibili_rooms",
            return_value=[],
        ):
            result = manager.search_rooms_with_diagnostics("YYF")

        rooms = result["rooms"]
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["url"], "https://www.douyu.com/9999")
        self.assertEqual(rooms[0]["name"], "yyfyyf")
        self.assertEqual(rooms[0]["avatar_url"], "https://example.com/avatar.jpg")
        self.assertTrue(rooms[0]["is_live"])
        self.assertEqual(result["platforms"][1]["count"], 1)

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
        self.assertEqual(rooms[0]["runtime"]["segment_time"], "01:00:00")

    def test_attaches_current_video_file_to_recording_room(self):
        manager = LiveRecorderManager()
        rooms = LiveRecorderManager._merge_room_runtime(
            self.rooms[:1],
            True,
            {
                "rooms": [{
                    "downloader_status": "Ok(Working)",
                    "live_streamer": {
                        "remark": "开播主播_aaaaaa",
                        "url": "https://www.douyu.com/100",
                    },
                }]
            },
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            recording_path = Path(temp_dir) / "开播主播_aaaaaa_2026-07-24.flv"
            recording_path.write_bytes(b"video")
            with mock.patch.object(recorder_module, "RECORDINGS_DIR", Path(temp_dir)):
                enriched = manager._attach_current_recording_files(rooms)

        self.assertEqual(enriched[0]["runtime"]["current_file"], recording_path.name)
        self.assertEqual(enriched[0]["runtime"]["current_file_size_bytes"], 5)

    def test_prefers_current_ffmpeg_part_over_previous_finalized_segment(self):
        manager = LiveRecorderManager()
        rooms = LiveRecorderManager._merge_room_runtime(
            self.rooms[:1],
            True,
            {
                "rooms": [{
                    "downloader_status": "Ok(Working)",
                    "live_streamer": {
                        "remark": "开播主播_aaaaaa",
                        "url": "https://www.douyu.com/100",
                    },
                }]
            },
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            previous = root / "开播主播_aaaaaa_2026-07-24_13-00.flv"
            current = root / "开播主播_aaaaaa_2026-07-24_14-00.flv.part"
            previous.write_bytes(b"previous")
            current.write_bytes(b"current")
            os.utime(previous, (time.time() - 60, time.time() - 60))
            with mock.patch.object(recorder_module, "RECORDINGS_DIR", root):
                enriched = manager._attach_current_recording_files(rooms)

        self.assertEqual(enriched[0]["runtime"]["current_file"], current.name)
        self.assertEqual(enriched[0]["runtime"]["current_file_size_bytes"], 7)

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

    def test_restarting_room_clears_stale_failed_multipart_session(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rooms_path = root / "rooms.json"
            control_path = root / "control.json"
            state_path = root / "state.sqlite3"
            rooms = [dict(self.rooms[0], enabled=False)]
            rooms_path.write_text(json.dumps(rooms), encoding="utf-8")
            with sqlite3.connect(state_path) as db:
                db.execute(
                    """CREATE TABLE multipart_sessions (
                       session_key TEXT PRIMARY KEY, status TEXT NOT NULL,
                       result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL)"""
                )
                db.execute(
                    "INSERT INTO multipart_sessions VALUES (?, 'open', '{}', 'old', 'old')",
                    ("aaaaaa111111",),
                )

            with mock.patch.object(recorder_module, "ROOMS_PATH", rooms_path), mock.patch.object(
                recorder_module, "CONTROL_PATH", control_path
            ), mock.patch.object(manager, "_pipeline_state_path", return_value=state_path), mock.patch.object(
                manager, "_pid", return_value=4321
            ):
                room = manager.set_room_recording("aaaaaa111111", True)

            with sqlite3.connect(state_path) as db:
                session = db.execute(
                    "SELECT status FROM multipart_sessions WHERE session_key=?",
                    ("aaaaaa111111",),
                ).fetchone()

        self.assertTrue(room["enabled"])
        self.assertIsNone(session)

    def test_pipeline_state_path_matches_bridge_config_symlink_target(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_root = root / "app"
            data_root = root / "data"
            app_root.mkdir()
            data_root.mkdir()
            real_config = data_root / "bridge.config.json"
            real_config.write_text(
                json.dumps({"state_db": ".bridge/state.sqlite3"}),
                encoding="utf-8",
            )
            config_link = app_root / "bridge.config.json"
            config_link.symlink_to(real_config)

            with mock.patch.object(recorder_module, "BRIDGE_CONFIG_PATH", config_link):
                state_path = manager._pipeline_state_path()

        self.assertEqual(state_path, (data_root / ".bridge" / "state.sqlite3").resolve())

    def test_stale_pid_reused_by_web_process_is_not_treated_as_worker(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pid_path = root / "recorder.pid"
            status_path = root / "status.json"
            pid_path.write_text(str(os.getpid()), encoding="utf-8")
            status_path.write_text(
                json.dumps({"pid": os.getpid(), "updated_at": time.time() - 90}),
                encoding="utf-8",
            )
            with mock.patch.object(recorder_module, "PID_PATH", pid_path), mock.patch.object(
                recorder_module, "STATUS_PATH", status_path
            ):
                pid = manager._pid()

            self.assertIsNone(pid)
            self.assertFalse(pid_path.exists())

    def test_container_restart_marks_interrupted_cover_as_retryable_failure(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            with sqlite3.connect(state_path) as db:
                db.executescript(
                    """
                    CREATE TABLE uploads (
                        fingerprint TEXT PRIMARY KEY, status TEXT, error TEXT, updated_at TEXT
                    );
                    CREATE TABLE upload_stages (
                        fingerprint TEXT, stage TEXT, status TEXT, error TEXT,
                        finished_at TEXT, updated_at TEXT
                    );
                    """
                )
                db.execute(
                    "INSERT INTO uploads VALUES ('job-1', 'processing', NULL, 'old')"
                )
                db.execute(
                    "INSERT INTO upload_stages VALUES ('job-1', 'cover', 'running', NULL, NULL, 'old')"
                )

            with mock.patch.object(manager, "_pipeline_state_path", return_value=state_path):
                recovered = manager.recover_interrupted_pipeline_jobs()

            with sqlite3.connect(state_path) as db:
                upload = db.execute(
                    "SELECT status, error FROM uploads WHERE fingerprint='job-1'"
                ).fetchone()
                stage = db.execute(
                    "SELECT status, error, finished_at FROM upload_stages WHERE fingerprint='job-1'"
                ).fetchone()

        self.assertEqual(recovered, 1)
        self.assertEqual(upload[0], "failed")
        self.assertIn("点击重试", upload[1])
        self.assertEqual(stage[0], "failed")
        self.assertIn("点击重试", stage[1])
        self.assertIsNotNone(stage[2])

    def test_container_restart_repairs_failed_stage_with_processing_parent(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            with sqlite3.connect(state_path) as db:
                db.executescript(
                    """
                    CREATE TABLE uploads (
                        fingerprint TEXT PRIMARY KEY, status TEXT, error TEXT, updated_at TEXT
                    );
                    CREATE TABLE upload_stages (
                        fingerprint TEXT, stage TEXT, status TEXT, error TEXT,
                        finished_at TEXT, updated_at TEXT
                    );
                    """
                )
                db.execute(
                    "INSERT INTO uploads VALUES ('job-1', 'processing', NULL, 'old')"
                )
                db.execute(
                    """INSERT INTO upload_stages
                       VALUES ('job-1', 'upload', 'failed', '上传中断', 'old', 'old')"""
                )

            with mock.patch.object(manager, "_pipeline_state_path", return_value=state_path):
                recovered = manager.recover_interrupted_pipeline_jobs()

            with sqlite3.connect(state_path) as db:
                upload = db.execute(
                    "SELECT status, error FROM uploads WHERE fingerprint='job-1'"
                ).fetchone()

        self.assertEqual(recovered, 1)
        self.assertEqual(upload[0], "failed")
        self.assertIn("点击重试", upload[1])

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

    def test_delete_room_reloads_running_idle_worker(self):
        manager = LiveRecorderManager()
        with mock.patch.object(manager, "_pid", return_value=4321), mock.patch.object(
            manager, "list_rooms", side_effect=[self.rooms, [self.rooms[1]]]
        ), mock.patch.object(
            manager,
            "rooms_with_status",
            return_value=[
                dict(self.rooms[0], runtime={"recording": False}),
                dict(self.rooms[1], runtime={"recording": False}),
            ],
        ), mock.patch.object(manager, "delete_room", return_value=True) as delete, mock.patch.object(
            manager, "stop"
        ) as stop, mock.patch.object(manager, "start") as start:
            state = manager.delete_room_and_reload("aaaaaa111111")

        self.assertEqual(state, "reloaded")
        delete.assert_called_once_with("aaaaaa111111")
        stop.assert_called_once_with()
        start.assert_called_once_with()

    def test_delete_recording_room_requires_safe_stop_first(self):
        manager = LiveRecorderManager()
        with mock.patch.object(manager, "_pid", return_value=4321), mock.patch.object(
            manager, "list_rooms", return_value=self.rooms
        ), mock.patch.object(
            manager,
            "rooms_with_status",
            return_value=[
                dict(self.rooms[0], runtime={"recording": True}),
                dict(self.rooms[1], runtime={"recording": False}),
            ],
        ), mock.patch.object(manager, "delete_room") as delete:
            with self.assertRaises(RecorderConfigError):
                manager.delete_room_and_reload("aaaaaa111111")

        delete.assert_not_called()

    def test_delete_room_defers_reload_while_other_room_records(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            reload_path = Path(temp_dir) / "reload.json"
            with mock.patch.object(recorder_module, "RELOAD_PATH", reload_path), mock.patch.object(
                manager, "_pid", return_value=4321
            ), mock.patch.object(
                manager, "list_rooms", side_effect=[self.rooms, [self.rooms[1]]]
            ), mock.patch.object(
                manager,
                "rooms_with_status",
                return_value=[
                    dict(self.rooms[0], runtime={"recording": False}),
                    dict(self.rooms[1], runtime={"recording": True}),
                ],
            ), mock.patch.object(manager, "delete_room", return_value=True), mock.patch.object(
                manager, "_ensure_reload_thread"
            ) as ensure_thread:
                state = manager.delete_room_and_reload("aaaaaa111111")

            self.assertTrue(reload_path.exists())

        self.assertEqual(state, "pending")
        ensure_thread.assert_called_once_with()

    def test_bridge_profiles_receive_streamer_name_and_default_title_template(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "title_template": "{stem}",
                        "bilibili_cookies": "y2a-auto/cookies/bili_cookies.json",
                        "danmaku_fonts_dir": "y2a-auto/fonts",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(recorder_module, "BRIDGE_CONFIG_PATH", config_path):
                rooms = [dict(self.rooms[0], avatar_url="https://example.com/a.jpg"), self.rooms[1]]
                manager._sync_bridge_profiles(rooms)
            config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(
            config["title_template"],
            "{streamer}｜{ai_topic}｜{date}",
        )
        self.assertEqual(config["profiles"][0]["streamer_name"], "开播主播")
        self.assertEqual(
            config["profiles"][0]["streamer_avatar_url"],
            "https://example.com/a.jpg",
        )
        self.assertEqual(
            config["bilibili_cookies"],
            str(recorder_module.APP_ROOT / "cookies" / "bili_cookies.json"),
        )
        self.assertEqual(
            config["profiles"][0]["bilibili_cookies"],
            str(recorder_module.APP_ROOT / "cookies" / "bili_cookies.json"),
        )
        self.assertEqual(
            config["danmaku_fonts_dir"],
            str(recorder_module.APP_ROOT / "fonts"),
        )

    def test_bridge_profiles_preserve_absolute_runtime_paths(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bilibili_cookies": "/custom/cookies.json",
                        "danmaku_fonts_dir": "/custom/fonts",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(recorder_module, "BRIDGE_CONFIG_PATH", config_path):
                manager._sync_bridge_profiles(self.rooms)
            config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["bilibili_cookies"], "/custom/cookies.json")
        self.assertEqual(config["danmaku_fonts_dir"], "/custom/fonts")

    def test_bridge_profiles_migrate_previous_default_title_template(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "title_template": "【直播回放】{streamer}｜{ai_topic}｜{date}",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(recorder_module, "BRIDGE_CONFIG_PATH", config_path):
                manager._sync_bridge_profiles(self.rooms)
            config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(
            config["title_template"],
            "{streamer}｜{ai_topic}｜{date}",
        )

    def test_bridge_profiles_migrate_legacy_description_and_enable_pinned_comment(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.config.json"
            config_path.write_text(
                json.dumps({"description_template": "直播录播：{stem}"}),
                encoding="utf-8",
            )
            with mock.patch.object(recorder_module, "BRIDGE_CONFIG_PATH", config_path):
                manager._sync_bridge_profiles(self.rooms)
            config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["description_template"], "{recording_intro}")
        self.assertTrue(config["post_description_comment"])
        self.assertTrue(config["pin_description_comment"])

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

    def test_live_page_does_not_load_finished_pipeline_history(self):
        source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")

        self.assertNotIn("const jobLogStates = new Map()", source)
        self.assertNotIn("loadJobLog(", source)
        self.assertNotIn('data-role="job-select"', source)
        self.assertNotIn("bindCurrentPipeline", source)
        self.assertNotIn("当前录制流程", source)
        self.assertNotIn("最近事件", source)
        self.assertIn("generated-files-card", source)
        self.assertIn("显示该直播间最近生成的视频、XML 弹幕和 ASS", source)

    def test_recording_dark_mode_covers_stop_modal_and_mobile_progress(self):
        stylesheet = (Y2A_ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
        tasks = (Y2A_ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")

        self.assertIn(
            'html[data-theme="dark"] .stop-recording-modal .modal-content',
            stylesheet,
        )
        self.assertIn(
            'html[data-theme="dark"] .delete-room-modal .modal-content',
            stylesheet,
        )
        self.assertIn(
            'html[data-theme="dark"] .recording-task-card .recording-progress-trigger',
            tasks,
        )
        self.assertIn("{{ job.progress_label }}", tasks)

    def test_recording_details_show_partition_names_and_hide_empty_ai_fields(self):
        tasks = (Y2A_ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")
        app_source = (Y2A_ROOT / "app.py").read_text(encoding="utf-8")
        bridge_source = (Y2A_ROOT.parent / "bridge.py").read_text(encoding="utf-8")

        self.assertIn("bilibili_partition_names=_build_bilibili_partition_name_map()", app_source)
        self.assertIn("const recordingPartitionNames = {{ bilibili_partition_names | tojson }};", tasks)
        self.assertIn("recordingPartitionNames[String(value)]", tasks)
        self.assertIn("Math.round(confidence * 100)", tasks)
        self.assertIn("rule_fallback: '规则兜底'", tasks)
        self.assertIn("shouldShowRecordingDetail", tasks)
        self.assertIn("unverified_hero_description_removed: '已清理未验证英雄描述'", tasks)
        self.assertIn("streamer_neutral: '对局中立装备'", tasks)
        self.assertIn("streamer_scepter: '已有 A 杖'", tasks)
        self.assertIn("streamer_shard: '已有魔晶'", tasks)
        self.assertGreaterEqual(bridge_source.count('"streamer_neutral"'), 2)

    def test_recording_cover_refreshes_when_pipeline_exposes_generated_image(self):
        tasks = (Y2A_ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")

        self.assertIn('data-recording-cover-available="{{ 1 if job.cover_available else 0 }}"', tasks)
        self.assertIn('data-recording-cover-version="{{ job.cover_updated_at }}"', tasks)
        self.assertIn("scheduleRecordingCoverRefresh(jobId)", tasks)
        self.assertIn("await refreshTasksData(true)", tasks)
        self.assertIn("if (refreshInProgress)", tasks)
        self.assertIn("window.setTimeout(refreshWhenIdle, 100)", tasks)
        self.assertNotIn(
            "root.dataset.recordingCoverAvailable = coverAvailable",
            tasks,
        )
        self.assertNotIn(
            "root.dataset.recordingCoverVersion = coverVersion",
            tasks,
        )

    def test_live_room_files_and_selected_room_refresh_without_page_reload(self):
        template = (Y2A_ROOT / "templates" / "live_recording.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-generated-files-room="{{ room.id }}"', template)
        self.assertIn("function renderRoomFileLists()", template)
        self.assertIn("list.replaceChildren(...nextRows)", template)
        self.assertIn("existing?.dataset.fileSignature === fileSignature", template)
        self.assertIn("loadFiles({silent: true})", template)
        self.assertIn("window.setInterval(refreshLiveRecordingPage, 5000)", template)
        self.assertIn("function selectRoom(roomId, updateLocation = true)", template)
        self.assertIn('data-room-uid="{{ room.uid }}"', template)
        self.assertIn("url.searchParams.set('room', selectedItem.dataset.roomUid)", template)
        self.assertNotIn("selectedItem.dataset.roomUid || roomId", template)
        self.assertIn('data-action="refresh-live-recording"', template)
        self.assertNotIn(
            "href=\"{{ url_for('live_recording') }}\"><i class=\"bi bi-arrow-clockwise\"></i> 刷新",
            template,
        )

    def test_room_uid_prefers_platform_room_id_and_falls_back_to_url(self):
        self.assertEqual(
            LiveRecorderManager.room_uid({
                "id": "internal-uuid",
                "platform_room_id": "9999",
                "url": "https://www.douyu.com/100",
            }),
            "9999",
        )
        self.assertEqual(
            LiveRecorderManager.room_uid({
                "id": "internal-uuid",
                "url": "https://live.bilibili.com/200?from=search",
            }),
            "200",
        )
        self.assertEqual(
            LiveRecorderManager.room_uid({"id": "internal-uuid"}),
            "",
        )

    def test_pipeline_jobs_expose_unified_task_metadata(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            with sqlite3.connect(state_path) as db:
                db.executescript(
                    """
                    CREATE TABLE uploads (
                        fingerprint TEXT PRIMARY KEY, video_path TEXT, platform TEXT,
                        status TEXT, attempts INTEGER, result_json TEXT, error TEXT,
                        created_at TEXT, updated_at TEXT
                    );
                    CREATE TABLE upload_stages (
                        fingerprint TEXT, stage TEXT, status TEXT, details_json TEXT,
                        error TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT
                    );
                    """
                )
                db.execute(
                    "INSERT INTO uploads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "fingerprint-123",
                        "/data/recordings/Alice_abcdef2026-07-23_09-00-00.flv",
                        "bilibili",
                        "completed",
                        1,
                        json.dumps({"bilibili": {"bvid": "BV1potato", "url": "https://www.bilibili.com/video/BV1potato"}}),
                        None,
                        "2026-07-23T01:00:00+00:00",
                        "2026-07-23T02:00:00+00:00",
                    ),
                )
                for stage in ("detect", "record", "ass", "ai", "upload"):
                    details = (
                        {"video_duration_seconds": 647.4}
                        if stage == "ass"
                        else {"title": "【直播回放】Alice｜测试主题｜2026-07-23"}
                        if stage == "upload"
                        else {}
                    )
                    db.execute(
                        "INSERT INTO upload_stages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "fingerprint-123", stage, "completed", json.dumps(details),
                            None, None, None, "2026-07-23T02:00:00+00:00",
                        ),
                    )
            rooms = [{
                "id": "abcdef123456",
                "name": "Alice",
                "avatar_url": "https://example.com/alice.jpg",
                "platform": "bilibili",
            }]
            with mock.patch.object(manager, "_pipeline_state_path", return_value=state_path), mock.patch.object(
                manager, "list_rooms", return_value=rooms
            ):
                jobs = manager.pipeline_jobs()
            with sqlite3.connect(state_path) as db:
                persisted_display_id = db.execute(
                    "SELECT display_id FROM recording_display_ids WHERE fingerprint = ?",
                    ("fingerprint-123",),
                ).fetchone()[0]

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source"], "recording")
        self.assertEqual(jobs[0]["display_id"], "BL-ALICE-0723-001")
        self.assertEqual(jobs[0]["room_name"], "Alice")
        self.assertIn("bilibili_account_avatar_url", jobs[0])
        self.assertEqual(jobs[0]["bvid"], "BV1potato")
        self.assertTrue(jobs[0]["cover_available"])
        self.assertTrue(jobs[0]["cover_route_available"])
        self.assertFalse(jobs[0]["local_cover_available"])
        self.assertEqual(jobs[0]["completed_stages"], 5)
        self.assertEqual(jobs[0]["progress_label"], "全部处理完成")
        self.assertEqual(jobs[0]["title"], "【直播回放】Alice｜测试主题｜2026-07-23")
        self.assertEqual(jobs[0]["duration_seconds"], 647)
        self.assertEqual(jobs[0]["duration_text"], "10:47")
        self.assertEqual(persisted_display_id, "BL-ALICE-0723-001")

    def test_pipeline_display_ids_use_stable_daily_sequence(self):
        manager = LiveRecorderManager()
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.execute(
                """CREATE TABLE uploads (
                    fingerprint TEXT PRIMARY KEY, video_path TEXT, platform TEXT,
                    created_at TEXT
                )"""
            )
            db.executemany(
                "INSERT INTO uploads VALUES (?, ?, ?, ?)",
                [
                    (
                        "first",
                        "/data/recordings/YYF_abcdef2026-07-28_09-00-00.flv",
                        "bilibili",
                        "2026-07-28T01:00:00+00:00",
                    ),
                    (
                        "second",
                        "/data/recordings/YYF_abcdef2026-07-28_10-00-00.flv",
                        "bilibili",
                        "2026-07-28T02:00:00+00:00",
                    ),
                ],
            )
            markers = [{
                "id": "room-1",
                "name": "YYF",
                "avatar_url": "",
                "platform": "douyu",
                "marker": "YYF",
            }]
            assigned = manager._ensure_pipeline_display_ids(db, markers)
            self.assertEqual(assigned["first"], "DYU-YYF-0728-001")
            self.assertEqual(assigned["second"], "DYU-YYF-0728-002")

            markers[0]["name"] = "YYF-Renamed"
            assigned_again = manager._ensure_pipeline_display_ids(db, markers)
            self.assertEqual(assigned_again, assigned)

    def test_pipeline_cover_fetches_bilibili_cover_by_bvid_and_caches_it(self):
        manager = LiveRecorderManager()
        fingerprint = "a" * 64
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = {
                "recordings": root / "recordings",
                "artifacts": root / "artifacts",
            }
            job = {
                "bvid": "BV1potato",
                "bilibili_cover_url": "",
                "review_override": {},
                "stages": [],
            }
            with mock.patch.object(manager, "pipeline_job", return_value=job), mock.patch.object(
                manager, "_recording_file_roots", return_value=roots
            ), mock.patch.object(
                recorder_module,
                "_response_json",
                return_value={"data": {"pic": "https://i0.hdslb.com/cover.jpg"}},
            ) as response_json, mock.patch.object(
                recorder_module,
                "_open_url",
                return_value=(b"\xff\xd8\xff" + b"x" * 128, "https://i0.hdslb.com/cover.jpg"),
            ) as open_url:
                first = manager.pipeline_cover(fingerprint)
                second = manager.pipeline_cover(fingerprint)

            self.assertEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertEqual(first.read_bytes(), b"\xff\xd8\xff" + b"x" * 128)
            response_json.assert_called_once()
            open_url.assert_called_once()

    def test_pipeline_cover_reads_independent_stage_paths_before_upload_finishes(self):
        manager = LiveRecorderManager()
        fingerprint = "d" * 64
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = {
                "recordings": root / "recordings",
                "artifacts": root / "artifacts",
            }
            roots["recordings"].mkdir()
            roots = {key: path.resolve() for key, path in roots.items()}
            cover16 = roots["recordings"] / "clip.jpg"
            cover43 = roots["recordings"] / "clip_4x3.png"
            cover16.write_bytes(b"16x9-cover")
            cover43.write_bytes(b"4x3-cover")
            job = {
                "bvid": "",
                "review_override": {},
                "stages": [
                    {
                        "key": "cover_16x9",
                        "details": {"ai_cover_16x9_path": str(cover16)},
                    },
                    {
                        "key": "cover_4x3",
                        "details": {"ai_cover_4x3_path": str(cover43)},
                    },
                ],
            }
            with mock.patch.object(manager, "pipeline_job", return_value=job), mock.patch.object(
                manager, "_recording_file_roots", return_value=roots
            ):
                resolved16 = manager.pipeline_cover(fingerprint, "16x9")
                resolved43 = manager.pipeline_cover(fingerprint, "4x3")

        self.assertEqual(resolved16, cover16.resolve())
        self.assertEqual(resolved43, cover43.resolve())

    def test_pipeline_cover_corrects_cached_extension_from_image_content(self):
        manager = LiveRecorderManager()
        fingerprint = "b" * 64
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = {
                "recordings": root / "recordings",
                "artifacts": root / "artifacts",
            }
            cache_dir = roots["artifacts"] / "task-covers"
            cache_dir.mkdir(parents=True)
            wrong_path = cache_dir / f"{fingerprint}.jpg"
            wrong_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 128)
            job = {
                "bvid": "BV1potato",
                "bilibili_cover_url": "",
                "review_override": {},
                "stages": [],
            }
            with mock.patch.object(manager, "pipeline_job", return_value=job), mock.patch.object(
                manager, "_recording_file_roots", return_value=roots
            ):
                corrected = manager.pipeline_cover(fingerprint)

            self.assertEqual(corrected.suffix, ".png")
            self.assertTrue(corrected.is_file())
            self.assertFalse(wrong_path.exists())

    def test_pipeline_cover_fetches_remote_four_by_three_cover(self):
        manager = LiveRecorderManager()
        fingerprint = "c" * 64
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = {
                "recordings": root / "recordings",
                "artifacts": root / "artifacts",
            }
            job = {
                "bvid": "BV1potato",
                "bilibili_cover43_url": "https://i0.hdslb.com/cover43.png",
                "review_override": {},
                "stages": [],
            }
            with mock.patch.object(manager, "pipeline_job", return_value=job), mock.patch.object(
                manager, "_recording_file_roots", return_value=roots
            ), mock.patch.object(
                recorder_module,
                "_open_url",
                return_value=(b"\x89PNG\r\n\x1a\n" + b"x" * 128, "https://i0.hdslb.com/cover43.png"),
            ):
                cover = manager.pipeline_cover(fingerprint, "4x3")

            self.assertEqual(
                cover,
                (roots["artifacts"] / "task-covers" / f"{fingerprint}-4x3.png").resolve(),
            )
            self.assertTrue(cover.is_file())

    def test_pipeline_jobs_accept_biliup_speed_field(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            fingerprint = "a" * 64
            with sqlite3.connect(state_path) as db:
                db.executescript(
                    """
                    CREATE TABLE uploads (
                        fingerprint TEXT PRIMARY KEY, video_path TEXT, platform TEXT,
                        status TEXT, attempts INTEGER, result_json TEXT, error TEXT,
                        created_at TEXT, updated_at TEXT
                    );
                    CREATE TABLE upload_stages (
                        fingerprint TEXT, stage TEXT, status TEXT, details_json TEXT,
                        error TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT
                    );
                    """
                )
                db.execute(
                    "INSERT INTO uploads VALUES (?, ?, 'bilibili', 'processing', 2, '{}', NULL, ?, ?)",
                    (
                        fingerprint,
                        "/data/recordings/test.flv",
                        "2026-07-27T13:00:00+00:00",
                        "2026-07-27T13:00:00+00:00",
                    ),
                )
                db.execute(
                    "INSERT INTO upload_stages VALUES (?, 'upload', 'running', ?, NULL, NULL, NULL, ?)",
                    (
                        fingerprint,
                        json.dumps(
                            {
                                "upload_progress": {
                                    "uploaded_bytes": 50 * 1024 * 1024,
                                    "total_bytes": 100 * 1024 * 1024,
                                    "speed_bytes_per_sec": 5 * 1024 * 1024,
                                    "peak_speed_bytes_per_second": 8 * 1024 * 1024,
                                    "eta_seconds": 10,
                                    "percent": 50,
                                }
                            }
                        ),
                        "2026-07-27T13:00:00+00:00",
                    ),
                )
            with mock.patch.object(
                manager, "_pipeline_state_path", return_value=state_path
            ), mock.patch.object(manager, "list_rooms", return_value=[]):
                job = manager.pipeline_jobs()[0]

        self.assertIn("当前速度：5.0MB/s", job["upload_progress_text"])
        self.assertIn("最高速度：8.0MB/s", job["upload_progress_text"])

    def test_completed_pipeline_job_keeps_peak_upload_speed(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            fingerprint = "b" * 64
            with sqlite3.connect(state_path) as db:
                db.executescript(
                    """
                    CREATE TABLE uploads (
                        fingerprint TEXT PRIMARY KEY, video_path TEXT, platform TEXT,
                        status TEXT, attempts INTEGER, result_json TEXT, error TEXT,
                        created_at TEXT, updated_at TEXT
                    );
                    CREATE TABLE upload_stages (
                        fingerprint TEXT, stage TEXT, status TEXT, details_json TEXT,
                        error TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT
                    );
                    """
                )
                timestamp = "2026-07-31T05:00:00+00:00"
                db.execute(
                    "INSERT INTO uploads VALUES (?, ?, 'bilibili', 'completed', 1, '{}', NULL, ?, ?)",
                    (fingerprint, "/data/recordings/test.flv", timestamp, timestamp),
                )
                db.execute(
                    "INSERT INTO upload_stages VALUES (?, 'upload', 'completed', ?, NULL, ?, ?, ?)",
                    (
                        fingerprint,
                        json.dumps({
                            "upload_progress": {
                                "uploaded_bytes": 100 * 1024 * 1024,
                                "total_bytes": 100 * 1024 * 1024,
                                "speed_bytes_per_second": 3 * 1024 * 1024,
                                "peak_speed_bytes_per_second": 12 * 1024 * 1024,
                                "eta_seconds": 0,
                            },
                            "peak_speed_bytes_per_second": 12 * 1024 * 1024,
                        }),
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
            with mock.patch.object(
                manager, "_pipeline_state_path", return_value=state_path
            ), mock.patch.object(manager, "list_rooms", return_value=[]):
                job = manager.pipeline_jobs()[0]

        self.assertEqual(job["upload_progress_text"], "最高上传速度：12.0MB/s")

    def test_upload_queue_positions_and_paused_job_can_be_deleted(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            first_id, second_id = "a" * 64, "b" * 64
            with sqlite3.connect(state_path) as db:
                db.executescript(
                    """
                    CREATE TABLE uploads (
                        fingerprint TEXT PRIMARY KEY, video_path TEXT, platform TEXT,
                        status TEXT, attempts INTEGER, result_json TEXT, error TEXT,
                        created_at TEXT, updated_at TEXT
                    );
                    CREATE TABLE upload_stages (
                        fingerprint TEXT, stage TEXT, status TEXT, details_json TEXT,
                        error TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT
                    );
                    """
                )
                for index, fingerprint in enumerate((first_id, second_id), 1):
                    updated_at = f"2026-07-26T01:00:0{index}+00:00"
                    db.execute(
                        "INSERT INTO uploads VALUES (?, ?, 'bilibili', 'processing', 1, '{}', NULL, ?, ?)",
                        (fingerprint, f"/data/recordings/{fingerprint}.flv", updated_at, updated_at),
                    )
                    db.execute(
                        "INSERT INTO upload_stages VALUES (?, 'upload', 'queued', ?, NULL, NULL, NULL, ?)",
                        (
                            fingerprint,
                            json.dumps({"worker_pid": 999999}),
                            updated_at,
                        ),
                    )

            with mock.patch.object(
                manager, "_pipeline_state_path", return_value=state_path
            ), mock.patch.object(manager, "list_rooms", return_value=[]):
                jobs = manager.pipeline_jobs()
                positions = {
                    job["id"]: job["upload_queue_position"]
                    for job in jobs
                }
                self.assertEqual(positions[first_id], 1)
                self.assertEqual(positions[second_id], 2)
                labels = {job["id"]: job["progress_label"] for job in jobs}
                self.assertEqual(labels[first_id], "等待投稿队列（第 1 位）")
                self.assertEqual(labels[second_id], "等待投稿队列（第 2 位）")
                self.assertTrue(manager.pause_pipeline_job(first_id))
                paused = manager.pipeline_job(first_id)
                self.assertTrue(paused["paused"])
                self.assertTrue(paused["retryable"])
                self.assertFalse(paused["pausable"])
                manager.delete_pipeline_job(first_id)
                self.assertIsNone(manager.pipeline_job(first_id))

    def test_pre_upload_processing_job_can_be_paused(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            fingerprint = "c" * 64
            updated_at = "2026-07-28T06:00:00+00:00"
            with sqlite3.connect(state_path) as db:
                db.executescript(
                    """
                    CREATE TABLE uploads (
                        fingerprint TEXT PRIMARY KEY, video_path TEXT, platform TEXT,
                        status TEXT, attempts INTEGER, result_json TEXT, error TEXT,
                        created_at TEXT, updated_at TEXT
                    );
                    CREATE TABLE upload_stages (
                        fingerprint TEXT, stage TEXT, status TEXT, details_json TEXT,
                        error TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT
                    );
                    """
                )
                db.execute(
                    "INSERT INTO uploads VALUES (?, ?, 'bilibili', 'processing', 1, ?, NULL, ?, ?)",
                    (
                        fingerprint,
                        "/data/recordings/pre-upload.flv",
                        json.dumps({"worker_pid": 4321}),
                        updated_at,
                        updated_at,
                    ),
                )
                db.execute(
                    "INSERT INTO upload_stages VALUES (?, 'cover', 'running', '{}', NULL, ?, NULL, ?)",
                    (fingerprint, updated_at, updated_at),
                )
                db.execute(
                    "INSERT INTO upload_stages VALUES (?, 'upload', 'pending', '{}', NULL, NULL, NULL, ?)",
                    (fingerprint, updated_at),
                )

            with mock.patch.object(
                manager, "_pipeline_state_path", return_value=state_path
            ), mock.patch.object(
                manager, "list_rooms", return_value=[]
            ), mock.patch.object(
                manager, "_terminate_pipeline_worker", return_value=4321
            ) as terminate:
                self.assertTrue(manager.pipeline_job(fingerprint)["pausable"])
                self.assertTrue(manager.pause_pipeline_job(fingerprint))
                paused = manager.pipeline_job(fingerprint)

            terminate.assert_called_once()
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(
                next(
                    stage["status"]
                    for stage in paused["stages"]
                    if stage["key"] == "cover"
                ),
                "paused",
            )
            self.assertTrue(paused["retryable"])
            self.assertFalse(paused["pausable"])

    def test_delete_active_pipeline_job_stops_it_first(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            fingerprint = "d" * 64
            updated_at = "2026-07-28T06:00:00+00:00"
            with sqlite3.connect(state_path) as db:
                db.executescript(
                    """
                    CREATE TABLE uploads (
                        fingerprint TEXT PRIMARY KEY, video_path TEXT, platform TEXT,
                        status TEXT, attempts INTEGER, result_json TEXT, error TEXT,
                        created_at TEXT, updated_at TEXT
                    );
                    CREATE TABLE upload_stages (
                        fingerprint TEXT, stage TEXT, status TEXT, details_json TEXT,
                        error TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT
                    );
                    """
                )
                db.execute(
                    "INSERT INTO uploads VALUES (?, ?, 'bilibili', 'processing', 1, '{}', NULL, ?, ?)",
                    (
                        fingerprint,
                        "/data/recordings/delete-active.flv",
                        updated_at,
                        updated_at,
                    ),
                )
                db.execute(
                    "INSERT INTO upload_stages VALUES (?, 'ai', 'running', '{}', NULL, ?, NULL, ?)",
                    (fingerprint, updated_at, updated_at),
                )

            with mock.patch.object(
                manager, "_pipeline_state_path", return_value=state_path
            ), mock.patch.object(
                manager, "list_rooms", return_value=[]
            ), mock.patch.object(
                manager, "_terminate_pipeline_worker", return_value=9876
            ) as terminate:
                result = manager.delete_pipeline_job(fingerprint)
                deleted = manager.pipeline_job(fingerprint)

            terminate.assert_called_once()
            self.assertEqual(result["deleted_file_count"], 0)
            self.assertIsNone(deleted)

    def test_delete_allows_stale_processing_row_with_failed_stage(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            fingerprint = "e" * 64
            updated_at = "2026-07-28T06:00:00+00:00"
            with sqlite3.connect(state_path) as db:
                db.executescript(
                    """
                    CREATE TABLE uploads (
                        fingerprint TEXT PRIMARY KEY, video_path TEXT, platform TEXT,
                        status TEXT, attempts INTEGER, result_json TEXT, error TEXT,
                        created_at TEXT, updated_at TEXT
                    );
                    CREATE TABLE upload_stages (
                        fingerprint TEXT, stage TEXT, status TEXT, details_json TEXT,
                        error TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT
                    );
                    """
                )
                db.execute(
                    "INSERT INTO uploads VALUES (?, ?, 'bilibili', 'processing', 1, '{}', NULL, ?, ?)",
                    (
                        fingerprint,
                        "/data/recordings/stale-failed.flv",
                        updated_at,
                        updated_at,
                    ),
                )
                db.execute(
                    "INSERT INTO upload_stages VALUES (?, 'cover', 'failed', '{}', '封面失败', ?, ?, ?)",
                    (fingerprint, updated_at, updated_at, updated_at),
                )

            with mock.patch.object(
                manager, "_pipeline_state_path", return_value=state_path
            ), mock.patch.object(manager, "list_rooms", return_value=[]):
                self.assertEqual(manager.pipeline_job(fingerprint)["status"], "failed")
                manager.delete_pipeline_job(fingerprint)
                self.assertIsNone(manager.pipeline_job(fingerprint))

    def test_failed_upload_is_scheduled_for_five_minute_auto_retry(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            failed_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(
                timespec="seconds"
            )
            with sqlite3.connect(state_path) as db:
                db.executescript(
                    """
                    CREATE TABLE uploads (
                        fingerprint TEXT PRIMARY KEY, video_path TEXT, platform TEXT,
                        status TEXT, attempts INTEGER, result_json TEXT, error TEXT,
                        created_at TEXT, updated_at TEXT
                    );
                    CREATE TABLE upload_stages (
                        fingerprint TEXT, stage TEXT, status TEXT, details_json TEXT,
                        error TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT
                    );
                    """
                )
                db.execute(
                    "INSERT INTO uploads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "a" * 64,
                        "/data/recordings/test.flv",
                        "bilibili",
                        "failed",
                        1,
                        "{}",
                        "网络失败",
                        failed_at,
                        failed_at,
                    ),
                )
                db.execute(
                    "INSERT INTO upload_stages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("a" * 64, "upload", "failed", "{}", "网络失败", failed_at, failed_at, failed_at),
                )
            with mock.patch.object(manager, "_pipeline_state_path", return_value=state_path), mock.patch.object(
                manager, "list_rooms", return_value=[]
            ):
                job = manager.pipeline_jobs()[0]

        self.assertTrue(job["auto_retry_scheduled"])
        self.assertEqual(job["auto_retry_number"], 1)
        self.assertEqual(job["auto_retry_max_retries"], 3)
        self.assertGreater(job["auto_retry_remaining_seconds"], 150)
        self.assertLessEqual(job["auto_retry_remaining_seconds"], 180)

    def test_only_due_bilibili_upload_failure_is_automatically_retried(self):
        manager = LiveRecorderManager()
        due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
        jobs = [
            {"id": "a" * 64, "auto_retry_scheduled": True, "auto_retry_at": due},
            {"id": "b" * 64, "auto_retry_scheduled": False, "auto_retry_at": due},
        ]
        with mock.patch.object(manager, "pipeline_jobs", return_value=jobs), mock.patch.object(
            manager, "retry_pipeline_job", return_value=True
        ) as retry:
            count = manager.retry_due_upload_jobs()

        self.assertEqual(count, 1)
        retry.assert_called_once_with("a" * 64, automatic=True)

    def test_unified_task_views_include_recording_jobs(self):
        tasks_source = (Y2A_ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")
        overview_source = (Y2A_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        live_source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")

        self.assertIn("直播录播任务", tasks_source)
        self.assertIn("recording_jobs", tasks_source)
        self.assertIn("recording-retry-btn", tasks_source)
        self.assertIn("暂停录播任务失败", tasks_source)
        self.assertIn("如果任务仍在处理，将先停止当前处理进程", tasks_source)
        self.assertIn("live_recording_job_delete", tasks_source)
        self.assertIn('class="recording-progress-trigger', tasks_source)
        self.assertIn('id="recordingJobDetailModal"', tasks_source)
        self.assertIn(
            'html[data-theme="dark"] #recordingJobDetailModal .recording-detail-summary',
            tasks_source,
        )
        self.assertIn(
            'html[data-theme="dark"] #recordingJobDetailModal .recording-detail-stage',
            tasks_source,
        )
        self.assertIn("background: var(--studio-surface-raised)", tasks_source)
        self.assertIn("无损封装 MP4", tasks_source)
        self.assertIn("检查 MP4 内嵌封面", tasks_source)
        self.assertIn("清理录播源文件", tasks_source)
        self.assertIn("为什么跳过：", tasks_source)
        self.assertIn("查看生成参数、文件路径和完整 Prompt", tasks_source)
        self.assertIn("job.record_only", tasks_source)
        self.assertIn("openRecordingJobDetail", tasks_source)
        self.assertIn("完整任务日志", tasks_source)
        self.assertIn("已经上传：", tasks_source)
        self.assertIn("当前速度：", tasks_source)
        self.assertIn("最高速度：", tasks_source)
        self.assertIn("最高上传速度：", tasks_source)
        self.assertIn("剩余时间：", tasks_source)
        self.assertIn("data-recording-upload-live", tasks_source)
        self.assertIn("5 分钟后自动重试投稿", tasks_source)
        self.assertIn("auto_retry_scheduled", tasks_source)
        self.assertIn("refreshRecordingUploadMetrics", tasks_source)
        self.assertIn("applyRecordingJobProgress", tasks_source)
        self.assertIn('data-recording-job-id="{{ job.id }}"', tasks_source)
        self.assertIn("setInterval(refreshActiveRecordingDetail, 2000)", tasks_source)
        self.assertIn("previousScrollTop", tasks_source)
        self.assertIn("openStageKeys", tasks_source)
        self.assertIn("recording-task-cover", tasks_source)
        self.assertIn("recording-cover-trigger", tasks_source)
        self.assertIn("recording-cover-duration", tasks_source)
        self.assertIn("job.duration_text", tasks_source)
        self.assertIn("font-variant-numeric: tabular-nums", tasks_source)
        self.assertIn('id="recordingCoverPreviewModal"', tasks_source)
        self.assertIn("live_recording_job_cover", tasks_source)
        self.assertNotIn("url_for('live_recording', job=job.id)", tasks_source)
        self.assertNotIn("> 查看流水线</a>", tasks_source)
        self.assertNotIn("> 流水线</a>", tasks_source)
        self.assertGreaterEqual(tasks_source.count("'recording'"), 2)
        self.assertNotIn("t.source == 'recording'", overview_source)
        self.assertIn("recording_summary", overview_source)
        self.assertIn("直播录播", overview_source)
        self.assertNotIn("live_recording_job_delete", overview_source)
        self.assertNotIn('data-role="job-select"', live_source)
        self.assertNotIn("requestedPipelineJob", live_source)
        self.assertNotIn("直播结束后，该步骤会自动转入", live_source)
        self.assertIn("录制文件", live_source)
        self.assertNotIn("查看上传任务", live_source)

    def test_orphan_recording_scan_finds_only_old_unclaimed_room_videos(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recordings = root / "recordings"
            recordings.mkdir()
            state_path = root / "state.sqlite3"
            known = recordings / "开播主播_aaaaaa2026-07-23_09-00-00.flv"
            orphan = recordings / "开播主播_aaaaaa2026-07-23_10-00-00.flv"
            recent = recordings / "开播主播_aaaaaa2026-07-23_11-00-00.flv"
            unknown = recordings / "其他主播_cccccc2026-07-23_10-00-00.flv"
            for path in (known, orphan, recent, unknown):
                path.write_bytes(b"video")
            old = time.time() - 600
            for path in (known, orphan, unknown):
                os.utime(path, (old, old))
            with sqlite3.connect(state_path) as db:
                db.execute("CREATE TABLE uploads (video_path TEXT NOT NULL)")
                db.execute("INSERT INTO uploads VALUES (?)", (str(known),))

            with mock.patch.object(recorder_module, "RECORDINGS_DIR", recordings), mock.patch.object(
                manager, "_pipeline_state_path", return_value=state_path
            ), mock.patch.object(manager, "list_rooms", return_value=[self.rooms[0]]):
                candidates = manager._orphan_recording_candidates(120)

        self.assertEqual(candidates, [(orphan.resolve(), "aaaaaa111111")])

    def test_orphan_scan_skips_permanently_excluded_record_only_files(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recordings = root / "recordings"
            recordings.mkdir()
            state_path = root / "state.sqlite3"
            excluded = recordings / "开播主播_aaaaaa2026-07-23_09-00-00.flv"
            excluded.write_bytes(b"video")
            old = time.time() - 600
            os.utime(excluded, (old, old))
            with sqlite3.connect(state_path) as db:
                db.execute("CREATE TABLE uploads (video_path TEXT NOT NULL)")
                db.execute(
                    """CREATE TABLE recording_exclusions (
                        video_path TEXT NOT NULL,
                        room_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
                db.execute(
                    "INSERT INTO recording_exclusions VALUES (?, ?, ?, ?)",
                    (str(excluded), "aaaaaa111111", "record_only", "now"),
                )

            with mock.patch.object(recorder_module, "RECORDINGS_DIR", recordings), mock.patch.object(
                manager, "_pipeline_state_path", return_value=state_path
            ), mock.patch.object(manager, "list_rooms", return_value=[self.rooms[0]]):
                candidates = manager._orphan_recording_candidates(120)

        self.assertEqual(candidates, [])

    def test_orphan_recordings_are_reingested_sequentially_with_room_session(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.flv"
            second = root / "second.flv"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            completed = mock.Mock(returncode=0)
            with mock.patch.object(
                manager,
                "_orphan_recording_candidates",
                return_value=[(first, "room-1"), (second, "room-1")],
            ), mock.patch.object(recorder_module, "APP_ROOT", root), mock.patch.object(
                recorder_module.subprocess, "run", return_value=completed
            ) as run, mock.patch.object(
                manager, "room_multipart_enabled", return_value=True
            ):
                recovered = manager.recover_orphan_recordings()

        self.assertEqual(recovered, 2)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[0].args[0][-3:],
            ["--session-key", "room-1", str(first)],
        )
        self.assertEqual(
            run.call_args_list[1].args[0][-3:],
            ["--session-key", "room-1", str(second)],
        )

    def test_orphan_recordings_are_independent_when_multipart_is_disabled(self):
        manager = LiveRecorderManager()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "segment.flv"
            video.write_bytes(b"video")
            with mock.patch.object(
                manager,
                "_orphan_recording_candidates",
                return_value=[(video, "room-1")],
            ), mock.patch.object(recorder_module, "APP_ROOT", root), mock.patch.object(
                recorder_module.subprocess, "run", return_value=mock.Mock(returncode=0)
            ) as run, mock.patch.object(
                manager, "room_multipart_enabled", return_value=False
            ):
                recovered = manager.recover_orphan_recordings()

        self.assertEqual(recovered, 1)
        self.assertEqual(run.call_args.args[0][-2:], ["ingest", str(video)])
        self.assertNotIn("--session-key", run.call_args.args[0])

    def test_add_room_form_supports_name_search_and_recording_settings(self):
        source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")

        self.assertNotIn('name="name"', source)
        self.assertIn("直播间链接、分享文案或主播昵称", source)
        self.assertIn('data-role="room-url" maxlength="2000"', source)
        self.assertIn("抖音可直接粘贴 App 生成的整段直播分享文案", source)
        self.assertIn("(?:bilibili|douyu|douyin)", source)
        self.assertIn("(room.web_rid || room.room_id)", source)
        self.assertIn("/live-recording/rooms/resolve", source)
        self.assertIn("/live-recording/rooms/search", source)
        self.assertIn('data-role="room-search-results"', source)
        self.assertIn("room.avatar_url", source)
        self.assertIn('name="segment_enabled"', source)
        self.assertIn('name="segment_minutes"', source)
        self.assertIn('value="60"', source)
        self.assertIn('name="multipart_enabled"', source)
        self.assertIn('name="record_only"', source)
        self.assertIn('value="{{ room.segment_minutes }}"', source)
        self.assertIn("整场直播不分段", source)
        self.assertNotIn("按 2.5 GB 自动分段", source)

    def test_live_room_exposes_per_room_segmentation_and_multipart_settings(self):
        settings_source = (Y2A_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
        live_source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")
        app_source = (Y2A_ROOT / "app.py").read_text(encoding="utf-8")
        style_source = (Y2A_ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")

        self.assertNotIn('name="RECORDING_MULTIPART_ENABLED"', settings_source)
        self.assertIn('name="segment_enabled"', live_source)
        self.assertIn('name="segment_minutes"', live_source)
        self.assertIn('name="multipart_enabled"', live_source)
        self.assertIn('name="record_only"', live_source)
        self.assertIn("分 P 投稿", live_source)
        self.assertIn("不分 P 投稿", live_source)
        self.assertIn('data-role="multipart-state"', live_source)
        self.assertIn("multipart?.addEventListener('change', syncState)", live_source)
        self.assertIn("仅录制，不自动投稿", live_source)
        self.assertIn("live_recording_room_recording_settings", app_source)
        self.assertIn("font-size: 14px !important", style_source)
        self.assertIn("margin-right: clamp(24px, 5vw, 90px)", style_source)
        self.assertIn("grid-template-columns: 32px minmax(0, 1fr) auto 16px", style_source)

    def test_record_only_ui_hides_upload_account_identity(self):
        live_source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")
        tasks_source = (Y2A_ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")

        self.assertIn("{% if not room.record_only %}", live_source)
        self.assertIn('data-role="bilibili-account-field"', live_source)
        self.assertIn("accountField?.classList.toggle('d-none', recordOnlyActive)", live_source)
        self.assertIn("投稿账号", tasks_source)
        self.assertIn("recording-account-identity", tasks_source)
        self.assertIn("无需投稿", tasks_source)
        self.assertNotIn("job.bilibili_account_uid", tasks_source)

    def test_recording_queue_uses_absolute_local_time_and_explicit_bvid_label(self):
        tasks_source = (Y2A_ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")

        self.assertIn("formatToParts(date)", tasks_source)
        self.assertIn("`${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}`", tasks_source)
        self.assertNotIn("return '刚刚'", tasks_source)
        self.assertIn("<span>BV号</span>{{ job.bvid }}", tasks_source)
        self.assertIn("recording-updated-heading", tasks_source)

    def test_delete_room_button_is_enabled_while_worker_runs(self):
        source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")

        self.assertNotIn('disabled title="请先停止录制引擎"', source)
        self.assertIn('title="删除直播间"', source)


if __name__ == "__main__":
    unittest.main()
