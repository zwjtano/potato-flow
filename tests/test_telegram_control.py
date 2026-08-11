import pathlib
import sys
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.telegram_control import TelegramControlService  # noqa: E402


class _Response:
    status_code = 200

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"ok": True, "result": {}}

    def json(self):
        return self.payload


class _Session:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()

    @property
    def last_payload(self):
        return self.calls[-1][1]["json"]


class _Manager:
    def __init__(self):
        self.rooms = [
            {
                "id": "room-1",
                "name": "YYF",
                "url": "https://www.douyu.com/9999",
                "record_only": False,
                "segment_enabled": True,
                "segment_minutes": 60,
                "multipart_enabled": False,
                "recording_quality": "source",
                "danmaku_settings_inherit": True,
                "runtime": {
                    "state": "checking",
                    "label": "检测中",
                    "recording": False,
                },
            },
            {
                "id": "room-2",
                "name": "果小果",
                "url": "https://www.douyu.com/123",
                "record_only": True,
                "runtime": {
                    "state": "paused",
                    "label": "已手动停止",
                    "recording": False,
                },
            },
        ]
        self.added = []
        self.controls = []
        self.deleted = []
        self.retried = []
        self.paused = []
        self.deleted_tasks = []
        self.regenerated = []
        self.saved_room_settings = []
        self.engine_starts = 0
        self.engine_stops = 0

    def rooms_with_status(self):
        return [dict(room) for room in self.rooms]

    def add_room_from_url_and_reload(self, url):
        self.added.append(url)
        return {
            "id": "room-3",
            "name": "新主播",
            "url": url,
        }, "reloaded"

    def set_room_recording(self, room_id, enabled):
        self.controls.append((room_id, enabled))
        room = next(item for item in self.rooms if item["id"] == room_id)
        return dict(room)

    def delete_room_and_reload(self, room_id):
        self.deleted.append(room_id)
        return "reloaded"

    def pipeline_jobs(self, limit):
        return [
            {
                "id": "a" * 64,
                "display_id": "DYU-YYF-0728-001",
                "short_id": "a" * 12,
                "status": "processing",
                "active_stage": "upload",
                "room_name": "YYF",
                "title": "正在投稿的录播",
                "completed_stages": 4,
                "total_stages": 6,
                "pausable": True,
                "retryable": False,
                "upload_progress": {
                    "uploaded_bytes": 50,
                    "total_bytes": 100,
                    "speed_bytes_per_second": 10,
                    "eta_seconds": 5,
                },
                "stages": [
                    {"key": "ai", "status": "completed"},
                    {"key": "upload", "status": "running"},
                ],
            },
            {
                "id": "b" * 64,
                "display_id": "DYU-YYF-0728-002",
                "short_id": "b" * 12,
                "status": "failed",
                "failed_stage": "upload",
                "room_name": "YYF",
                "title": "失败的录播",
                "completed_stages": 5,
                "total_stages": 6,
                "pausable": False,
                "retryable": True,
                "error": "投稿连接中断",
                "stages": [
                    {"key": "ai", "status": "completed"},
                    {"key": "cover", "status": "completed"},
                    {"key": "upload", "status": "failed", "error": "投稿连接中断"},
                ],
            }
        ][:limit]

    def status(self):
        return {"running": True, "pid": 123}

    def start(self):
        self.engine_starts += 1
        return {"running": True, "pid": 456}

    def stop(self):
        self.engine_stops += 1
        return {"running": False, "pid": None}

    def retry_pipeline_job(self, fingerprint):
        self.retried.append(fingerprint)
        return True

    def pause_pipeline_job(self, fingerprint):
        self.paused.append(fingerprint)
        return True

    def delete_pipeline_job(self, fingerprint, delete_files=False):
        self.deleted_tasks.append((fingerprint, delete_files))
        return {"deleted_file_count": 0}

    def save_room_recording_settings(self, room_id, **values):
        self.saved_room_settings.append((room_id, dict(values)))
        room = next(item for item in self.rooms if item["id"] == room_id)
        room.update(values)
        return dict(room), "saved"

    def bilibili_archive_accounts(self):
        return [{"id": "account-1", "name": "萨豆士哈", "uid": "3707033578637692"}]

    def regenerate_published_metadata(self, fingerprint, fields):
        self.regenerated.append((fingerprint, set(fields)))
        return {"ai_regenerated_fields": sorted(fields)}

    def recording_files(self, limit):
        return {
            "files": [
                {
                    "name": "YYF_录播.flv",
                    "type": "video",
                    "size_bytes": 1024 * 1024,
                    "locked": True,
                    "lock_reason": "正在录制",
                }
            ][:limit],
            "total_files": 1,
            "total_size_bytes": 1024 * 1024,
        }


