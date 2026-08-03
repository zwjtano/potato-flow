import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE_FILE = ROOT / "potatoflow-app" / "static" / "css" / "style.css"
REFINEMENT_STYLE_FILE = ROOT / "potatoflow-app" / "static" / "css" / "ui-refinement.css"
SETTINGS_TEMPLATE = ROOT / "potatoflow-app" / "templates" / "settings.html"
BASE_TEMPLATE = ROOT / "potatoflow-app" / "templates" / "base.html"
APP_SOURCE = ROOT / "potatoflow-app" / "app.py"


class SettingsSwitchTests(unittest.TestCase):
    def test_sidebar_and_topbar_remove_retired_entries(self):
        template = BASE_TEMPLATE.read_text(encoding="utf-8")
        refinement_css = REFINEMENT_STYLE_FILE.read_text(encoding="utf-8")

        self.assertNotIn('aria-label="文件库"', template)
        self.assertNotIn('aria-label="诊断与日志"', template)
        self.assertNotIn('id="topbarRuntime"', template)
        self.assertNotIn('id="topbarRecording"', template)
        self.assertNotIn('id="topbarDisk"', template)
        self.assertIn('.app-theme-toggle { display: inline-grid !important; }', refinement_css)
        self.assertNotIn('.app-theme-toggle, .app-mobile-header', refinement_css)

    def test_settings_boolean_values_render_as_state_buttons(self):
        css = STYLE_FILE.read_text(encoding="utf-8")
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn(".settings-boolean-choice", css)
        self.assertIn(".settings-boolean-choice.is-active", css)
        self.assertIn("settings-boolean-actions", template)
        self.assertIn("enableButton.setAttribute('aria-pressed'", template)
        self.assertIn("disableButton.setAttribute('aria-pressed'", template)
        self.assertIn("input.checked = enabled", template)

    def test_configuration_workbench_is_not_rendered(self):
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

        self.assertNotIn('id="settingsWorkbenchTitle"', template)
        self.assertNotIn('data-settings-mode="essential"', template)
        self.assertNotIn('id="settings-mobile-section"', template)
        self.assertNotIn("potatoflow-settings-mode", template)

    def test_settings_categories_follow_user_workflows(self):
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

        for category in (
            "录播与投稿",
            "平台账号",
            "网络与下载",
            "AI 内容",
            "字幕与语音",
            "通知与远程",
            "存储与安全",
        ):
            self.assertIn("name: '" + category + "'", template)
        self.assertIn("settings-appearance-strip", template)

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
