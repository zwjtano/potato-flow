import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "potatoflow-app" / "app.py").read_text(encoding="utf-8")
TEMPLATE_SOURCE = (ROOT / "potatoflow-app" / "templates" / "index.html").read_text(
    encoding="utf-8"
)
STYLE_SOURCE = (ROOT / "potatoflow-app" / "static" / "css" / "style.css").read_text(
    encoding="utf-8"
)
BASE_TEMPLATE_SOURCE = (
    ROOT / "potatoflow-app" / "templates" / "base.html"
).read_text(encoding="utf-8")
SETTINGS_TEMPLATE_SOURCE = (
    ROOT / "potatoflow-app" / "templates" / "settings.html"
).read_text(encoding="utf-8")


class OverviewDashboardTests(unittest.TestCase):
    def test_recent_activity_data_keeps_stable_display_ids(self):
        self.assertIn("SELECT id, display_id, video_title_translated", APP_SOURCE)
        self.assertIn("'display_id': r[1] or r[0]", APP_SOURCE)
        self.assertIn("'display_id': job.get('display_id') or job['id']", APP_SOURCE)
        self.assertNotIn("{{ t.id[:6] }}", TEMPLATE_SOURCE)

    def test_overview_is_centered_on_the_two_core_workflows(self):
        self.assertIn("今天想做什么？", TEMPLATE_SOURCE)
        self.assertIn("YouTube 监控转载", TEMPLATE_SOURCE)
        self.assertIn("直播录播", TEMPLATE_SOURCE)
        self.assertIn("进入监控中心", TEMPLATE_SOURCE)
        self.assertIn("进入录播中心", TEMPLATE_SOURCE)
        self.assertIn("home-choice-grid", TEMPLATE_SOURCE)
        self.assertNotIn("quickStartModal", TEMPLATE_SOURCE)

    def test_workflow_cards_receive_source_specific_summaries(self):
        self.assertIn("monitor_configs = youtube_monitor.get_monitor_configs()", APP_SOURCE)
        self.assertIn("'monitor_enabled':", APP_SOURCE)
        self.assertIn("recording_rooms = live_recorder_manager.rooms_with_status()", APP_SOURCE)
        self.assertIn("'recording_now':", APP_SOURCE)
        self.assertIn("youtube_summary=youtube_summary", APP_SOURCE)
        self.assertIn("recording_summary=recording_summary", APP_SOURCE)
        self.assertIn("youtube_summary.monitor_enabled", TEMPLATE_SOURCE)
        self.assertIn("recording_summary.recording_now", TEMPLATE_SOURCE)

    def test_home_stays_focused_on_core_workflows(self):
        self.assertNotIn("最近任务", TEMPLATE_SOURCE)
        self.assertNotIn("最近动态", TEMPLATE_SOURCE)
        self.assertNotIn("任务中心", TEMPLATE_SOURCE)
        self.assertNotIn("home-overall-state", TEMPLATE_SOURCE)
        self.assertNotIn("个任务需要处理", TEMPLATE_SOURCE)
        self.assertNotIn("home-task-hub", TEMPLATE_SOURCE)
        self.assertNotIn("home-activity-card", TEMPLATE_SOURCE)
        self.assertNotIn("live_recording_job_delete", TEMPLATE_SOURCE)
        self.assertNotIn("delete_task_route", TEMPLATE_SOURCE)

    def test_home_can_open_the_add_room_flow(self):
        live_template = (
            ROOT / "potatoflow-app" / "templates" / "live_recording.html"
        ).read_text(encoding="utf-8")
        self.assertIn("url_for('live_recording', add_room=1)", TEMPLATE_SOURCE)
        self.assertIn("get('add_room') === '1'", live_template)

    def test_desktop_sidebar_uses_professional_dark_console_tokens(self):
        self.assertIn("--studio-sidebar: #111a24", STYLE_SOURCE)
        self.assertIn("--studio-primary: #00aeec", STYLE_SOURCE)
        self.assertIn("--studio-canvas: #0e141b", STYLE_SOURCE)
        self.assertIn("document.documentElement.dataset.theme = 'dark'", BASE_TEMPLATE_SOURCE)
        self.assertIn("录播工作流", BASE_TEMPLATE_SOURCE)
        self.assertIn("任务中心", BASE_TEMPLATE_SOURCE)
        self.assertIn("<span>总览</span>", BASE_TEMPLATE_SOURCE)
        self.assertIn("shell_section = '首页'", BASE_TEMPLATE_SOURCE)
        self.assertLess(
            BASE_TEMPLATE_SOURCE.index("直播间"),
            BASE_TEMPLATE_SOURCE.index("YouTube 监控"),
        )

    def test_theme_can_toggle_or_follow_the_system(self):
        self.assertIn("potatoflow-theme", BASE_TEMPLATE_SOURCE)
        self.assertIn("prefers-color-scheme: dark", BASE_TEMPLATE_SOURCE)
        self.assertIn("data-theme-toggle", BASE_TEMPLATE_SOURCE)
        self.assertIn("window.PotatoTheme", BASE_TEMPLATE_SOURCE)
        self.assertIn('html[data-theme="dark"]', STYLE_SOURCE)
        self.assertIn("--studio-canvas: #0e141b", STYLE_SOURCE)
        self.assertIn('data-theme-choice="light"', SETTINGS_TEMPLATE_SOURCE)
        self.assertIn('data-theme-choice="dark"', SETTINGS_TEMPLATE_SOURCE)
        self.assertIn('data-theme-choice="auto"', SETTINGS_TEMPLATE_SOURCE)
        self.assertIn('data-theme-choice="schedule"', SETTINGS_TEMPLATE_SOURCE)
        self.assertIn("hour >= 19 || hour < 7", BASE_TEMPLATE_SOURCE)
        self.assertIn("scheduleNextThemeBoundary", BASE_TEMPLATE_SOURCE)

    def test_dark_theme_covers_operational_content_and_modals(self):
        for selector in (
            'html[data-theme="dark"] .studio-empty-state',
            'html[data-theme="dark"] .task-section-empty',
            'html[data-theme="dark"] .modern-table table tbody tr',
            'html[data-theme="dark"] .add-room-section',
            'html[data-theme="dark"] .recording-files-modal .modal-body',
            'html[data-theme="dark"] .file-table-wrap',
            'html[data-theme="dark"] .page-header-stat',
            'html[data-theme="dark"] .settings-container .form-control:not(.form-control-sm)',
            'html[data-theme="dark"] .settings-actions-bar',
            'html[data-theme="dark"] .progress',
            'html[data-theme="dark"] .room-recording-settings-form',
            'html[data-theme="dark"] .room-prompt-field',
            'html[data-theme="dark"] .default-prompts-preview',
            'html[data-theme="dark"] .room-recording-settings-note',
            'html[data-theme="dark"] .room-setting-number',
            'html[data-theme="dark"] .engine-state.is-stopped small',
            'html[data-theme="dark"] .engine-icon',
            'html[data-theme="dark"] #mobileNav',
        ):
            self.assertIn(selector, STYLE_SOURCE)

    def test_home_metrics_use_semantic_number_colors(self):
        for metric_class in ("metric-total", "metric-active", "metric-completed"):
            self.assertGreaterEqual(TEMPLATE_SOURCE.count(metric_class), 2)
            self.assertIn(
                f'html[data-theme="dark"] .home-product-metrics .{metric_class} strong',
                STYLE_SOURCE,
            )

    def test_sidebar_icons_use_one_consistent_line_icon_set(self):
        for icon in (
            "bi-grid-1x2",
            "bi-record-circle",
            "bi-youtube",
            "bi-cloud-arrow-up",
            "bi-shield-check",
            "bi-sliders2",
        ):
            self.assertIn(icon, BASE_TEMPLATE_SOURCE)
        self.assertIn(
            'html[data-theme="dark"] .app-nav-link.active i',
            STYLE_SOURCE,
        )


if __name__ == "__main__":
    unittest.main()
