#!/usr/bin/env python3
"""Windows portable entry point for PotatoFlow."""

from __future__ import annotations

import locale
import os
import platform
import runpy
import sys
from pathlib import Path


INTERNAL_YT_DLP_FLAG = "--y2a-internal-yt-dlp"


def run_internal_yt_dlp_cli(argv: list[str] | None = None) -> int | None:
    """Expose yt-dlp through the frozen executable for existing runtime callers."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != INTERNAL_YT_DLP_FLAG:
        return None

    from yt_dlp import main as yt_dlp_main

    result = yt_dlp_main(args[1:])
    return result if isinstance(result, int) else 0


def configure_windows_runtime() -> Path:
    """Use the portable directory for mutable state and UTF-8 console output."""
    if platform.system() == "Windows":
        os.system("chcp 65001 >nul 2>&1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass

    app_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    os.chdir(app_root)
    os.environ.setdefault("PORT", "5001")
    return app_root


def main() -> int:
    internal_exit_code = run_internal_yt_dlp_cli()
    if internal_exit_code is not None:
        return internal_exit_code

    configure_windows_runtime()
    runpy.run_module("app", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
