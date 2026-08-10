#!/usr/bin/env python3
"""Windows desktop entry point for the installed PotatoFlow application."""

from __future__ import annotations

import json
import locale
import os
import platform
import runpy
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from contextlib import contextmanager
from pathlib import Path


INTERNAL_YT_DLP_FLAG = "--potatoflow-internal-yt-dlp"
INTERNAL_BRIDGE_FLAG = "--potatoflow-internal-bridge"
INTERNAL_DOUYU_STATS_FLAG = "--potatoflow-internal-douyu-stats"
SERVER_ONLY_FLAG = "--server-only"
CHECK_DESKTOP_ASSETS_FLAG = "--check-desktop-assets"
ACTIVATION_PORT = 45160
LATEST_RELEASE_API = "https://api.github.com/repos/zwjtano/potato-flow/releases/latest"
RELEASES_URL = "https://github.com/zwjtano/potato-flow/releases/latest"


def _set_windows_system_awake(enabled: bool, kernel32=None) -> bool:
    """Keep Windows awake without forcing the display to remain on."""
    if os.name != "nt" and kernel32 is None:
        return True
    if kernel32 is None:
        import ctypes

        kernel32 = ctypes.windll.kernel32
    es_continuous = 0x80000000
    es_system_required = 0x00000001
    flags = es_continuous | es_system_required if enabled else es_continuous
    return bool(kernel32.SetThreadExecutionState(flags))


@contextmanager
def keep_windows_system_awake():
    """Prevent automatic sleep while the desktop launcher remains open."""
    enabled = _set_windows_system_awake(True)
    try:
        yield
    finally:
        if enabled:
            _set_windows_system_awake(False)


def assign_kill_on_close_job(process: subprocess.Popen) -> object | None:
    """Put the server and all descendants in a Windows Job Object."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]
    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return None
    limits = EXTENDED_LIMIT()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel32.CloseHandle(handle); return None
    if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
        kernel32.CloseHandle(handle); return None
    return handle


def resource_root() -> Path:
    internal = str(getattr(sys, "_MEIPASS", "") or "").strip()
    if internal:
        return Path(internal).resolve()
    return Path(__file__).resolve().parents[1]


def configure_windows_runtime() -> tuple[Path, Path]:
    if platform.system() == "Windows":
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
    from modules.desktop_runtime import (
        configure_runtime_environment,
        ensure_windows_layout,
        resolve_windows_runtime,
    )

    layout = ensure_windows_layout(resolve_windows_runtime(sys.executable, resource_root()))
    configure_runtime_environment(layout)
    data_root = layout.data_root
    os.chdir(data_root)
    return resource_root(), data_root


def run_internal_cli(args: list[str]) -> int | None:
    if not args:
        return None
    if args[0] == INTERNAL_YT_DLP_FLAG:
        from yt_dlp import main as yt_dlp_main

        result = yt_dlp_main(args[1:])
        return result if isinstance(result, int) else 0
    if args[0] == INTERNAL_BRIDGE_FLAG:
        import bridge

        previous = sys.argv
        try:
            sys.argv = ["bridge.py", *args[1:]]
            return int(bridge.main())
        finally:
            sys.argv = previous
    if args[0] == INTERNAL_DOUYU_STATS_FLAG:
        from modules import douyu_stats_daemon

        douyu_stats_daemon.run()
        return 0
    return None


def run_internal_yt_dlp_cli(argv: list[str] | None = None) -> int | None:
    """Backward-compatible reserved yt-dlp dispatcher used by runtime callers."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != INTERNAL_YT_DLP_FLAG:
        return None
    return run_internal_cli(args)


def run_server() -> int:
    os.environ["POTATOFLOW_DESKTOP_MODE"] = "1"
    runpy.run_module("app", run_name="__main__")
    return 0


def start_douyu_stats_process(data_root: Path, child_env: dict[str, str], creationflags: int):
    """Start the singleton live-statistics collector for the desktop lifetime."""
    log_path = data_root / "logs" / "douyu-stats.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        return subprocess.Popen(
            [sys.executable, INTERNAL_DOUYU_STATS_FLAG],
            cwd=data_root,
            env=child_env,
            creationflags=creationflags,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )


