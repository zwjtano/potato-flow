import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BrandingTests(unittest.TestCase):
    def test_live_room_delete_uses_centered_modal_and_flash_auto_dismisses(self):
        live_template = (
            ROOT / "y2a-auto" / "templates" / "live_recording.html"
        ).read_text(encoding="utf-8")
        base_template = (
            ROOT / "y2a-auto" / "templates" / "base.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="deleteRoomConfirmModal"', live_template)
        self.assertIn('data-action="confirm-delete-room"', live_template)
        self.assertNotIn(
            "onsubmit=\"return confirm('确定删除这个直播间",
            live_template,
        )
        self.assertIn('data-auto-dismiss=', base_template)
        self.assertIn("bootstrap.Alert.getOrCreateInstance", base_template)

    def test_native_browser_dialogs_are_replaced_by_shared_ui(self):
        base_template = (
            ROOT / "y2a-auto" / "templates" / "base.html"
        ).read_text(encoding="utf-8")
        sources = list((ROOT / "y2a-auto" / "templates").glob("*.html"))
        sources.extend((ROOT / "y2a-auto" / "static" / "js").glob("*.js"))

        self.assertIn('id="appConfirmModal"', base_template)
        self.assertIn("window.PotatoUI", base_template)
        self.assertIn('id="appToastStack"', base_template)
        for path in sources:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertNotRegex(text, r"(?<!PotatoUI\.)\b(?:window\.)?confirm\s*\(")
                self.assertNotRegex(text, r"(?<!PotatoUI\.)\b(?:window\.)?alert\s*\(")
                self.assertNotRegex(text, r"\b(?:window\.)?prompt\s*\(")

    def test_settings_exposes_recordings_directory(self):
        settings_template = (
            ROOT / "y2a-auto" / "templates" / "settings.html"
        ).read_text(encoding="utf-8")
        config_source = (
            ROOT / "y2a-auto" / "modules" / "config_manager.py"
        ).read_text(encoding="utf-8")

        self.assertIn('name="RECORDINGS_PATH"', settings_template)
        self.assertIn("potato-flow/recordings/", settings_template)
        self.assertIn('"RECORDINGS_PATH": "recordings"', config_source)

    def test_tasks_page_uses_manual_refresh_without_detail_polling(self):
        template = (ROOT / "y2a-auto" / "templates" / "tasks.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-auto-refresh="off"', template)
        self.assertIn('id="manualRefreshTasksBtn"', template)
        self.assertIn('id="manualRefreshRecordingDetailBtn"', template)
        self.assertNotIn(
            "recordingDetailTimer = window.setInterval",
            template,
        )
        dom_ready = template.rsplit(
            "document.addEventListener('DOMContentLoaded', function()",
            1,
        )[-1]
        self.assertNotIn("initTasksEventStream();", dom_ready)
        self.assertNotIn("refreshTasksData(true), 400", dom_ready)

    def test_sidebar_shows_centralized_version_and_author(self):
        version_source = (ROOT / "y2a-auto" / "version.py").read_text(encoding="utf-8")
        base_template = (ROOT / "y2a-auto" / "templates" / "base.html").read_text(encoding="utf-8")
        app_source = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")

        self.assertRegex(version_source, r'__version__ = "\d+\.\d+\.\d+"')
        self.assertIn('__author__ = "zwjtano"', version_source)
        self.assertIn("Potato Flow v{{ app_version }}", base_template)
        self.assertIn('data-app-version="{{ app_version }}"', base_template)
        self.assertIn("response.headers['X-PotatoFlow-Version'] = __version__", app_source)
        self.assertIn("@app.route('/api/version')", app_source)
        self.assertIn("by {{ app_author }}", base_template)

    def test_release_version_has_only_one_project_source(self):
        version_source = (
            ROOT / "y2a-auto" / "version.py"
        ).read_text(encoding="utf-8")
        current = re.search(
            r'__version__ = "(\d+\.\d+\.\d+)"',
            version_source,
        ).group(1)
        project_files = [
            ROOT / "bridge.py",
            ROOT / "README.md",
            ROOT / "Dockerfile",
            ROOT / "docker-compose.yml",
            ROOT / "y2a-auto" / "app.py",
        ]
        project_files.extend((ROOT / "y2a-auto" / "templates").glob("*.html"))
        for path in project_files:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertNotRegex(text, rf"\bv?{re.escape(current)}\b")
