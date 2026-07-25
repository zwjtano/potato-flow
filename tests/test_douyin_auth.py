import json
import sys
import tempfile
import unittest
from pathlib import Path


Y2A_ROOT = Path(__file__).resolve().parents[1] / "y2a-auto"
sys.path.insert(0, str(Y2A_ROOT))

from modules.douyin_auth import DouyinQrLoginSession, load_douyin_cookie  # noqa: E402


class DouyinAuthTests(unittest.TestCase):
    def test_loads_browser_cookie_json_as_header(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "douyin.json"
            path.write_text(json.dumps([
                {"name": "LOGIN_STATUS", "value": "1", "domain": ".douyin.com"},
                {"name": "sessionid", "value": "abc", "domain": ".douyin.com"},
            ]), encoding="utf-8")

            self.assertEqual(
                load_douyin_cookie(path),
                "LOGIN_STATUS=1; sessionid=abc",
            )

    def test_session_saves_only_douyin_cookies(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "douyin.json"
            session = DouyinQrLoginSession(path)
            saved = session._save_cookies([
                {"name": "sessionid", "value": "abc", "domain": ".douyin.com", "path": "/"},
                {"name": "other", "value": "ignored", "domain": ".example.com", "path": "/"},
            ])

            self.assertTrue(saved)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([item["name"] for item in payload], ["sessionid"])


if __name__ == "__main__":
    unittest.main()
