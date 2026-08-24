import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.llm_orchestrator import (
    AiRequestGovernor,
    LLMRequestPolicy,
    apply_prompt_cache_policy,
    resolve_llm_request_policy,
)


class LLMRequestPolicyTests(unittest.TestCase):
    def test_official_gpt56_uses_explicit_cache(self):
        policy = resolve_llm_request_policy({
            "OPENAI_API_KEY": "secret",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "OPENAI_MODEL_NAME": "gpt-5.6-sol",
        })
        self.assertEqual(policy.prompt_cache_mode, "explicit")
        self.assertEqual(policy.rpm_limit, 30)
        self.assertEqual(policy.prompt_cache_key_shards, 2)

    def test_compatible_endpoint_is_opt_in(self):
        policy = resolve_llm_request_policy({
            "OPENAI_BASE_URL": "https://compatible.example/v1",
            "OPENAI_MODEL_NAME": "gpt-5.6-sol",
        })
        self.assertEqual(policy.prompt_cache_mode, "off")

    def test_compatible_endpoint_can_force_explicit_cache(self):
        policy = resolve_llm_request_policy({
            "OPENAI_BASE_URL": "https://compatible.example/v1",
            "OPENAI_MODEL_NAME": "vendor-model",
            "OPENAI_PROMPT_CACHE_MODE": "explicit",
            "OPENAI_RPM_LIMIT": 12,
        })
        self.assertEqual(policy.prompt_cache_mode, "explicit")
        self.assertEqual(policy.rpm_limit, 12)

    def test_explicit_cache_keeps_stable_system_prefix(self):
        policy = LLMRequestPolicy(
            provider_bucket="provider",
            prompt_cache_mode="explicit",
            prompt_cache_ttl="30m",
            prompt_cache_key_shards=1,
            rpm_limit=30,
        )
        first = {
            "model": "gpt-5.6-sol",
            "messages": [
                {"role": "system", "content": "stable rules"},
                {"role": "user", "content": "recording one"},
            ],
        }
        second = {
            "model": "gpt-5.6-sol",
            "messages": [
                {"role": "system", "content": "stable rules"},
                {"role": "user", "content": "recording two"},
            ],
        }
        apply_prompt_cache_policy(
            first, policy,
            model_name="gpt-5.6-sol", scene_name="metadata",
            system_prompt="stable rules", user_content="recording one",
        )
        apply_prompt_cache_policy(
            second, policy,
            model_name="gpt-5.6-sol", scene_name="metadata",
            system_prompt="stable rules", user_content="recording two",
        )

        self.assertEqual(first["prompt_cache_key"], second["prompt_cache_key"])
        self.assertEqual(
            first["messages"][0]["content"][0]["prompt_cache_breakpoint"],
            {"mode": "explicit"},
        )
        self.assertEqual(first["prompt_cache_options"]["ttl"], "30m")


class AiRequestGovernorTests(unittest.TestCase):
    def test_disabled_governor_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "governor.sqlite3"
            governor = AiRequestGovernor(path)
            self.assertEqual(governor.acquire("provider", 0), 0.0)
            self.assertFalse(path.exists())

    def test_governor_waits_until_sliding_window_expires(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "governor.sqlite3"
            governor = AiRequestGovernor(path)
            connection = governor._connect()
            try:
                connection.execute(
                    "INSERT INTO ai_rate_events(bucket, created_at) VALUES (?, ?)",
                    ("provider", 41.0),
                )
                connection.commit()
            finally:
                connection.close()

            with patch("modules.llm_orchestrator.time.time", side_effect=[100.0, 101.1]), patch(
                "modules.llm_orchestrator.time.monotonic", side_effect=[0.0, 1.1]
            ), patch("modules.llm_orchestrator.time.sleep") as sleep:
                waited = governor.acquire("provider", 1)

            sleep.assert_called_once()
            self.assertAlmostEqual(waited, 1.1)
            connection = sqlite3.connect(path)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM ai_rate_events WHERE bucket='provider'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
