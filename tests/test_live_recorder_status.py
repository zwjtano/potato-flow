import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
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
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "biliup.yaml"
            with mock.patch.object(recorder_module, "CONFIG_DIR", root / "config"), \
                    mock.patch.object(recorder_module, "RECORDINGS_DIR", root / "recordings"), \
                    mock.patch.object(recorder_module, "LOG_PATH", root / "logs" / "recorder.log"), \
                    mock.patch.object(recorder_module, "PID_PATH", root / "run" / "recorder.pid"), \
                    mock.patch.object(recorder_module, "BILIUP_CONFIG_PATH", config_path), \
                    mock.patch.object(manager, "_sync_bridge_profiles"):
                manager.sync_configs([self.rooms[0]])

            content = config_path.read_text(encoding="utf-8")
            self.assertIn("file_size: null", content)
            self.assertIn('segment_time: "01:00:00"', content)
            self.assertIn('filename_prefix: "{streamer}_{title}_%Y-%m-%d_%H-%M"', content)
            self.assertNotIn("file_size: 2621440000", content)
            self.assertIn("filtering_threshold: 0", content)
            self.assertIn("segment_processor:", content)
            self.assertIn("ingest --session-key", content)
            self.assertIn("aaaaaa111111", content)
            self.assertIn("finalize-session --session-key", content)

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
                manager._sync_bridge_profiles(self.rooms)
            config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(
            config["title_template"],
            "【直播回放】{streamer}｜{ai_topic}｜{date}",
        )
        self.assertEqual(config["profiles"][0]["streamer_name"], "开播主播")
        self.assertEqual(
            config["bilibili_cookies"],
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
                    details = {"title": "【直播回放】Alice｜测试主题｜2026-07-23"} if stage == "upload" else {}
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
            }]
            with mock.patch.object(manager, "_pipeline_state_path", return_value=state_path), mock.patch.object(
                manager, "list_rooms", return_value=rooms
            ):
                jobs = manager.pipeline_jobs()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source"], "recording")
        self.assertEqual(jobs[0]["room_name"], "Alice")
        self.assertEqual(jobs[0]["bvid"], "BV1potato")
        self.assertEqual(jobs[0]["completed_stages"], 5)
        self.assertEqual(jobs[0]["title"], "【直播回放】Alice｜测试主题｜2026-07-23")

    def test_unified_task_views_include_recording_jobs(self):
        tasks_source = (Y2A_ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")
        overview_source = (Y2A_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        live_source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")

        self.assertIn("直播录播任务", tasks_source)
        self.assertIn("recording_jobs", tasks_source)
        self.assertIn("recording-retry-btn", tasks_source)
        self.assertIn("live_recording_job_delete", tasks_source)
        self.assertGreaterEqual(tasks_source.count("'recording'"), 2)
        self.assertIn("t.source == 'recording'", overview_source)
        self.assertIn("直播录播", overview_source)
        self.assertIn("live_recording_job_delete", overview_source)
        self.assertIn("requestedPipelineJob", live_source)
        self.assertIn("job.status !== 'completed'", live_source)

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
            ) as run:
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

    def test_add_room_form_only_requires_room_url(self):
        source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")

        self.assertNotIn('name="name"', source)
        self.assertIn("粘贴链接，自动识别主播名称和头像", source)
        self.assertIn("/live-recording/rooms/resolve", source)
        self.assertIn("room.avatar_url", source)
        self.assertIn("每 60 分钟自动分段", source)
        self.assertNotIn("按 2.5 GB 自动分段", source)

    def test_delete_room_button_is_enabled_while_worker_runs(self):
        source = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")

        self.assertNotIn('disabled title="请先停止录制引擎"', source)
        self.assertIn('title="删除直播间"', source)


if __name__ == "__main__":
    unittest.main()
