"""Runtime/build metadata used by diagnostics and upgrade checks."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict


_VERSION_PATTERN = re.compile(
    r"""^__version__\s*=\s*["'](?P<version>[^"']+)["']""",
    re.MULTILINE,
)


def read_source_version(version_file: Path) -> str:
    """Read version.py without importing or executing changed source code."""
    try:
        match = _VERSION_PATTERN.search(version_file.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return match.group("version").strip() if match else ""


def build_runtime_info(loaded_version: str, version_file: Path) -> Dict[str, object]:
    source_version = read_source_version(version_file)
    loaded = str(loaded_version or "").strip()
    restart_required = bool(source_version and loaded and source_version != loaded)
    return {
        "loaded_version": loaded,
        "source_version": source_version or loaded,
        "restart_required": restart_required,
        "build_commit": str(os.environ.get("POTATOFLOW_BUILD_COMMIT") or "").strip(),
        "build_version": str(os.environ.get("POTATOFLOW_BUILD_VERSION") or loaded).strip(),
    }
