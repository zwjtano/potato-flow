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

BUILD_TOOLS = Path(__file__).resolve().parent
PROJECT_ROOT = BUILD_TOOLS.parent
DIST_ROOT = BUILD_TOOLS / "dist"
BUNDLE_DIR = DIST_ROOT / "PotatoFlow-Windows-x64"
SPEC_PATH = BUILD_TOOLS / "PotatoFlow.spec"
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"


def create_spec_file() -> None:
    """Store the module manifest in a spec file to avoid Windows command limits."""
    setup_path = str(BUILD_TOOLS / "setup_app.py")
    project_root = str(PROJECT_ROOT)
    templates = str(PROJECT_ROOT / "templates")
    static = str(PROJECT_ROOT / "static")
    fonts = str(PROJECT_ROOT / "fonts")
    spec = f'''# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

curl_datas, curl_binaries, curl_hiddenimports = collect_all("curl_cffi")
datas = [
    ({templates!r}, "templates"),
    ({static!r}, "static"),
    ({fonts!r}, "fonts"),
] + curl_datas
hiddenimports = [
    "app",
    "Cryptodome",
    "curl_cffi",
    "googleapiclient.discovery",
    "PIL",
    "qrcode.image.pil",
] + collect_submodules('yt_dlp') + curl_hiddenimports

a = Analysis(
    [{setup_path!r}],
    pathex=[{project_root!r}],
    binaries=curl_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PotatoFlow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="PotatoFlow",
)
'''
    SPEC_PATH.write_text(spec, encoding="utf-8")


def build_executable() -> None:
    """Create a one-directory PyInstaller application."""
    create_spec_file()
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(BUILD_TOOLS / "build"),
        str(SPEC_PATH),
    ]
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
    license_file = find_gplv3_license(extract_dir)
    if not ffmpeg or not ffprobe or not license_file:
        raise RuntimeError("Downloaded FFmpeg archive is missing binaries or GPLv3 license text")

    target = BUNDLE_DIR / "ffmpeg"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ffmpeg, target / "ffmpeg.exe")
    shutil.copy2(ffprobe, target / "ffprobe.exe")
    shutil.copy2(license_file, target / "FFMPEG_GPLv3.txt")
    subprocess.run([target / "ffmpeg.exe", "-version"], check=True)
    subprocess.run([target / "ffprobe.exe", "-version"], check=True)


def find_gplv3_license(extract_dir: Path) -> Path | None:
    """Return a verified GPLv3 text from FFmpeg or the GPLv3 project license."""
    expected_names = {
        "COPYING.GPLv3",
        "LICENSE.GPLv3",
        "GPLv3.txt",
        "LICENSE.txt",
        "COPYING",
    }
    candidates = [
        path
        for path in extract_dir.rglob("*")
        if path.is_file() and path.name in expected_names
    ]
    candidates.append(PROJECT_ROOT.parent / "LICENSE")
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "GNU GENERAL PUBLIC LICENSE" in text and "Version 3" in text:
            return path
    return None


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
