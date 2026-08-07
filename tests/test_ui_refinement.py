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
YOUTUBE_MONITOR_SOURCE = (
    APP_ROOT / "templates" / "youtube_monitor.html"
).read_text(encoding="utf-8")
MANUAL_REVIEW_SOURCE = (
    APP_ROOT / "templates" / "manual_review.html"
).read_text(encoding="utf-8")
BILIBILI_ARCHIVES_SOURCE = (
    APP_ROOT / "templates" / "bilibili_archives.html"
).read_text(encoding="utf-8")
LIVE_RECORDING_SOURCE = (
    APP_ROOT / "templates" / "live_recording.html"
).read_text(encoding="utf-8")


class UiRefinementTests(unittest.TestCase):
    def test_recording_files_stays_inside_live_room_without_duplicate_nav_entry(self):
        self.assertNotIn('aria-label="录播文件"', BASE_SOURCE)
        self.assertNotIn('#recording-files', BASE_SOURCE)
        self.assertIn('data-bs-target="#recordingFilesModal"', LIVE_RECORDING_SOURCE)
        self.assertIn('id="recordingFilesModal"', LIVE_RECORDING_SOURCE)

    def test_refinement_layer_loads_after_page_specific_styles(self):
        self.assertIn("css/ui-refinement.css", BASE_SOURCE)
        self.assertIn("?v={{ app_version }}-6", BASE_SOURCE)
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

    def test_collapsed_navigation_rules_do_not_hide_mobile_nav_labels(self):
        self.assertIn(
            "body.sidebar-collapsed .app-sidebar .app-nav-link span",
            REFINEMENT_SOURCE,
        )
        self.assertNotIn(
            "body.sidebar-collapsed .app-nav-link span",
            REFINEMENT_SOURCE,
        )

    def test_mobile_diagnostics_can_reflow_without_horizontal_overflow(self):
        self.assertIn("grid-template-columns: auto minmax(0, 1fr);", REFINEMENT_SOURCE)
        self.assertIn(".component-row code,", REFINEMENT_SOURCE)
        self.assertIn("grid-column: 2;", REFINEMENT_SOURCE)

    def test_mobile_controls_and_onboarding_are_touch_safe_and_fluid(self):
        self.assertIn(".app-mobile-header > .btn-dark", REFINEMENT_SOURCE)
        self.assertIn("min-height: 44px;", REFINEMENT_SOURCE)
        self.assertIn("body {\n        min-width: 0;", ONBOARDING_STYLE_SOURCE)
        self.assertIn("@media (max-width: 1179.98px)", ONBOARDING_STYLE_SOURCE)
        self.assertNotIn("@media (max-width: 767.98px)", ONBOARDING_STYLE_SOURCE)
        self.assertIn("grid-template-columns: repeat(5, minmax(0, 1fr));", ONBOARDING_STYLE_SOURCE)
        self.assertIn("?v={{ app_version }}-2", ONBOARDING_SOURCE)

    def test_mobile_youtube_cards_preserve_touch_targets_and_dark_theme(self):
        self.assertIn("width: 44px;\n        height: 44px;", YOUTUBE_MONITOR_SOURCE)
        self.assertIn(
            'html[data-theme="dark"] .youtube-monitor-table tbody tr',
            YOUTUBE_MONITOR_SOURCE,
        )

    def test_mobile_review_actions_stack_without_overflow(self):
        self.assertIn(
            ".review-container .card-footer > .d-flex",
            MANUAL_REVIEW_SOURCE,
        )
        self.assertIn("flex-direction: column;", MANUAL_REVIEW_SOURCE)
        self.assertIn("min-height: 44px;", MANUAL_REVIEW_SOURCE)

    def test_mobile_archive_danger_action_stacks_safely(self):
        self.assertIn("archive-danger-action", BILIBILI_ARCHIVES_SOURCE)
        self.assertIn(
            ".archive-danger-action { align-items: stretch !important; flex-direction: column; }",
            BILIBILI_ARCHIVES_SOURCE,
        )

    def test_stylesheet_braces_are_balanced(self):
        self.assertEqual(
            REFINEMENT_SOURCE.count("{"),
            REFINEMENT_SOURCE.count("}"),
        )


if __name__ == "__main__":
    unittest.main()
