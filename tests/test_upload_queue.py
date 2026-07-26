import concurrent.futures
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
Y2A_ROOT = ROOT / "y2a-auto"
if str(Y2A_ROOT) not in sys.path:
    sys.path.insert(0, str(Y2A_ROOT))

from modules.upload_queue import bilibili_upload_slot


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


if __name__ == "__main__":
    unittest.main()
