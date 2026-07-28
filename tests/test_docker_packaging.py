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
        self.assertIn(
            '"${POTATO_RECORDINGS_DIR:-./docker-data/recordings}:/data/recordings"',
            compose,
        )

    def test_image_contains_headless_recorder(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        requirements = (ROOT / "y2a-auto" / "requirements.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("cargo build --release -p biliup-cli", dockerfile)
        self.assertIn("BILIUP_BIN=/app/upstream-biliup/target/release/biliup", dockerfile)
        self.assertIn("EXPOSE 5001", dockerfile)
        self.assertNotIn("chromium", dockerfile.lower())
        self.assertNotIn("playwright", requirements.lower())

    def test_image_contains_yyf_cover_reference(self):
        reference_root = ROOT / "assets" / "streamer-references"
        for filename in ("yyf.png", "guoxiaoguo.png"):
            with self.subTest(filename=filename):
                reference = reference_root / filename
                self.assertTrue(reference.is_file())
                self.assertGreater(reference.stat().st_size, 1024)

    def test_entrypoint_persists_runtime_data(self):
        entrypoint = (ROOT / "deploy" / "docker-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        for directory in (
            ".bridge",
            "config",
            "cookies",
            "db",
            "recordings",
            "temp",
        ):
            self.assertIn(f'"${{DATA_DIR}}/{directory}"', entrypoint)
        self.assertIn(
            "for writable_dir in .bridge bridge config cookies db logs security static-covers temp",
            entrypoint,
        )
        self.assertIn(
            'link_persistent_path "${DATA_DIR}/recordings" "${APP_DIR}/recordings"',
            entrypoint,
        )
        self.assertIn(
            'link_persistent_path "${APP_DIR}/recordings" "${Y2A_DIR}/recordings"',
            entrypoint,
        )
        self.assertIn("chown -R biliup-y2a:biliup-y2a", entrypoint)
        self.assertIn("exec gosu biliup-y2a", entrypoint)

    def test_potato_flow_branding_and_systemd_service(self):
        base = (ROOT / "y2a-auto" / "templates" / "base.html").read_text(encoding="utf-8")
        installer = (ROOT / "scripts" / "install-systemd.sh").read_text(encoding="utf-8")

        self.assertIn("PotatoFlow · 土豆录播姬", base)
        self.assertIn("img/potato-flow.png", base)
        self.assertIn("img/favicon.png", base)
        self.assertTrue((ROOT / "y2a-auto" / "static" / "img" / "potato-flow.png").is_file())
        self.assertTrue((ROOT / "y2a-auto" / "static" / "img" / "favicon.png").is_file())
        self.assertIn('SERVICE_NAME="potato-flow"', installer)
        self.assertTrue((ROOT / "deploy" / "potato-flow.service").is_file())

    def test_recorder_upload_actor_does_not_block_other_room_sessions(self):
        source = (
            ROOT
            / "upstream-biliup"
            / "crates"
            / "biliup-cli"
            / "src"
            / "server"
            / "common"
            / "upload.rs"
        ).read_text(encoding="utf-8")

        self.assertIn("tokio::spawn(async move", source)
        self.assertIn("Self::handle_message(msg).await", source)
        self.assertNotIn("self.handle_message(msg).await", source)

    def test_recorder_without_upload_config_still_runs_segment_processors(self):
        source = (
            ROOT
            / "upstream-biliup"
            / "crates"
            / "biliup-cli"
            / "src"
            / "server"
            / "common"
            / "upload.rs"
        ).read_text(encoding="utf-8")

        invocation = (
            "process_without_upload(inspect, &ctx, &segment_processors).await"
        )
        self.assertGreaterEqual(source.count(invocation), 2)
        self.assertIn(
            "No upload config; running segment processors without upload",
            source,
        )


if __name__ == "__main__":
    unittest.main()
