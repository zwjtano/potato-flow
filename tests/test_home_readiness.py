import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_security_boundaries import app_module


class HomeReadinessTests(unittest.TestCase):
    def test_home_template_renders_readiness_items(self):
        readiness = {
            "state": "attention",
            "title": "系统尚未完全就绪",
            "detail": "还有 1 项需要配置或启动",
            "items": [
                {
                    "key": "recorder",
                    "label": "录制引擎",
                    "icon": "bi-broadcast",
                    "url": "/live-recording",
                    "state": "attention",
                    "value": "引擎待启动",
                    "detail": "直播间不会自动检测",
                }
            ],
        }
        with app_module.app.test_request_context("/"):
            rendered = app_module.render_template(
                "index.html",
                system_readiness=readiness,
                youtube_summary={
                    "monitor_enabled": 0,
                    "monitor_total": 0,
                    "queued": 0,
                    "processing": 0,
                    "completed_today": 0,
                    "review": 0,
                },
                recording_summary={
                    "engine_running": False,
                    "recording_now": 0,
                    "room_total": 0,
                    "room_enabled": 0,
                    "completed_today": 0,
                    "review": 0,
                },
            )

        self.assertIn("系统尚未完全就绪", rendered)
        self.assertIn("录制引擎", rendered)

    def test_ready_when_all_local_requirements_are_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cookie = root / "bili.json"
            cookie.write_text("{}", encoding="utf-8")
            config = {
                "BILIBILI_COOKIES_PATH": str(cookie),
                "OPENAI_API_KEY": "configured",
                "OPENAI_MODEL_NAME": "test-model",
                "RECORDINGS_PATH": str(root),
            }
            with app_module.app.test_request_context("/"):
                result = app_module._build_home_readiness(
                    config,
                    {"room_total": 1, "engine_running": True},
                    {"awaiting_review": 0},
                )

        self.assertEqual(result["state"], "ready")
        self.assertTrue(all(item["state"] == "ready" for item in result["items"]))

    def test_stopped_engine_and_missing_services_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "BILIBILI_COOKIES_PATH": str(Path(temp_dir) / "missing.json"),
                "OPENAI_API_KEY": "",
                "OPENAI_MODEL_NAME": "",
                "RECORDINGS_PATH": temp_dir,
            }
            with app_module.app.test_request_context("/"):
                result = app_module._build_home_readiness(
                    config,
                    {"room_total": 3, "engine_running": False},
                    {"awaiting_review": 0},
                )

        by_key = {item["key"]: item for item in result["items"]}
        self.assertEqual(result["state"], "attention")
        self.assertEqual(by_key["recorder"]["value"], "引擎待启动")
        self.assertEqual(by_key["bilibili"]["state"], "attention")
        self.assertEqual(by_key["ai"]["state"], "attention")

    def test_review_tasks_take_priority_over_configuration_warnings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "BILIBILI_COOKIES_PATH": "",
                "OPENAI_API_KEY": "",
                "OPENAI_MODEL_NAME": "",
                "RECORDINGS_PATH": temp_dir,
            }
            with app_module.app.test_request_context("/"):
                result = app_module._build_home_readiness(
                    config,
                    {"room_total": 0, "engine_running": False},
                    {"awaiting_review": 2},
                )

        self.assertEqual(result["state"], "error")
        self.assertIn("2 个任务", result["detail"])


if __name__ == "__main__":
    unittest.main()