def stop_child_process(process: subprocess.Popen | None, timeout: float = 10) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def _log_desktop_shutdown_warning(data_root: Path, step: str, exc: BaseException) -> None:
    """Record best-effort shutdown failures without turning them into startup errors."""
    try:
        log_path = data_root / "logs" / "desktop-shutdown.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{step}: {type(exc).__name__}: {exc}\n")
    except Exception:
        pass


def load_tray_icon():
    """Load the bundled tray icon completely before pystray uses it."""
    from PIL import Image

    icon_path = resource_root() / "static" / "img" / "favicon.png"
    icon_image = Image.open(icon_path)
    icon_image.load()
    return icon_image


def _http_json(
    url: str,
    *,
    token: str = "",
    method: str = "GET",
    timeout: float = 3,
    headers: dict[str, str] | None = None,
) -> dict:
    request = urllib.request.Request(url, method=method)
    if token:
        request.add_header("X-PotatoFlow-Desktop-Token", token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _version_key(value: str) -> tuple[int, ...]:
    normalized = str(value or "").strip().lower().lstrip("v")
    parts = normalized.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return ()
    return tuple(int(part) for part in parts)


def _latest_release(current_version: str) -> dict[str, str] | None:
    payload = _http_json(
        LATEST_RELEASE_API,
        timeout=8,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"PotatoFlow/{current_version}",
        },
    )
    latest_version = str(payload.get("tag_name") or "").strip().lstrip("v")
    if (
        not _version_key(latest_version)
        or _version_key(latest_version) <= _version_key(current_version)
    ):
        return None
    return {
        "version": latest_version,
        "url": str(payload.get("html_url") or RELEASES_URL),
        "name": str(payload.get("name") or f"PotatoFlow v{latest_version}"),
    }


def _select_server_port(requested: str | None = None) -> int:
    """Return an available loopback port instead of trusting a stale fixed-port service."""
    requested_port = int(requested or 0)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", requested_port))
        return int(probe.getsockname()[1])


def _wait_for_health(
    process: subprocess.Popen,
    url: str,
    *,
    token: str,
    instance_id: str,
    timeout: float = 120,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"PotatoFlow 内部服务启动失败（{process.returncode}）")
        try:
            health = _http_json(f"{url}/healthz")
            desktop = _http_json(f"{url}/api/desktop/status", token=token)
            if (
                health.get("status") == "ok"
                and health.get("desktop_instance") == instance_id
                and desktop.get("desktop_instance") == instance_id
            ):
                return
        except Exception:
            time.sleep(1)
    raise RuntimeError("PotatoFlow 内部服务启动超时")


def _acquire_single_instance() -> object | None:
    if os.name != "nt":
        return object()
    import ctypes

    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\PotatoFlowDesktop-v1")
    if not handle or ctypes.windll.kernel32.GetLastError() == 183:
        try:
            with socket.create_connection(("127.0.0.1", ACTIVATION_PORT), timeout=1) as client:
                client.sendall(b"show")
        except OSError:
            pass
        return None
    return handle


def _confirm_recording_exit(window) -> bool:
    """Ask outside WebView's UI thread so a tray callback cannot deadlock it."""
    if os.name == "nt":
        import ctypes

        # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND
        return ctypes.windll.user32.MessageBoxW(
            None,
            "退出 PotatoFlow 会停止当前录制。是否确定退出？",
            "正在录制",
            0x00000004 | 0x00000030 | 0x00010000,
        ) == 6
    return bool(window.create_confirmation_dialog(
        "正在录制",
        "退出 PotatoFlow 会停止当前录制。是否确定退出？",
    ))


def _show_update_message(window, title: str, message: str, *, question: bool) -> bool:
    if os.name == "nt":
        import ctypes

        flags = (0x00000004 if question else 0x00000000) | 0x00000040 | 0x00010000
        result = ctypes.windll.user32.MessageBoxW(None, message, title, flags)
        return result == 6 if question else True
    if question:
        return bool(window.create_confirmation_dialog(title, message))
    return True


