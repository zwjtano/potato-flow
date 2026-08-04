import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
BASE_SOURCE = (APP_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
REFINEMENT_SOURCE = (
    APP_ROOT / "static" / "css" / "ui-refinement.css"
).read_text(encoding="utf-8")
ONBOARDING_SOURCE = (APP_ROOT / "templates" / "onboarding.html").read_text(encoding="utf-8")
ONBOARDING_STYLE_SOURCE = (
    APP_ROOT / "static" / "css" / "onboarding.css"
).read_text(encoding="utf-8")


class UiRefinementTests(unittest.TestCase):
    def test_refinement_layer_loads_after_page_specific_styles(self):
        self.assertIn("css/ui-refinement.css", BASE_SOURCE)
        self.assertIn("?v={{ app_version }}-5", BASE_SOURCE)
        self.assertLess(
            BASE_SOURCE.index("{% block extra_css %}"),
            BASE_SOURCE.index("css/ui-refinement.css"),
        )

    def test_refinement_defines_product_specific_visual_language(self):
        for token in (
            "--pf-font-display",
            "--pf-font-body",
            "--pf-font-data",
            "--pf-signal",
            "--pf-surface",
            "--pf-shadow-card",
        ):
            self.assertIn(token, REFINEMENT_SOURCE)
        for selector in (
            ".studio-page-header::before",
            ".settings-page-header::before",
            ".studio-empty-state.compact",
            ".settings-navigation-panel",
            ".home-product-card",
            ".recorder-workspace",
        ):
            self.assertIn(selector, REFINEMENT_SOURCE)

    def test_dark_mobile_and_reduced_motion_states_are_explicit(self):
        self.assertIn('html[data-theme="dark"]', REFINEMENT_SOURCE)
        self.assertIn("@media (max-width: 480px)", REFINEMENT_SOURCE)
        self.assertIn(
            "grid-template-columns: repeat(2, minmax(0, 1fr));",
            REFINEMENT_SOURCE,
        )
        self.assertIn("@media (prefers-reduced-motion: reduce)", REFINEMENT_SOURCE)

    def test_mobile_shell_drops_collapsed_sidebar_offset(self):
        self.assertIn("body.sidebar-collapsed .app-frame", REFINEMENT_SOURCE)
        self.assertIn("width: 100%;", REFINEMENT_SOURCE)
        self.assertIn("margin-left: 0;", REFINEMENT_SOURCE)

    def test_mobile_diagnostics_can_reflow_without_horizontal_overflow(self):
        self.assertIn("grid-template-columns: auto minmax(0, 1fr);", REFINEMENT_SOURCE)
        self.assertIn(".component-row code,", REFINEMENT_SOURCE)
        self.assertIn("grid-column: 2;", REFINEMENT_SOURCE)

    def test_mobile_controls_and_onboarding_are_touch_safe_and_fluid(self):
        self.assertIn(".app-mobile-header > .btn-dark", REFINEMENT_SOURCE)
        self.assertIn("min-height: 44px;", REFINEMENT_SOURCE)
        self.assertIn("body {\n        min-width: 0;", ONBOARDING_STYLE_SOURCE)
        self.assertIn("grid-template-columns: repeat(5, minmax(0, 1fr));", ONBOARDING_STYLE_SOURCE)
        self.assertIn("?v={{ app_version }}-1", ONBOARDING_SOURCE)

    def test_stylesheet_braces_are_balanced(self):
        self.assertEqual(
            REFINEMENT_SOURCE.count("{"),
            REFINEMENT_SOURCE.count("}"),
        )


if __name__ == "__main__":
    unittest.main()
