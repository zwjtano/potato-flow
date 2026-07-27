import pathlib
import sys
import unittest
from unittest import mock

import requests


ROOT = pathlib.Path(__file__).resolve().parents[1]
Y2A_ROOT = ROOT / "y2a-auto"
if str(Y2A_ROOT) not in sys.path:
    sys.path.insert(0, str(Y2A_ROOT))

from modules.notifications.adapters import (  # noqa: E402
    CHANNEL_TELEGRAM,
    NotificationSendError,
    TelegramNotifier,
    build_notifier_registry,
    iter_enabled_channel_ids,
    validate_channel_config_fields,
)
from modules.notifications.models import NotificationMessage  # noqa: E402


class _Response:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {"ok": True}
        self.status_code = status_code

    def json(self):
        return self._payload


class TelegramNotificationTests(unittest.TestCase):
    def test_channel_requires_token_and_chat_id(self):
        self.assertEqual(
            validate_channel_config_fields(CHANNEL_TELEGRAM, {}),
            ["NOTIFY_TELEGRAM_BOT_TOKEN", "NOTIFY_TELEGRAM_CHAT_ID"],
        )
        config = {
            "NOTIFY_TELEGRAM_ENABLED": True,
            "NOTIFY_TELEGRAM_BOT_TOKEN": "123:secret",
            "NOTIFY_TELEGRAM_CHAT_ID": "-100123",
        }
        self.assertIn(CHANNEL_TELEGRAM, iter_enabled_channel_ids(config))
        self.assertIn(CHANNEL_TELEGRAM, build_notifier_registry())

    @mock.patch("modules.notifications.adapters.requests.post")
    def test_send_uses_bot_api_and_plain_text(self, post):
        post.return_value = _Response()
        message = NotificationMessage(
            title="PotatoFlow ✅ 任务完成",
            summary="测试任务",
            markdown="**✅ 任务完成**\n\n> **标题：**`测试任务`",
        )

        TelegramNotifier().send(
            message,
            {
                "NOTIFY_TELEGRAM_BOT_TOKEN": "123:secret",
                "NOTIFY_TELEGRAM_CHAT_ID": "-100123",
            },
        )

        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.telegram.org/bot123:secret/sendMessage")
        self.assertEqual(kwargs["json"]["chat_id"], "-100123")
        self.assertNotIn("**", kwargs["json"]["text"])
        self.assertNotIn("`", kwargs["json"]["text"])
        self.assertLessEqual(len(kwargs["json"]["text"]), 4096)
        self.assertEqual(kwargs["timeout"], 10)

    @mock.patch("modules.notifications.adapters.requests.post")
    def test_network_error_does_not_leak_bot_token(self, post):
        post.side_effect = requests.ConnectionError(
            "failed https://api.telegram.org/bot123:secret/sendMessage"
        )

        with self.assertRaises(NotificationSendError) as context:
            TelegramNotifier().send(
                NotificationMessage(title="test", summary="test", markdown="test"),
                {
                    "NOTIFY_TELEGRAM_BOT_TOKEN": "123:secret",
                    "NOTIFY_TELEGRAM_CHAT_ID": "123",
                },
            )

        self.assertNotIn("123:secret", str(context.exception))

    @mock.patch("modules.notifications.adapters.requests.post")
    def test_api_error_description_is_preserved_and_token_is_redacted(self, post):
        post.return_value = _Response(
            {"ok": False, "description": "invalid token 123:secret"},
            status_code=400,
        )

        with self.assertRaises(NotificationSendError) as context:
            TelegramNotifier().send(
                NotificationMessage(title="test", summary="test", markdown="test"),
                {
                    "NOTIFY_TELEGRAM_BOT_TOKEN": "123:secret",
                    "NOTIFY_TELEGRAM_CHAT_ID": "123",
                },
            )

        self.assertIn("invalid token", str(context.exception))
        self.assertNotIn("123:secret", str(context.exception))

    def test_settings_and_defaults_expose_telegram_notification(self):
        template = (Y2A_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
        config_source = (Y2A_ROOT / "modules" / "config_manager.py").read_text(encoding="utf-8")
        app_source = (Y2A_ROOT / "app.py").read_text(encoding="utf-8")

        for field in (
            "NOTIFY_TELEGRAM_ENABLED",
            "NOTIFY_TELEGRAM_BOT_TOKEN",
            "NOTIFY_TELEGRAM_CHAT_ID",
        ):
            self.assertIn(field, template)
            self.assertIn(field, config_source)
        self.assertIn('data-channel="telegram"', template)
        self.assertIn("CHANNEL_TELEGRAM", app_source)


if __name__ == "__main__":
    unittest.main()
