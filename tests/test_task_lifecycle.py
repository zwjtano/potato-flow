import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.task_lifecycle import (  # noqa: E402
    can_automatically_cleanup_youtube_download,
    recording_task_capabilities,
    youtube_task_capabilities,
)
from modules import task_manager  # noqa: E402
from tests.test_security_boundaries import app_module  # noqa: E402


class TaskLifecyclePolicyTests(unittest.TestCase):
    def test_delete_buttons_keep_explicit_task_source_after_event_rebind(self):
        tasks_source = (APP_ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")
        row_source = (APP_ROOT / "templates" / "partials" / "task_row.html").read_text(encoding="utf-8")
        card_source = (APP_ROOT / "templates" / "partials" / "task_card.html").read_text(encoding="utf-8")

        self.assertIn("this.dataset.taskSource || 'standard'", tasks_source)
        self.assertNotIn("this.closest('.recording-task-row, .recording-task-card')", tasks_source)
        self.assertIn('data-task-source="recording"', tasks_source)
        self.assertIn('data-task-source="standard"', row_source)
        self.assertIn('data-task-source="standard"', card_source)

    def test_youtube_capabilities_are_consistent(self):
        self.assertTrue(youtube_task_capabilities("uploading")["pausable"])
        self.assertTrue(youtube_task_capabilities("uploading")["active"])
        self.assertFalse(youtube_task_capabilities("uploading")["retryable"])

        paused = youtube_task_capabilities("paused")
        self.assertTrue(paused["paused"])
        self.assertTrue(paused["retryable"])
        self.assertFalse(paused["active"])

        completed = youtube_task_capabilities("completed")
        self.assertTrue(completed["terminal"])
        self.assertFalse(completed["pausable"])

    def test_recording_capabilities_match_worker_actions(self):
        processing = recording_task_capabilities("processing")
        self.assertTrue(processing["active"])
        self.assertTrue(processing["pausable"])
        self.assertFalse(processing["retryable"])

        paused = recording_task_capabilities("paused")
        self.assertFalse(paused["terminal"])
        self.assertTrue(paused["retryable"])
        self.assertFalse(paused["pausable"])

    def test_automatic_cleanup_requires_completed_upload(self):
        self.assertFalse(can_automatically_cleanup_youtube_download({
            "status": "uploading",
            "bilibili_upload_response": '{"bvid":"BV1"}',
        }))
        self.assertFalse(can_automatically_cleanup_youtube_download({
            "status": "failed",
            "bilibili_upload_response": None,
        }))
        self.assertFalse(can_automatically_cleanup_youtube_download({
            "status": "completed",
            "bilibili_upload_response": None,
        }))
        self.assertTrue(can_automatically_cleanup_youtube_download({
            "status": "completed",
            "bilibili_upload_response": '{"bvid":"BV1"}',
        }))
        self.assertTrue(can_automatically_cleanup_youtube_download(None))

    def test_active_task_delete_never_removes_files_before_worker_stops(self):
        task = {"id": "task-id", "status": "uploading"}
        with (
            patch.object(task_manager, "get_task", return_value=task),
            patch.object(task_manager, "request_task_cancel") as request_cancel,
            patch.object(task_manager, "_wait_for_task_inactive", return_value=False),
            patch.object(task_manager, "delete_task_files") as delete_files,
            patch.object(task_manager, "get_db_connection") as get_connection,
        ):
            result = task_manager.delete_task("task-id", delete_files=True)

        self.assertFalse(result)
        request_cancel.assert_called_once_with("task-id")
        delete_files.assert_not_called()
        get_connection.assert_not_called()

    def test_pause_requests_stop_and_preserves_files(self):
        task = {"id": "task-id", "status": "uploading"}
        with (
            patch.object(task_manager, "get_task", return_value=task),
            patch.object(task_manager, "request_task_cancel") as request_cancel,
            patch.object(task_manager, "update_task", return_value=True) as update_task,
            patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
            patch.object(task_manager, "delete_task_files") as delete_files,
        ):
            result = task_manager.pause_task("task-id")

        self.assertTrue(result)
        request_cancel.assert_called_once_with("task-id")
        self.assertEqual(update_task.call_args.kwargs["status"], "paused")
        delete_files.assert_not_called()

    def test_interrupted_recovery_skips_task_with_live_worker_lease(self):
        row = {
            "id": "task-id",
            "status": "uploading",
            "upload_target": "bilibili",
            "bilibili_upload_response": None,
        }
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [row]
        with (
            patch.object(task_manager, "get_db_connection", return_value=connection),
            patch.object(task_manager._TASK_LEASE_STORE, "is_live", return_value=True),
        ):
            recovered = task_manager.recover_interrupted_tasks_to_pending()

        self.assertEqual(recovered, 0)
        update_calls = [
            call for call in connection.execute.call_args_list
            if str(call.args[0]).lstrip().startswith("UPDATE tasks")
        ]
        self.assertEqual(update_calls, [])

    def test_interrupted_recovery_requeues_task_after_lease_expires(self):
        row = {
            "id": "task-id",
            "status": "uploading",
            "upload_target": "bilibili",
            "bilibili_upload_response": None,
        }
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [row]
        with (
            patch.object(task_manager, "get_db_connection", return_value=connection),
            patch.object(task_manager._TASK_LEASE_STORE, "is_live", return_value=False),
        ):
            recovered = task_manager.recover_interrupted_tasks_to_pending()

        self.assertEqual(recovered, 1)
        self.assertTrue(any(
            str(call.args[0]).lstrip().startswith("UPDATE tasks")
            for call in connection.execute.call_args_list
        ))


class DownloadCleanupLifecycleTests(unittest.TestCase):
    def test_age_cleanup_preserves_active_and_failed_task_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            downloads = Path(temp_dir)
            active_id = "11111111-1111-1111-1111-111111111111"
            failed_id = "22222222-2222-2222-2222-222222222222"
            complete_id = "33333333-3333-3333-3333-333333333333"
            orphan_id = "44444444-4444-4444-4444-444444444444"
            for task_id in (active_id, failed_id, complete_id, orphan_id):
                task_dir = downloads / task_id
                task_dir.mkdir()
                (task_dir / "video.mp4").write_bytes(b"video")
                old = time.time() - 7200
                task_dir.touch()
                import os
                os.utime(task_dir, (old, old))

            tasks = [
                {"id": active_id, "status": "uploading", "bilibili_upload_response": None},
                {"id": failed_id, "status": "failed", "bilibili_upload_response": None},
                {
                    "id": complete_id,
                    "status": "completed",
                    "bilibili_upload_response": '{"bvid":"BV1"}',
                },
            ]
            with (
                patch.object(app_module, "get_app_subdir", return_value=str(downloads)),
                patch.object(app_module, "get_all_tasks", return_value=tasks),
            ):
                result = app_module.cleanup_downloads(1)

            self.assertTrue(result["success"])
            self.assertEqual(result["skipped_protected"], 2)
            self.assertTrue((downloads / active_id).exists())
            self.assertTrue((downloads / failed_id).exists())
            self.assertFalse((downloads / complete_id).exists())
            self.assertFalse((downloads / orphan_id).exists())


class TaskLifecycleTemplateTests(unittest.TestCase):
    @staticmethod
    def _task(status):
        return {
            "id": "12345678-1234-5678-1234-567812345678",
            "display_id": "YT-VIDEO-0728-001",
            "status": status,
            "video_title_original": "测试任务",
            "video_title_translated": "",
            "upload_target": "bilibili",
            "upload_progress": None,
            "subtitle_qc_score": None,
            "subtitle_qc_reason": None,
            "subtitle_qc_failed": 0,
            "asr_warning_message": None,
            "error_message": None,
        }

    def test_paused_task_renders_continue_action(self):
        with app_module.app.test_request_context("/tasks"):
            rendered = app_module.render_template(
                "partials/task_card.html",
                task=self._task("paused"),
                config={},
            )
        self.assertIn("> 继续</button>", rendered)
        self.assertNotIn("> 暂停</button>", rendered)

    def test_active_task_renders_pause_action(self):
        with app_module.app.test_request_context("/tasks"):
            rendered = app_module.render_template(
                "partials/task_row.html",
                task=self._task("uploading"),
                config={},
            )
        self.assertIn("> 暂停", rendered)
        self.assertIn("/pause", rendered)


if __name__ == "__main__":
    unittest.main()
