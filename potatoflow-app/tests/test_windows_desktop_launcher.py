import importlib.util
import socket
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


def _load_launcher():
    path = Path(__file__).resolve().parents[1] / "build-tools" / "setup_app.py"
    spec = importlib.util.spec_from_file_location("potatoflow_windows_launcher_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_default_server_port_does_not_reuse_occupied_legacy_port():
    launcher = _load_launcher()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as legacy:
        legacy.bind(("127.0.0.1", 0))
        legacy.listen(1)
        occupied = legacy.getsockname()[1]
        selected = launcher._select_server_port()
    assert selected != occupied
    assert selected > 0


def test_health_wait_requires_current_desktop_instance_and_token():
    launcher = _load_launcher()
    process = mock.Mock()
    process.poll.return_value = None

    def stale_service(url, **kwargs):
        if url.endswith("/healthz"):
            return {"status": "ok", "desktop_instance": "old-instance"}
        raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)

    with (
        mock.patch.object(launcher, "_http_json", side_effect=stale_service),
        mock.patch.object(launcher.time, "sleep"),
        mock.patch.object(launcher.time, "monotonic", side_effect=[0.0, 0.0, 2.0]),
    ):
        try:
            launcher._wait_for_health(
                process, "http://127.0.0.1:5001", token="new-token",
                instance_id="new-instance", timeout=1,
            )
        except RuntimeError as exc:
            assert "启动超时" in str(exc)
        else:
            raise AssertionError("stale service must not pass the startup handshake")


def test_health_wait_accepts_matching_desktop_instance():
    launcher = _load_launcher()
    process = mock.Mock()
    process.poll.return_value = None

    def current_service(url, **kwargs):
        assert kwargs.get("token", "") in {"", "new-token"}
        return {"status": "ok", "desktop_instance": "new-instance"}

    with mock.patch.object(launcher, "_http_json", side_effect=current_service):
        launcher._wait_for_health(
            process, "http://127.0.0.1:54321", token="new-token",
            instance_id="new-instance", timeout=1,
        )


def test_recording_exit_confirmation_uses_window_fallback_off_windows():
    launcher = _load_launcher()
    window = mock.Mock()
    window.create_confirmation_dialog.return_value = True

    with mock.patch.object(launcher.os, "name", "posix"):
        assert launcher._confirm_recording_exit(window) is True

    window.create_confirmation_dialog.assert_called_once_with(
        "正在录制",
        "退出 PotatoFlow 会停止当前录制。是否确定退出？",
    )


def test_latest_release_detects_only_strictly_newer_versions():
    launcher = _load_launcher()
    with mock.patch.object(
        launcher,
        "_http_json",
        return_value={
            "tag_name": "v1.6.42",
            "html_url": "https://example.com/v1.6.42",
            "name": "PotatoFlow v1.6.42",
        },
    ) as request:
        release = launcher._latest_release("1.6.41")

    assert release == {
        "version": "1.6.42",
        "url": "https://example.com/v1.6.42",
        "name": "PotatoFlow v1.6.42",
    }
    assert request.call_args.kwargs["headers"]["User-Agent"] == "PotatoFlow/1.6.41"

    with mock.patch.object(
        launcher,
        "_http_json",
        return_value={"tag_name": "v1.6.41"},
    ):
        assert launcher._latest_release("1.6.41") is None


def test_version_key_rejects_non_release_tags():
    launcher = _load_launcher()

    assert launcher._version_key("v1.6.41") == (1, 6, 41)
    assert launcher._version_key("nightly") == ()


def test_windows_awake_request_keeps_display_power_policy_unchanged():
    launcher = _load_launcher()
    kernel32 = mock.Mock()
    kernel32.SetThreadExecutionState.return_value = 1

    with mock.patch.object(launcher.os, "name", "nt"):
        assert launcher._set_windows_system_awake(True, kernel32) is True
        assert launcher._set_windows_system_awake(False, kernel32) is True

    assert kernel32.SetThreadExecutionState.call_args_list == [
        mock.call(0x80000001),  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        mock.call(0x80000000),  # restore the default power policy
    ]
    assert all(
        not (call.args[0] & 0x00000002)  # ES_DISPLAY_REQUIRED must stay disabled
        for call in kernel32.SetThreadExecutionState.call_args_list
    )


def test_tray_exit_runs_off_the_pystray_callback_thread():
    source = (
        Path(__file__).resolve().parents[1] / "build-tools" / "setup_app.py"
    ).read_text(encoding="utf-8")

    assert 'name="desktop-exit"' in source
    assert "threading.Timer(20, os._exit" in source
    assert 'pystray.MenuItem("检查更新", check_updates)' in source
    assert "check_updates(manual=False)" in source
    assert 'getattr(subprocess, "CREATE_NO_WINDOW", 0)' in source
    assert source.index('name="desktop-exit"') < source.index("webview.start(")


def test_installer_stops_processes_and_never_deletes_documents_root():
    generator = (
        Path(__file__).resolve().parents[1] / "build-tools" / "build_exe.py"
    ).read_text(encoding="utf-8")
    assert "function InitializeUninstall" in generator
    assert "taskkill.exe" in generator
    assert "Type: filesandordirs; Name: \"{{app}}\\\\*\"" in generator
    assert "DelTree(UserFiles + '\\\\recordings'" in generator
    assert "DelTree(UserFiles + '\\\\exports'" in generator
    assert "DelTree(UserFiles, True, True, True)" not in generator
    assert "检测到源码仓库，已拒绝清理该目录" in generator


def load_tests(_loader, _tests, _pattern):
    """Expose the module's function-style Windows checks to unittest discovery."""
    suite = unittest.TestSuite()
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            suite.addTest(unittest.FunctionTestCase(function, description=name))
    return suite
