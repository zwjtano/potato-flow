import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules import path_policy  # noqa: E402


class PathPolicyTests(unittest.TestCase):
    def test_atomic_write_retries_transient_windows_sharing_violation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "live_recorders.json"
            destination.write_text("old", encoding="utf-8")
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source, target):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("file is temporarily in use")
                return real_replace(source, target)

            with mock.patch.object(
                path_policy.os, "replace", side_effect=flaky_replace
            ), mock.patch.object(path_policy.time, "sleep") as sleep:
                path_policy.atomic_write_text(destination, "new")

            self.assertEqual(destination.read_text(encoding="utf-8"), "new")
            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
