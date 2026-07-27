import json
import sys
import tempfile
import unittest
from pathlib import Path


Y2A_ROOT = Path(__file__).resolve().parents[1] / "y2a-auto"
sys.path.insert(0, str(Y2A_ROOT))

from modules.douyin_auth import load_douyin_cookie  # noqa: E402


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

    def test_loads_plain_text_cookie_header(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "douyin.json"
            path.write_text("sessionid=abc; ttwid=xyz", encoding="utf-8")
            self.assertEqual(
                load_douyin_cookie(path),
                "sessionid=abc; ttwid=xyz",
            )

    def test_loads_netscape_cookie_export(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cookies.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc\n"
                ".douyin.com\tTRUE\t/\tTRUE\t0\tttwid\txyz\n",
                encoding="utf-8",
            )
            self.assertEqual(
                load_douyin_cookie(path),
                "sessionid=abc; ttwid=xyz",
            )


if __name__ == "__main__":
    unittest.main()
