import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import app as app_module  # noqa: E402


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True, SECRET_KEY="security-test")
        self.client = app_module.app.test_client()

    def test_public_health_probe_is_minimal(self):
        with patch.object(
            app_module,
            "load_config",
            return_value={"password_protection_enabled": True},
        ):
            response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.get_json()),
            {
                "status", "version", "application_version", "runtime_mode",
                "architecture", "recorder_core_version", "desktop_instance",
            },
        )
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")

    def test_detailed_health_requires_login_when_protection_is_enabled(self):
        with patch.object(
            app_module,
            "load_config",
            return_value={"password_protection_enabled": True},
        ):
            response = self.client.get("/system_health")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_csrf_rejects_missing_token_and_accepts_session_token(self):
        protected = {"password_protection_enabled": True}
        with patch.object(app_module, "load_config", return_value=protected):
            blocked = self.client.post("/route-that-does-not-exist", json={})
            with self.client.session_transaction() as session_state:
                session_state[app_module._CSRF_SESSION_KEY] = "known-token"
            accepted = self.client.post(
                "/route-that-does-not-exist",
                json={},
                headers={"X-CSRF-Token": "known-token"},
            )

        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(accepted.status_code, 404)

    def test_open_recording_file_folder_uses_containing_directory_on_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            recording = Path(temp) / "主播" / "finished.xml"
            recording.parent.mkdir()
            recording.write_text("<i/>", encoding="utf-8")
            with self.client.session_transaction() as session_state:
                session_state[app_module._CSRF_SESSION_KEY] = "known-token"
            with (
                patch.object(app_module, "load_config", return_value={}),
                patch.object(
                    app_module.live_recorder_manager,
                    "recording_file",
                    return_value=(recording, {"name": recording.name}),
                ),
                patch.object(app_module.sys, "platform", "win32"),
                patch.object(app_module.os, "startfile", create=True) as startfile,
            ):
                response = self.client.post(
                    "/live-recording/files/open-folder",
                    json={"file_id": "safe-file-id"},
                    headers={"X-CSRF-Token": "known-token"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["path"], str(recording.parent))
        self.assertEqual(response.get_json()["message"], "已打开文件所在文件夹。")
        startfile.assert_called_once_with(str(recording.parent))

    def test_delete_room_system_error_redirects_with_message_instead_of_500(self):
        with self.client.session_transaction() as session_state:
            session_state[app_module._CSRF_SESSION_KEY] = "known-token"
        with (
            patch.object(app_module, "load_config", return_value={}),
            patch.object(
                app_module.live_recorder_manager,
                "delete_room_and_reload",
                side_effect=PermissionError("file is temporarily in use"),
            ),
        ):
            response = self.client.post(
                "/live-recording/rooms/test-room/delete",
                headers={"X-CSRF-Token": "known-token"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/live-recording"))
        with self.client.session_transaction() as session_state:
            flashes = session_state.get("_flashes", [])
        self.assertIn(
            ("danger", "删除直播间时录制进程清理失败，请重试；已有录播文件和上传任务未删除。"),
            flashes,
        )

    def test_password_hash_and_legacy_password_verification(self):
        hashed = app_module.generate_password_hash("correct horse")

        self.assertEqual(
            app_module._verify_login_password(hashed, "correct horse"),
            (True, False),
        )
        self.assertEqual(
            app_module._verify_login_password("legacy-password", "legacy-password"),
            (True, True),
        )
        self.assertEqual(
            app_module._verify_login_password(hashed, "wrong"),
            (False, False),
        )

    def test_admin_username_normalization(self):
        self.assertEqual(app_module._normalize_admin_username(" 管理员_01 "), "管理员_01")
        self.assertEqual(app_module._normalize_admin_username("a"), "")
        self.assertEqual(app_module._normalize_admin_username("admin name"), "")

    def test_login_requires_matching_admin_username_and_password(self):
        protected = {
            "password_protection_enabled": True,
            "admin_username": "owner",
            "password": app_module.generate_password_hash("correct horse"),
            "LOGIN_MAX_FAILED_ATTEMPTS": 5,
            "LOGIN_LOCKOUT_MINUTES": 15,
        }
        with self.client.session_transaction() as session_state:
            session_state[app_module._CSRF_SESSION_KEY] = "known-token"
        with (
            patch.object(app_module, "load_config", return_value=protected),
            patch.object(
                app_module,
                "_load_security_state",
                return_value={"failed_attempts": 0, "locked_until": 0, "last_attempt": 0},
            ),
            patch.object(app_module, "_save_security_state"),
            patch.object(app_module, "_emit_login_event"),
        ):
            response = self.client.post(
                "/login",
                data={
                    "_csrf_token": "known-token",
                    "username": "owner",
                    "password": "correct horse",
                },
            )

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session_state:
            self.assertTrue(session_state["logged_in"])
            self.assertEqual(session_state["admin_username"], "owner")

    def test_admin_avatar_upload_is_cropped_and_persisted(self):
        image_bytes = io.BytesIO()
        Image.new("RGB", (900, 500), (32, 120, 180)).save(image_bytes, format="JPEG")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            avatar_path = root / "admin" / "avatar.png"
            form_data = {}
            with (
                patch.object(app_module, "_admin_avatar_file", return_value=avatar_path),
                patch.object(
                    app_module,
                    "get_app_subdir",
                    side_effect=lambda name: str(root / name),
                ),
            ):
                app_module._persist_settings_uploads(
                    form_data,
                    {
                        "admin_avatar_file": {
                            "filename": "avatar.jpg",
                            "content": image_bytes.getvalue(),
                        }
                    },
                )

            self.assertEqual(form_data["admin_avatar_path"], "admin/avatar.png")
            with Image.open(avatar_path) as avatar:
                self.assertEqual(avatar.size, (512, 512))

    def test_secret_form_value_never_returns_the_secret(self):
        rendered = app_module._secret_form_value(
            {"OPENAI_API_KEY": "sk-sensitive-value"},
            "OPENAI_API_KEY",
        )

        self.assertEqual(rendered, app_module._SECRET_FORM_SENTINEL)
        self.assertNotIn("sk-sensitive-value", rendered)

    def test_settings_template_uses_secret_placeholders(self):
        template = (APP_ROOT / "templates" / "settings.html").read_text(
            encoding="utf-8"
        )

        for key in app_module._SENSITIVE_SETTING_FIELDS:
            if f'name="{key}"' not in template:
                continue
            self.assertIn(
                f"secret_field_value('{key}')",
                template,
                msg=f"{key} must not be rendered from config directly",
            )

    def test_login_form_has_a_csrf_token_without_javascript(self):
        template = (APP_ROOT / "templates" / "login.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('name="_csrf_token"', template)
        self.assertIn('value="{{ csrf_token }}"', template)
        self.assertIn('name="username"', template)
        self.assertIn('login-admin-avatar', template)

    def test_fetch_csrf_header_is_restricted_to_same_origin(self):
        script = (APP_ROOT / "static" / "js" / "main.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("requestUrl.origin === window.location.origin", script)
        self.assertIn("action.origin !== window.location.origin", script)

    def test_scoped_settings_only_default_checkboxes_in_current_group(self):
        form_data = {"AUTO_MODE_ENABLED": "on"}

        app_module._apply_missing_checkbox_defaults(
            form_data,
            ["AUTO_MODE_ENABLED", "GENERATE_TAGS", "NOTIFY_ENABLED"],
            {"AUTO_MODE_ENABLED", "GENERATE_TAGS"},
        )

        self.assertEqual(form_data["AUTO_MODE_ENABLED"], "on")
        self.assertEqual(form_data["GENERATE_TAGS"], "off")
        self.assertNotIn("NOTIFY_ENABLED", form_data)

    def test_empty_settings_scope_does_not_clear_other_checkboxes(self):
        form_data = {}

        app_module._apply_missing_checkbox_defaults(
            form_data,
            ["AUTO_MODE_ENABLED", "NOTIFY_ENABLED"],
            set(),
        )

        self.assertEqual(form_data, {})

    def test_legacy_full_settings_submission_keeps_existing_behavior(self):
        form_data = {}

        app_module._apply_missing_checkbox_defaults(
            form_data,
            ["AUTO_MODE_ENABLED", "NOTIFY_ENABLED"],
        )

        self.assertEqual(
            form_data,
            {"AUTO_MODE_ENABLED": "off", "NOTIFY_ENABLED": "off"},
        )

    def test_recording_ai_prompt_rejects_more_than_six_thousand_characters(self):
        with patch.object(app_module, "load_config", return_value={}):
            result = app_module._perform_settings_save(
                {"RECORDING_AI_TITLE_PROMPT": "题" * 6001},
                {},
            )

        self.assertFalse(result["success"])
        self.assertIn("不能超过 6000 字", result["final_detail"])

    def test_saving_recording_ai_prompt_syncs_bridge_config_without_restart(self):
        updated = {
            "RECORDINGS_PATH": "docker-data/recordings",
            "RECORDING_AI_TITLE_PROMPT": "  先写人物与英雄  ",
        }
        with (
            patch.object(
                app_module,
                "load_config",
                return_value={"RECORDINGS_PATH": "docker-data/recordings"},
            ),
            patch.object(app_module, "update_config", return_value=updated) as update,
            patch.object(app_module.live_recorder_manager, "sync_configs") as sync,
            patch.object(app_module.live_recorder_manager, "refresh_credentials") as refresh,
            patch.object(app_module, "configure_app"),
            patch("modules.task_manager.get_global_task_processor"),
            patch.object(app_module, "_sync_notification_service"),
            patch.object(
                app_module.youtube_monitor,
                "reload_api_client",
                return_value=(False, "missing_api_key"),
            ),
            patch.object(app_module.youtube_monitor, "stop_all_schedules"),
        ):
            result = app_module._perform_settings_save(
                {
                    "settings_scope": "vtab-ai-models",
                    "settings_scope_fields": "RECORDING_AI_TITLE_PROMPT",
                    "RECORDING_AI_TITLE_PROMPT": "  先写人物与英雄  ",
                },
                {},
            )

        self.assertTrue(result["success"])
        self.assertEqual(
            update.call_args.args[0]["RECORDING_AI_TITLE_PROMPT"],
            "先写人物与英雄",
        )
        sync.assert_called_once_with()
        refresh.assert_not_called()

    @unittest.skip("v1.6.59 removed the Dota 2 cover layout setting")
    def test_saving_dota2_cover_layout_mode_syncs_bridge_config(self):
        updated = {
            "RECORDINGS_PATH": "docker-data/recordings",
            "RECORDING_DOTA2_COVER_LAYOUT_MODE": "fusion",
        }
        with (
            patch.object(
                app_module,
                "load_config",
                return_value={"RECORDINGS_PATH": "docker-data/recordings"},
            ),
            patch.object(app_module, "update_config", return_value=updated) as update,
            patch.object(app_module.live_recorder_manager, "sync_configs") as sync,
            patch.object(app_module.live_recorder_manager, "refresh_credentials") as refresh,
            patch.object(app_module, "configure_app"),
            patch("modules.task_manager.get_global_task_processor"),
            patch.object(app_module, "_sync_notification_service"),
            patch.object(
                app_module.youtube_monitor,
                "reload_api_client",
                return_value=(False, "missing_api_key"),
            ),
            patch.object(app_module.youtube_monitor, "stop_all_schedules"),
        ):
            result = app_module._perform_settings_save(
                {
                    "settings_scope": "vtab-ai-models",
                    "settings_scope_fields": "RECORDING_DOTA2_COVER_LAYOUT_MODE",
                    "RECORDING_DOTA2_COVER_LAYOUT_MODE": "fusion",
                },
                {},
            )

        self.assertTrue(result["success"])
        self.assertEqual(
            update.call_args.args[0]["RECORDING_DOTA2_COVER_LAYOUT_MODE"],
            "fusion",
        )
        sync.assert_called_once_with()
        refresh.assert_not_called()

    def test_windows_desktop_mode_requires_windows_and_desktop_launcher(self):
        with patch.dict(
            app_module.os.environ,
            {"POTATOFLOW_DESKTOP_MODE": "1"},
            clear=False,
        ), patch.object(app_module.sys, "platform", "win32"):
            self.assertTrue(app_module._is_windows_desktop_mode())

        with patch.dict(
            app_module.os.environ,
            {"POTATOFLOW_DESKTOP_MODE": "1"},
            clear=False,
        ), patch.object(app_module.sys, "platform", "linux"):
            self.assertFalse(app_module._is_windows_desktop_mode())


if __name__ == "__main__":
    unittest.main()
