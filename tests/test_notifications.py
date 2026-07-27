import pathlib
import sys
import unittest
import uuid
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
from modules.notifications import (  # noqa: E402
    EVENT_COOKIE_INVALID,
    EVENT_RECORDING_STARTED,
    EVENT_RECORDING_STOPPED,
    NotificationEvent,
    build_notification_message,
)
from modules.notifications.service import (  # noqa: E402
    emit_notification_event_deduplicated,
)
from modules.live_recorder_manager import LiveRecorderManager  # noqa: E402
import bridge  # noqa: E402


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
                "NOTIFY_TELEGRAM_PROXY_URL": "http://127.0.0.1:7890",
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
        self.assertEqual(
            kwargs["proxies"],
            {
                "http": "http://127.0.0.1:7890",
                "https": "http://127.0.0.1:7890",
            },
        )

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
            "NOTIFY_TELEGRAM_PROXY_URL",
        ):
            self.assertIn(field, template)
            self.assertIn(field, config_source)
        self.assertIn('data-channel="telegram"', template)
        self.assertIn("CHANNEL_TELEGRAM", app_source)


class NotificationEventExtensionTests(unittest.TestCase):
    def test_task_added_message_distinguishes_recording_job_types(self):
        recording_upload = build_notification_message(
            NotificationEvent(
                "TASK_ADDED",
                {
                    "task_id": "recording-1",
                    "task_kind": "recording_upload",
                    "streamer": "YYF",
                    "video_file": "YYF_陪伴每一天.flv",
                    "upload_target": "bilibili",
                },
            )
        )
        record_only = build_notification_message(
            NotificationEvent(
                "TASK_ADDED",
                {
                    "task_id": "recording-2",
                    "task_kind": "record_only",
                    "streamer": "果小果",
                    "video_file": "果小果_天梯冲分.flv",
                    "upload_target": "local",
                },
            )
        )

        self.assertIn("录播投稿任务已添加", recording_upload.title)
        self.assertIn("哔哩哔哩", recording_upload.markdown)
        self.assertIn("仅录制任务已添加", record_only.title)
        self.assertIn("仅本地处理，不投稿", record_only.markdown)

    def test_task_result_messages_distinguish_recording_job_types(self):
        completed = build_notification_message(
            NotificationEvent(
                "TASK_COMPLETED",
                {
                    "task_id": "recording-1",
                    "task_kind": "recording_upload",
                    "streamer": "YYF",
                    "video_file": "YYF_陪伴每一天.flv",
                    "bvid": "BV1TEST",
                },
            )
        )
        failed = build_notification_message(
            NotificationEvent(
                "TASK_FAILED",
                {
                    "task_id": "recording-2",
                    "task_kind": "record_only",
                    "streamer": "果小果",
                    "video_file": "果小果_天梯冲分.flv",
                    "stage": "cover",
                    "error_message": "图片模型不可用",
                },
            )
        )

        self.assertIn("录播投稿任务已完成", completed.title)
        self.assertIn("BV1TEST", completed.markdown)
        self.assertIn("仅录制任务失败", failed.title)
        self.assertIn("图片模型不可用", failed.markdown)
        self.assertIn("cover", failed.markdown)

    @mock.patch("modules.notifications.emit_notification_event")
    def test_bridge_emits_task_added_for_new_recording_job(self, emit):
        bridge.emit_recording_task_added_notification(
            {
                "_config_dir": str(ROOT),
                "y2a_root": str(Y2A_ROOT),
                "streamer_name": "yyfyyf",
                "source_url": "https://www.douyu.com/9999",
            },
            fingerprint_value="fingerprint-1",
            video=pathlib.Path("/tmp/YYF_陪伴每一天.flv"),
            task_kind="recording_upload",
        )

        event = emit.call_args.args[0]
        self.assertEqual(event.event_type, "TASK_ADDED")
        self.assertEqual(event.payload["task_kind"], "recording_upload")
        self.assertEqual(event.payload["streamer"], "YYF")
        self.assertEqual(event.payload["upload_target"], "bilibili")

    @mock.patch("modules.notifications.emit_notification_event")
    def test_bridge_emits_recording_completion_and_failure(self, emit):
        cfg = {
            "_config_dir": str(ROOT),
            "y2a_root": str(Y2A_ROOT),
            "streamer_name": "果小果",
            "source_url": "https://www.douyu.com/123",
        }
        video = pathlib.Path("/tmp/果小果_天梯冲分.flv")

        bridge.emit_recording_task_result_notification(
            cfg,
            fingerprint_value="fingerprint-2",
            video=video,
            task_kind="recording_upload",
            status="completed",
            result={"bilibili": {"bvid": "BV1TEST"}},
        )
        bridge.emit_recording_task_result_notification(
            cfg,
            fingerprint_value="fingerprint-3",
            video=video,
            task_kind="record_only",
            status="failed",
            error="封面生成失败",
            stage="cover",
        )

        completed = emit.call_args_list[0].args[0]
        failed = emit.call_args_list[1].args[0]
        self.assertEqual(completed.event_type, "TASK_COMPLETED")
        self.assertEqual(completed.payload["bvid"], "BV1TEST")
        self.assertEqual(failed.event_type, "TASK_FAILED")
        self.assertEqual(failed.payload["stage"], "cover")
        self.assertEqual(failed.payload["error_message"], "封面生成失败")

    def test_recording_messages_include_streamer_and_title(self):
        started = build_notification_message(
            NotificationEvent(
                EVENT_RECORDING_STARTED,
                {
                    "streamer": "YYF",
                    "platform": "douyu",
                    "live_title": "陪伴每一天",
                    "room_url": "https://www.douyu.com/9999",
                    "started_at": "2026-07-27 12:00:00",
                },
            )
        )
        stopped = build_notification_message(
            NotificationEvent(
                EVENT_RECORDING_STOPPED,
                {
                    "streamer": "YYF",
                    "platform": "douyu",
                    "live_title": "陪伴每一天",
                    "duration_text": "1小时2分3秒",
                },
            )
        )

        self.assertIn("录制已开始", started.title)
        self.assertIn("YYF", started.markdown)
        self.assertIn("陪伴每一天", started.markdown)
        self.assertIn("录制已停止", stopped.title)
        self.assertIn("1小时2分3秒", stopped.markdown)

    def test_cookie_invalid_message_has_relogin_guidance(self):
        message = build_notification_message(
            NotificationEvent(
                EVENT_COOKIE_INVALID,
                {
                    "platform": "Bilibili",
                    "reason": "登录态校验未通过",
                    "source": "投稿登录态校验",
                },
            )
        )

        self.assertIn("Cookie 已失效", message.title)
        self.assertIn("重新登录", message.markdown)
        self.assertIn("投稿登录态校验", message.markdown)

    @mock.patch("modules.notifications.service.emit_notification_event")
    def test_deduplicated_event_only_emits_once_during_cooldown(self, emit):
        emit.return_value = 1
        event = NotificationEvent(
            EVENT_COOKIE_INVALID,
            {"platform": "Bilibili", "reason": "expired"},
        )
        key = f"test-{uuid.uuid4()}"

        first = emit_notification_event_deduplicated(
            event,
            dedupe_key=key,
            cooldown_seconds=3600,
        )
        second = emit_notification_event_deduplicated(
            event,
            dedupe_key=key,
            cooldown_seconds=3600,
        )

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        emit.assert_called_once_with(event)

    @mock.patch("modules.notifications.emit_notification_event")
    def test_recording_state_transitions_emit_once(self, emit):
        manager = LiveRecorderManager()
        room = {
            "id": "room-1",
            "name": "果小果",
            "url": "https://www.douyu.com/123",
            "runtime": {
                "recording": False,
                "live_title": "",
                "started_at": "",
                "duration_seconds": 0,
                "current_file": "",
            },
        }
        manager._reconcile_recording_notifications([room])
        room["runtime"].update(
            {
                "recording": True,
                "live_title": "天梯冲分",
                "started_at": "2026-07-27 12:00:00",
                "current_file": "果小果_天梯冲分.flv.part",
            }
        )
        manager._reconcile_recording_notifications([room])
        manager._reconcile_recording_notifications([room])
        room["runtime"]["recording"] = False
        manager._reconcile_recording_notifications([room])

        self.assertEqual(emit.call_count, 2)
        start_event = emit.call_args_list[0].args[0]
        stop_event = emit.call_args_list[1].args[0]
        self.assertEqual(start_event.event_type, EVENT_RECORDING_STARTED)
        self.assertEqual(stop_event.event_type, EVENT_RECORDING_STOPPED)
        self.assertEqual(start_event.payload["streamer"], "果小果")
        self.assertEqual(start_event.payload["live_title"], "天梯冲分")

    def test_settings_expose_new_event_switches_and_cookie_hooks(self):
        template = (Y2A_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
        config_source = (Y2A_ROOT / "modules" / "config_manager.py").read_text(encoding="utf-8")
        app_source = (Y2A_ROOT / "app.py").read_text(encoding="utf-8")
        task_source = (Y2A_ROOT / "modules" / "task_manager.py").read_text(encoding="utf-8")
        uploader_source = (Y2A_ROOT / "modules" / "bilibili_uploader.py").read_text(encoding="utf-8")

        for field in (
            "NOTIFY_EVENT_RECORDING_STARTED",
            "NOTIFY_EVENT_RECORDING_STOPPED",
            "NOTIFY_EVENT_COOKIE_INVALID",
        ):
            self.assertIn(field, template)
            self.assertIn(field, config_source)
            self.assertIn(field, app_source)
        self.assertIn("notify_cookie_invalid", task_source)
        self.assertIn("notify_cookie_invalid", uploader_source)


if __name__ == "__main__":
    unittest.main()
