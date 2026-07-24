import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BrandingTests(unittest.TestCase):
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
