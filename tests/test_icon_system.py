import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
ICON_FONT_SOURCE = (
    APP_ROOT / "static" / "lib" / "icons" / "bootstrap-icons.css"
).read_text(encoding="utf-8")
STYLE_SOURCE = (APP_ROOT / "static" / "css" / "style.css").read_text(
    encoding="utf-8"
)
BASE_SOURCE = (APP_ROOT / "templates" / "base.html").read_text(encoding="utf-8")


class IconSystemTests(unittest.TestCase):
    def test_every_referenced_bootstrap_icon_exists_in_bundled_font(self):
        sources = [STYLE_SOURCE]
        sources.extend(
            path.read_text(encoding="utf-8")
            for path in (APP_ROOT / "templates").rglob("*.html")
        )
        sources.extend(
            path.read_text(encoding="utf-8")
            for path in (APP_ROOT / "static" / "js").rglob("*.js")
            if ".min." not in path.name
        )
        referenced = set(re.findall(r"\bbi-[a-z0-9-]+\b", "\n".join(sources)))
        available = set(
            re.findall(r"\.(bi-[a-z0-9-]+)::before", ICON_FONT_SOURCE)
        )
        self.assertEqual(sorted(referenced - available), [])

    def test_core_navigation_uses_one_semantic_line_icon_set(self):
        for icon in (
            "bi-grid-1x2",
            "bi-record-circle",
            "bi-youtube",
            "bi-cloud-arrow-up",
            "bi-shield-check",
            "bi-sliders2",
        ):
            self.assertIn(icon, BASE_SOURCE)

    def test_icon_language_covers_navigation_workflows_and_dark_theme(self):
        for selector in (
            ".app-nav-link > i",
            ".home-product-icon::after",
            ".home-workflow-path > span i",
            ".task-source-icon.recording",
            ".task-source-icon.youtube",
            ".settings-nav-icon",
            ".generated-file-icon.video",
            'html[data-theme="dark"] .home-workflow-path > span i',
        ):
            self.assertIn(selector, STYLE_SOURCE)


if __name__ == "__main__":
    unittest.main()
