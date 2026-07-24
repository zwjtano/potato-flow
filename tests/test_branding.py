import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BrandingTests(unittest.TestCase):
    def test_sidebar_shows_centralized_version_and_author(self):
        version_source = (ROOT / "y2a-auto" / "version.py").read_text(encoding="utf-8")
        base_template = (ROOT / "y2a-auto" / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn('__version__ = "1.3.20"', version_source)
        self.assertIn('__author__ = "zwjtano"', version_source)
        self.assertIn("Potato Flow v{{ app_version }}", base_template)
        self.assertIn("by {{ app_author }}", base_template)
