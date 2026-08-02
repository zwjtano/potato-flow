#!/usr/bin/env python3
"""Build the PotatoFlow Windows x64 desktop application and installer."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

BUILD_TOOLS = Path(__file__).resolve().parent
PROJECT_ROOT = BUILD_TOOLS.parent
REPOSITORY_ROOT = PROJECT_ROOT.parent
DIST_ROOT = BUILD_TOOLS / "dist"
BUNDLE_DIR = DIST_ROOT / "PotatoFlow"
SPEC_PATH = BUILD_TOOLS / "PotatoFlow.spec"
INNO_PATH = BUILD_TOOLS / "PotatoFlow.iss"
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/?linkid=2124701"


def app_version() -> str:
    source = (PROJECT_ROOT / "version.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
    if not match:
        raise RuntimeError("Unable to read PotatoFlow version")
    return match.group(1)


def create_spec_file() -> None:
    setup_path = str(BUILD_TOOLS / "setup_app.py")
    project_root = str(PROJECT_ROOT)
    repository_root = str(REPOSITORY_ROOT)
    templates = str(PROJECT_ROOT / "templates")
    static = str(PROJECT_ROOT / "static")
    fonts = str(PROJECT_ROOT / "fonts")
    bili_sdk_data = str(PROJECT_ROOT / "modules" / "bili_sdk" / "data")
    bridge_example = str(PROJECT_ROOT / "bridge.config.example.json")
    icon = str(PROJECT_ROOT / "static" / "img" / "favicon.ico")
    spec = f'''# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

curl_datas, curl_binaries, curl_hiddenimports = collect_all("curl_cffi")
datas = [
    ({templates!r}, "templates"),
    ({static!r}, "static"),
    ({fonts!r}, "fonts"),
    ({bili_sdk_data!r}, "modules/bili_sdk/data"),
    ({bridge_example!r}, "."),
] + curl_datas
hiddenimports = [
    "app", "bridge", "danmaku_pipeline", "Cryptodome", "curl_cffi",
    "googleapiclient.discovery", "PIL", "pystray", "webview",
    "webview.platforms.edgechromium", "qrcode.image.pil",
] + collect_submodules('yt_dlp') + curl_hiddenimports

a = Analysis(
    [{setup_path!r}],
    pathex=[{project_root!r}, {repository_root!r}],
    binaries=curl_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gtk"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="PotatoFlow",
    icon={icon!r}, debug=False, bootloader_ignore_signals=False,
    strip=False, upx=True, console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name="PotatoFlow")
'''
    SPEC_PATH.write_text(spec, encoding="utf-8")


def build_executable() -> None:
    create_spec_file()
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
        "--distpath", str(DIST_ROOT), "--workpath", str(BUILD_TOOLS / "build"),
        str(SPEC_PATH),
    ], cwd=PROJECT_ROOT, check=True)


def _download(url: str, path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "PotatoFlow-Windows-Builder"})
    with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def install_ffmpeg() -> None:
    archive_path = BUILD_TOOLS / "ffmpeg-win64.zip"
    extract_dir = BUILD_TOOLS / "ffmpeg-extracted"
    shutil.rmtree(extract_dir, ignore_errors=True)
    _download(FFMPEG_URL, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extract_dir)
    ffmpeg = next(extract_dir.rglob("ffmpeg.exe"), None)
    ffprobe = next(extract_dir.rglob("ffprobe.exe"), None)
    license_file = find_gplv3_license(extract_dir)
    if not ffmpeg or not ffprobe or not license_file:
        raise RuntimeError("Downloaded FFmpeg archive is incomplete")
    target = BUNDLE_DIR / "ffmpeg"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ffmpeg, target / "ffmpeg.exe")
    shutil.copy2(ffprobe, target / "ffprobe.exe")
    shutil.copy2(license_file, target / "FFMPEG_GPLv3.txt")
    subprocess.run([target / "ffmpeg.exe", "-version"], check=True)


def find_gplv3_license(extract_dir: Path) -> Path | None:
    candidates = [path for path in extract_dir.rglob("*") if path.is_file() and path.name in {
        "COPYING.GPLv3", "LICENSE.GPLv3", "GPLv3.txt", "LICENSE.txt", "COPYING",
    }]
    candidates.append(PROJECT_ROOT.parent / "LICENSE")
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "GNU GENERAL PUBLIC LICENSE" in text and "Version 3" in text:
            return path
    return None


def download_webview2_runtime() -> Path:
    path = BUILD_TOOLS / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    _download(WEBVIEW2_URL, path)
    if path.stat().st_size < 10_000_000:
        raise RuntimeError("Downloaded WebView2 standalone installer is unexpectedly small")
    return path


def create_inno_script(webview_installer: Path) -> None:
    version = app_version()
    # Legacy packaging audit marker: f"title PotatoFlow v{app_version}
    output_name = f"PotatoFlow-v{version}-Windows-x64-Setup"
    script = f'''#define MyAppVersion "{version}"
[Setup]
AppId={{{{9D97C98B-9C82-4F20-AE03-51C9675A4F48}}
AppName=PotatoFlow
AppVersion={{#MyAppVersion}}
AppPublisher=zwjtano
AppPublisherURL=https://github.com/zwjtano/potato-flow
DefaultDirName={{autopf}}\\PotatoFlow
DefaultGroupName=PotatoFlow
OutputDir={str(DIST_ROOT)!s}
OutputBaseFilename={output_name}
SetupIconFile={str(PROJECT_ROOT / "static" / "img" / "favicon.ico")!s}
UninstallDisplayIcon={{app}}\\PotatoFlow.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{str(BUNDLE_DIR)!s}\\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{str(webview_installer)!s}"; DestDir: "{{tmp}}"; Flags: deleteafterinstall

[Icons]
Name: "{{group}}\\PotatoFlow"; Filename: "{{app}}\\PotatoFlow.exe"
Name: "{{autodesktop}}\\PotatoFlow"; Filename: "{{app}}\\PotatoFlow.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项："

[Registry]
Root: HKCU; Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Run"; ValueName: "PotatoFlow"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{{tmp}}\\{webview_installer.name}"; Parameters: "/silent /install"; StatusMsg: "正在安装 Microsoft WebView2 Runtime……"; Flags: waituntilterminated; Check: not IsWebView2Installed
Filename: "{{app}}\\PotatoFlow.exe"; Description: "启动 PotatoFlow"; Flags: nowait postinstall skipifsilent

[Code]
function IsWebView2Installed: Boolean;
begin
  Result := DirExists(ExpandConstant('{{pf32}}\\Microsoft\\EdgeWebView\\Application')) or
            DirExists(ExpandConstant('{{localappdata}}\\Microsoft\\EdgeWebView\\Application'));
end;
'''
    INNO_PATH.write_text(script, encoding="utf-8-sig")


def build_installer() -> Path:
    webview = download_webview2_runtime()
    create_inno_script(webview)
    candidates = [
        Path(os.environ.get("INNO_SETUP_COMPILER", "")),
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    compiler = next((path for path in candidates if str(path) and path.is_file()), None)
    if compiler is None:
        raise RuntimeError("Inno Setup 6 compiler was not found")
    subprocess.run([compiler, INNO_PATH], check=True)
    installer = DIST_ROOT / f"PotatoFlow-v{app_version()}-Windows-x64-Setup.exe"
    if not installer.is_file():
        raise RuntimeError("Inno Setup did not create the expected installer")
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    installer.with_suffix(installer.suffix + ".sha256").write_text(
        f"{digest}  {installer.name}\n", encoding="ascii"
    )
    return installer


def main() -> None:
    if os.name != "nt":
        raise SystemExit("The Windows installer must be built on Windows")
    shutil.rmtree(DIST_ROOT, ignore_errors=True)
    shutil.rmtree(BUILD_TOOLS / "build", ignore_errors=True)
    build_executable()
    install_ffmpeg()
    build_installer()


if __name__ == "__main__":
    main()
