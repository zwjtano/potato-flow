import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE_FILE = ROOT / "y2a-auto" / "static" / "css" / "style.css"
SETTINGS_TEMPLATE = ROOT / "y2a-auto" / "templates" / "settings.html"
APP_SOURCE = ROOT / "y2a-auto" / "app.py"


class SettingsSwitchTests(unittest.TestCase):
    def test_dark_switches_have_explicit_state_and_high_contrast_track(self):
        css = STYLE_FILE.read_text(encoding="utf-8")

        self.assertIn('content: "关闭"', css)
        self.assertIn('content: "开启"', css)
        self.assertIn(".form-check-input:not(:checked)", css)
        self.assertIn("border-color: #7890a5", css)
        self.assertIn(
            ".settings-toggle-label:has(input[type='checkbox']:checked)",
            css,
        )
        self.assertIn('content: "固定开启"', css)

    def test_configuration_workbench_is_not_rendered(self):
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

        self.assertNotIn('id="settingsWorkbenchTitle"', template)
        self.assertNotIn('data-settings-mode="essential"', template)
        self.assertNotIn('id="settings-mobile-section"', template)
        self.assertNotIn("potatoflow-settings-mode", template)

    def test_douyu_recording_pipeline_switches_are_visible(self):
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn('name="DOUYU_STATS_ENABLED"', template)
        self.assertIn('name="DOUYU_STATS_APPEND_DESCRIPTION"', template)
        self.assertIn('name="DOUYU_STATS_COVER_CONTEXT_ENABLED"', template)
        self.assertIn("用斗鱼主播视角识别英雄、最终装备与 KDA 供封面参考", template)
        app_source = APP_SOURCE.read_text(encoding="utf-8")
        self.assertIn("douyu_pipeline_settings_changed", app_source)
        self.assertIn("live_recorder_manager.refresh_credentials()", app_source)

    def test_windows_desktop_controls_are_hidden_from_server_deployments(self):
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
        app_source = APP_SOURCE.read_text(encoding="utf-8")

        guard = template.index("{% if windows_desktop_mode %}")
        allow_lan = template.index('name="DESKTOP_ALLOW_LAN"')
        startup = template.index('name="DESKTOP_START_WITH_WINDOWS"')
        end_guard = template.index("{% endif %}", startup)
        self.assertLess(guard, allow_lan)
        self.assertLess(allow_lan, startup)
        self.assertLess(startup, end_guard)
        self.assertIn("sys.platform == 'win32'", app_source)
        self.assertIn("form_data.pop('DESKTOP_ALLOW_LAN', None)", app_source)

    def test_legacy_general_video_transcode_card_is_removed(self):
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

        self.assertNotIn('<h4>视频转码</h4>', template)
        self.assertNotIn('name="VIDEO_ENCODER"', template)
        self.assertNotIn('name="VIDEO_CUSTOM_PARAMS_ENABLED"', template)
        self.assertIn('id="danmaku-encoder"', template)


if __name__ == "__main__":
    unittest.main()
