import concurrent.futures
import multiprocessing
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.upload_queue import bilibili_upload_slot


def _process_upload_slot(lock_path, result_queue, hold_seconds):
    with bilibili_upload_slot(lock_path):
        started = time.monotonic()
        time.sleep(hold_seconds)
        result_queue.put((started, time.monotonic()))


class BilibiliUploadQueueTests(unittest.TestCase):
    def test_parallel_callers_are_serialized(self):
        active = 0
        maximum_active = 0
        guard = threading.Lock()
        statuses = {"one": [], "two": []}

        with TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "upload.lock"

            def run(name):
                nonlocal active, maximum_active
                with bilibili_upload_slot(lock_path, statuses[name].append):
                    with guard:
                        active += 1
                        maximum_active = max(maximum_active, active)
                    time.sleep(0.06)
                    with guard:
                        active -= 1

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(run, ("one", "two")))

        self.assertEqual(maximum_active, 1)
        self.assertEqual(statuses["one"], ["queued", "uploading"])
        self.assertEqual(statuses["two"], ["queued", "uploading"])

    def test_parallel_processes_are_serialized(self):
        context = multiprocessing.get_context("spawn")
        with TemporaryDirectory() as temp_dir:
            lock_path = str(Path(temp_dir) / "upload.lock")
            result_queue = context.Queue()
            first = context.Process(
                target=_process_upload_slot,
                args=(lock_path, result_queue, 0.2),
            )
            second = context.Process(
                target=_process_upload_slot,
                args=(lock_path, result_queue, 0.2),
            )
            first.start()
            second.start()
            intervals = sorted(
                (result_queue.get(timeout=10), result_queue.get(timeout=10)),
                key=lambda interval: interval[0],
            )
            first.join(timeout=10)
            second.join(timeout=10)

        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        self.assertLessEqual(intervals[0][1], intervals[1][0])


if __name__ == "__main__":
    unittest.main()
