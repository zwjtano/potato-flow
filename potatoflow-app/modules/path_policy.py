#!/usr/bin/env python3
"""Shared naming and permission rules for PotatoFlow-managed data."""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

DIRECTORY_MODE = 0o750
FILE_MODE = 0o640
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

_UNSAFE_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]+')
_DOT_RUN = re.compile(r"\.{2,}")
_REPEATED_SEPARATOR = re.compile(r"[\s_-]+")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_path_component(value: Any, *, fallback: str = "直播间", max_length: int = 80) -> str:
    """Return one readable, cross-platform-safe path component.

    Chinese and other Unicode letters are preserved. Separators, control
    characters, Windows-reserved punctuation and ambiguous repeated spacing
    are normalized to a single underscore.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = _UNSAFE_COMPONENT.sub("_", text)
    text = _DOT_RUN.sub("_", text)
    text = _REPEATED_SEPARATOR.sub("_", text).strip(" ._-")
    if not text or text in {".", ".."}:
        text = fallback
    if text.upper() in _WINDOWS_RESERVED:
        text = f"{text}_room"
    text = text[:max(1, int(max_length))].rstrip(" ._-")
    return text or fallback


def ensure_directory(path: Path, *, private: bool = False) -> Path:
    """Create an app-owned directory and enforce its intended access mode."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(PRIVATE_DIRECTORY_MODE if private else DIRECTORY_MODE)
    return path


def enforce_file_mode(path: Path, *, private: bool = False) -> Path:
    """Enforce the mode of a file produced by the application."""
    path.chmod(PRIVATE_FILE_MODE if private else FILE_MODE)
    return path


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    private: bool = False,
) -> None:
    """Atomically replace a text file without inheriting unsafe source modes."""
    destination = path.resolve() if path.is_symlink() else path
    ensure_directory(destination.parent, private=private)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.{os.getpid()}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(text, encoding=encoding)
        enforce_file_mode(temporary, private=private)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
