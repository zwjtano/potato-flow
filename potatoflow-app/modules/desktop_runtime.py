"""Windows desktop integration helpers kept out of the web/server runtime."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable

STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE = "PotatoFlow"
MIGRATION_ITEMS = (
    "config", "cookies", "db", "downloads", "logs", "recordings", "state", "temp", ".bridge"
)
MIGRATION_FILES = ("bridge.config.json",)


def default_windows_data_dir() -> Path:
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if not local_app_data:
        local_app_data = str(Path.home() / "AppData" / "Local")
    return Path(local_app_data).expanduser().resolve() / "PotatoFlow"


def ensure_data_layout(data_dir: Path) -> Path:
    root = Path(data_dir).expanduser().resolve()
    for name in ("config", "cookies", "db", "downloads", "logs", "recordings", "state", "temp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


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
