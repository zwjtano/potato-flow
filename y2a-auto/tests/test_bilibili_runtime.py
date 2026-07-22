import ast
import importlib
import json
import os
import pathlib
import re
import sys
import tempfile
import types
import unittest
from typing import Optional
from unittest import mock


class BilibiliRuntimeTests(unittest.TestCase):
    def test_pyinstaller_configs_collect_curl_cffi_runtime(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        source = (root / "build-tools" / "build_exe.py").read_text(encoding="utf-8")

        self.assertIn("collect_all('curl_cffi')", source)
        self.assertIn("datas += curl_cffi_datas", source)
        self.assertIn("binaries=curl_cffi_binaries", source)
        self.assertIn("+ curl_cffi_hiddenimports", source)

    def test_configure_runtime_sets_impersonate_once(self):
        import modules.bilibili_runtime as runtime

        runtime = importlib.reload(runtime)
        calls = []
        fake_settings = types.SimpleNamespace(set=lambda key, value: calls.append((key, value)))
        fake_bili_sdk = types.SimpleNamespace(request_settings=fake_settings)

        with mock.patch.dict(sys.modules, {"modules.bili_sdk": fake_bili_sdk}), mock.patch.object(
            runtime,
            "resolve_bilibili_ca_bundle",
            return_value="/tmp/test-ca.pem",
        ):
            self.assertTrue(runtime.configure_bilibili_runtime())
            self.assertTrue(runtime.configure_bilibili_runtime())

        self.assertEqual(
            calls,
            [("verify_ssl", "/tmp/test-ca.pem"), ("impersonate", "chrome131")],
        )
        self.assertIsNone(runtime.get_bilibili_runtime_error())

    def test_ca_bundle_prefers_explicit_environment_path(self):
        import modules.bilibili_runtime as runtime

        with tempfile.TemporaryDirectory() as temp_dir:
            ca_path = pathlib.Path(temp_dir) / "custom-ca.pem"
            ca_path.write_text("custom", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"BILIBILI_CA_BUNDLE": str(ca_path)},
                clear=False,
            ):
                self.assertEqual(
                    pathlib.Path(runtime.resolve_bilibili_ca_bundle()),
                    ca_path.resolve(),
                )

    def test_macos_ca_bundle_merges_keychain_certificates(self):
        import modules.bilibili_runtime as runtime

        cert_a = b"-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----"
        cert_b = b"-----BEGIN CERTIFICATE-----\nB\n-----END CERTIFICATE-----"
        with tempfile.TemporaryDirectory() as temp_dir:
            certifi_path = pathlib.Path(temp_dir) / "cacert.pem"
            certifi_path.write_bytes(cert_a)
            fake_certifi = types.SimpleNamespace(where=lambda: str(certifi_path))
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.dict(
                sys.modules,
                {"certifi": fake_certifi},
            ), mock.patch.object(runtime.platform, "system", return_value="Darwin"), mock.patch.object(
                runtime,
                "_read_macos_keychain_certificates",
                return_value=[cert_a, cert_b],
            ), mock.patch.object(runtime.tempfile, "gettempdir", return_value=temp_dir):
                bundle = pathlib.Path(runtime.resolve_bilibili_ca_bundle())

            content = bundle.read_bytes()
            self.assertEqual(content.count(b"BEGIN CERTIFICATE"), 2)

    def test_linux_ca_bundle_prefers_system_trust_store(self):
        import modules.bilibili_runtime as runtime

        with tempfile.TemporaryDirectory() as temp_dir:
            certifi_path = pathlib.Path(temp_dir) / "certifi.pem"
            system_path = pathlib.Path(temp_dir) / "system-ca.pem"
            certifi_path.write_text("certifi", encoding="utf-8")
            system_path.write_text("system", encoding="utf-8")
            fake_certifi = types.SimpleNamespace(where=lambda: str(certifi_path))
            verify_paths = types.SimpleNamespace(cafile=str(system_path))
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.dict(
                sys.modules,
                {"certifi": fake_certifi},
            ), mock.patch.object(runtime.platform, "system", return_value="Linux"), mock.patch.object(
                runtime.ssl,
                "get_default_verify_paths",
                return_value=verify_paths,
            ):
                bundle = runtime.resolve_bilibili_ca_bundle()

            self.assertEqual(pathlib.Path(bundle), system_path.resolve())

    def test_zone_wrapper_returns_sdk_data(self):
        import modules.bilibili_zones as zones

        fake_video_zone = types.SimpleNamespace(get_zone_list_sub=lambda: [{"tid": 1, "sub": [{"tid": 2}]}])
        fake_bili_sdk = types.SimpleNamespace(video_zone=fake_video_zone)

        with mock.patch.dict(sys.modules, {"modules.bili_sdk": fake_bili_sdk}):
            with mock.patch.object(zones, "configure_bilibili_runtime", return_value=True):
                self.assertEqual(zones.get_zone_list_sub(), [{"tid": 1, "sub": [{"tid": 2}]}])
                self.assertEqual(zones.collect_valid_tids(), {"1", "2"})

    def test_zone_wrapper_falls_back_to_empty_list(self):
        import modules.bilibili_zones as zones

        with mock.patch.dict(sys.modules, {"modules.bili_sdk": None}):
            with mock.patch.object(zones, "configure_bilibili_runtime", return_value=False):
                self.assertEqual(zones.get_zone_list_sub(), [])

    def test_credential_extra_cookies_are_whitelisted(self):
        from modules.bili_sdk import Credential

        credential = Credential(
            sessdata="sess",
            bili_jct="csrf",
            DedeUserID__ckMd5="allowed",
            _logger="not-a-cookie",
            random_state="not-a-cookie",
        )

        cookies = credential.get_cookies()

        self.assertEqual(cookies["DedeUserID__ckMd5"], "allowed")
        self.assertNotIn("_logger", cookies)
        self.assertNotIn("random_state", cookies)