def run_desktop(data_root: Path) -> int:
    instance = _acquire_single_instance()
    if instance is None:
        return 0

    import pystray
    import webview
    port = _select_server_port(os.environ.get("PORT"))
    url = f"http://127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)
    instance_id = secrets.token_urlsafe(24)
    child_env = dict(os.environ)
    child_env["PORT"] = str(port)
    child_env["POTATOFLOW_DESKTOP_TOKEN"] = token
    child_env["POTATOFLOW_DESKTOP_INSTANCE_ID"] = instance_id
    child_env["POTATOFLOW_DESKTOP_MODE"] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [sys.executable, SERVER_ONLY_FLAG],
        cwd=data_root,
        env=child_env,
        creationflags=creationflags,
    )
    process_job = assign_kill_on_close_job(process)
    stats_process = None
    stats_process_job = None
    try:
        stats_process = start_douyu_stats_process(data_root, child_env, creationflags)
        stats_process_job = assign_kill_on_close_job(stats_process)
        _wait_for_health(process, url, token=token, instance_id=instance_id)
        if stats_process.poll() is not None:
            raise RuntimeError(
                "斗鱼直播数据采集进程启动失败，请查看 logs\\douyu-stats.log"
            )
    except Exception:
        stop_child_process(stats_process)
        stop_child_process(process)
        raise

    initial_html = "<html><body style='font-family:Segoe UI;padding:32px'><h2>PotatoFlow</h2><p>正在准备桌面界面……</p></body></html>"
    window = webview.create_window(
        "PotatoFlow", html=initial_html, width=1440, height=900,
        min_size=(1180, 720), confirm_close=False,
    )
    state = {"exiting": False, "exit_requested": False, "force_exit_timer": None}

    def show_window(*_args) -> None:
        try:
            window.show()
            window.restore()
        except Exception:
            pass

    def open_recordings(*_args) -> None:
        path = Path(os.environ["POTATOFLOW_RECORDINGS_DIR"])
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def recording_label(_item=None) -> str:
        try:
            status = _http_json(f"{url}/api/desktop/status", token=token)
            return f"当前录制：{len(status.get('rooms') or [])}"
        except Exception:
            return "当前录制：状态不可用"

    def activation_listener() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", ACTIVATION_PORT))
            server.listen(2)
            while not state["exiting"]:
                try:
                    server.settimeout(1)
                    connection, _ = server.accept()
                except OSError:
                    continue
                with connection:
                    if connection.recv(16) == b"show":
                        show_window()

    threading.Thread(target=activation_listener, daemon=True, name="desktop-activation").start()

    icon_image = load_tray_icon()
    tray: pystray.Icon

    def check_updates(*_args, manual: bool = True) -> None:
        def update_worker() -> None:
            try:
                from version import __version__

                release = _latest_release(__version__)
                if release:
                    open_page = _show_update_message(
                        window,
                        "PotatoFlow 发现新版本",
                        f"发现 PotatoFlow v{release['version']}。\n\n是否打开下载页？",
                        question=True,
                    )
                    if open_page:
                        webbrowser.open(release["url"])
                elif manual:
                    _show_update_message(
                        window,
                        "PotatoFlow 更新",
                        f"当前已是最新版本 v{__version__}。",
                        question=False,
                    )
            except Exception as exc:
                if manual:
                    _show_update_message(
                        window,
                        "PotatoFlow 更新",
                        f"检查更新失败：{exc}",
                        question=False,
                    )

        threading.Thread(
            target=update_worker,
            daemon=True,
            name="desktop-update-check",
        ).start()

    def shutdown(*_args) -> None:
        if state["exiting"] or state["exit_requested"]:
            return
        state["exit_requested"] = True

        def exit_worker() -> None:
            try:
                try:
                    status = _http_json(f"{url}/api/desktop/status", token=token)
                except Exception:
                    status = {"recording": False}
                if status.get("recording") and not _confirm_recording_exit(window):
                    state["exit_requested"] = False
                    return
                state["exiting"] = True
                # A final watchdog guarantees that a stuck WebView/runtime or
                # recorder shutdown cannot leave the desktop process hanging.
                force_exit_timer = threading.Timer(20, os._exit, args=(0,))
                force_exit_timer.daemon = True
                state["force_exit_timer"] = force_exit_timer
                force_exit_timer.start()
                try:
                    _http_json(
                        f"{url}/api/desktop/shutdown",
                        token=token,
                        method="POST",
                        timeout=3,
                    )
                except Exception:
                    pass
                try:
                    window.destroy()
                finally:
                    try:
                        tray.stop()
                    except Exception:
                        pass
            except Exception:
                state["exiting"] = True
                try:
                    process.terminate()
                except Exception:
                    pass
                try:
                    window.destroy()
                except Exception:
                    pass

        # pystray invokes menu handlers on its own platform thread. Returning
        # immediately avoids blocking that message loop while WebView closes.
        threading.Thread(
            target=exit_worker,
            daemon=True,
            name="desktop-exit",
        ).start()

    def on_closing() -> bool:
        if state["exiting"]:
            return True
        window.hide()
        return False

    window.events.closing += on_closing
    tray = pystray.Icon(
        "PotatoFlow", icon_image, "PotatoFlow",
        menu=pystray.Menu(
            pystray.MenuItem("打开 PotatoFlow", show_window, default=True),
            pystray.MenuItem(recording_label, None, enabled=False),
            pystray.MenuItem("打开录播目录", open_recordings),
            pystray.MenuItem("检查更新", check_updates),
            pystray.MenuItem("退出", shutdown),
        ),
    )
    threading.Thread(target=tray.run, daemon=True, name="desktop-tray").start()

    def on_started() -> None:
        marker = data_root / "state" / "onboarding.json"
        window.load_url(f"{url}/onboarding" if not marker.exists() else url)
        check_updates(manual=False)

    try:
        try:
            webview.start(on_started, gui="edgechromium", debug=False)
        except BaseException as exc:
            # pywebview/Edge may report a late native `kill` exception after
            # window.destroy() has already completed. During an intentional
            # exit this is a cleanup warning, not an application startup error.
            if not (state["exiting"] or state["exit_requested"]):
                raise
            _log_desktop_shutdown_warning(data_root, "webview", exc)
    finally:
        state["exiting"] = True
        force_exit_timer = state.get("force_exit_timer")
        if force_exit_timer is not None:
            force_exit_timer.cancel()
        try:
            tray.stop()
        except Exception:
            pass
        if process.poll() is None:
            try:
                _http_json(f"{url}/api/desktop/shutdown", token=token, method="POST", timeout=5)
                process.wait(timeout=15)
            except Exception as exc:
                _log_desktop_shutdown_warning(data_root, "server-graceful-stop", exc)
                try:
                    process.terminate()
                except Exception as terminate_exc:
                    _log_desktop_shutdown_warning(
                        data_root,
                        "server-terminate",
                        terminate_exc,
                    )
        try:
            stop_child_process(stats_process)
        except Exception as exc:
            _log_desktop_shutdown_warning(data_root, "stats-stop", exc)
        if process_job and os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(process_job)
            except Exception as exc:
                _log_desktop_shutdown_warning(data_root, "server-job-close", exc)
        if stats_process_job and os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(stats_process_job)
            except Exception as exc:
                _log_desktop_shutdown_warning(data_root, "stats-job-close", exc)
    return 0


def main() -> int:
    _resource_root, data_root = configure_windows_runtime()
    args = list(sys.argv[1:])
    internal_exit_code = run_internal_cli(args)
    if internal_exit_code is not None:
        return internal_exit_code
    if CHECK_DESKTOP_ASSETS_FLAG in args:
        load_tray_icon().close()
        return 0
    if SERVER_ONLY_FLAG in args:
        return run_server()
    with keep_windows_system_awake():
        return run_desktop(data_root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        try:
            data_dir = Path(os.environ.get("POTATOFLOW_DATA_DIR") or Path.home())
            log_path = data_dir / "logs" / "desktop-startup-error.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        except Exception:
            pass
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"PotatoFlow 启动失败：{exc}\n\n请查看 LocalAppData\\PotatoFlow\\logs。",
                    "PotatoFlow",
                    0x10,
                )
            except Exception:
                pass
        raise
