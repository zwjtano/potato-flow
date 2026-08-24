import unittest
from pathlib import Path


class SettingsFirstPaintTests(unittest.TestCase):
    @staticmethod
    def _template():
        return (
            Path(__file__).resolve().parents[1] / "templates" / "settings.html"
        ).read_text(encoding="utf-8")

    def test_legacy_layout_is_inert_until_redesign_mounts(self):
        template = self._template()

        mount_index = template.index('id="settings-layout-mount"')
        source_index = template.index('id="settings-layout-source"')
        legacy_layout_index = template.index('<div class="settings-layout">')
        template_end_index = template.index("</template>", legacy_layout_index)
        initializer_index = template.index(
            "(function initializeSettingsLayoutBeforeFirstPaint()"
        )
        clone_index = template.index(
            "settingsLayoutSource.content.cloneNode(true)", initializer_index
        )
        layout_query_index = template.index(
            "document.querySelector('.settings-layout')", initializer_index
        )

        self.assertLess(mount_index, source_index)
        self.assertLess(source_index, legacy_layout_index)
        self.assertLess(legacy_layout_index, template_end_index)
        self.assertLess(template_end_index, initializer_index)
        self.assertLess(clone_index, layout_query_index)
        self.assertNotIn("settings-layout-pending", template)

    def test_free_asr_credentials_stay_in_subtitle_category(self):
        template = self._template()
        subtitle_category = template[
            template.index("id: 'vtab-subtitles'"):
            template.index("id: 'vtab-notify-control'")
        ]

        self.assertIn("'免费 ASR Provider'", subtitle_category)
        self.assertIn('id="free-asr-provider-count"', template)
        self.assertIn('name="GROQ_API_KEY"', template)
        self.assertIn('name="CLOUDFLARE_ACCOUNT_ID"', template)
        self.assertIn('name="CLOUDFLARE_API_TOKEN"', template)
        self.assertIn("免费额度不等于免密钥", template)


if __name__ == "__main__":
    unittest.main()