class BilibiliAuthTests(unittest.TestCase):
    def test_save_credential_writes_required_cookies(self):
        from modules.bilibili_auth import save_credential_to_file

        credential = mock.Mock()
        credential.get_cookies.return_value = {
            "SESSDATA": "sess",
            "bili_jct": "csrf",
            "DedeUserID": "123",
            "buvid3": "buvid",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = pathlib.Path(temp_dir) / "cookies" / "bili_cookies.json"

            self.assertTrue(save_credential_to_file(credential, str(cookie_path)))

            cookie_items = json.loads(cookie_path.read_text(encoding="utf-8"))
            cookies = {item["name"]: item["value"] for item in cookie_items}
            self.assertEqual(cookies["SESSDATA"], "sess")
            self.assertEqual(cookies["bili_jct"], "csrf")
            self.assertEqual(cookies["DedeUserID"], "123")
            self.assertEqual(cookies["buvid3"], "buvid")

    def test_save_credential_does_not_write_when_cookie_extraction_fails(self):
        from modules.bilibili_auth import save_credential_to_file

        credential = mock.Mock()
        credential.get_cookies.side_effect = RuntimeError("cookie extraction failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = pathlib.Path(temp_dir) / "cookies" / "bili_cookies.json"

            self.assertFalse(save_credential_to_file(credential, str(cookie_path)))
            self.assertFalse(cookie_path.exists())

    def test_save_credential_does_not_overwrite_for_missing_required_cookie(self):
        from modules.bilibili_auth import save_credential_to_file

        credential = mock.Mock()
        credential.get_cookies.return_value = {
            "SESSDATA": "sess",
            "bili_jct": "csrf",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = pathlib.Path(temp_dir) / "bili_cookies.json"
            cookie_path.write_text("existing", encoding="utf-8")

            self.assertFalse(save_credential_to_file(credential, str(cookie_path)))
            self.assertEqual(cookie_path.read_text(encoding="utf-8"), "existing")

    def test_qrcode_done_becomes_failed_when_cookie_save_fails(self):
        import modules.bilibili_auth as auth

        session = object.__new__(auth.BilibiliQrLoginSession)
        session.generated = True
        session.last_state = None
        session.qr = mock.Mock()
        credential = mock.Mock()
        session.qr.get_credential.return_value = credential

        with mock.patch.object(
            auth,
            "_run_async",
            return_value=auth.login_v2.QrCodeLoginEvents.DONE,
        ), mock.patch.object(
            auth,
            "validate_credential_remote",
            return_value=(True, "ok"),
        ), mock.patch.object(
            auth,
            "save_credential_to_file",
            return_value=False,
        ):
            payload = session.check_status("cookies/bili_cookies.json")

        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["cookies_saved"])
        self.assertIn("保存失败", payload["message"])
        self.assertNotIn("credential_ok", payload)


class BilibiliUploaderDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "bilibili_uploader.py"
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
        function_names = {
            "_extract_response_code_from_exception",
            "_compact_exception_text",
            "_is_bilibili_http_406",
            "_bilibili_406_hint",
        }
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in function_names]
        namespace = {"re": re, "Optional": Optional}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(module_path), "exec"), namespace)
        cls.helpers = namespace

    def test_detects_bilibili_406_status_text(self):
        exc = Exception("网络错误，状态码：406 - 。")
        self.assertTrue(self.helpers["_is_bilibili_http_406"](exc))

    def test_detects_bilibili_406_code_attribute(self):
        exc = Exception("blocked")
        exc.code = 406
        self.assertTrue(self.helpers["_is_bilibili_http_406"](exc))

    def test_406_hint_mentions_relogin_and_network(self):
        hint = self.helpers["_bilibili_406_hint"]()
        self.assertIn("preupload", hint)
        self.assertIn("重新扫码登录", hint)
        self.assertIn("网络环境", hint)


if __name__ == "__main__":
    unittest.main()
