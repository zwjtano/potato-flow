import unittest
from pathlib import Path


class SettingsFirstPaintTests(unittest.TestCase):
    def test_legacy_layout_is_inert_until_redesign_mounts(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "settings.html"
        ).read_text(encoding="utf-8")

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


if __name__ == "__main__":
    unittest.main()
