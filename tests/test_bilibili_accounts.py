import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
Y2A_ROOT = ROOT / "y2a-auto"
if str(Y2A_ROOT) not in sys.path:
    sys.path.insert(0, str(Y2A_ROOT))

from modules.bilibili_accounts import (  # noqa: E402
    LEGACY_ACCOUNT_ID,
    create_account_record,
    default_account_id,
    normalize_accounts,
    resolve_account,
    serialize_custom_accounts,
)


class BilibiliAccountsTests(unittest.TestCase):
    def test_legacy_account_remains_compatible_and_exposes_real_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            cookie = Path(temp) / "bili.json"
            cookie.write_text(
                json.dumps([
                    {"name": "DedeUserID", "value": "64340982"},
                    {"name": "SESSDATA", "value": "session"},
                    {"name": "bili_jct", "value": "csrf"},
                ]),
                encoding="utf-8",
            )
            accounts = normalize_accounts({
                "BILIBILI_COOKIES_PATH": str(cookie),
                "BILIBILI_ACCOUNT_NAME": "土豆萨哈",
            })

        self.assertEqual(accounts[0]["id"], LEGACY_ACCOUNT_ID)
        self.assertEqual(accounts[0]["name"], "土豆萨哈")
        self.assertEqual(accounts[0]["bilibili_uid"], "64340982")

    def test_custom_account_uses_persisted_real_name_uid_and_default(self):
        config = {
            "BILIBILI_COOKIES_PATH": "cookies/bili_cookies.json",
            "BILIBILI_ACCOUNTS": [{
                "id": "bili-editor",
                "name": "旧备注",
                "bilibili_name": "真实昵称",
                "bilibili_uid": "123456",
                "cookies_path": "cookies/bilibili_accounts/editor.json",
            }],
            "BILIBILI_DEFAULT_ACCOUNT_ID": "bili-editor",
        }

        account = resolve_account(config)
        self.assertEqual(default_account_id(config), "bili-editor")
        self.assertEqual(account["name"], "真实昵称")
        self.assertEqual(account["bilibili_uid"], "123456")
        self.assertEqual(
            serialize_custom_accounts(normalize_accounts(config))[0]["bilibili_name"],
            "真实昵称",
        )

    def test_unknown_account_falls_back_to_default(self):
        config = {
            "BILIBILI_ACCOUNTS": [{
                "id": "bili-main",
                "name": "主账号",
                "cookies_path": "cookies/bilibili_accounts/main.json",
            }],
            "BILIBILI_DEFAULT_ACCOUNT_ID": "bili-main",
        }
        self.assertEqual(resolve_account(config, "missing")["id"], "bili-main")

    def test_new_account_record_uses_isolated_cookie_path(self):
        account = create_account_record("", "cookies.txt")
        self.assertTrue(account["id"].startswith("bili-"))
        self.assertIn("cookies/bilibili_accounts/", account["cookies_path"])
        self.assertTrue(account["cookies_path"].endswith(".txt"))

    def test_templates_expose_real_identity_and_account_binding(self):
        settings = (Y2A_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
        tasks = (Y2A_ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")
        live = (Y2A_ROOT / "templates" / "live_recording.html").read_text(encoding="utf-8")
        youtube_config = (
            Y2A_ROOT / "templates" / "youtube_monitor_config.html"
        ).read_text(encoding="utf-8")
        youtube_monitor = (
            Y2A_ROOT / "modules" / "youtube_monitor.py"
        ).read_text(encoding="utf-8")
        manager = (Y2A_ROOT / "modules" / "live_recorder_manager.py").read_text(encoding="utf-8")

        self.assertIn("UID 未识别", settings)
        self.assertIn('data-role="set-default-bilibili-account"', settings)
        self.assertNotIn(
            'formaction="{{ url_for(\'set_default_bilibili_account\'',
            settings,
        )
        self.assertIn("bilibili_account_id", tasks)
        self.assertIn("bilibili_account_id", live)
        self.assertIn('name="bilibili_account_id"', youtube_config)
        self.assertIn("'bilibili_account_id': ''", youtube_monitor)
        self.assertIn("bilibili_account_id=bilibili_account_id", youtube_monitor)
        self.assertIn('config["bilibili_cookies"] = _workspace_runtime_path', manager)
        self.assertIn(
            '"bilibili_cookies": str(resolve_cookie_path(account.get("cookies_path")))',
            manager,
        )


if __name__ == "__main__":
    unittest.main()
