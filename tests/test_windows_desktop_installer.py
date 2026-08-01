import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
Y2A_ROOT = ROOT / "y2a-auto"
sys.path.insert(0, str(Y2A_ROOT))

from modules.desktop_runtime import import_legacy_data  # noqa: E402
from modules.utils import get_app_root_dir, get_resource_root_dir  # noqa: E402


class WindowsDesktopInstallerTests(unittest.TestCase):
    def test_data_root_override_does_not_change_resource_root(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"POTATOFLOW_DATA_DIR": temp}
        ):
            self.assertEqual(Path(get_app_root_dir()), Path(temp))
            self.assertEqual(Path(get_resource_root_dir()), Y2A_ROOT)

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

    def test_release_build_outputs_installer_only(self):
        workflow = (ROOT / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
        builder = (Y2A_ROOT / "build-tools" / "build_exe.py").read_text(encoding="utf-8")
        launcher = (Y2A_ROOT / "build-tools" / "setup_app.py").read_text(encoding="utf-8")

        self.assertIn("Windows-x64-Setup.exe", workflow)
        self.assertNotIn("Compress-Archive", workflow)
        self.assertIn("console=False", builder)
        self.assertIn("Inno Setup 6", builder)
        self.assertIn('SERVER_ONLY_FLAG = "--server-only"', launcher)
        self.assertIn("POTATOFLOW_DATA_DIR", launcher)


if __name__ == "__main__":
    unittest.main()
