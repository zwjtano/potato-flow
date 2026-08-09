"""Cross-process upload queue shared by recording and regular Bilibili tasks."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - unavailable on Linux and macOS
    msvcrt = None


_THREAD_LOCK = threading.Lock()


def _acquire_process_lock(lock_handle) -> None:
    if fcntl is not None:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is None:
        return

    # msvcrt locks a byte range starting at the current file position. Ensure
    # that byte zero exists, then wait without the ten-second LK_LOCK timeout.
    lock_handle.seek(0, os.SEEK_END)
    if lock_handle.tell() == 0:
        lock_handle.write(b"\0")
        lock_handle.flush()
    while True:
        lock_handle.seek(0)
        try:
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {11, 13, 36} and getattr(exc, "winerror", None) not in {33, 36}:
                raise
            time.sleep(0.1)


def _release_process_lock(lock_handle) -> None:
    if fcntl is not None:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)


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
            _acquire_process_lock(lock_handle)
            try:
                report("uploading")
                yield
            finally:
                _release_process_lock(lock_handle)


def default_bilibili_upload_lock(temp_dir: str | Path) -> Path:
    configured = os.environ.get("POTATO_BILIBILI_UPLOAD_LOCK", "").strip()
    return Path(configured) if configured else Path(temp_dir) / "bilibili-upload.lock"
