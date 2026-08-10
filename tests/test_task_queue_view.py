import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.task_queue_view import (  # noqa: E402
    build_queue_summary,
    filter_recording_jobs,
    filter_queue_items,
    normalize_queue_filter,
    normalize_recording_time_filter,
    normalize_recording_type_filter,
    normalize_source_filter,
    paginate_items,
    recording_queue_bucket,
    recording_room_options,
    youtube_queue_bucket,
)
from tests.test_security_boundaries import app_module  # noqa: E402


class TaskQueueViewTests(unittest.TestCase):
    def test_sources_and_filters_are_fail_closed(self):
        self.assertEqual(normalize_source_filter("recording"), "recording")
        self.assertEqual(normalize_source_filter("unknown"), "all")
        self.assertEqual(normalize_queue_filter("failed"), "failed")
        self.assertEqual(normalize_queue_filter("<script>"), "all")
        self.assertEqual(normalize_recording_type_filter("record_only"), "record_only")
        self.assertEqual(normalize_recording_type_filter("invalid"), "all")
        self.assertEqual(normalize_recording_time_filter("7d"), "7d")
        self.assertEqual(normalize_recording_time_filter("forever"), "all")

    def test_recording_filters_match_search_room_type_and_time(self):
        jobs = [
            {
                "id": "full-fingerprint-one",
                "display_id": "DYU-YYFYYF-0801-003",
                "title": "蓝猫残局",
                "video_name": "yyf.flv",
                "bvid": "BV1new",
                "room_id": "room-yyf",
                "room_name": "YYF",
                "record_only": False,
                "created_at": "2026-08-01T12:00:00+00:00",
            },
            {
                "id": "full-fingerprint-two",
                "display_id": "DYU-GXG-0730-001",
                "title": "巫医对局",
                "video_name": "gxg.flv",
                "room_id": "room-gxg",
                "room_name": "果小果",
                "record_only": True,
                "created_at": "2026-07-01T12:00:00+00:00",
            },
        ]

        filtered = filter_recording_jobs(
            jobs,
            keyword="0801-003",
            room="room-yyf",
            task_type="upload",
            time_range="3d",
            now=datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
        )

        self.assertEqual([item["id"] for item in filtered], ["full-fingerprint-one"])
        self.assertEqual(
            filter_recording_jobs(jobs, keyword="full-fingerprint-one"),
            [],
        )
        self.assertEqual(
            recording_room_options(jobs),
            [
                {"value": "room-yyf", "name": "YYF"},
                {"value": "room-gxg", "name": "果小果"},
            ],
        )

    def test_youtube_buckets_are_mutually_exclusive(self):
        self.assertEqual(youtube_queue_bucket({"status": "uploading"}), "active")
        self.assertEqual(youtube_queue_bucket({"status": "pending"}), "queued")
        self.assertEqual(
            youtube_queue_bucket({"status": "awaiting_manual_review"}),
            "review",
        )
        self.assertEqual(youtube_queue_bucket({"status": "failed"}), "failed")
        self.assertEqual(youtube_queue_bucket({"status": "paused"}), "paused")
        self.assertEqual(youtube_queue_bucket({"status": "completed"}), "completed")

    def test_recording_failures_distinguish_review_from_local_failure(self):
        self.assertEqual(
            recording_queue_bucket({"status": "failed", "record_only": False}),
            "review",
        )
        self.assertEqual(
            recording_queue_bucket({"status": "failed", "record_only": True}),
            "failed",
        )

    def test_recording_ai_queue_is_reported_as_queued(self):
        self.assertEqual(
            recording_queue_bucket({"status": "processing", "ai_queued": True}),
            "queued",
        )
        self.assertEqual(
            recording_queue_bucket({"status": "processing", "ai_queued": False}),
            "active",
        )

    def test_summary_combines_both_sources_without_double_counting(self):
        summary = build_queue_summary(
            [
                {"status": "pending"},
                {"status": "uploading"},
                {"status": "failed"},
            ],
            [
                {"status": "completed"},
                {"status": "failed", "record_only": False},
            ],
        )
        self.assertEqual(summary["all"], 5)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["review"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["completed"], 1)

    def test_filter_and_pagination_preserve_complete_filtered_result(self):
        tasks = [
            {"id": "1", "status": "failed"},
            {"id": "2", "status": "pending"},
            {"id": "3", "status": "failed"},
        ]
        failed = filter_queue_items(tasks, "failed", youtube_queue_bucket)
        page = paginate_items(failed, page=2, per_page=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["tasks"][0]["id"], "3")


class TaskQueueRouteTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True, SECRET_KEY="task-queue-test")
        self.client = app_module.app.test_client()

    def test_tasks_route_applies_source_and_status_to_both_sections(self):
        youtube = [
            {"id": "youtube-active", "status": "uploading"},
            {"id": "youtube-failed", "status": "failed"},
        ]
        recording = [
            {
                "id": "recording-active",
                "status": "processing",
                "completed_stages": 1,
                "total_stages": 6,
            },
            {
                "id": "recording-complete",
                "status": "completed",
                "completed_stages": 6,
                "total_stages": 6,
            },
        ]
        with (
            patch.object(app_module, "load_config", return_value={}),
            patch.object(app_module, "get_all_tasks", return_value=youtube),
            patch.object(
                app_module.live_recorder_manager,
                "pipeline_jobs",
                return_value=recording,
            ),
        ):
            response = self.client.get("/tasks?status=active&source=recording")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("统一队列", body)
        self.assertIn("recording-active", body)
        self.assertNotIn("recording-complete", body)
        self.assertNotIn("YouTube / 手动上传任务</h2>", body)

    def test_tasks_route_can_filter_by_task_number_without_displaying_it(self):
        recording = [
            {
                "id": "matching-fingerprint",
                "display_id": "DYU-YYFYYF-0801-003",
                "title": "蓝猫残局",
                "video_name": "yyf.flv",
                "room_id": "room-yyf",
                "room_name": "YYF",
                "record_only": False,
                "status": "completed",
                "created_at": "2026-08-01T12:00:00+00:00",
                "completed_stages": 6,
                "total_stages": 6,
            },
            {
                "id": "hidden-fingerprint",
                "display_id": "DYU-GXG-0801-001",
                "title": "巫医对局",
                "video_name": "gxg.flv",
                "room_id": "room-gxg",
                "room_name": "果小果",
                "record_only": True,
                "status": "completed",
                "created_at": "2026-08-01T12:00:00+00:00",
                "completed_stages": 6,
                "total_stages": 6,
            },
        ]
        with (
            patch.object(app_module, "load_config", return_value={}),
            patch.object(app_module, "get_all_tasks", return_value=[]),
            patch.object(
                app_module.live_recorder_manager,
                "pipeline_jobs",
                return_value=recording,
            ),
        ):
            response = self.client.get(
                "/tasks?source=recording&recording_q=0801-003"
                "&recording_room=room-yyf&recording_type=upload"
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("蓝猫残局", body)
        self.assertNotIn("任务编号", body)
        self.assertNotIn("DYU-YYFYYF-0801-003", body)
        self.assertNotIn("DYU-GXG-0801-001", body)
