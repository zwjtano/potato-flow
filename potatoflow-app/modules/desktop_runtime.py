"""Windows desktop integration helpers kept out of the web/server runtime."""

from __future__ import annotations

import json
import os
import shutil
import sys
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE = "PotatoFlow"
MIGRATION_ITEMS = (
    "config", "cookies", "db", "downloads", "logs", "recordings", "state", "temp", ".bridge"
)
MIGRATION_FILES = ("bridge.config.json",)
RUNTIME_MANIFEST = "runtime-manifest.json"
PORTABLE_MARKER = "portable.mode"


@dataclass(frozen=True)
class WindowsRuntimeLayout:
    mode: str
    install_root: Path
    resource_root: Path
    data_root: Path
    recordings_root: Path
    exports_root: Path
    bin_root: Path
    manifest_path: Path

    def public_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


MacOSRuntimeLayout = WindowsRuntimeLayout


def default_windows_data_dir() -> Path:
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if not local_app_data:
        local_app_data = str(Path.home() / "AppData" / "Local")
    return Path(local_app_data).expanduser().resolve() / "PotatoFlow"


def default_windows_documents_dir() -> Path:
    """Return the visible per-user Documents folder, including redirected folders."""
    configured = str(os.environ.get("POTATOFLOW_DOCUMENTS_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            path = ctypes.create_unicode_buffer(32768)
            # CSIDL_PERSONAL. This also follows OneDrive/domain folder redirection.
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, path) == 0:
                return Path(path.value).resolve()
        except (AttributeError, OSError):
            pass
    return (Path.home() / "Documents").resolve()


def resolve_windows_runtime(
    executable: Path | None = None,
    resources: Path | None = None,
) -> WindowsRuntimeLayout:
    executable = Path(executable or sys.executable).resolve()
    install_root = executable.parent
    resource_root = Path(resources or install_root).resolve()
    portable = (install_root / PORTABLE_MARKER).is_file()
    mode = "portable" if portable else "installed"
    if portable:
        data_root = install_root / "data"
        recordings_root = install_root / "recordings"
        exports_root = install_root / "exports"
    else:
        data_root = default_windows_data_dir()
        documents_root = default_windows_documents_dir() / "PotatoFlow"
        recordings_root = documents_root / "recordings"
        exports_root = documents_root / "exports"
    bin_root = install_root / "bin"
    if not bin_root.is_dir():
        bin_root = resource_root / "bin"
    manifest_path = install_root / RUNTIME_MANIFEST
    if not manifest_path.is_file():
        manifest_path = resource_root / RUNTIME_MANIFEST
    return WindowsRuntimeLayout(
        mode=mode,
        install_root=install_root,
        resource_root=resource_root,
        data_root=data_root.resolve(),
        recordings_root=recordings_root.resolve(),
        exports_root=exports_root.resolve(),
        bin_root=bin_root.resolve(),
        manifest_path=manifest_path.resolve(),
    )


def resolve_macos_runtime(resources: Path | None = None) -> MacOSRuntimeLayout:
    """Resolve writable and bundled paths for the Apple Silicon desktop app."""
    resource_root = Path(resources or Path(sys.executable).resolve().parent).resolve()
    install_root = Path(sys.executable).resolve().parent
    user_root = Path.home()
    data_root = user_root / "Library" / "Application Support" / "PotatoFlow"
    movies_root = user_root / "Movies" / "PotatoFlow"
    bin_root = resource_root / "bin"
    manifest_path = resource_root / RUNTIME_MANIFEST
    return MacOSRuntimeLayout(
        mode="installed",
        install_root=install_root,
        resource_root=resource_root,
        data_root=data_root.resolve(),
        recordings_root=(movies_root / "recordings").resolve(),
        exports_root=(movies_root / "exports").resolve(),
        bin_root=bin_root.resolve(),
        manifest_path=manifest_path.resolve(),
    )


def ensure_data_layout(data_dir: Path) -> Path:
    root = Path(data_dir).expanduser().resolve()
    for name in ("config", "cookies", "db", "downloads", "logs", "recordings", "state", "temp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def ensure_windows_layout(layout: WindowsRuntimeLayout) -> WindowsRuntimeLayout:
    ensure_data_layout(layout.data_root)
    layout.recordings_root.mkdir(parents=True, exist_ok=True)
    layout.exports_root.mkdir(parents=True, exist_ok=True)
    return layout


def configure_runtime_environment(layout: WindowsRuntimeLayout) -> None:
    os.environ["POTATOFLOW_DATA_DIR"] = str(layout.data_root)
    os.environ["POTATOFLOW_RECORDINGS_DIR"] = str(layout.recordings_root)
    # The Douyu statistics collector predates the desktop runtime and reads
    # RECORDINGS_DIR/BRIDGE_CONFIG directly. Keep both contracts pointed at
    # the same writable Windows data layout.
    os.environ["RECORDINGS_DIR"] = str(layout.recordings_root)
    os.environ["BRIDGE_CONFIG"] = str(layout.data_root / "bridge.config.json")
    os.environ["POTATOFLOW_EXPORTS_DIR"] = str(layout.exports_root)
    os.environ["POTATOFLOW_RUNTIME_MODE"] = layout.mode
    os.environ["PATH"] = str(layout.bin_root) + os.pathsep + os.environ.get("PATH", "")
    components = (
        (("biliup.exe", "biliup"), "RECORDER_BIN"),
        (("ffmpeg.exe", "ffmpeg"), "FFMPEG_LOCATION"),
        (("ffprobe.exe", "ffprobe"), "FFPROBE_LOCATION"),
    )
    for names, variable in components:
        candidate = next((layout.bin_root / name for name in names if (layout.bin_root / name).is_file()), None)
        if candidate is not None:
            os.environ[variable] = str(candidate)


def load_runtime_manifest(layout: WindowsRuntimeLayout) -> dict[str, object]:
    try:
        value = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def component_diagnostics(layout: WindowsRuntimeLayout) -> list[dict[str, object]]:
    manifest = load_runtime_manifest(layout)
    expected = manifest.get("components") if isinstance(manifest.get("components"), list) else []
    bundled_names = {path.name for path in layout.bin_root.iterdir()} if layout.bin_root.is_dir() else set()
    has_windows_tools = any(name.endswith(".exe") for name in bundled_names)
    has_extensionless_tools = any(name in bundled_names for name in ("biliup", "ffmpeg", "ffprobe"))
    windows_bundle = has_windows_tools or (not has_extensionless_tools and os.name == "nt")
    names = (
        {"biliup.exe", "ffmpeg.exe", "ffprobe.exe"}
        if windows_bundle
        else {"biliup", "ffmpeg", "ffprobe"}
    )
    names.update(str(item.get("name")) for item in expected if isinstance(item, dict))
    results = []
    for name in sorted(filter(None, names)):
        path = layout.bin_root / name
        digest = ""
        if path.is_file():
            sha256 = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    sha256.update(block)
            digest = sha256.hexdigest()
        results.append({"name": name, "path": str(path), "exists": path.is_file(), "sha256": digest})
    return results


def legacy_data_candidates(path: Path) -> Iterable[Path]:
    root = Path(path).expanduser().resolve()
    yield root
    nested = root / "PotatoFlow-Windows-x64"
    if nested.is_dir():
        yield nested


def import_legacy_data(source: Path, destination: Path) -> dict[str, object]:
    """Copy mutable data from a former portable directory without deleting it."""
    destination = ensure_data_layout(destination)
    source_root = next(
        (
            candidate
            for candidate in legacy_data_candidates(source)
            if any((candidate / name).exists() for name in (*MIGRATION_ITEMS, *MIGRATION_FILES))
        ),
        None,
    )
    if source_root is None or source_root == destination:
        return {"imported": False, "reason": "no_legacy_data"}
    copied: list[str] = []
    for name in MIGRATION_ITEMS:
        old = source_root / name
        if not old.exists():
            continue
        target = destination / name
        if old.is_dir():
            shutil.copytree(old, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, target)
        copied.append(name)
    for name in MIGRATION_FILES:
        old = source_root / name
        if old.is_file():
            shutil.copy2(old, destination / name)
            copied.append(name)
    result = {"imported": bool(copied), "source": str(source_root), "items": copied}
    (destination / "state" / "desktop-import.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def sync_windows_startup(enabled: bool, executable: str | None = None) -> None:
    if os.name != "nt":
        return
    import winreg

    executable = str(executable or sys.executable)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_VALUE, 0, winreg.REG_SZ, f'"{executable}" --startup')
        else:
            try:
                winreg.DeleteValue(key, STARTUP_VALUE)
            except FileNotFoundError:
                pass
