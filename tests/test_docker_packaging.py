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
        self.assertIn("container_name: potato-flow", compose)
        self.assertIn("image: potato-flow:local", compose)

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

    def test_potato_flow_branding_and_systemd_service(self):
        base = (ROOT / "y2a-auto" / "templates" / "base.html").read_text(encoding="utf-8")
        installer = (ROOT / "scripts" / "install-systemd.sh").read_text(encoding="utf-8")

        self.assertIn("PotatoFlow · 土豆录播姬", base)
        self.assertIn("img/potato-flow.svg", base)
        self.assertIn('SERVICE_NAME="potato-flow"', installer)
        self.assertTrue((ROOT / "deploy" / "potato-flow.service").is_file())


if __name__ == "__main__":
    unittest.main()