def _config(**overrides):
    config = {
        "TELEGRAM_CONTROL_ENABLED": True,
        "TELEGRAM_CONTROL_ADMIN_USER_IDS": "1001, 1002",
        "NOTIFY_TELEGRAM_BOT_TOKEN": "123:secret",
        "NOTIFY_TELEGRAM_CHAT_ID": "-100200",
        "NETWORK_PROXY_URL": "",
        "NETWORK_PROXY_USERNAME": "",
        "NETWORK_PROXY_PASSWORD": "",
    }
    config.update(overrides)
    return config


def _message_update(text, *, user_id="1001", chat_id="-100200"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": int(user_id)},
            "chat": {"id": int(chat_id)},
            "text": text,
        },
    }


def _callback_update(data, *, user_id="1001", chat_id="-100200", message_id=20):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "callback-nav",
            "from": {"id": int(user_id)},
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": int(chat_id)},
            },
        },
    }


class TelegramControlTests(unittest.TestCase):
    def setUp(self):
        self.manager = _Manager()
        self.session = _Session()
        self.service = TelegramControlService(
            self.manager,
            _config(),
            request_session=self.session,
        )

    def test_requires_token_chat_and_admin_allowlist(self):
        service = TelegramControlService(
            self.manager,
            _config(
                NOTIFY_TELEGRAM_BOT_TOKEN="",
                NOTIFY_TELEGRAM_CHAT_ID="",
                TELEGRAM_CONTROL_ADMIN_USER_IDS="",
            ),
        )
        self.assertEqual(
            service.validation_errors(),
            ["Bot Token", "Chat ID", "管理员 User ID"],
        )

    def test_start_opens_dashboard_with_counts_and_navigation(self):
        self.service.process_update(_message_update("/start"))

        payload = self.session.last_payload
        self.assertIn("PotatoFlow 控制台", payload["text"])
        self.assertIn("进行中任务：1 个", payload["text"])
        self.assertIn("异常任务：1 个", payload["text"])
        callbacks = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("nav:rooms", callbacks)
        self.assertIn("nav:active", callbacks)
        self.assertIn("nav:ai", callbacks)
        self.assertNotIn("nav:engine", callbacks)
        self.assertNotIn("nav:status", callbacks)
        self.assertNotIn("nav:notifications", callbacks)
        self.assertIn("录制引擎：🟢 运行中", payload["text"])
        self.assertIn("磁盘可用：", payload["text"])

    def test_rooms_page_can_start_guided_add(self):
        self.service.process_update(_callback_update("nav:rooms"))
        edit_call = next(
            kwargs["json"] for url, kwargs in self.session.calls
            if url.endswith("/editMessageText")
        )
        callbacks = [
            button["callback_data"]
            for row in edit_call["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("roomadd:prompt", callbacks)

        self.service.process_update(_callback_update("roomadd:prompt"))
        prompt = next(
            kwargs["json"] for url, kwargs in reversed(self.session.calls)
            if url.endswith("/sendMessage")
        )
        self.assertIn("请直接发送完整直播间链接", prompt["text"])

    def test_ai_editor_buttons_show_titles_not_task_ids(self):
        self.service.process_update(_message_update("/ai"))
        buttons = [
            button["text"]
            for row in self.session.last_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertTrue(any("正在投稿的录播" in text for text in buttons))
        self.assertFalse(any("DYU-YYF" in text for text in buttons))

    def test_running_and_abnormal_pages_filter_tasks(self):
        self.service.process_update(_message_update("/running"))
        active_text = self.session.last_payload["text"]
        self.assertIn("正在投稿的录播", active_text)
        self.assertNotIn("失败的录播", active_text)

        self.service.process_update(_message_update("/errors"))
        abnormal_text = self.session.last_payload["text"]
        self.assertIn("失败的录播", abnormal_text)
        self.assertNotIn("正在投稿的录播", abnormal_text)

    def test_task_list_buttons_show_titles_not_task_ids(self):
        self.service.process_update(_message_update("/tasks"))
        # Legacy /tasks text remains copyable; the button-driven page uses titles.
        self.service.process_update(_callback_update("nav:tasks"))
        edit_call = next(
            kwargs["json"] for url, kwargs in reversed(self.session.calls)
            if url.endswith("/editMessageText")
        )
        button_texts = [
            button["text"]
            for row in edit_call["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertTrue(any("正在投稿的录播" in text for text in button_texts))
        self.assertFalse(any("DYU-YYF" in text for text in button_texts))

    def test_task_lists_support_next_page(self):
        original = self.manager.pipeline_jobs

        def many_jobs(limit):
            template = original(None)[0]
            jobs = []
            for index in range(10):
                job = dict(template)
                job["id"] = f"{index:064x}"
                job["short_id"] = f"{index:012x}"
                job["display_id"] = f"TASK-{index + 1}"
                job["title"] = f"分页任务 {index + 1}"
                jobs.append(job)
            return jobs[:limit]

        self.manager.pipeline_jobs = many_jobs
        self.service.process_update(_callback_update("nav:tasks"))
        first_page = next(
            kwargs["json"] for url, kwargs in reversed(self.session.calls)
            if url.endswith("/editMessageText")
        )
        callbacks = [
            button["callback_data"]
            for row in first_page["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("taskpage:tasks:2", callbacks)

        self.service.process_update(_callback_update("taskpage:tasks:2"))
        second_page = next(
            kwargs["json"] for url, kwargs in reversed(self.session.calls)
            if url.endswith("/editMessageText")
        )
        self.assertIn("分页任务 9", second_page["text"])
        self.assertIn("第 2/2 页", second_page["text"])

    def test_navigation_refresh_edits_existing_message(self):
        self.service.process_update(_callback_update("nav:active"))

        methods = [url.rsplit("/", 1)[-1] for url, _ in self.session.calls]
        self.assertIn("editMessageText", methods)
        self.assertIn("answerCallbackQuery", methods)
        edit_call = next(
            kwargs["json"] for url, kwargs in self.session.calls
            if url.endswith("/editMessageText")
        )
        self.assertIn("进行中的任务", edit_call["text"])

    def test_unauthorized_navigation_callback_is_rejected(self):
        self.service.process_update(
            _callback_update("nav:home", user_id="9999")
        )

        self.assertEqual(len(self.session.calls), 1)
        self.assertTrue(self.session.calls[0][0].endswith("/answerCallbackQuery"))
        self.assertEqual(self.session.last_payload["text"], "无权操作")

    def test_task_detail_refresh_has_progress_and_actions(self):
        self.service.process_update(
            _callback_update("taskview:DYU-YYF-0728-001")
        )

        edit_call = next(
            kwargs["json"] for url, kwargs in self.session.calls
            if url.endswith("/editMessageText")
        )
        self.assertIn("上传：50.00%", edit_call["text"])
        callbacks = [
            button["callback_data"]
            for row in edit_call["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("taskview:" + "a" * 12, callbacks)
        self.assertIn("taskpause:" + "a" * 12, callbacks)

    def test_ai_regeneration_requires_confirmation(self):
        self.service.process_update(
            _callback_update("airegen:title:DYU-YYF-0728-001")
        )
        confirmation = next(
            kwargs["json"] for url, kwargs in reversed(self.session.calls)
            if url.endswith("/sendMessage")
        )
        callback_data = confirmation["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]
        self.assertEqual(self.manager.regenerated, [])

        self.service.process_update(_callback_update(callback_data, message_id=21))

        deadline = time.time() + 1
        while not self.manager.regenerated and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(
            self.manager.regenerated,
            [("a" * 64, {"title"})],
        )

    def test_rooms_command_lists_numbered_rooms_and_modes(self):
        self.service.process_update(_message_update("/rooms"))

        payload = self.session.last_payload
        self.assertEqual(payload["chat_id"], "-100200")
        self.assertIn("1. 🔎 YYF", payload["text"])
        self.assertIn("2. ⏸ 果小果", payload["text"])
        self.assertIn("仅录制", payload["text"])

    def test_unauthorized_user_cannot_execute_commands(self):
        self.service.process_update(_message_update("/stop 1", user_id="9999"))

        self.assertEqual(self.manager.controls, [])
        self.assertIn("无权操作", self.session.last_payload["text"])
        self.assertIn("User ID：<code>9999</code>", self.session.last_payload["text"])
        self.assertEqual(self.session.last_payload["parse_mode"], "HTML")

    def test_commands_from_other_chat_are_ignored(self):
        self.service.process_update(_message_update("/stop 1", chat_id="-999"))

        self.assertEqual(self.manager.controls, [])
        self.assertEqual(self.session.calls, [])

    def test_add_and_room_control_commands_use_manager(self):
        self.service.process_update(
            _message_update("/add https://www.douyu.com/5556")
        )
        self.service.process_update(_message_update("/stop 1"))
        self.service.process_update(_message_update("/start 果小果"))

        self.assertEqual(self.manager.added, ["https://www.douyu.com/5556"])
        self.assertEqual(
            self.manager.controls,
            [("room-1", False), ("room-2", True)],
        )

    def test_parameter_command_guides_then_accepts_next_message(self):
        self.service.process_update(_message_update("/add"))

        prompt = self.session.last_payload
        self.assertIn("请直接发送完整直播间链接", prompt["text"])
        self.assertIn("inputcancel:", prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"])
        self.assertEqual(self.manager.added, [])

        self.service.process_update(
            _message_update("https://www.douyu.com/7788")
        )

        self.assertEqual(self.manager.added, ["https://www.douyu.com/7788"])
        self.assertIn("已添加", self.session.last_payload["text"])

    def test_room_settings_cover_toggle_text_and_account_values(self):
        self.service.process_update(_callback_update("roomsettings:1"))
        settings_text = next(
            kwargs["json"]["text"] for url, kwargs in self.session.calls
            if url.endswith("/editMessageText")
        )
        self.assertIn("直播间设置", settings_text)
        self.assertIn("B站账号", settings_text)
        self.assertIn("ASS 参数", settings_text)

        self.service.process_update(_callback_update("roomsetting:1:segment"))
        self.assertFalse(self.manager.saved_room_settings[-1][1]["segment_enabled"])

        self.service.process_update(_callback_update("roominput:1:schedule_start"))
        self.service.process_update(_message_update("08:30"))
        self.assertEqual(
            self.manager.saved_room_settings[-1][1]["recording_schedule_start"],
            "08:30",
        )

        self.service.process_update(_callback_update("roomaccountset:1:1"))
        self.assertEqual(
            self.manager.saved_room_settings[-1][1]["bilibili_account_id"],
            "account-1",
        )

    def test_guided_input_can_be_cancelled(self):
        self.service.process_update(_message_update("/add"))
        callback_data = self.session.last_payload["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]

        self.service.process_update(_callback_update(callback_data))
        self.service.process_update(_message_update("https://www.douyu.com/7788"))

        self.assertEqual(self.manager.added, [])

    def test_delete_requires_matching_inline_confirmation(self):
        self.service.process_update(_message_update("/delete 2"))
        confirmation = self.session.last_payload
        callback_data = confirmation["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]
        self.assertEqual(self.manager.deleted, [])

        self.service.process_update(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "callback-1",
                    "from": {"id": 1001},
                    "data": callback_data,
                    "message": {
                        "message_id": 11,
                        "chat": {"id": -100200},
                    },
                },
            }
        )

        self.assertEqual(self.manager.deleted, ["room-2"])
        api_methods = [url.rsplit("/", 1)[-1] for url, _ in self.session.calls]
        self.assertIn("answerCallbackQuery", api_methods)
        self.assertIn("editMessageText", api_methods)

    def test_recording_room_cannot_enter_delete_confirmation(self):
        self.manager.rooms[0]["runtime"]["recording"] = True

        self.service.process_update(_message_update("/delete 1"))

        self.assertEqual(self.manager.deleted, [])
        self.assertNotIn("reply_markup", self.session.last_payload)
        self.assertIn("请先 /stop", self.session.last_payload["text"])

    def test_tasks_command_shows_active_stage(self):
        self.service.process_update(_message_update("/tasks"))

        self.assertIn("1. ⏳ YYF · processing · upload", self.session.last_payload["text"])
        self.assertIn("正在投稿的录播", self.session.last_payload["text"])
        self.assertIn(
            "ID: <code>DYU-YYF-0728-001</code>",
            self.session.last_payload["text"],
        )
        self.assertEqual(self.session.last_payload["parse_mode"], "HTML")

    def test_task_detail_shows_upload_percentage_speed_and_actions(self):
        self.service.process_update(_message_update("/task 1"))

        text = self.session.last_payload["text"]
        self.assertIn("上传：50.00%", text)
        self.assertIn("速度：10.0B/s", text)
        self.assertIn("/pause <code>DYU-YYF-0728-001</code>", text)

    def test_retry_and_pause_task_commands_use_manager(self):
        self.service.process_update(_message_update("/retry 2"))
        self.service.process_update(_message_update("/pause DYU-YYF-0728-001"))

        self.assertEqual(self.manager.retried, ["b" * 64])
        self.assertEqual(self.manager.paused, ["a" * 64])
        self.assertIn("已暂停任务", self.session.last_payload["text"])

    def test_delete_task_keeps_files_and_requires_confirmation(self):
        self.service.process_update(_message_update("/delete_task 2"))
        confirmation = self.session.last_payload
        callback_data = confirmation["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]
        self.assertEqual(self.manager.deleted_tasks, [])

        self.service.process_update(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "callback-task",
                    "from": {"id": 1001},
                    "data": callback_data,
                    "message": {
                        "message_id": 12,
                        "chat": {"id": -100200},
                    },
                },
            }
        )

        self.assertEqual(self.manager.deleted_tasks, [("b" * 64, False)])

    def test_active_task_can_request_delete_confirmation(self):
        self.service.process_update(_message_update("/delete_task 1"))

        confirmation = self.session.last_payload
        self.assertIn("若任务仍在处理会先停止", confirmation["text"])
        self.assertIn(
            "task_delete:",
            confirmation["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
        )

    def test_files_lists_recent_recording_artifacts(self):
        self.service.process_update(_message_update("/files"))

        self.assertIn("YYF_录播.flv", self.session.last_payload["text"])
        self.assertIn("🔒正在录制", self.session.last_payload["text"])

    def test_settings_expose_control_switch_and_allowlist(self):
        template = (APP_ROOT / "templates" / "settings.html").read_text(
            encoding="utf-8"
        )
        config_source = (
            APP_ROOT / "modules" / "config_manager.py"
        ).read_text(encoding="utf-8")
        app_source = (APP_ROOT / "app.py").read_text(encoding="utf-8")

        for field in (
            "TELEGRAM_CONTROL_ENABLED",
            "TELEGRAM_CONTROL_ADMIN_USER_IDS",
        ):
            self.assertIn(field, template)
            self.assertIn(field, config_source)
            self.assertIn(field, app_source)
        self.assertIn("configure_global_telegram_control", app_source)


if __name__ == "__main__":
    unittest.main()
