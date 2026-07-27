import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")
TEMPLATE_SOURCE = (ROOT / "y2a-auto" / "templates" / "index.html").read_text(
    encoding="utf-8"
)
STYLE_SOURCE = (ROOT / "y2a-auto" / "static" / "css" / "style.css").read_text(
    encoding="utf-8"
)
BASE_TEMPLATE_SOURCE = (
    ROOT / "y2a-auto" / "templates" / "base.html"
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
        self.assertNotIn("home-task-hub", TEMPLATE_SOURCE)
        self.assertNotIn("home-activity-card", TEMPLATE_SOURCE)
        self.assertNotIn("live_recording_job_delete", TEMPLATE_SOURCE)
        self.assertNotIn("delete_task_route", TEMPLATE_SOURCE)

    def test_home_can_open_the_add_room_flow(self):
        live_template = (
            ROOT / "y2a-auto" / "templates" / "live_recording.html"
        ).read_text(encoding="utf-8")
        self.assertIn("url_for('live_recording', add_room=1)", TEMPLATE_SOURCE)
        self.assertIn("get('add_room') === '1'", live_template)

    def test_desktop_sidebar_uses_light_theme_tokens(self):
        self.assertIn("--studio-sidebar: #eef4fa", STYLE_SOURCE)
        self.assertIn("background: #dfeefd", STYLE_SOURCE)
        self.assertIn("核心功能", BASE_TEMPLATE_SOURCE)
        self.assertIn("任务中心", BASE_TEMPLATE_SOURCE)
        self.assertIn("<span>首页</span>", BASE_TEMPLATE_SOURCE)
        self.assertIn("shell_section = '首页'", BASE_TEMPLATE_SOURCE)
        self.assertLess(
            BASE_TEMPLATE_SOURCE.index("直播录制"),
            BASE_TEMPLATE_SOURCE.index("YouTube 监控"),
        )
        self.assertNotIn("--studio-sidebar: #191c23", STYLE_SOURCE)


if __name__ == "__main__":
    unittest.main()
