import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
IGNORED_PARTS = {".venv", "__pycache__", "build", "dist"}


class SubprocessContractTests(unittest.TestCase):
    def test_text_subprocesses_define_utf8_error_handling(self):
        violations: list[str] = []
        for path in sorted(APP_ROOT.rglob("*.py")):
            if any(part in IGNORED_PARTS for part in path.parts):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                keywords = {item.arg: item.value for item in node.keywords if item.arg}
                text_mode = keywords.get("text")
                if not (
                    isinstance(text_mode, ast.Constant)
                    and text_mode.value is True
                ):
                    continue
                missing = [
                    name for name in ("encoding", "errors") if name not in keywords
                ]
                if missing:
                    relative = path.relative_to(ROOT)
                    violations.append(
                        f"{relative}:{node.lineno} missing {', '.join(missing)}"
                    )

        self.assertEqual(
            violations,
            [],
            "Text subprocesses must decode deterministically: " + "; ".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
