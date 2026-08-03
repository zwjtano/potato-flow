import importlib.util
import socket
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
