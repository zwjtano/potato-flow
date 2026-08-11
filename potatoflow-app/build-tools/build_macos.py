#!/usr/bin/env python3
"""Build the PotatoFlow Apple Silicon application and DMG."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


BUILD_TOOLS = Path(__file__).resolve().parent
PROJECT_ROOT = BUILD_TOOLS.parent
REPOSITORY_ROOT = PROJECT_ROOT.parent
DIST_ROOT = BUILD_TOOLS / "dist-macos"
WORK_ROOT = BUILD_TOOLS / "build-macos"
SPEC_PATH = BUILD_TOOLS / "PotatoFlow-macOS.spec"
ICON_PATH = BUILD_TOOLS / "PotatoFlow.icns"
COMPONENT_ROOT = BUILD_TOOLS / "macos-components"


def app_version() -> str:
    source = (PROJECT_ROOT / "version.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
    if not match:
        raise RuntimeError("Unable to read PotatoFlow version")
    return match.group(1)


def require_apple_silicon() -> None:
    if sys.platform != "darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
        raise RuntimeError("The macOS release must be built natively on Apple Silicon")


def create_icon() -> None:
    source = PROJECT_ROOT / "static" / "img" / "potato-flow.png"
    iconset = BUILD_TOOLS / "PotatoFlow.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    sizes = ((16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
             (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
             (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x"))
    for pixels, suffix in sizes:
        subprocess.run(
            ["sips", "-z", str(pixels), str(pixels), str(source), "--out", str(iconset / f"icon_{suffix}.png")],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICON_PATH)], check=True)
    shutil.rmtree(iconset)


def resolve_components() -> tuple[Path, Path, Path]:
    ffmpeg = Path(os.environ.get("FFMPEG_LOCATION") or shutil.which("ffmpeg") or "")
    ffprobe = Path(os.environ.get("FFPROBE_LOCATION") or shutil.which("ffprobe") or "")
    recorder = Path(
        os.environ.get("RECORDER_BIN")
        or REPOSITORY_ROOT / "recorder-core" / "target" / "release" / "biliup"
    )
    missing = [name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe), ("biliup", recorder)) if not path.is_file()]
    if missing:
        raise RuntimeError(f"Missing macOS release components: {', '.join(missing)}")
    return ffmpeg.resolve(), ffprobe.resolve(), recorder.resolve()


def write_runtime_manifest(components: tuple[Path, Path, Path]) -> Path:
    COMPONENT_ROOT.mkdir(parents=True, exist_ok=True)
    records = []
    for source, name in zip(components, ("ffmpeg", "ffprobe", "biliup"), strict=True):
        target = COMPONENT_ROOT / name
        shutil.copy2(source, target)
        target.chmod(0o755)
        records.append({
            "name": name,
            "version": "bundled",
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "size": target.stat().st_size,
        })
    manifest = {
        "schema": 1,
        "application": {"name": "PotatoFlow", "version": app_version(), "architecture": "arm64"},
        "components": records,
    }
    path = COMPONENT_ROOT / "runtime-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def create_spec_file(manifest: Path) -> None:
    setup_path = str(BUILD_TOOLS / "setup_app.py")
    project_root = str(PROJECT_ROOT)
    repository_root = str(REPOSITORY_ROOT)
    templates = str(PROJECT_ROOT / "templates")
    static = str(PROJECT_ROOT / "static")
    font_file = str(PROJECT_ROOT / "fonts" / "NotoSansCJKsc-Regular.otf")
    font_license = str(PROJECT_ROOT / "fonts" / "LICENSE.txt")
    bili_sdk_data = str(PROJECT_ROOT / "modules" / "bili_sdk" / "data")
    bridge_example = str(PROJECT_ROOT / "bridge.config.example.json")
    component_root = str(COMPONENT_ROOT)
    spec = f'''# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

curl_datas, curl_binaries, curl_hiddenimports = collect_all("curl_cffi")
datas = [
    ({templates!r}, "templates"),
    ({static!r}, "static"),
    ({font_file!r}, "fonts"),
    ({font_license!r}, "licenses/fonts"),
    ({bili_sdk_data!r}, "modules/bili_sdk/data"),
    ({bridge_example!r}, "."),
    ({str(manifest)!r}, "."),
] + curl_datas
binaries = [
    ({str(COMPONENT_ROOT / "ffmpeg")!r}, "bin"),
    ({str(COMPONENT_ROOT / "ffprobe")!r}, "bin"),
    ({str(COMPONENT_ROOT / "biliup")!r}, "bin"),
] + curl_binaries
hiddenimports = [
    "app", "bridge", "danmaku_pipeline", "Cryptodome", "curl_cffi",
    "modules.douyu_stats_daemon", "googleapiclient.discovery", "PIL",
    "webview", "webview.platforms.cocoa", "qrcode.image.pil",
] + collect_submodules("yt_dlp") + curl_hiddenimports

a = Analysis(
    [{setup_path!r}], pathex=[{project_root!r}, {repository_root!r}],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={{}}, runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gtk", "webview.platforms.edgechromium"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="PotatoFlow",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, target_arch="arm64",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PotatoFlow")
app = BUNDLE(
    coll, name="PotatoFlow.app", icon={str(ICON_PATH)!r},
    bundle_identifier="io.github.zwjtano.potatoflow",
    info_plist={{
        "CFBundleDisplayName": "PotatoFlow",
        "CFBundleShortVersionString": {app_version()!r},
        "CFBundleVersion": {app_version()!r},
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    }},
)
'''
    SPEC_PATH.write_text(spec, encoding="utf-8")


def build_application() -> Path:
    shutil.rmtree(DIST_ROOT, ignore_errors=True)
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    create_icon()
    components = resolve_components()
    manifest = write_runtime_manifest(components)
    create_spec_file(manifest)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(DIST_ROOT), "--workpath", str(WORK_ROOT), str(SPEC_PATH),
    ], cwd=PROJECT_ROOT, check=True)
    app = DIST_ROOT / "PotatoFlow.app"
    if not (app / "Contents" / "MacOS" / "PotatoFlow").is_file():
        raise RuntimeError("PyInstaller did not create PotatoFlow.app")
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    return app


def build_dmg(app: Path) -> Path:
    staging = DIST_ROOT / "dmg-root"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    shutil.copytree(app, staging / app.name, symlinks=True)
    (staging / "Applications").symlink_to("/Applications")
    dmg = DIST_ROOT / f"PotatoFlow-v{app_version()}-macOS-Apple-Silicon.dmg"
    subprocess.run([
        "hdiutil", "create", "-volname", "PotatoFlow", "-srcfolder", str(staging),
        "-ov", "-format", "UDZO", str(dmg),
    ], check=True)
    digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
    dmg.with_suffix(dmg.suffix + ".sha256").write_text(f"{digest}  {dmg.name}\n", encoding="ascii")
    return dmg


def main() -> None:
    require_apple_silicon()
    app = build_application()
    dmg = build_dmg(app)
    print(dmg)


if __name__ == "__main__":
    main()
