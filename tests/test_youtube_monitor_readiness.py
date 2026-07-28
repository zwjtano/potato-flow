import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_security_boundaries import app_module


class YouTubeMonitorReadinessTests(unittest.TestCase):
    def test_missing_api_and_cookie_are_explicit(self):
        with app_module.app.test_request_context("/youtube_monitor"):
            with patch.object(app_module.youtube_monitor, "youtube", None):
                result = app_module._build_youtube_monitor_readiness({
                    "YOUTUBE_API_KEY": "",
                    "YOUTUBE_COOKIES_PATH": "/missing/cookies.txt",
                    "YOUTUBE_API_PROXY_ENABLED": False,
                })

        by_key = {item["key"]: item for item in result["items"]}
        self.assertFalse(result["ready"])
        self.assertEqual(by_key["api"]["state"], "attention")
        self.assertEqual(by_key["cookies"]["state"], "attention")
        self.assertEqual(by_key["network"]["value"], "直连模式")

    def test_ready_with_api_client_cookie_and_direct_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie = Path(temp_dir) / "youtube.txt"
            cookie.write_text("cookie", encoding="utf-8")
            with app_module.app.test_request_context("/youtube_monitor"):
                with patch.object(app_module.youtube_monitor, "youtube", object()):
                    result = app_module._build_youtube_monitor_readiness({
                        "YOUTUBE_API_KEY": "configured",
                        "YOUTUBE_COOKIES_PATH": str(cookie),
                        "YOUTUBE_API_PROXY_ENABLED": False,
                    })

        self.assertTrue(result["ready"])
        self.assertFalse(result["blocking"])

    def test_enabled_proxy_without_address_is_blocking(self):
        with app_module.app.test_request_context("/youtube_monitor"):
            with patch.object(app_module.youtube_monitor, "youtube", object()):
                result = app_module._build_youtube_monitor_readiness({
                    "YOUTUBE_API_KEY": "configured",
                    "YOUTUBE_COOKIES_PATH": "/missing/cookies.txt",
                    "YOUTUBE_API_PROXY_ENABLED": True,
                    "NETWORK_PROXY_URL": "",
                })

        network = next(item for item in result["items"] if item["key"] == "network")
        self.assertEqual(network["state"], "error")
        self.assertTrue(result["blocking"])

    def test_template_renders_prerequisite_links(self):
        readiness = {
            "ready": False,
            "blocking": False,
            "items": [{
                "key": "api",
                "label": "YouTube Data API",
                "icon": "bi-key",
                "state": "attention",
                "value": "API Key 未配置",
                "detail": "监控发现功能暂不可用",
                "url": "/settings#vtab-ops",
            }],
        }
        with app_module.app.test_request_context("/youtube_monitor"):
            rendered = app_module.render_template(
                "youtube_monitor.html",
                configs=[],
                history=[],
                readiness=readiness,
            )

        self.assertIn("监控前置检查", rendered)
        self.assertIn("API Key 未配置", rendered)
        self.assertIn("/settings#vtab-ops", rendered)


if __name__ == "__main__":
    unittest.main()
