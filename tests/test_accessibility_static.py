from html.parser import HTMLParser
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "potatoflow-app" / "templates"
REFINEMENT_CSS = ROOT / "potatoflow-app" / "static" / "css" / "ui-refinement.css"


class _IconOnlyActionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.violations = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"button", "a"}:
            self.stack.append({
                "tag": tag,
                "attrs": attributes,
                "line": self.getpos()[0],
                "text": "",
                "has_icon": False,
                "depth": 1,
            })
        elif self.stack:
            self.stack[-1]["depth"] += 1
            if tag == "i":
                self.stack[-1]["has_icon"] = True

    def handle_startendtag(self, tag, attrs):
        if self.stack and tag == "i":
            self.stack[-1]["has_icon"] = True

    def handle_data(self, data):
        if self.stack:
            self.stack[-1]["text"] += data

    def handle_endtag(self, tag):
        if not self.stack:
            return
        current = self.stack[-1]
        if tag == current["tag"] and current["depth"] == 1:
            self.stack.pop()
            visible_text = " ".join(current["text"].split())
            if (
                current["has_icon"]
                and not visible_text
                and not str(current["attrs"].get("aria-label") or "").strip()
            ):
                self.violations.append((current["line"], current["tag"]))
            return
        current["depth"] = max(1, current["depth"] - 1)


class AccessibilityStaticTests(unittest.TestCase):
    def test_icon_only_template_actions_have_accessible_names(self):
        violations = []
        for path in TEMPLATES.rglob("*.html"):
            parser = _IconOnlyActionParser()
            parser.feed(path.read_text(encoding="utf-8"))
            violations.extend(
                f"{path.relative_to(ROOT)}:{line} <{tag}>"
                for line, tag in parser.violations
            )
        self.assertEqual(violations, [])

    def test_dynamic_file_actions_include_accessible_names(self):
        template = (TEMPLATES / "live_recording.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="下载文件 ${escapeHtml(file.name)}"', template)
        self.assertIn('aria-label="${file.locked ?', template)

    def test_focus_and_auxiliary_text_floors_are_explicit(self):
        css = REFINEMENT_CSS.read_text(encoding="utf-8")
        self.assertIn("outline: 3px solid var(--pf-signal) !important", css)
        self.assertIn("font-size: max(12px, .75rem)", css)
        self.assertIn("--pf-muted: #9aacc0", css)


if __name__ == "__main__":
    unittest.main()
