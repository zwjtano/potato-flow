import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
sys.path.insert(0, str(APP_ROOT))

from modules.desktop_runtime import (  # noqa: E402
    configure_runtime_environment,
    import_legacy_data,
    resolve_windows_runtime,
)
from modules.utils import get_app_root_dir, get_resource_root_dir  # noqa: E402


class WindowsDesktopInstallerTests(unittest.TestCase):
    def test_data_root_override_does_not_change_resource_root(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"POTATOFLOW_DATA_DIR": temp}
        ):
            self.assertEqual(Path(get_app_root_dir()), Path(temp))
            self.assertEqual(Path(get_resource_root_dir()), APP_ROOT)

    def test_legacy_import_copies_data_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, destination = root / "old", root / "new"
            (source / "config").mkdir(parents=True)
            (source / "config" / "config.json").write_text('{"version": 1}', encoding="utf-8")
            result = import_legacy_data(source, destination)

            self.assertTrue(result["imported"])
            self.assertTrue((source / "config" / "config.json").is_file())
            self.assertEqual(
                (destination / "config" / "config.json").read_text(encoding="utf-8"),
                '{"version": 1}',
            )

    def test_portable_runtime_keeps_all_mutable_data_beside_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "portable.mode").touch()
            layout = resolve_windows_runtime(root / "PotatoFlow.exe", root / "resources")
            self.assertEqual(layout.mode, "portable")
            self.assertEqual(layout.data_root, root / "data")
            self.assertEqual(layout.recordings_root, root / "recordings")
            self.assertEqual(layout.exports_root, root / "exports")

    def test_installed_runtime_separates_internal_and_user_files(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(Path(temp) / "local"), "POTATOFLOW_DOCUMENTS_DIR": str(Path(temp) / "docs")},
        ):
            root = Path(temp) / "program"
            layout = resolve_windows_runtime(root / "PotatoFlow.exe", root)
            self.assertEqual(layout.mode, "installed")
            self.assertEqual(layout.data_root, Path(temp) / "local" / "PotatoFlow")
            self.assertEqual(layout.recordings_root, Path(temp) / "docs" / "PotatoFlow" / "recordings")

    def test_runtime_environment_points_components_at_locked_bin(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            (root / "portable.mode").touch()
            (root / "bin").mkdir()
            for name in ("biliup.exe", "ffmpeg.exe", "ffprobe.exe"):
                (root / "bin" / name).touch()
            layout = resolve_windows_runtime(root / "PotatoFlow.exe", root)
            configure_runtime_environment(layout)
            self.assertEqual(os.environ["POTATOFLOW_RECORDINGS_DIR"], str(root / "recordings"))
            self.assertEqual(os.environ["RECORDER_BIN"], str(root / "bin" / "biliup.exe"))

    def test_release_build_outputs_installer_and_portable(self):
        workflow = (ROOT / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
        builder = (APP_ROOT / "build-tools" / "build_exe.py").read_text(encoding="utf-8")
        launcher = (APP_ROOT / "build-tools" / "setup_app.py").read_text(encoding="utf-8")

        self.assertIn("Windows-x64-Setup.exe", workflow)
        self.assertIn("Portable.zip", workflow)
        self.assertIn("console=False", builder)
        self.assertIn("Inno Setup 6", builder)
        self.assertIn("build_portable", builder)
        self.assertIn("runtime-manifest.json", builder)
        self.assertIn('SERVER_ONLY_FLAG = "--server-only"', launcher)
        self.assertIn("POTATOFLOW_DATA_DIR", launcher)

    def test_desktop_tray_uses_decodable_png_icon(self):
        launcher_path = APP_ROOT / "build-tools" / "setup_app.py"
        launcher = launcher_path.read_text(encoding="utf-8")
        namespace = {"__file__": str(launcher_path), "__name__": "setup_app_test"}
        exec(compile(launcher, str(launcher_path), "exec"), namespace)

        self.assertIn('CHECK_DESKTOP_ASSETS_FLAG = "--check-desktop-assets"', launcher)
        with namespace["load_tray_icon"]() as icon:
            self.assertEqual(icon.size, (128, 128))

    def test_desktop_shell_is_adaptive_and_supports_saved_theme(self):
        base = (APP_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        css = (APP_ROOT / "static" / "css" / "ui-refinement.css").read_text(encoding="utf-8")
        shell = (APP_ROOT / "static" / "js" / "desktop-shell.js").read_text(encoding="utf-8")
        self.assertIn("localStorage.getItem('potatoflow-theme')", base)
        self.assertIn("dataset.themePreference = preference", base)
        self.assertIn("sidebarCollapse", base)
        self.assertIn("max-width:1320px", css)
        self.assertIn("max-height:780px", css)
        self.assertIn("matchMedia('(max-width: 1320px)')", shell)

    def test_first_run_wizard_covers_five_required_steps(self):
        wizard = (APP_ROOT / "templates" / "onboarding.html").read_text(encoding="utf-8")
        for label in ("数据位置", "组件自检", "录制设置", "编码检测", "可选服务"):
            self.assertIn(label, wizard)


if __name__ == "__main__":
    unittest.main()
