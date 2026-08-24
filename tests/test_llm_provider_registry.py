import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.llm_provider_registry import (
    ProviderHealthStore,
    get_last_provider_route,
    preferred_available_profile,
    resolve_provider_profiles,
)


def _profiles_config():
    return {
        "OPENAI_API_KEY": "legacy",
        "OPENAI_MODEL_NAME": "legacy-model",
        "OPENAI_PROVIDER_LEGACY_FALLBACK_ENABLED": False,
        "OPENAI_PROVIDER_1_ENABLED": True,
        "OPENAI_PROVIDER_1_ID": "primary",
        "OPENAI_PROVIDER_1_API_KEY": "primary-key",
        "OPENAI_PROVIDER_1_BASE_URL": "https://primary.test/v1",
        "OPENAI_PROVIDER_1_MODEL_NAME": "primary-model",
        "OPENAI_PROVIDER_1_RPM_LIMIT": 0,
        "OPENAI_PROVIDER_1_SCENES": "ai_enhancer_tags",
        "OPENAI_PROVIDER_2_ENABLED": True,
        "OPENAI_PROVIDER_2_ID": "fallback",
        "OPENAI_PROVIDER_2_API_KEY": "fallback-key",
        "OPENAI_PROVIDER_2_BASE_URL": "https://fallback.test/v1",
        "OPENAI_PROVIDER_2_MODEL_NAME": "fallback-model",
        "OPENAI_PROVIDER_2_RPM_LIMIT": 0,
        "OPENAI_PROVIDER_2_SCENES": "*",
        "OPENAI_PROMPT_CACHE_MODE": "off",
        "OPENAI_REQUEST_MAX_RETRIES": 0,
        "OPENAI_RPM_LIMIT": 0,
    }


class LlmProviderRegistryTests(unittest.TestCase):
    def test_profiles_are_ordered_and_filtered_by_scene(self):
        config = _profiles_config()
        self.assertEqual(
            [profile.profile_id for profile in resolve_provider_profiles(
                config, scene_name="ai_enhancer_tags"
            )],
            ["primary", "fallback"],
        )
        self.assertEqual(
            [profile.profile_id for profile in resolve_provider_profiles(
                config, scene_name="subtitle_qc"
            )],
            ["fallback"],
        )

    def test_legacy_config_is_automatic_compatibility_profile(self):
        profiles = resolve_provider_profiles({
            "OPENAI_API_KEY": "legacy-key",
            "OPENAI_BASE_URL": "https://legacy.test/v1",
            "OPENAI_MODEL_NAME": "legacy-model",
        }, scene_name="anything")
        self.assertEqual(len(profiles), 1)
        self.assertTrue(profiles[0].legacy)
        self.assertEqual(profiles[0].profile_id, "legacy-default")

    def test_cross_process_health_circuit_selects_next_profile(self):
        profiles = resolve_provider_profiles(_profiles_config(), scene_name="ai_enhancer_tags")
        with tempfile.TemporaryDirectory() as temporary:
            health = ProviderHealthStore(Path(temporary) / "health.sqlite3")
            self.assertEqual(preferred_available_profile(profiles, health), profiles[0])
            health.mark_unavailable(
                profiles[0], cooldown_seconds=60, reason="RateLimitError", now=time.time()
            )
            self.assertEqual(
                preferred_available_profile(profiles, health),
                profiles[1],
            )

    def test_scene_mismatch_does_not_reenable_disabled_legacy_fallback(self):
        config = _profiles_config()
        config["OPENAI_PROVIDER_2_SCENES"] = "subtitle_qc"
        self.assertEqual(
            resolve_provider_profiles(config, scene_name="metadata_translation"),
            (),
        )

    def test_replacing_key_gets_a_fresh_health_circuit(self):
        config = _profiles_config()
        first = resolve_provider_profiles(config, scene_name="ai_enhancer_tags")[0]
        config["OPENAI_PROVIDER_1_API_KEY"] = "replacement-key"
        replacement = resolve_provider_profiles(config, scene_name="ai_enhancer_tags")[0]
        self.assertNotEqual(first.health_key, replacement.health_key)


class LlmProviderFailoverIntegrationTests(unittest.TestCase):
    def test_429_switches_profile_and_records_actual_route(self):
        from modules import ai_enhancer

        class RateLimitError(RuntimeError):
            status_code = 429

        primary_create = Mock(side_effect=RateLimitError("quota exhausted"))
        fallback_response = SimpleNamespace(
            id="fallback-response",
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        )
        fallback_create = Mock(return_value=fallback_response)
        primary_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=primary_create))
        )
        fallback_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fallback_create))
        )
        config = _profiles_config()
        ai_enhancer._OPENAI_CLIENT_CONFIGS[id(primary_client)] = dict(config)
        with tempfile.TemporaryDirectory() as temporary:
            health = ProviderHealthStore(Path(temporary) / "health.sqlite3")
            try:
                with patch.object(
                    ai_enhancer, "get_provider_health_store", return_value=health
                ), patch.object(
                    ai_enhancer, "get_openai_client", return_value=fallback_client
                ):
                    response = ai_enhancer._request_chat_completion(
                        primary_client,
                        "legacy-model",
                        "system",
                        {"input": "value"},
                        max_tokens=50,
                        temperature=0.0,
                        thinking_enabled=True,
                        logger_obj=None,
                        scene_name="ai_enhancer_tags",
                        logical_request_id="test-run",
                        request_ledger=None,
                    )
            finally:
                ai_enhancer._OPENAI_CLIENT_CONFIGS.pop(id(primary_client), None)

        self.assertIs(response, fallback_response)
        primary_create.assert_called_once()
        fallback_create.assert_called_once()
        self.assertEqual(fallback_create.call_args.kwargs["model"], "fallback-model")
        self.assertEqual(get_last_provider_route()["profile_id"], "fallback")


if __name__ == "__main__":
    unittest.main()
