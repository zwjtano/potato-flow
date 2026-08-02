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
from pathlib import Path


INTERNAL_YT_DLP_FLAG = "--potatoflow-internal-yt-dlp"
INTERNAL_BRIDGE_FLAG = "--potatoflow-internal-bridge"
SERVER_ONLY_FLAG = "--server-only"
CHECK_DESKTOP_ASSETS_FLAG = "--check-desktop-assets"
ACTIVATION_PORT = 45160


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
    from modules.desktop_runtime import default_windows_data_dir, ensure_data_layout

    data_root = ensure_data_layout(default_windows_data_dir())
    os.environ.setdefault("POTATOFLOW_DATA_DIR", str(data_root))
    os.environ.setdefault("PORT", "5001")
    bundled_ffmpeg = Path(sys.executable).resolve().parent / "ffmpeg"
    if bundled_ffmpeg.is_dir():
        os.environ["PATH"] = str(bundled_ffmpeg) + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("FFMPEG_LOCATION", str(bundled_ffmpeg / "ffmpeg.exe"))
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


def load_tray_icon():
    """Load the bundled tray icon completely before pystray uses it."""
    from PIL import Image

    icon_path = resource_root() / "static" / "img" / "favicon.png"
    icon_image = Image.open(icon_path)
    icon_image.load()
    return icon_image


def _http_json(url: str, *, token: str = "", method: str = "GET", timeout: float = 3) -> dict:
    request = urllib.request.Request(url, method=method)
    if token:
        request.add_header("X-PotatoFlow-Desktop-Token", token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_health(process: subprocess.Popen, url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"PotatoFlow 内部服务启动失败（{process.returncode}）")
        try:
            if _http_json(f"{url}/healthz").get("status") == "ok":
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


def run_desktop(data_root: Path) -> int:
    instance = _acquire_single_instance()
    if instance is None:
        return 0

    import pystray
    import webview
    from modules.desktop_runtime import import_legacy_data

    port = int(os.environ.get("PORT", "5001"))
    url = f"http://127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)
    child_env = dict(os.environ)
    child_env["POTATOFLOW_DESKTOP_TOKEN"] = token
    child_env["POTATOFLOW_DESKTOP_MODE"] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [sys.executable, SERVER_ONLY_FLAG],
        cwd=data_root,
        env=child_env,
        creationflags=creationflags,
    )
    try:
        _wait_for_health(process, url)
    except Exception:
        process.terminate()
        raise

    initial_html = "<html><body style='font-family:Segoe UI;padding:32px'><h2>PotatoFlow</h2><p>正在准备桌面界面……</p></body></html>"
    window = webview.create_window(
        "PotatoFlow", html=initial_html, width=1280, height=820,
        min_size=(960, 640), confirm_close=False,
    )
    state = {"exiting": False}

    def show_window(*_args) -> None:
        try:
            window.show()
            window.restore()
        except Exception:
            pass

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

    def shutdown(*_args) -> None:
        if state["exiting"]:
            return
        try:
            status = _http_json(f"{url}/api/desktop/status", token=token)
        except Exception:
            status = {"recording": False}
        if status.get("recording"):
            answer = window.create_confirmation_dialog(
                "正在录制",
                "退出 PotatoFlow 会停止当前录制。是否确定退出？",
            )
            if not answer:
                return
        state["exiting"] = True
        try:
            _http_json(f"{url}/api/desktop/shutdown", token=token, method="POST", timeout=5)
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass
        window.destroy()

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
            pystray.MenuItem("退出", shutdown),
        ),
    )
    threading.Thread(target=tray.run, daemon=True, name="desktop-tray").start()

    def on_started() -> None:
        marker = data_root / "state" / "desktop-first-run.json"
        if not marker.exists():
            try:
                selected = window.create_file_dialog(webview.FOLDER_DIALOG, allow_multiple=False)
                if selected:
                    import_legacy_data(Path(selected[0]), data_root)
            except Exception:
                pass
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"completed": True}), encoding="utf-8")
        window.load_url(url)

    try:
        webview.start(on_started, gui="edgechromium", debug=False)
    finally:
        state["exiting"] = True
        try:
            tray.stop()
        except Exception:
            pass
        if process.poll() is None:
            try:
                _http_json(f"{url}/api/desktop/shutdown", token=token, method="POST", timeout=5)
                process.wait(timeout=15)
            except Exception:
                process.terminate()
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
