import sys
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
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

    def test_task_record_is_preserved_when_file_staging_fails(self):
        task_id = "11111111-1111-1111-1111-111111111111"
        task = {"id": task_id, "status": "failed"}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_dir = root / task_id
            task_dir.mkdir()
            with (
                patch.object(task_manager, "DOWNLOADS_DIR", str(root)),
                patch.object(task_manager, "get_task", return_value=task),
                patch.object(task_manager, "request_task_cancel"),
                patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
                patch.object(task_manager.os, "replace", side_effect=OSError("busy")),
                patch.object(task_manager, "get_db_connection") as get_connection,
            ):
                result = task_manager.delete_task(task_id, delete_files=True)

            self.assertFalse(result)
            self.assertTrue(task_dir.exists())
            get_connection.assert_not_called()

    def test_single_task_delete_purges_staged_files_after_commit(self):
        task_id = "11111111-1111-1111-1111-111111111111"
        task = {"id": task_id, "status": "failed"}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "tasks.db"
            task_dir = root / task_id
            task_dir.mkdir()
            (task_dir / "video.mp4").write_bytes(b"video")
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO tasks VALUES (?)", (task_id,))
            with (
                patch.object(task_manager, "DB_PATH", str(db_path)),
                patch.object(task_manager, "DOWNLOADS_DIR", str(root)),
                patch.object(task_manager, "get_task", return_value=task),
                patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
                patch.object(task_manager, "publish_task_event") as publish,
            ):
                result = task_manager.delete_task(task_id, delete_files=True)

            self.assertTrue(result)
            self.assertFalse(task_dir.exists())
            self.assertEqual(list(root.glob(".*.deleting-*")), [])
            with closing(sqlite3.connect(db_path)) as connection, connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                    0,
                )
            publish.assert_called_once_with("task_deleted", {"task_id": task_id})

    def test_committed_delete_is_successful_even_if_staged_purge_is_deferred(self):
        task_id = "11111111-1111-1111-1111-111111111111"
        connection = MagicMock()
        connection.execute.return_value.rowcount = 1
        with (
            patch.object(task_manager, "get_task", return_value={"id": task_id}),
            patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
            patch.object(
                task_manager,
                "_stage_task_directory_for_deletion",
                return_value=("/original", "/staged"),
            ),
            patch.object(task_manager, "get_db_connection", return_value=connection),
            patch.object(task_manager, "publish_task_event"),
            patch.object(
                task_manager,
                "_purge_staged_task_directories",
                return_value=False,
            ) as purge,
        ):
            result = task_manager.delete_task(task_id, delete_files=True)

        self.assertTrue(result)
        connection.commit.assert_called_once()
        purge.assert_called_once_with([("/original", "/staged")])

    def test_single_task_delete_restores_files_when_database_delete_fails(self):
        task_id = "11111111-1111-1111-1111-111111111111"
        task = {"id": task_id, "status": "failed"}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_dir = root / task_id
            task_dir.mkdir()
            (task_dir / "video.mp4").write_bytes(b"video")
            connection = MagicMock()
            connection.execute.side_effect = sqlite3.OperationalError("database busy")
            with (
                patch.object(task_manager, "DOWNLOADS_DIR", str(root)),
                patch.object(task_manager, "get_task", return_value=task),
                patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
                patch.object(task_manager, "get_db_connection", return_value=connection),
                patch.object(task_manager, "publish_task_event") as publish,
                patch.object(task_manager, "clear_task_cancel") as clear_cancel,
            ):
                result = task_manager.delete_task(task_id, delete_files=True)

            self.assertFalse(result)
            self.assertTrue((task_dir / "video.mp4").exists())
            self.assertEqual(list(root.glob(".*.deleting-*")), [])
            connection.rollback.assert_called_once()
            publish.assert_not_called()
            clear_cancel.assert_called_once_with(task_id, clear_flag=True)

    def test_clear_all_stages_directories_until_database_commit(self):
        task_ids = [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "tasks.db"
            for task_id in task_ids:
                task_dir = root / task_id
                task_dir.mkdir()
                (task_dir / "video.mp4").write_bytes(b"video")
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE tasks (id TEXT PRIMARY KEY, created_at TEXT)"
                )
                connection.executemany(
                    "INSERT INTO tasks VALUES (?, '2026-08-11 00:00:00')",
                    [(task_id,) for task_id in task_ids],
                )

            with (
                patch.object(task_manager, "DB_PATH", str(db_path)),
                patch.object(task_manager, "DOWNLOADS_DIR", str(root)),
                patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
                patch.object(task_manager, "publish_task_event") as publish,
            ):
                result = task_manager.clear_all_tasks(delete_files=True)

            self.assertTrue(result)
            self.assertTrue(all(not (root / task_id).exists() for task_id in task_ids))
            self.assertEqual(list(root.glob(".*.deleting-*")), [])
            with closing(sqlite3.connect(db_path)) as connection, connection:
                count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            self.assertEqual(count, 0)
            publish.assert_called_once_with("tasks_cleared", {})

    def test_clear_all_restores_staged_directory_when_database_delete_fails(self):
        task_id = "11111111-1111-1111-1111-111111111111"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_dir = root / task_id
            task_dir.mkdir()
            (task_dir / "video.mp4").write_bytes(b"video")
            connection = MagicMock()
            connection.execute.side_effect = sqlite3.OperationalError("database busy")
            with (
                patch.object(task_manager, "DOWNLOADS_DIR", str(root)),
                patch.object(task_manager, "get_all_tasks", return_value=[{"id": task_id}]),
                patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
                patch.object(task_manager, "get_db_connection", return_value=connection),
                patch.object(task_manager, "publish_task_event") as publish,
                patch.object(task_manager, "clear_task_cancel") as clear_cancel,
            ):
                result = task_manager.clear_all_tasks(delete_files=True)

            self.assertFalse(result)
            self.assertTrue(task_dir.exists())
            self.assertTrue((task_dir / "video.mp4").exists())
            self.assertEqual(list(root.glob(".*.deleting-*")), [])
            connection.rollback.assert_called_once()
            publish.assert_not_called()
            clear_cancel.assert_called_once_with(task_id, clear_flag=True)

    def test_clear_all_does_not_restore_files_after_committed_event_failure(self):
        task_id = "11111111-1111-1111-1111-111111111111"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "tasks.db"
            task_dir = root / task_id
            task_dir.mkdir()
            (task_dir / "video.mp4").write_bytes(b"video")
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE tasks (id TEXT PRIMARY KEY, created_at TEXT)"
                )
                connection.execute(
                    "INSERT INTO tasks VALUES (?, '2026-08-11 00:00:00')",
                    (task_id,),
                )

            with (
                patch.object(task_manager, "DB_PATH", str(db_path)),
                patch.object(task_manager, "DOWNLOADS_DIR", str(root)),
                patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
                patch.object(
                    task_manager,
                    "publish_task_event",
                    side_effect=RuntimeError("listener closed"),
                ),
            ):
                result = task_manager.clear_all_tasks(delete_files=True)

            self.assertTrue(result)
            self.assertFalse(task_dir.exists())
            self.assertEqual(list(root.glob(".*.deleting-*")), [])
            with closing(sqlite3.connect(db_path)) as connection, connection:
                count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            self.assertEqual(count, 0)

    def test_clear_all_preserves_task_added_after_initial_snapshot(self):
        old_id = "11111111-1111-1111-1111-111111111111"
        new_id = "22222222-2222-2222-2222-222222222222"
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.db"
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
                connection.executemany(
                    "INSERT INTO tasks VALUES (?)",
                    [(old_id,), (new_id,)],
                )
            with (
                patch.object(task_manager, "DB_PATH", str(db_path)),
                patch.object(task_manager, "get_all_tasks", return_value=[{"id": old_id}]),
                patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
                patch.object(task_manager, "publish_task_event"),
            ):
                result = task_manager.clear_all_tasks(delete_files=False)

            self.assertTrue(result)
            with closing(sqlite3.connect(db_path)) as connection, connection:
                remaining = [row[0] for row in connection.execute("SELECT id FROM tasks")]
            self.assertEqual(remaining, [new_id])

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
        self.assertEqual(
            update_task.call_args.kwargs["expected_status"],
            task_manager.YOUTUBE_PAUSABLE_STATUSES,
        )
        delete_files.assert_not_called()

    def test_pause_does_not_overwrite_task_that_completed_concurrently(self):
        task = {"id": "task-id", "status": "uploading"}
        with (
            patch.object(task_manager, "get_task", return_value=task),
            patch.object(task_manager, "request_task_cancel") as request_cancel,
            patch.object(task_manager, "update_task", return_value=False) as update,
            patch.object(task_manager, "_wait_for_task_inactive") as wait,
        ):
            result = task_manager.pause_task("task-id")

        self.assertFalse(result)
        self.assertEqual(
            update.call_args.kwargs["expected_status"],
            task_manager.YOUTUBE_PAUSABLE_STATUSES,
        )
        request_cancel.assert_not_called()
        wait.assert_not_called()

    def test_process_finalizer_preserves_concurrently_paused_status(self):
        processor = object.__new__(task_manager.TaskProcessor)
        processor.config = {
            "TRANSLATE_TITLE": False,
            "TRANSLATE_DESCRIPTION": False,
            "GENERATE_TAGS": False,
            "RECOMMEND_PARTITION": False,
            "CONTENT_MODERATION_ENABLED": False,
            "SUBTITLE_TRANSLATION_ENABLED": False,
            "SUBTITLE_EMBED_IN_VIDEO": False,
            "AUTO_MODE_ENABLED": False,
        }
        task = {
            "id": "task-id",
            "status": task_manager.TASK_STATES["PAUSED"],
            "youtube_url": "https://youtu.be/example",
            "upload_target": "bilibili",
            "pipeline_checkpoint": json.dumps({
                "version": 1,
                "completed": [
                    task_manager.PIPELINE_STAGE_FETCH_INFO,
                    task_manager.PIPELINE_STAGE_DOWNLOAD_VIDEO,
                ],
            }),
        }
        semaphore = MagicMock()
        task_logger = MagicMock()
        with (
            patch.object(task_manager, "get_task", return_value=task),
            patch.object(task_manager, "setup_task_logger", return_value=task_logger),
            patch.object(task_manager, "update_task", return_value=True) as update,
            patch.object(task_manager, "clear_task_cancel"),
            patch.object(task_manager.threading, "Thread") as thread,
        ):
            thread.return_value.start.side_effect = RuntimeError(
                "cannot start queue check"
            )
            processor.process_task(
                "task-id",
                slot_already_acquired=True,
                acquired_task_semaphore=semaphore,
            )

        status_updates = [
            call for call in update.call_args_list if "status" in call.kwargs
        ]
        self.assertEqual(status_updates, [])
        semaphore.release.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_process_exception_does_not_overwrite_concurrent_pause(self):
        processor = object.__new__(task_manager.TaskProcessor)
        processor.config = {
            "TRANSLATE_TITLE": False,
            "TRANSLATE_DESCRIPTION": False,
            "GENERATE_TAGS": False,
            "RECOMMEND_PARTITION": False,
            "CONTENT_MODERATION_ENABLED": False,
            "SUBTITLE_TRANSLATION_ENABLED": False,
            "SUBTITLE_EMBED_IN_VIDEO": False,
            "AUTO_MODE_ENABLED": False,
        }
        active = {
            "id": "task-id",
            "status": task_manager.TASK_STATES["PENDING"],
            "youtube_url": "https://youtu.be/example",
            "upload_target": "bilibili",
        }
        paused = {**active, "status": task_manager.TASK_STATES["PAUSED"]}
        semaphore = MagicMock()
        with (
            patch.object(
                task_manager,
                "get_task",
                side_effect=[active, active, active, paused],
            ),
            patch.object(task_manager, "setup_task_logger", return_value=MagicMock()),
            patch.object(task_manager, "update_task", return_value=True) as update,
            patch.object(
                processor,
                "_fetch_video_info",
                side_effect=RuntimeError("download interrupted"),
            ),
            patch.object(task_manager, "clear_task_cancel"),
            patch.object(task_manager.threading, "Thread"),
        ):
            processor.process_task(
                "task-id",
                slot_already_acquired=True,
                acquired_task_semaphore=semaphore,
            )

        status_updates = [
            call for call in update.call_args_list if "status" in call.kwargs
        ]
        self.assertEqual(status_updates, [])
        semaphore.release.assert_called_once()

    def test_abandon_clears_cancel_flag_after_worker_stops(self):
        task = {"id": "task-id", "status": "uploading"}
        with (
            patch.object(task_manager, "get_task", return_value=task),
            patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
            patch.object(task_manager, "update_task", return_value=True),
        ):
            result = task_manager.abandon_task("task-id", delete_files=False)

        self.assertTrue(result)
        self.assertFalse(task_manager.is_task_cancelled("task-id"))

    def test_abandon_clears_cancel_flag_when_status_update_fails(self):
        task = {"id": "task-id", "status": "uploading"}
        with (
            patch.object(task_manager, "get_task", return_value=task),
            patch.object(task_manager, "_wait_for_task_inactive", return_value=True),
            patch.object(task_manager, "update_task", return_value=False),
        ):
            result = task_manager.abandon_task("task-id", delete_files=False)

        self.assertFalse(result)
        self.assertFalse(task_manager.is_task_cancelled("task-id"))

    def test_concurrent_terminal_updates_emit_one_notification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.db"
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute(
                    """CREATE TABLE tasks (
                        id TEXT PRIMARY KEY,
                        status TEXT,
                        updated_at TEXT
                    )"""
                )
                connection.execute(
                    "INSERT INTO tasks (id, status, updated_at) VALUES (?, ?, ?)",
                    ("task-id", "uploading", "2026-08-11 00:00:00"),
                )

            barrier = threading.Barrier(2)
            results = []

            def complete_task():
                barrier.wait()
                results.append(
                    task_manager.update_task("task-id", status="completed")
                )

            with (
                patch.object(task_manager, "DB_PATH", str(db_path)),
                patch.object(task_manager, "publish_task_event"),
                patch.object(task_manager, "emit_notification_event") as emit,
            ):
                workers = [threading.Thread(target=complete_task) for _ in range(2)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=5)

            self.assertEqual(results, [True, True])
            self.assertEqual(emit.call_count, 1)

    def test_update_missing_task_returns_false(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.db"
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute(
                    """CREATE TABLE tasks (
                        id TEXT PRIMARY KEY,
                        status TEXT,
                        updated_at TEXT
                    )"""
                )
            with (
                patch.object(task_manager, "DB_PATH", str(db_path)),
                patch.object(task_manager, "publish_task_event") as publish,
                patch.object(task_manager, "emit_notification_event") as emit,
            ):
                result = task_manager.update_task("missing", status="failed")

            self.assertFalse(result)
            publish.assert_not_called()
            emit.assert_not_called()

    def test_committed_update_stays_successful_when_event_listener_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.db"
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, updated_at TEXT)"
                )
                connection.execute(
                    "INSERT INTO tasks VALUES ('task-id', 'pending', '2026-08-11 00:00:00')"
                )
            with (
                patch.object(task_manager, "DB_PATH", str(db_path)),
                patch.object(
                    task_manager,
                    "publish_task_event",
                    side_effect=RuntimeError("listener closed"),
                ),
                patch.object(task_manager, "emit_notification_event"),
            ):
                result = task_manager.update_task("task-id", status="downloading")

            self.assertTrue(result)
            with closing(sqlite3.connect(db_path)) as connection, connection:
                status = connection.execute(
                    "SELECT status FROM tasks WHERE id='task-id'"
                ).fetchone()[0]
            self.assertEqual(status, "downloading")

    def test_expected_status_allows_only_one_concurrent_retry_claim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.db"
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute(
                    """CREATE TABLE tasks (
                        id TEXT PRIMARY KEY,
                        status TEXT,
                        updated_at TEXT
                    )"""
                )
                connection.execute(
                    "INSERT INTO tasks (id, status, updated_at) VALUES (?, ?, ?)",
                    ("task-id", "failed", "2026-08-11 00:00:00"),
                )

            barrier = threading.Barrier(2)
            results = []

            def claim_retry():
                barrier.wait()
                results.append(
                    task_manager.update_task(
                        "task-id",
                        silent=True,
                        expected_status="failed",
                        status="pending",
                    )
                )

            with (
                patch.object(task_manager, "DB_PATH", str(db_path)),
                patch.object(task_manager, "publish_task_event"),
                patch.object(task_manager, "emit_notification_event"),
            ):
                workers = [threading.Thread(target=claim_retry) for _ in range(2)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=5)

            self.assertCountEqual(results, [True, False])
            with closing(sqlite3.connect(db_path)) as connection, connection:
                status = connection.execute(
                    "SELECT status FROM tasks WHERE id='task-id'"
                ).fetchone()[0]
            self.assertEqual(status, "pending")

    def test_bulk_retry_does_not_start_worker_when_claim_is_lost(self):
        task = {
            "id": "task-id",
            "status": "failed",
            "upload_target": "bilibili",
            "bilibili_upload_response": None,
        }
        with (
            patch.object(task_manager, "get_tasks_by_status", return_value=[task]),
            patch.object(task_manager, "_is_upload_stage_failure", return_value=True),
            patch.object(task_manager, "update_task", return_value=False) as update,
            patch.object(task_manager, "_start_background_upload_retry") as start,
        ):
            result = task_manager.retry_failed_tasks(config={})

        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(
            update.call_args.kwargs["expected_status"],
            task_manager.TASK_STATES["FAILED"],
        )
        start.assert_not_called()

    def test_bulk_upload_retry_rolls_back_when_thread_cannot_start(self):
        task = {
            "id": "task-id",
            "status": "failed",
            "error_message": "上传失败",
            "upload_target": "bilibili",
            "bilibili_upload_response": None,
        }
        with (
            patch.object(task_manager, "get_tasks_by_status", return_value=[task]),
            patch.object(task_manager, "_is_upload_stage_failure", return_value=True),
            patch.object(task_manager, "update_task", return_value=True) as update,
            patch.object(task_manager, "_start_background_upload_retry", return_value=False),
        ):
            result = task_manager.retry_failed_tasks(config={})

        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(result["failed_ids"], ["task-id"])
        rollback = update.call_args_list[-1].kwargs
        self.assertEqual(rollback["expected_status"], "ready_for_upload")
        self.assertEqual(rollback["status"], "failed")

    def test_bulk_retry_counts_already_uploaded_task_as_reconciled(self):
        task = {
            "id": "task-id",
            "status": "failed",
            "upload_target": "bilibili",
            "bilibili_upload_response": json.dumps({"bvid": "BV1done"}),
        }
        with (
            patch.object(task_manager, "get_tasks_by_status", return_value=[task]),
            patch.object(task_manager, "update_task", return_value=True) as update,
            patch.object(task_manager, "start_task") as start,
        ):
            result = task_manager.retry_failed_tasks(config={})

        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(update.call_args.kwargs["status"], "completed")
        start.assert_not_called()

    def test_force_upload_returns_false_without_persisted_success(self):
        initial = {
            "id": "task-id",
            "status": task_manager.TASK_STATES["READY_FOR_UPLOAD"],
            "upload_target": "bilibili",
        }
        failed = {
            **initial,
            "status": task_manager.TASK_STATES["FAILED"],
            "error_message": "upload failed",
        }
        processor = MagicMock()
        with (
            patch.object(task_manager, "get_task", side_effect=[initial, failed]),
            patch.object(task_manager, "get_global_task_processor", return_value=processor),
            patch.object(task_manager._TASK_LEASE_STORE, "acquire", return_value=True),
            patch.object(task_manager._TASK_LEASE_STORE, "release"),
            patch.object(task_manager, "_mark_task_active", return_value=True),
            patch.object(task_manager, "_mark_task_inactive"),
            patch.object(task_manager, "setup_task_logger", return_value=MagicMock()),
            patch.object(task_manager, "clear_task_cancel"),
        ):
            result = task_manager.force_upload_task("task-id", config={})

        self.assertFalse(result)
        processor._upload_to_target.assert_called_once()

    def test_resume_paused_task_uses_compare_and_set_transition(self):
        task = {
            "id": "task-id",
            "status": task_manager.TASK_STATES["PAUSED"],
        }
        with (
            patch.object(task_manager, "get_task", return_value=task),
            patch.object(task_manager, "_is_task_active", return_value=False),
            patch.object(task_manager, "clear_task_cancel"),
            patch.object(task_manager, "update_task", return_value=False) as update,
            patch.object(task_manager, "get_global_task_processor") as get_processor,
        ):
            result = task_manager.start_task("task-id", config={"configured": True})

        self.assertFalse(result)
        self.assertEqual(
            update.call_args.kwargs["expected_status"],
            task_manager.TASK_STATES["PAUSED"],
        )
        get_processor.assert_not_called()

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
        select_cursor = MagicMock()
        select_cursor.fetchall.return_value = [row]
        update_cursor = MagicMock(rowcount=1)
        connection.execute.side_effect = [select_cursor, update_cursor]
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

    def test_interrupted_recovery_sql_guard_preserves_new_live_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.db"
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE tasks (
                        id TEXT PRIMARY KEY, status TEXT, upload_target TEXT,
                        bilibili_upload_response TEXT, updated_at TEXT
                    );
                    CREATE TABLE task_worker_leases (
                        task_id TEXT PRIMARY KEY, owner_id TEXT,
                        acquired_at REAL, heartbeat_at REAL, lease_until REAL
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, 'bilibili', NULL, ?)",
                    ("task-id", "uploading", "2026-08-11 00:00:00"),
                )
                connection.execute(
                    "INSERT INTO task_worker_leases VALUES (?, 'new-worker', ?, ?, ?)",
                    ("task-id", time.time(), time.time(), time.time() + 60),
                )

            with (
                patch.object(task_manager, "DB_PATH", str(db_path)),
                patch.object(task_manager._TASK_LEASE_STORE, "is_live", return_value=False),
            ):
                recovered = task_manager.recover_interrupted_tasks_to_pending()

            self.assertEqual(recovered, 0)
            with closing(sqlite3.connect(db_path)) as connection, connection:
                status = connection.execute(
                    "SELECT status FROM tasks WHERE id='task-id'"
                ).fetchone()[0]
            self.assertEqual(status, "uploading")

    def test_stuck_reset_defaults_to_preserving_live_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.db"
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE tasks (
                        id TEXT PRIMARY KEY, status TEXT, error_message TEXT,
                        updated_at TEXT
                    );
                    CREATE TABLE task_worker_leases (
                        task_id TEXT PRIMARY KEY, owner_id TEXT,
                        acquired_at REAL, heartbeat_at REAL, lease_until REAL
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, NULL, ?)",
                    ("task-id", "uploading", "2026-08-10 00:00:00"),
                )
                connection.execute(
                    "INSERT INTO task_worker_leases VALUES (?, 'new-worker', ?, ?, ?)",
                    ("task-id", time.time(), time.time(), time.time() + 60),
                )

            with (
                patch.object(task_manager, "DB_PATH", str(db_path)),
                patch.object(task_manager, "_is_task_active", return_value=False),
                patch.object(task_manager._TASK_LEASE_STORE, "is_live", return_value=False),
            ):
                reset = task_manager.reset_stuck_tasks()

            self.assertEqual(reset, 0)
            with closing(sqlite3.connect(db_path)) as connection, connection:
                status = connection.execute(
                    "SELECT status FROM tasks WHERE id='task-id'"
                ).fetchone()[0]
            self.assertEqual(status, "uploading")


class DownloadCleanupLifecycleTests(unittest.TestCase):
    def test_negative_retention_is_rejected_without_deleting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            downloads = Path(temp_dir)
            task_dir = downloads / "11111111-1111-1111-1111-111111111111"
            task_dir.mkdir()
            with patch.object(app_module, "get_app_subdir", return_value=str(downloads)):
                result = app_module.cleanup_downloads(-1)

            self.assertFalse(result["success"])
            self.assertTrue(task_dir.exists())

    def test_partial_cleanup_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            downloads = Path(temp_dir)
            task_dir = downloads / "11111111-1111-1111-1111-111111111111"
            task_dir.mkdir()
            old = time.time() - 7200
            import os
            os.utime(task_dir, (old, old))
            with (
                patch.object(app_module, "get_app_subdir", return_value=str(downloads)),
                patch.object(app_module, "get_all_tasks", return_value=[]),
                patch.object(app_module.shutil, "rmtree", side_effect=PermissionError("busy")),
            ):
                result = app_module.cleanup_downloads(1)

            self.assertFalse(result["success"])
            self.assertIn("1 个下载目录", result["error"])

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
