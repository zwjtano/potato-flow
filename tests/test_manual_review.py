import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ManualReviewTests(unittest.TestCase):
    def test_failed_standard_and_recording_tasks_enter_review_queue(self):
        app_source = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")

        self.assertIn("failed_tasks = get_tasks_by_status(TASK_STATES['FAILED'])", app_source)
        self.assertIn("if job.get('status') == 'failed'", app_source)
        self.assertIn("recording_jobs=recording_review_jobs", app_source)

    def test_review_page_exposes_recording_failure_details_and_retry(self):
        template = (
            ROOT / "y2a-auto" / "templates" / "manual_review.html"
        ).read_text(encoding="utf-8")

        self.assertIn("录播失败任务", template)
        self.assertIn("查看流水线与日志", template)
        self.assertIn("进入编辑审核", template)
        self.assertIn("live_recording_job_review", template)

    def test_recording_review_has_full_editor_and_persistent_override(self):
        app_source = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")
        manager_source = (
            ROOT / "y2a-auto" / "modules" / "live_recorder_manager.py"
        ).read_text(encoding="utf-8")
        bridge_source = (ROOT / "bridge.py").read_text(encoding="utf-8")
        template = (
            ROOT / "y2a-auto" / "templates" / "recording_review_edit.html"
        ).read_text(encoding="utf-8")

        self.assertIn("def live_recording_job_review(fingerprint)", app_source)
        self.assertIn("save_pipeline_review(", app_source)
        self.assertIn("保存并重新投稿", template)
        self.assertIn('name="cover_file"', template)
        self.assertIn('name="partition_id"', template)
        self.assertIn("recording_review_overrides", manager_source)
        self.assertIn("review_override = store.review_override(key)", bridge_source)
        self.assertIn('"manual_review_applied": True', bridge_source)

    def test_overview_review_count_includes_failed_recordings(self):
        app_source = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")

        self.assertIn(
            "awaiting_review += sum(job.get('status') == 'failed' for job in recording_jobs)",
            app_source,
        )


if __name__ == "__main__":
    unittest.main()
