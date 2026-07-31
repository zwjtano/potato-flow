#!/usr/bin/env python3
"""Build the verified Windows x64 portable PotatoFlow bundle."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


BUILD_TOOLS = Path(__file__).resolve().parent
PROJECT_ROOT = BUILD_TOOLS.parent
DIST_ROOT = BUILD_TOOLS / "dist"
BUNDLE_DIR = DIST_ROOT / "PotatoFlow-Windows-x64"
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"


def _data_arg(source: str, destination: str) -> str:
    return f"{PROJECT_ROOT / source}{os.pathsep}{destination}"


def build_executable() -> None:
    """Create a one-directory PyInstaller application."""
    hidden_imports = [
        "app",
        "Cryptodome",
        "curl_cffi",
        "googleapiclient.discovery",
        "PIL",
        "qrcode.image.pil",
        *collect_submodules('yt_dlp'),
    ]
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onedir",
        "--console",
        "--name",
        "PotatoFlow",
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(BUILD_TOOLS / "build"),
        "--specpath",
        str(BUILD_TOOLS),
        "--paths",
        str(PROJECT_ROOT),
        "--collect-all",
        "curl_cffi",
        "--add-data",
        _data_arg("templates", "templates"),
        "--add-data",
        _data_arg("static", "static"),
        "--add-data",
        _data_arg("fonts", "fonts"),
    ]
    for module in hidden_imports:
        command.extend(("--hidden-import", module))
    command.append(str(BUILD_TOOLS / "setup_app.py"))

    subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    generated = DIST_ROOT / "PotatoFlow"
    if BUNDLE_DIR.exists():
        shutil.rmtree(BUNDLE_DIR)
    generated.rename(BUNDLE_DIR)


def install_ffmpeg() -> None:
    """Download a current GPL Windows FFmpeg bundle during the trusted CI build."""
    archive_path = BUILD_TOOLS / "ffmpeg-win64.zip"
    extract_dir = BUILD_TOOLS / "ffmpeg-extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)

    urllib.request.urlretrieve(FFMPEG_URL, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extract_dir)

    ffmpeg = next(extract_dir.rglob("ffmpeg.exe"), None)
    ffprobe = next(extract_dir.rglob("ffprobe.exe"), None)
    license_file = next((path for path in extract_dir.rglob("*") if path.is_file() and path.name in {"COPYING.GPLv3", "LICENSE.GPLv3", "GPLv3.txt"}), None)
    if not ffmpeg or not ffprobe or not license_file:
        raise RuntimeError("Downloaded FFmpeg archive is missing binaries or GPLv3 license text")

    target = BUNDLE_DIR / "ffmpeg"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ffmpeg, target / "ffmpeg.exe")
    shutil.copy2(ffprobe, target / "ffprobe.exe")
    shutil.copy2(license_file, target / "FFMPEG_GPLv3.txt")
    subprocess.run([target / "ffmpeg.exe", "-version"], check=True)
    subprocess.run([target / "ffprobe.exe", "-version"], check=True)


def create_portable_files() -> None:
    for name in ("config", "cookies", "db", "downloads", "logs", "recordings", "temp"):
        (BUNDLE_DIR / name).mkdir(parents=True, exist_ok=True)

    (BUNDLE_DIR / "start.bat").write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "title PotatoFlow v1.5.38\r\n"
        "start \"\" http://127.0.0.1:5001\r\n"
        "PotatoFlow.exe\r\n"
        "if errorlevel 1 pause\r\n",
        encoding="utf-8-sig",
    )
    (BUNDLE_DIR / "README.txt").write_text(
        "PotatoFlow v1.5.38 Windows x64 portable\n\n"
        "1. Extract the complete ZIP archive to a writable directory.\n"
        "2. Double-click start.bat.\n"
        "3. Open http://127.0.0.1:5001 in a browser.\n\n"
        "Configuration, cookies, databases, recordings and logs remain inside this directory.\n"
        "Windows 10 or Windows 11 x64 is required. FFmpeg and ffprobe are included.\n",
        encoding="utf-8",
    )


def main() -> None:
    if os.name != "nt":
        raise SystemExit("The portable executable must be built on Windows")
    shutil.rmtree(DIST_ROOT, ignore_errors=True)
    shutil.rmtree(BUILD_TOOLS / "build", ignore_errors=True)
    build_executable()
    install_ffmpeg()
    create_portable_files()


if __name__ == "__main__":
    main()
