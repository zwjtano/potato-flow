import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE_FILE = ROOT / "y2a-auto" / "static" / "css" / "style.css"
SETTINGS_TEMPLATE = ROOT / "y2a-auto" / "templates" / "settings.html"


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


if __name__ == "__main__":
    unittest.main()
