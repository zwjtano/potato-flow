import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.network_proxy import (  # noqa: E402
    build_common_proxy_url,
    common_proxy_values,
)
from modules import config_manager  # noqa: E402
from modules.youtube_handler import build_proxy_url  # noqa: E402


class CommonNetworkProxyTests(unittest.TestCase):
    def test_common_proxy_is_shared_with_youtube(self):
        config = {
            "YOUTUBE_PROXY_ENABLED": True,
            "NETWORK_PROXY_URL": "socks5://proxy.example.com:1080",
            "NETWORK_PROXY_USERNAME": "user name",
            "NETWORK_PROXY_PASSWORD": "p@ss",
        }
        expected = "socks5://user%20name:p%40ss@proxy.example.com:1080"
        self.assertEqual(build_common_proxy_url(config), expected)
        self.assertEqual(build_proxy_url(config), expected)

    def test_disabled_youtube_stays_direct_even_when_common_proxy_exists(self):
        config = {
            "YOUTUBE_PROXY_ENABLED": False,
            "NETWORK_PROXY_URL": "http://127.0.0.1:7890",
        }
        self.assertIsNone(build_proxy_url(config))

    def test_legacy_youtube_proxy_is_migratable(self):
        self.assertEqual(
            common_proxy_values({
                "YOUTUBE_PROXY_URL": "http://legacy.example.com:7890",
                "YOUTUBE_PROXY_USERNAME": "alice",
                "YOUTUBE_PROXY_PASSWORD": "secret",
            }),
            ("http://legacy.example.com:7890", "alice", "secret"),
        )

    def test_legacy_telegram_proxy_is_migratable(self):
        self.assertEqual(
            common_proxy_values({
                "NOTIFY_TELEGRAM_PROXY_URL": "https://telegram-proxy.example.com",
            }),
            ("https://telegram-proxy.example.com", "", ""),
        )

    def test_empty_common_proxy_does_not_fall_back_to_legacy_values(self):
        config = {
            "NETWORK_PROXY_URL": "",
            "NETWORK_PROXY_USERNAME": "",
            "NETWORK_PROXY_PASSWORD": "",
            "YOUTUBE_PROXY_URL": "http://legacy.example.com:7890",
            "NOTIFY_TELEGRAM_PROXY_URL": "http://telegram.example.com:8080",
        }
        self.assertEqual(common_proxy_values(config), ("", "", ""))
        self.assertEqual(build_common_proxy_url(config), "")

    def test_invalid_proxy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "通用代理地址无效"):
            build_common_proxy_url({"NETWORK_PROXY_URL": "ftp://proxy.example.com"})

    def test_load_config_migrates_enabled_legacy_proxy(self):
        with tempfile.TemporaryDirectory() as temp:
            config_dir = pathlib.Path(temp) / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.json"
            config_file.write_text(
                json.dumps({
                    "YOUTUBE_PROXY_ENABLED": True,
                    "YOUTUBE_PROXY_URL": "http://legacy.example.com:7890",
                    "YOUTUBE_PROXY_USERNAME": "alice",
                    "YOUTUBE_PROXY_PASSWORD": "secret",
                    "NOTIFY_TELEGRAM_PROXY_URL": "http://telegram.example.com:8080",
                }),
                encoding="utf-8",
            )
            with patch.object(config_manager, "get_app_subdir", return_value=str(config_dir)):
                loaded = config_manager.load_config()
            self.assertEqual(loaded["NETWORK_PROXY_URL"], "http://legacy.example.com:7890")
            self.assertEqual(loaded["NETWORK_PROXY_USERNAME"], "alice")
            self.assertEqual(loaded["NETWORK_PROXY_PASSWORD"], "secret")
            persisted = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["NETWORK_PROXY_URL"], "http://legacy.example.com:7890")

    def test_load_config_keeps_explicitly_cleared_common_proxy_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            config_dir = pathlib.Path(temp) / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.json"
            config_file.write_text(
                json.dumps({
                    "NETWORK_PROXY_URL": "",
                    "NETWORK_PROXY_USERNAME": "",
                    "NETWORK_PROXY_PASSWORD": "",
                    "YOUTUBE_PROXY_URL": "http://legacy.example.com:7890",
                    "NOTIFY_TELEGRAM_PROXY_URL": "http://telegram.example.com:8080",
                }),
                encoding="utf-8",
            )
            with patch.object(config_manager, "get_app_subdir", return_value=str(config_dir)):
                loaded = config_manager.load_config()
            self.assertEqual(loaded["NETWORK_PROXY_URL"], "")


if __name__ == "__main__":
    unittest.main()
