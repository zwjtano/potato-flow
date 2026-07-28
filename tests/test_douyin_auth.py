import json
import sys
import tempfile
import unittest
from pathlib import Path


Y2A_ROOT = Path(__file__).resolve().parents[1] / "y2a-auto"
sys.path.insert(0, str(Y2A_ROOT))

from modules.douyin_auth import (  # noqa: E402
    load_douyin_cookie,
    missing_douyin_cookie_names,
    normalize_douyin_cookie,
)


class DouyinAuthTests(unittest.TestCase):
    def test_loads_browser_cookie_json_as_header(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "douyin.json"
            path.write_text(json.dumps([
                {"name": "LOGIN_STATUS", "value": "1", "domain": ".douyin.com"},
                {"name": "__ac_nonce", "value": "nonce", "domain": ".douyin.com"},
                {"name": "__ac_signature", "value": "signature", "domain": ".douyin.com"},
                {"name": "sessionid", "value": "abc", "domain": ".douyin.com"},
            ]), encoding="utf-8")

            self.assertEqual(
                load_douyin_cookie(path),
                "__ac_nonce=nonce; __ac_signature=signature; sessionid=abc",
            )

    def test_loads_plain_text_cookie_header(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "douyin.json"
            path.write_text(
                "ttwid=xyz; sessionid=abc; __ac_signature=signature; "
                "__ac_nonce=nonce; LOGIN_STATUS=1",
                encoding="utf-8",
            )
            self.assertEqual(
                load_douyin_cookie(path),
                "__ac_nonce=nonce; __ac_signature=signature; sessionid=abc",
            )

    def test_loads_netscape_cookie_export(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cookies.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".douyin.com\tTRUE\t/\tTRUE\t0\t__ac_signature\tsignature\n"
                ".douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc\n"
                ".douyin.com\tTRUE\t/\tTRUE\t0\t__ac_nonce\tnonce\n"
                ".douyin.com\tTRUE\t/\tTRUE\t0\tttwid\txyz\n",
                encoding="utf-8",
            )
            self.assertEqual(
                load_douyin_cookie(path),
                "__ac_nonce=nonce; __ac_signature=signature; sessionid=abc",
            )

    def test_ignores_cookie_values_from_unrelated_domains(self):
        content = json.dumps([
            {"name": "__ac_nonce", "value": "wrong", "domain": ".example.com"},
            {"name": "__ac_nonce", "value": "nonce", "domain": ".douyin.com"},
            {"name": "__ac_signature", "value": "signature", "domain": "www.douyin.com"},
            {"name": "sessionid", "value": "abc", "domain": ".douyin.com"},
        ])

        self.assertEqual(
            normalize_douyin_cookie(content),
            "__ac_nonce=nonce; __ac_signature=signature; sessionid=abc",
        )

    def test_reports_missing_required_cookie_names(self):
        self.assertEqual(
            missing_douyin_cookie_names("sessionid=abc; ttwid=xyz"),
            ("__ac_nonce", "__ac_signature"),
        )


if __name__ == "__main__":
    unittest.main()
