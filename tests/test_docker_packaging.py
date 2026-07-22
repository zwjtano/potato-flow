from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DockerPackagingTests(unittest.TestCase):
    def test_compose_exposes_only_unified_port(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('"5001:5001"', compose)
        self.assertNotIn("19159", compose)
        self.assertNotIn("5050", compose)
        self.assertEqual(compose.count("container_name:"), 1)

    def test_image_contains_headless_recorder(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("cargo build --release -p biliup-cli", dockerfile)
        self.assertIn("BILIUP_BIN=/app/upstream-biliup/target/release/biliup", dockerfile)
        self.assertIn("EXPOSE 5001", dockerfile)

    def test_entrypoint_persists_runtime_data(self):
        entrypoint = (ROOT / "deploy" / "docker-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        for directory in ("config", "cookies", "db", "recordings", "temp"):
            self.assertIn(f'"${{DATA_DIR}}/{directory}"', entrypoint)
        self.assertIn("chown -R biliup-y2a:biliup-y2a", entrypoint)
        self.assertIn("exec gosu biliup-y2a", entrypoint)


if __name__ == "__main__":
    unittest.main()
