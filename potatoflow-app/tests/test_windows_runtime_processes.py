import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import ffmpeg_manager
from modules import live_recorder_manager
from modules import task_manager


def test_runtime_ffmpeg_environment_is_checked_before_download(tmp_path):
    ffmpeg = tmp_path / "bin" / "ffmpeg.exe"
    ffmpeg.parent.mkdir()
    ffmpeg.write_bytes(b"bundled")

    with (
        mock.patch.dict(os.environ, {"FFMPEG_LOCATION": str(ffmpeg)}),
        mock.patch.object(ffmpeg_manager, "load_config", return_value={}),
        mock.patch.object(ffmpeg_manager, "is_ffmpeg_usable", return_value=True) as usable,
        mock.patch.object(ffmpeg_manager, "download_ffmpeg_bundled") as download,
    ):
        resolved = ffmpeg_manager._resolve_ffmpeg_path(allow_system=False, logger=None)

    assert resolved == str(ffmpeg)
    usable.assert_called_once_with(str(ffmpeg), mock.ANY)
    download.assert_not_called()


def test_ffmpeg_download_uses_writable_application_data(tmp_path):
    target = tmp_path / "data" / "ffmpeg"
    with (
        mock.patch.object(ffmpeg_manager.os, "name", "nt"),
        mock.patch.object(ffmpeg_manager, "get_app_root_dir", return_value=str(tmp_path / "data")),
        mock.patch.object(ffmpeg_manager, "get_app_subdir", return_value=str(target)),
        mock.patch.object(ffmpeg_manager.requests, "get", side_effect=RuntimeError("offline")),
    ):
        ffmpeg_manager.download_ffmpeg_bundled()

    assert target.is_dir()


def test_windows_recorder_workers_never_open_a_console():
    expected = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    with mock.patch.object(live_recorder_manager.os, "name", "nt"):
        kwargs = live_recorder_manager._background_process_kwargs()

    assert kwargs == {"creationflags": expected}
    assert "start_new_session" not in kwargs


def test_windows_subtitle_burn_never_opens_a_console():
    expected = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    with mock.patch.object(task_manager.os, "name", "nt"):
        kwargs = task_manager._background_subprocess_kwargs()

    assert kwargs == {"creationflags": expected}
    source = Path(task_manager.__file__).read_text(encoding="utf-8")
    assert source.count("**_background_subprocess_kwargs(),") == 2


def test_incompatible_recorder_migration_database_is_backed_up(tmp_path):
    runtime = tmp_path / "recorder-engine"
    database = runtime / "data" / "data.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"old-index")
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"old-wal")

    with mock.patch.object(live_recorder_manager, "RECORDER_RUNTIME_DIR", runtime):
        backup = live_recorder_manager._backup_incompatible_recorder_database(
            "migration 1 was previously applied but has been modified"
        )

    assert backup is not None
    assert backup.read_bytes() == b"old-index"
    assert Path(f"{backup}-wal").read_bytes() == b"old-wal"
    assert not database.exists()


def test_other_recorder_failures_never_rotate_database(tmp_path):
    runtime = tmp_path / "recorder-engine"
    database = runtime / "data" / "data.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"keep-me")

    with mock.patch.object(live_recorder_manager, "RECORDER_RUNTIME_DIR", runtime):
        backup = live_recorder_manager._backup_incompatible_recorder_database(
            "network connection failed"
        )

    assert backup is None
    assert database.read_bytes() == b"keep-me"


def test_file_library_navigation_opens_the_file_manager():
    template_root = Path(__file__).resolve().parents[1] / "templates"
    base = (template_root / "base.html").read_text(encoding="utf-8")
    live_recording = (template_root / "live_recording.html").read_text(encoding="utf-8")

    assert "#recording-files" in base
    assert "window.addEventListener('hashchange', syncFileLibraryRoute)" in live_recording
    assert "bootstrap.Modal.getOrCreateInstance(filesModal).show()" in live_recording
