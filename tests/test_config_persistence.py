import copy
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules import config_manager  # noqa: E402


class ConfigPersistenceTests(unittest.TestCase):
    def test_concurrent_updates_preserve_both_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(config_manager, "get_app_subdir", return_value=temp):
                self.assertTrue(config_manager.save_config(copy.deepcopy(config_manager.DEFAULT_CONFIG)))
                threads = [
                    threading.Thread(
                        target=config_manager.update_config,
                        args=({"LOG_CLEANUP_HOURS": 111},),
                    ),
                    threading.Thread(
                        target=config_manager.update_config,
                        args=({"DOWNLOAD_CLEANUP_HOURS": 222},),
                    ),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                loaded = config_manager.load_config()

        self.assertEqual(loaded["LOG_CLEANUP_HOURS"], 111)
        self.assertEqual(loaded["DOWNLOAD_CLEANUP_HOURS"], 222)

    def test_corrupt_config_is_preserved_instead_of_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = pathlib.Path(temp) / "config.json"
            config_path.write_text('{"OPENAI_API_KEY":', encoding="utf-8")
            with patch.object(config_manager, "get_app_subdir", return_value=temp):
                loaded = config_manager.load_config()

            self.assertEqual(config_path.read_text(encoding="utf-8"), '{"OPENAI_API_KEY":')
            self.assertEqual(loaded["OPENAI_API_KEY"], config_manager.DEFAULT_CONFIG["OPENAI_API_KEY"])

    def test_update_raises_when_atomic_save_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = pathlib.Path(temp) / "config.json"
            config_path.write_text(json.dumps(config_manager.DEFAULT_CONFIG), encoding="utf-8")
            with (
                patch.object(config_manager, "get_app_subdir", return_value=temp),
                patch.object(config_manager, "save_config", return_value=False),
            ):
                with self.assertRaisesRegex(OSError, "保存配置文件失败"):
                    config_manager.update_config({"LOG_CLEANUP_HOURS": 24})


if __name__ == "__main__":
    unittest.main()
