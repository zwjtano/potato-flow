import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_TEMPLATE = ROOT / "y2a-auto" / "templates" / "settings.html"
STYLE_FILE = ROOT / "y2a-auto" / "static" / "css" / "style.css"


class SettingsWorkbenchTests(unittest.TestCase):
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

    @classmethod
    def setUpClass(cls):
        cls.template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
        cls.styles = STYLE_FILE.read_text(encoding="utf-8")

    def test_workbench_exposes_essential_and_all_modes(self):
        self.assertIn('id="settingsWorkbenchTitle"', self.template)
        self.assertIn('data-settings-mode="essential"', self.template)
        self.assertIn('data-settings-mode="all"', self.template)
        self.assertIn('data-settings-level="advanced"', self.template)
        self.assertIn("potatoflow-settings-mode", self.template)

    def test_mobile_section_selector_covers_every_settings_pane(self):
        self.assertIn('id="settings-mobile-section"', self.template)
        for pane_id in (
            "vtab-general",
            "vtab-accounts",
            "vtab-ai-models",
            "vtab-subtitle-voice",
            "vtab-notifications",
            "vtab-ops",
        ):
            self.assertIn(f'value="#{pane_id}"', self.template)

    def test_search_temporarily_includes_advanced_settings(self):
        self.assertIn("is-settings-searching", self.template)
        self.assertIn(
            '.settings-container[data-settings-mode="essential"]:not(.is-settings-searching)',
            self.styles,
        )

    def test_navigation_displays_field_counts(self):
        self.assertEqual(self.template.count('class="settings-nav-fields"'), 6)
        self.assertIn("fieldNames.size + ' 项'", self.template)
