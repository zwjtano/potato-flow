import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
Y2A_ROOT = ROOT / "y2a-auto"
if str(Y2A_ROOT) not in sys.path:
    sys.path.insert(0, str(Y2A_ROOT))

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
                "status": "processing",
                "active_stage": "upload",
                "room_name": "YYF",
                "title": "正在投稿的录播",
            }
        ][:limit]

    def status(self):
        return {"running": True}


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

        self.assertIn("YYF · processing · upload", self.session.last_payload["text"])
        self.assertIn("正在投稿的录播", self.session.last_payload["text"])

    def test_settings_expose_control_switch_and_allowlist(self):
        template = (Y2A_ROOT / "templates" / "settings.html").read_text(
            encoding="utf-8"
        )
        config_source = (
            Y2A_ROOT / "modules" / "config_manager.py"
        ).read_text(encoding="utf-8")
        app_source = (Y2A_ROOT / "app.py").read_text(encoding="utf-8")

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
