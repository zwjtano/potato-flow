import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import ffmpeg_manager
from modules import live_recorder_manager


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


def test_file_library_navigation_opens_the_file_manager():
    template_root = Path(__file__).resolve().parents[1] / "templates"
    base = (template_root / "base.html").read_text(encoding="utf-8")
    live_recording = (template_root / "live_recording.html").read_text(encoding="utf-8")

    assert "#recording-files" in base
    assert "window.addEventListener('hashchange', syncFileLibraryRoute)" in live_recording
    assert "bootstrap.Modal.getOrCreateInstance(filesModal).show()" in live_recording
