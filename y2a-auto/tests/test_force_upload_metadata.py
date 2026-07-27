import ast
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from modules import task_manager as tm


def _load_app_partition_helper():
    app_path = pathlib.Path(__file__).resolve().parents[1] / 'app.py'
    source = app_path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(app_path))
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == '_missing_upload_partition_labels'
    ]
    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(isolated, str(app_path), 'exec'), namespace)
    return namespace['_missing_upload_partition_labels']


class ForceUploadMetadataTests(unittest.TestCase):
    def test_add_task_keeps_uuid_and_creates_display_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = pathlib.Path(temp_dir) / "tasks.db"
            with patch.object(tm, "DB_PATH", str(db_path)), patch.object(
                tm, "get_global_task_processor", return_value=None
            ), patch.object(tm, "publish_task_event"), patch.object(
                tm, "emit_notification_event"
            ), patch.object(tm, "_task_display_date", return_value="0728"):
                tm.init_db()
                task_id = tm.add_task("https://www.youtube.com/watch?v=test")
                task = tm.get_task(task_id)

        self.assertRegex(task_id, r"^[0-9a-f-]{36}$")
        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["display_id"], "YT-VIDEO-0728-001")

    def test_standard_task_display_ids_backfill_and_increment(self):
        with sqlite3.connect(":memory:") as db:
            db.execute(
                """CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    created_at TEXT,
                    display_id TEXT
                )"""
            )
            db.executemany(
                "INSERT INTO tasks VALUES (?, ?, ?)",
                [
                    ("first", "2026-07-28 01:00:00", None),
                    ("second", "2026-07-28 02:00:00", None),
                ],
            )
            tm._ensure_task_display_ids(db)
            assigned = dict(db.execute("SELECT id, display_id FROM tasks"))

            self.assertEqual(assigned["first"], "YT-VIDEO-0728-001")
            self.assertEqual(assigned["second"], "YT-VIDEO-0728-002")

            with patch.object(tm, "_task_display_date", return_value="0728"):
                self.assertEqual(tm._next_task_display_id(db), "YT-VIDEO-0728-003")

    def test_normalize_tags_list_handles_none_and_invalid_json(self):
        self.assertEqual(tm._normalize_tags_list(None), [])
        self.assertEqual(tm._normalize_tags_list(''), [])
        self.assertEqual(tm._normalize_tags_list('not-json'), [])
        self.assertEqual(tm._normalize_tags_list('["asmr", "", null, "game"]'), ['asmr', 'game'])

    def test_force_upload_generates_missing_tags_and_partitions(self):
        task_id = 'task-force-upload'
        task = {
            'id': task_id,
            'upload_target': 'bilibili',
            'video_title_original': 'Matching game in ASMR',
            'description_original': 'Ys: 45\nTo: 90',
            'video_title_translated': 'ASMR中的配对游戏',
            'description_translated': '',
            'tags_generated': None,
            'selected_partition_id_bilibili': '',
            'recommended_partition_id_bilibili': '',
        }

        def fake_get_task(_task_id):
            self.assertEqual(_task_id, task_id)
            return dict(task)

        def fake_update_task(_task_id, **kwargs):
            self.assertEqual(_task_id, task_id)
            task.update({k: v for k, v in kwargs.items() if k != 'silent'})
            return True

        processor = tm.TaskProcessor({
            'GENERATE_TAGS': True,
            'RECOMMEND_PARTITION': True,
            'CONTENT_MODERATION_ENABLED': True,
        })
        processor._generate_tags = MagicMock(side_effect=lambda *_args: task.update({
            'tags_generated': '["ASMR", "配对游戏"]'
        }) or True)
        processor._recommend_partition = MagicMock(side_effect=lambda *_args: task.update({
            'recommended_partition_id_bilibili': '2001',
            'selected_partition_id_bilibili': '2001',
        }) or True)
        processor._moderate_content = MagicMock(side_effect=lambda *_args: task.update({
            'moderation_result': '{"overall_pass": true}'
        }) or True)

        with patch.object(tm, 'get_task', side_effect=fake_get_task), \
             patch.object(tm, 'update_task', side_effect=fake_update_task):
            result = processor._ensure_force_upload_metadata_ready(task_id, MagicMock())

        self.assertEqual(result['tags_generated'], '["ASMR", "配对游戏"]')
        self.assertEqual(result['selected_partition_id_bilibili'], '2001')
        self.assertEqual(result['moderation_result'], '{"overall_pass": true}')
        processor._generate_tags.assert_called_once()
        processor._recommend_partition.assert_called_once()
        processor._moderate_content.assert_called_once()

    def test_force_upload_respects_existing_tags_and_partitions(self):
        task_id = 'task-existing-metadata'
        task = {
            'id': task_id,
            'upload_target': 'bilibili',
            'video_title_original': 'Original title',
            'description_original': 'Original description',
            'video_title_translated': '',
            'description_translated': '',
            'tags_generated': '["手动标签"]',
            'selected_partition_id_bilibili': '2001',
            'recommended_partition_id_bilibili': '',
            'moderation_result': '{"overall_pass": true}',
        }

        processor = tm.TaskProcessor({
            'GENERATE_TAGS': True,
            'RECOMMEND_PARTITION': True,
            'CONTENT_MODERATION_ENABLED': True,
        })
        processor._generate_tags = MagicMock()
        processor._recommend_partition = MagicMock()
        processor._moderate_content = MagicMock()

        with patch.object(tm, 'get_task', return_value=dict(task)):
            result = processor._ensure_force_upload_metadata_ready(task_id, MagicMock())

        self.assertEqual(result['tags_generated'], '["手动标签"]')
        self.assertEqual(result['selected_partition_id_bilibili'], '2001')
        processor._generate_tags.assert_not_called()
        processor._recommend_partition.assert_not_called()
        processor._moderate_content.assert_not_called()

    def test_force_upload_pauses_when_moderation_requires_review(self):
        task_id = 'task-moderation-review'
        task = {
            'id': task_id,
            'upload_target': 'bilibili',
            'status': tm.TASK_STATES['READY_FOR_UPLOAD'],
            'video_title_original': 'Original title',
            'description_original': 'Original description',
            'tags_generated': '["tag"]',
            'selected_partition_id_bilibili': '2001',
            'recommended_partition_id_bilibili': '',
            'moderation_result': None,
        }

        processor = tm.TaskProcessor({
            'GENERATE_TAGS': True,
            'RECOMMEND_PARTITION': True,
            'CONTENT_MODERATION_ENABLED': True,
        })
        processor._moderate_content = MagicMock(side_effect=lambda *_args: task.update({
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'moderation_result': '{"overall_pass": false}',
        }) or True)

        with patch.object(tm, 'get_task', side_effect=lambda _task_id: dict(task)), \
             patch.object(tm, 'update_task', side_effect=lambda _task_id, **kwargs: True):
            result = processor._ensure_force_upload_metadata_ready(task_id, MagicMock())

        self.assertIsNone(result)
        processor._moderate_content.assert_called_once()

    def test_partition_precheck_blocks_only_when_no_recommendation_or_fixed_partition(self):
        missing_upload_partition_labels = _load_app_partition_helper()
        task = {
            'upload_target': 'bilibili',
            'selected_partition_id_bilibili': '',
            'recommended_partition_id_bilibili': '',
        }

        self.assertEqual(
            missing_upload_partition_labels(task, {'RECOMMEND_PARTITION': 'false'}),
            ['bilibili 分区'],
        )
        self.assertEqual(
            missing_upload_partition_labels(task, {'RECOMMEND_PARTITION': True}),
            [],
        )
        self.assertEqual(
            missing_upload_partition_labels(task, {
                'RECOMMEND_PARTITION': False,
                'FIXED_PARTITION_ID_BILIBILI': '2001',
            }),
            [],
        )


if __name__ == '__main__':
    unittest.main()
