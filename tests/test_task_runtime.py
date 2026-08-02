import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.runtime_info import build_runtime_info, read_source_version  # noqa: E402
from modules.task_runtime import TaskLeaseStore  # noqa: E402


class MutableClock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class TaskLeaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "tasks.db")
        sqlite3.connect(self.db_path).close()
        self.clock = MutableClock()
        self.owner_a = TaskLeaseStore(
            self.db_path,
            owner_id="worker-a",
            lease_seconds=30,
            clock=self.clock,
        )
        self.owner_b = TaskLeaseStore(
            self.db_path,
            owner_id="worker-b",
            lease_seconds=30,
            clock=self.clock,
        )
        self.owner_a.ensure_schema()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_live_lease_prevents_second_worker_claim(self):
        self.assertTrue(self.owner_a.acquire("task-1"))
        self.assertFalse(self.owner_b.acquire("task-1"))
        self.assertEqual(self.owner_a.get("task-1").owner_id, "worker-a")

    def test_expired_lease_can_be_reclaimed(self):
        self.assertTrue(self.owner_a.acquire("task-1"))
        self.clock.value += 31
        self.assertTrue(self.owner_b.acquire("task-1"))
        self.assertEqual(self.owner_b.get("task-1").owner_id, "worker-b")

    def test_heartbeat_extends_only_current_owner_leases(self):
        self.assertTrue(self.owner_a.acquire("task-1"))
        first_expiry = self.owner_a.get("task-1").lease_until
        self.clock.value += 10
        self.assertEqual(self.owner_a.heartbeat(["task-1", "missing"]), 1)
        self.assertGreater(self.owner_a.get("task-1").lease_until, first_expiry)
        self.assertEqual(self.owner_b.heartbeat(["task-1"]), 0)

    def test_release_cannot_remove_another_workers_lease(self):
        self.assertTrue(self.owner_a.acquire("task-1"))
        self.assertFalse(self.owner_b.release("task-1"))
        self.assertTrue(self.owner_a.is_live("task-1"))
        self.assertTrue(self.owner_a.release("task-1"))
        self.assertFalse(self.owner_a.is_live("task-1"))


class RuntimeInfoTests(unittest.TestCase):
    def test_source_version_mismatch_requests_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            version_file = Path(temp_dir) / "version.py"
            version_file.write_text('__version__ = "2.0.0"\n', encoding="utf-8")

            info = build_runtime_info("1.9.0", version_file)

        self.assertEqual(read_source_version(version_file), "")
        self.assertEqual(info["loaded_version"], "1.9.0")
        self.assertEqual(info["source_version"], "2.0.0")
        self.assertTrue(info["restart_required"])

    def test_current_version_does_not_request_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            version_file = Path(temp_dir) / "version.py"
            version_file.write_text('__version__ = "2.0.0"\n', encoding="utf-8")
            info = build_runtime_info("2.0.0", version_file)

        self.assertFalse(info["restart_required"])


class ReproducibleDependencyTests(unittest.TestCase):
    def test_runtime_lock_uses_exact_direct_versions(self):
        lock_file = APP_ROOT / "requirements.lock"
        dependency_lines = [
            line.strip()
            for line in lock_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(dependency_lines)
        for line in dependency_lines:
            self.assertIn("==", line, msg=f"dependency is not pinned: {line}")

        dockerfile = (APP_ROOT.parent / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("pip install -r requirements.lock", dockerfile)
