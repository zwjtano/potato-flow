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
        self.assertIn("审核后重试", template)
        self.assertIn("/live-recording/jobs/${button.dataset.jobId}/retry", template)

    def test_overview_review_count_includes_failed_recordings(self):
        app_source = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")

        self.assertIn(
            "awaiting_review += sum(job.get('status') == 'failed' for job in recording_jobs)",
            app_source,
        )


if __name__ == "__main__":
    unittest.main()
