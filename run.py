#!/usr/bin/env python3
"""Single entry point for the unified recording and upload application."""

from __future__ import annotations

import os
from pathlib import Path

from runtime_environment import configure_linux_ca_environment


ROOT = Path(__file__).resolve().parent
APP_ROOT = ROOT / "y2a-auto"
PYTHON = APP_ROOT / ".venv" / "bin" / "python"

if not PYTHON.exists():
    raise SystemExit("缺少运行环境，请先在 y2a-auto/.venv 中安装 requirements.txt")

os.chdir(APP_ROOT)
os.environ.setdefault("PORT", "5001")
configure_linux_ca_environment()
os.execv(str(PYTHON), [str(PYTHON), "app.py"])
