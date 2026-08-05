from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DockerPackagingTests(unittest.TestCase):
    def test_compose_exposes_only_unified_port(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('"${POTATOFLOW_PORT:-5001}:5001"', compose)
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
        requirements = (ROOT / "potatoflow-app" / "requirements.lock").read_text(
            encoding="utf-8"
        )
        self.assertIn("cargo build --release -p biliup-cli", dockerfile)
        self.assertIn("RECORDER_BIN=/app/recorder-core/target/release/biliup", dockerfile)
        self.assertIn("EXPOSE 5001", dockerfile)
        self.assertNotIn("chromium", dockerfile.lower())
        self.assertNotIn("playwright", requirements.lower())

    def test_recorder_builder_copies_only_rust_workspace_inputs(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "COPY recorder-core/Cargo.toml recorder-core/Cargo.lock ./",
            dockerfile,
        )
        self.assertIn("COPY recorder-core/.sqlx ./.sqlx", dockerfile)
        self.assertIn("COPY recorder-core/crates ./crates", dockerfile)
        self.assertNotIn("COPY recorder-core/ ./", dockerfile)
        self.assertIn("COPY --chown=potatoflow:potatoflow potatoflow-app/ /app/potatoflow-app/", dockerfile)
        self.assertNotIn("COPY --chown=potatoflow:potatoflow . /app", dockerfile)

        upstream_root = ROOT / "recorder-core"
        for unused_path in (".github", "app", "biliup", "docs", "public", "tauri-app"):
            self.assertFalse((upstream_root / unused_path).exists())

    def test_linux_installer_uses_canonical_runtime_lock(self):
        installer = (ROOT / "ops" / "install-linux.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"${APP_ROOT}/requirements.lock"', installer)
        self.assertNotIn("requirements.txt", installer)
        self.assertIn('LEGACY_APP_ROOT="${ROOT}/y2a-auto"', installer)
        self.assertIn('mv "${legacy_path}" "${canonical_path}"', installer)
        self.assertFalse((ROOT / "potatoflow-app" / "requirements.txt").exists())

    def test_image_contains_only_current_bundled_cover_references(self):
        reference_root = ROOT / "potatoflow-app" / "assets" / "streamer-references"
        reference = reference_root / "guoxiaoguo.png"
        self.assertTrue(reference.is_file())
        self.assertGreater(reference.stat().st_size, 1024)
        self.assertFalse((reference_root / "yyf.png").exists())

    def test_entrypoint_persists_runtime_data(self):
        entrypoint = (ROOT / "ops" / "docker-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        for directory in (
            "config",
            "credentials/cookies",
            "credentials/security",
            "database",
            "state/pipeline",
            "state/recording",
            "recordings",
            "runtime",
        ):
            self.assertIn(f'"${{DATA_DIR}}/{directory}"', entrypoint)
        self.assertIn(
            'link_persistent_path "${DATA_DIR}/recordings" "${APP_DIR}/recordings"',
            entrypoint,
        )
        self.assertIn(
            'link_persistent_path "${APP_DIR}/recordings" "${APP_CODE_DIR}/recordings"',
            entrypoint,
        )
        self.assertIn("umask 0027", entrypoint)
        self.assertIn("-type d -exec chmod 0750", entrypoint)
        self.assertIn("-type f -exec chmod 0640", entrypoint)
        self.assertIn("-type d -exec chmod 0700", entrypoint)
        self.assertIn("-type f -exec chmod 0600", entrypoint)
        self.assertIn('exec gosu "${APP_USER}"', entrypoint)

    def test_potato_flow_branding_and_systemd_service(self):
        base = (ROOT / "potatoflow-app" / "templates" / "base.html").read_text(encoding="utf-8")
        installer = (ROOT / "ops" / "install-systemd.sh").read_text(encoding="utf-8")

        self.assertIn("PotatoFlow · 土豆录播姬", base)
        self.assertIn("img/potato-flow.png", base)
        self.assertIn("img/favicon.png", base)
        self.assertTrue((ROOT / "potatoflow-app" / "static" / "img" / "potato-flow.png").is_file())
        self.assertTrue((ROOT / "potatoflow-app" / "static" / "img" / "favicon.png").is_file())
        self.assertIn('SERVICE_NAME="potato-flow"', installer)
        self.assertTrue((ROOT / "ops" / "potato-flow.service").is_file())

    def test_one_click_docker_installer_uses_safe_domestic_defaults(self):
        installer = (ROOT / "ops" / "install-docker.sh").read_text(encoding="utf-8")

        self.assertIn('MIRROR_MODE="${POTATOFLOW_CHINA_MIRROR:-auto}"', installer)
        self.assertIn('WEB_PORT="${POTATOFLOW_PORT:-5001}"', installer)
        self.assertIn("detect_china_mirror", installer)
        self.assertIn("www.cloudflare.com/cdn-cgi/trace", installer)
        self.assertIn("m.daocloud.io/docker.io/library/rust:bookworm", installer)
        self.assertIn("https://gh-proxy.com/https://github.com/denoland/deno", installer)
        self.assertNotIn("docker.1ms.run", installer)
        self.assertNotIn("ghfast.top", installer)
        self.assertIn("sparse+https://rsproxy.cn/index/", installer)
        self.assertIn("docker-compose-v2", installer)
        self.assertIn('needs_compose=1', installer)
        self.assertIn('run_root env "POTATOFLOW_PORT=${POTATOFLOW_PORT}" docker', installer)
        self.assertIn('compose up -d --no-deps potato-flow', installer)
        self.assertIn('/healthz', installer)
        self.assertNotIn("docker compose down", installer)
        self.assertNotIn("docker-data/recordings --", installer)

    def test_recorder_upload_actor_does_not_block_other_room_sessions(self):
        source = (
            ROOT
            / "recorder-core"
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
            / "recorder-core"
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
