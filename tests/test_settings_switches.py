import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE_FILE = ROOT / "potatoflow-app" / "static" / "css" / "style.css"
REFINEMENT_STYLE_FILE = ROOT / "potatoflow-app" / "static" / "css" / "ui-refinement.css"
SETTINGS_REDESIGN_STYLE_FILE = ROOT / "potatoflow-app" / "static" / "css" / "settings-redesign.css"
SETTINGS_TEMPLATE = ROOT / "potatoflow-app" / "templates" / "settings.html"
BASE_TEMPLATE = ROOT / "potatoflow-app" / "templates" / "base.html"
APP_SOURCE = ROOT / "potatoflow-app" / "app.py"
LIVE_RECORDING_TEMPLATE = ROOT / "potatoflow-app" / "templates" / "live_recording.html"
CONFIG_MANAGER_SOURCE = ROOT / "potatoflow-app" / "modules" / "config_manager.py"


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

    def test_settings_layout_is_rebuilt_before_first_paint(self):
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("(function initializeSettingsLayoutBeforeFirstPaint() {", template)
        self.assertNotIn(
            "document.addEventListener('DOMContentLoaded', function () {\n"
            "    const settingsTabContent = document.getElementById('settings-tabContent');",
            template,
        )

    def test_live_room_exposes_per_room_bilibili_collection(self):
        template = LIVE_RECORDING_TEMPLATE.read_text(encoding="utf-8")

        self.assertGreaterEqual(template.count('name="bilibili_collection_id"'), 2)
        self.assertIn("自动加入 B站合集", template)
        self.assertIn('data-role="bilibili-collection-field"', template)

    def test_settings_exposes_global_recording_ai_prompts(self):
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
        app_source = APP_SOURCE.read_text(encoding="utf-8")
        config_source = CONFIG_MANAGER_SOURCE.read_text(encoding="utf-8")
        live_template = LIVE_RECORDING_TEMPLATE.read_text(encoding="utf-8")

        for config_key in (
            "RECORDING_AI_TITLE_PROMPT",
            "RECORDING_AI_DESCRIPTION_PROMPT",
            "RECORDING_AI_COVER_PROMPT",
        ):
            self.assertIn(f'name="{config_key}"', template)
            self.assertIn(f'"{config_key}": ""', config_source)
        self.assertIn("RECORDING_AI_PROMPT_MAX_LENGTH", app_source)
        self.assertIn("live_recorder_manager.sync_configs()", app_source)
        self.assertIn("直播间单独设置 → 这里的全局设置 → 系统内置", template)
        self.assertIn("查看三个系统内置提示词", template)
        self.assertIn("recording_prompt_inherited.title", live_template)
        self.assertIn("留空继承全局设置", live_template)
        self.assertNotIn('name="RECORDING_DOTA2_COVER_LAYOUT_MODE"', template)
        self.assertNotIn('value="fusion"', template)
        self.assertNotIn("经典分离", template)
        self.assertNotIn("英雄融合", template)
        self.assertNotIn('"RECORDING_DOTA2_COVER_LAYOUT_MODE": "classic"', config_source)

    def test_mobile_settings_navigation_uses_a_wrapping_grid(self):
        css = SETTINGS_REDESIGN_STYLE_FILE.read_text(encoding="utf-8")
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", css)
        self.assertIn("overflow: visible;", css)
        self.assertIn("?v={{ app_version }}-2", template)

    def test_runtime_settings_categories_use_the_available_width(self):
        css = SETTINGS_REDESIGN_STYLE_FILE.read_text(encoding="utf-8")
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("wrapper.className = 'settings-pane-item'", template)
        self.assertIn("wideCardTitles.has(title)", template)
        self.assertIn(
            ".settings-pane-item:last-child:nth-child(odd)",
            css,
        )
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", css)
        self.assertIn(
            '.settings-card[data-settings-title="自动化流程"] .settings-inline-options',
            css,
        )

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
