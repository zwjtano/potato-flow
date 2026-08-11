import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
sys.path.insert(0, str(APP_ROOT))

from modules.desktop_runtime import (  # noqa: E402
    component_diagnostics,
    configure_runtime_environment,
    import_legacy_data,
    resolve_macos_runtime,
    resolve_windows_runtime,
)
from modules.utils import get_app_root_dir, get_resource_root_dir  # noqa: E402


class WindowsDesktopInstallerTests(unittest.TestCase):
    def test_macos_runtime_uses_application_support_movies_and_bundled_tools(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            Path,
            "home",
            return_value=Path(temp) / "home",
        ):
            resources = Path(temp) / "PotatoFlow.app" / "Contents" / "Resources"
            layout = resolve_macos_runtime(resources)
            self.assertEqual(
                layout.data_root,
                (Path(temp) / "home" / "Library" / "Application Support" / "PotatoFlow").resolve(),
            )
            self.assertEqual(
                layout.recordings_root,
                (Path(temp) / "home" / "Movies" / "PotatoFlow" / "recordings").resolve(),
            )
            self.assertEqual(layout.bin_root, (resources / "bin").resolve())

    def test_macos_runtime_environment_uses_extensionless_bundled_tools(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=True):
            resources = Path(temp) / "resources"
            (resources / "bin").mkdir(parents=True)
            for name in ("biliup", "ffmpeg", "ffprobe"):
                (resources / "bin" / name).touch()
            with mock.patch.object(Path, "home", return_value=Path(temp) / "home"):
                layout = resolve_macos_runtime(resources)
            configure_runtime_environment(layout)
            self.assertEqual(os.environ["RECORDER_BIN"], str((resources / "bin" / "biliup").resolve()))
            self.assertEqual(os.environ["FFMPEG_LOCATION"], str((resources / "bin" / "ffmpeg").resolve()))
            self.assertEqual(os.environ["FFPROBE_LOCATION"], str((resources / "bin" / "ffprobe").resolve()))

    def test_macos_component_diagnostics_uses_extensionless_names(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(Path, "home", return_value=Path(temp) / "home"):
            resources = Path(temp) / "resources"
            (resources / "bin").mkdir(parents=True)
            for name in ("biliup", "ffmpeg", "ffprobe"):
                (resources / "bin" / name).write_bytes(name.encode())
            # A macOS bundle may be inspected by a Windows CI runner; the
            # package contents, not the host running the test, are authoritative.
            layout = resolve_macos_runtime(resources)
            with mock.patch("modules.desktop_runtime.os.name", "nt"):
                components = component_diagnostics(layout)
            self.assertEqual({item["name"] for item in components}, {"biliup", "ffmpeg", "ffprobe"})
            self.assertTrue(all(item["exists"] for item in components))

    def test_frozen_macos_uses_application_support_for_mutable_data(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            sys,
            "frozen",
            True,
            create=True,
        ), mock.patch.object(
            sys,
            "platform",
            "darwin",
        ), mock.patch(
            "modules.utils.os.path.expanduser",
            return_value=str(Path(temp) / "Library" / "Application Support"),
        ):
            self.assertEqual(
                Path(get_app_root_dir()),
                Path(temp) / "Library" / "Application Support" / "PotatoFlow",
            )

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
            self.assertEqual(layout.data_root, (root / "data").resolve())
            self.assertEqual(layout.recordings_root, (root / "recordings").resolve())
            self.assertEqual(layout.exports_root, (root / "exports").resolve())

    def test_installed_runtime_separates_internal_and_user_files(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(Path(temp) / "local"), "POTATOFLOW_DOCUMENTS_DIR": str(Path(temp) / "docs")},
        ):
            root = Path(temp) / "program"
            layout = resolve_windows_runtime(root / "PotatoFlow.exe", root)
            self.assertEqual(layout.mode, "installed")
            self.assertEqual(
                layout.data_root,
                (Path(temp) / "local" / "PotatoFlow").resolve(),
            )
            self.assertEqual(
                layout.recordings_root,
                (Path(temp) / "docs" / "PotatoFlow" / "recordings").resolve(),
            )

    def test_runtime_environment_points_components_at_locked_bin(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            (root / "portable.mode").touch()
            (root / "bin").mkdir()
            for name in ("biliup.exe", "ffmpeg.exe", "ffprobe.exe"):
                (root / "bin" / name).touch()
            layout = resolve_windows_runtime(root / "PotatoFlow.exe", root)
            configure_runtime_environment(layout)
            self.assertEqual(
                os.environ["POTATOFLOW_RECORDINGS_DIR"],
                str((root / "recordings").resolve()),
            )
            self.assertEqual(
                os.environ["RECORDINGS_DIR"],
                str((root / "recordings").resolve()),
            )
            self.assertEqual(
                os.environ["BRIDGE_CONFIG"],
                str((root / "data" / "bridge.config.json").resolve()),
            )
            self.assertEqual(
                os.environ["RECORDER_BIN"],
                str((root / "bin" / "biliup.exe").resolve()),
            )

    def test_release_build_outputs_installer_and_portable(self):
        workflow = (ROOT / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
        application_workflow = (ROOT / ".github" / "workflows" / "application.yml").read_text(encoding="utf-8")
        builder = (APP_ROOT / "build-tools" / "build_exe.py").read_text(encoding="utf-8")
        launcher = (APP_ROOT / "build-tools" / "setup_app.py").read_text(encoding="utf-8")

        self.assertIn("Windows-x64-Setup.exe", workflow)
        self.assertIn("Portable.zip", workflow)
        self.assertIn("console=False", builder)
        self.assertIn("Inno Setup 6", builder)
        self.assertIn("build_portable", builder)
        self.assertIn("runtime-manifest.json", builder)
        self.assertIn('SERVER_ONLY_FLAG = "--server-only"', launcher)
        self.assertIn('INTERNAL_DOUYU_STATS_FLAG = "--potatoflow-internal-douyu-stats"', launcher)
        self.assertIn('"modules.douyu_stats_daemon"', builder)
        self.assertIn("POTATOFLOW_DATA_DIR", launcher)
        self.assertIn("windows-tests:", application_workflow)
        self.assertIn("./ops/test-all.ps1", application_workflow)
        self.assertIn("Run all test suites before packaging", workflow)

    def test_macos_release_builds_and_validates_apple_silicon_dmg(self):
        workflow = (ROOT / ".github" / "workflows" / "macos-release.yml").read_text(encoding="utf-8")
        builder = (APP_ROOT / "build-tools" / "build_macos.py").read_text(encoding="utf-8")
        launcher = (APP_ROOT / "build-tools" / "setup_app.py").read_text(encoding="utf-8")

        self.assertIn('test "$(uname -m)" = "arm64"', workflow)
        self.assertIn("Run all test suites before packaging", workflow)
        self.assertIn("Smoke test packaged server mode", workflow)
        self.assertIn("codesign --verify --deep --strict", workflow)
        self.assertIn("macOS-Apple-Silicon.dmg", workflow)
        self.assertIn('target_arch="arm64"', builder)
        self.assertIn('bundle_identifier="io.github.zwjtano.potatoflow"', builder)
        self.assertIn('gui="edgechromium" if os.name == "nt" else "cocoa"', launcher)
        onboarding = (APP_ROOT / "templates" / "onboarding.html").read_text(encoding="utf-8")
        self.assertIn("{{ desktop_platform }} 首次设置", onboarding)
        self.assertIn("{{ desktop_webview }}", onboarding)

    def test_release_build_requires_tag_source_and_version_to_match(self):
        workflow = (ROOT / ".github" / "workflows" / "windows-release.yml").read_text(
            encoding="utf-8"
        )

        target = "github.event.release.tag_name || inputs.release_tag"
        self.assertIn(f"ref: ${{{{ {target} || github.ref }}}}", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn('$expectedTag = "v$version"', workflow)
        self.assertIn("does not match source version", workflow)
        self.assertIn('git rev-parse --verify "refs/tags/$releaseTag^{commit}"', workflow)
        self.assertIn("does not match $releaseTag commit", workflow)

    def test_release_build_rejects_assets_from_another_version(self):
        workflow = (ROOT / ".github" / "workflows" / "windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Validate existing Release assets", workflow)
        self.assertIn("Unable to inspect existing assets", workflow)
        self.assertIn("contains Windows assets for another version", workflow)
        self.assertIn("RELEASE_TAG: ${{ steps.release_meta.outputs.release_tag }}", workflow)
        self.assertIn(
            "group: windows-installer-${{ github.event.release.tag_name || inputs.release_tag || github.ref }}",
            workflow,
        )

    def test_desktop_tray_uses_decodable_png_icon(self):
        launcher_path = APP_ROOT / "build-tools" / "setup_app.py"
        launcher = launcher_path.read_text(encoding="utf-8")
        namespace = {"__file__": str(launcher_path), "__name__": "setup_app_test"}
        exec(compile(launcher, str(launcher_path), "exec"), namespace)

        self.assertIn('CHECK_DESKTOP_ASSETS_FLAG = "--check-desktop-assets"', launcher)
        with namespace["load_tray_icon"]() as icon:
            self.assertEqual(icon.size, (128, 128))

    def test_desktop_starts_stats_collector_with_shared_runtime_environment(self):
        launcher_path = APP_ROOT / "build-tools" / "setup_app.py"
        namespace = {"__file__": str(launcher_path), "__name__": "setup_app_test"}
        exec(compile(launcher_path.read_text(encoding="utf-8"), str(launcher_path), "exec"), namespace)

        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp)
            child_env = {"RECORDINGS_DIR": "C:/PotatoFlow/recordings"}
            fake_process = mock.Mock()
            with mock.patch.object(subprocess, "Popen", return_value=fake_process) as popen:
                result = namespace["start_douyu_stats_process"](data_root, child_env, 123)

            self.assertIs(result, fake_process)
            popen.assert_called_once_with(
                [sys.executable, namespace["INTERNAL_DOUYU_STATS_FLAG"]],
                cwd=data_root,
                env=child_env,
                creationflags=123,
                stdout=mock.ANY,
                stderr=subprocess.STDOUT,
            )
            self.assertTrue((data_root / "logs" / "douyu-stats.log").is_file())

    def test_internal_stats_command_runs_bundled_collector(self):
        launcher_path = APP_ROOT / "build-tools" / "setup_app.py"
        namespace = {"__file__": str(launcher_path), "__name__": "setup_app_test"}
        exec(compile(launcher_path.read_text(encoding="utf-8"), str(launcher_path), "exec"), namespace)

        with mock.patch("modules.douyu_stats_daemon.run") as run:
            result = namespace["run_internal_cli"](
                [namespace["INTERNAL_DOUYU_STATS_FLAG"]]
            )

        self.assertEqual(result, 0)
        run.assert_called_once_with()

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
        self.assertIn("apply(compact.matches||savedPreference(),false)", shell)
        self.assertIn("event.matches||savedPreference(),false", shell)
        self.assertIn("js/desktop-shell.js') }}?v={{ app_version }}-2", base)

    def test_first_run_wizard_covers_five_required_steps(self):
        wizard = (APP_ROOT / "templates" / "onboarding.html").read_text(encoding="utf-8")
        for label in ("数据位置", "组件自检", "录制设置", "编码检测", "可选服务"):
            self.assertIn(label, wizard)


if __name__ == "__main__":
    unittest.main()
