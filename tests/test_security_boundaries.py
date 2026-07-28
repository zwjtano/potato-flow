import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "y2a-auto"
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
        self.assertEqual(set(response.get_json()), {"status", "version"})
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


if __name__ == "__main__":
    unittest.main()
