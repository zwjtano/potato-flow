import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import bilibili_runtime
from modules import ffmpeg_manager
from modules import live_recorder_manager
from modules import speech_recognition
from modules import submission_engine
from modules import task_manager
from modules import vad_processor
from modules import youtube_handler
import danmaku_pipeline
import bridge
from tools import render_subtitle_preview


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


def test_windows_ffmpeg_probes_never_open_a_console():
    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with mock.patch.object(ffmpeg_manager.os, "name", "nt"):
        assert ffmpeg_manager._hidden_subprocess_kwargs() == {
            "creationflags": expected,
        }


def test_all_windows_media_and_upload_helpers_hide_short_lived_processes():
    expected = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    helpers = (
        bridge._hidden_subprocess_kwargs,
        bilibili_runtime._hidden_subprocess_kwargs,
        danmaku_pipeline._hidden_subprocess_kwargs,
        live_recorder_manager._hidden_process_kwargs,
        speech_recognition._hidden_subprocess_kwargs,
        submission_engine._hidden_subprocess_kwargs,
        task_manager._hidden_subprocess_kwargs,
        vad_processor._hidden_subprocess_kwargs,
        youtube_handler._hidden_subprocess_kwargs,
        render_subtitle_preview._hidden_subprocess_kwargs,
    )
    for helper in helpers:
        module = sys.modules[helper.__module__]
        with mock.patch.object(module.os, "name", "nt"):
            assert helper() == expected


def test_windows_pipeline_worker_is_terminated_with_hidden_taskkill():
    manager = live_recorder_manager.LiveRecorderManager()
    completed = mock.Mock(returncode=0, stdout="", stderr="")
    with (
        mock.patch.object(live_recorder_manager.os, "name", "nt"),
        mock.patch.object(manager, "_pipeline_worker_pid", return_value=4321),
        mock.patch.object(manager, "_pipeline_process_alive", return_value=False),
        mock.patch.object(
            live_recorder_manager.subprocess,
            "run",
            return_value=completed,
        ) as run,
    ):
        assert manager._terminate_pipeline_worker({}) == 4321

    assert run.call_args.args[0] == [
        "taskkill.exe", "/PID", "4321", "/T", "/F",
    ]
    assert run.call_args.kwargs["creationflags"] == getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0,
    )


def test_windows_pipeline_command_line_uses_hidden_powershell():
    completed = mock.Mock(
        returncode=0,
        stdout='python.exe bridge.py ingest "C:\\recordings\\clip.flv"',
    )
    with (
        mock.patch.object(live_recorder_manager.os, "name", "nt"),
        mock.patch.object(
            live_recorder_manager.shutil,
            "which",
            return_value="powershell.exe",
        ),
        mock.patch.object(
            live_recorder_manager.subprocess,
            "run",
            return_value=completed,
        ) as run,
    ):
        command = live_recorder_manager.LiveRecorderManager._pipeline_process_cmdline(4321)

    assert "bridge.py" in command
    assert run.call_args.kwargs["creationflags"] == getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0,
    )


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


def test_file_library_stays_in_live_room_without_sidebar_navigation():
    template_root = Path(__file__).resolve().parents[1] / "templates"
    base = (template_root / "base.html").read_text(encoding="utf-8")
    live_recording = (template_root / "live_recording.html").read_text(encoding="utf-8")

    assert '#recording-files' not in base
    assert 'aria-label="录播文件"' not in base
    assert 'data-bs-target="#recordingFilesModal"' in live_recording
    assert "bootstrap.Modal.getOrCreateInstance(filesModal).show()" in live_recording


def test_windows_invalid_process_handles_are_treated_as_stopped():
    source = Path(live_recorder_manager.__file__).read_text(encoding="utf-8")

    assert "except (OSError, SystemError):" in source
    assert "except (OSError, SystemError, subprocess.TimeoutExpired):" in source
    assert "except (OSError, SystemError, TimeoutError):" in source
    assert "except (OSError, SystemError, subprocess.SubprocessError) as exc:" in source


def load_tests(_loader, _tests, _pattern):
    """Expose the module's function-style Windows checks to unittest discovery."""
    fixture_tests = {
        "test_runtime_ffmpeg_environment_is_checked_before_download",
        "test_ffmpeg_download_uses_writable_application_data",
        "test_incompatible_recorder_migration_database_is_backed_up",
        "test_other_recorder_failures_never_rotate_database",
    }
    suite = unittest.TestSuite()
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        if name in fixture_tests:
            def run_with_tmp_path(test_function=function):
                with tempfile.TemporaryDirectory() as temp:
                    test_function(Path(temp))

            test_case = unittest.FunctionTestCase(
                run_with_tmp_path,
                description=name,
            )
        else:
            test_case = unittest.FunctionTestCase(function, description=name)
        suite.addTest(test_case)
    return suite
