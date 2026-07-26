"""Cross-process upload queue shared by recording and regular Bilibili tasks."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - PotatoFlow officially runs on Linux
    fcntl = None


_THREAD_LOCK = threading.Lock()


@contextmanager
def bilibili_upload_slot(
    lock_path: str | Path,
    status_callback: Callable[[str], None] | None = None,
) -> Iterator[None]:
    """Wait for the one global Bilibili upload slot, then release it safely."""

    def report(status: str) -> None:
        if not status_callback:
            return
        try:
            status_callback(status)
        except Exception:
            pass

    path = Path(lock_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    report("queued")
    with _THREAD_LOCK:
        with path.open("a+b") as lock_handle:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                report("uploading")
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def default_bilibili_upload_lock(temp_dir: str | Path) -> Path:
    configured = os.environ.get("POTATO_BILIBILI_UPLOAD_LOCK", "").strip()
    return Path(configured) if configured else Path(temp_dir) / "bilibili-upload.lock"
