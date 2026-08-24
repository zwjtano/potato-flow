import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.image_provider_registry import resolve_image_provider_profiles
from modules.provider_control_plane import (
    ProviderCircuitStore,
    ProviderKind,
    ProviderRegistration,
    ProviderRegistry,
    ProviderRequest,
    build_provider_registry,
)


class ProviderControlPlaneTests(unittest.TestCase):
    def test_registry_matches_kind_capability_billing_and_preference(self):
        registry = ProviderRegistry([
            ProviderRegistration(
                "segment-only", ProviderKind.ASR, "a",
                frozenset({"asr.transcribe", "asr.segment_timestamp"}),
                priority=1, billing_mode="free_only",
            ),
            ProviderRegistration(
                "word-timing", ProviderKind.ASR, "b",
                frozenset({"asr.transcribe", "asr.segment_timestamp", "asr.word_timestamp"}),
                priority=2, billing_mode="free_only",
            ),
        ])
        matched = registry.match(ProviderRequest(
            kind=ProviderKind.ASR,
            required_capabilities=frozenset({"asr.transcribe", "asr.segment_timestamp"}),
            preferred_capabilities=frozenset({"asr.word_timestamp"}),
            constraints={"billing_mode": "free_only"},
        ))
        self.assertEqual([item.provider_id for item in matched], ["segment-only", "word-timing"])

    def test_effective_registry_contains_all_three_provider_kinds(self):
        config = {
            "OPENAI_API_KEY": "llm-key",
            "OPENAI_MODEL_NAME": "text-model",
            "OPENAI_IMAGE_API_KEY": "image-key",
            "OPENAI_IMAGE_MODEL_NAME": "image-model",
            "GROQ_API_KEY": "groq-key",
        }
        registry = build_provider_registry(config)
        self.assertTrue(registry.all(ProviderKind.LLM))
        self.assertTrue(registry.all(ProviderKind.ASR))
        self.assertTrue(registry.all(ProviderKind.IMAGE))

    def test_shared_circuit_store_hides_exhausted_profile(self):
        profile = resolve_image_provider_profiles({
            "OPENAI_IMAGE_API_KEY": "image-key",
        }, scene_name="recording_cover_16x9")[0]
        with tempfile.TemporaryDirectory() as temporary:
            store = ProviderCircuitStore(Path(temporary) / "circuits.sqlite3")
            self.assertTrue(store.is_available(profile))
            store.mark_unavailable(profile, cooldown_seconds=30, reason="quota")
            self.assertFalse(store.is_available(profile))


class ImageProviderRegistryTests(unittest.TestCase):
    def test_profiles_are_ordered_and_legacy_can_be_disabled(self):
        config = {
            "OPENAI_IMAGE_API_KEY": "legacy",
            "IMAGE_PROVIDER_LEGACY_FALLBACK_ENABLED": False,
            "IMAGE_PROVIDER_1_ENABLED": True,
            "IMAGE_PROVIDER_1_ID": "primary",
            "IMAGE_PROVIDER_1_API_KEY": "primary-key",
            "IMAGE_PROVIDER_1_MODEL_NAME": "primary-image",
            "IMAGE_PROVIDER_1_SCENES": "recording_cover_16x9",
            "IMAGE_PROVIDER_2_ENABLED": True,
            "IMAGE_PROVIDER_2_ID": "fallback",
            "IMAGE_PROVIDER_2_API_KEY": "fallback-key",
            "IMAGE_PROVIDER_2_MODEL_NAME": "fallback-image",
            "IMAGE_PROVIDER_2_SCENES": "*",
        }
        profiles = resolve_image_provider_profiles(
            config, scene_name="recording_cover_16x9"
        )
        self.assertEqual([item.profile_id for item in profiles], ["primary", "fallback"])
        self.assertFalse(any(item.legacy for item in profiles))

    def test_unsupported_adapter_is_not_advertised_as_available(self):
        profiles = resolve_image_provider_profiles({
            "IMAGE_PROVIDER_LEGACY_FALLBACK_ENABLED": False,
            "IMAGE_PROVIDER_1_ENABLED": True,
            "IMAGE_PROVIDER_1_API_KEY": "gemini-key",
            "IMAGE_PROVIDER_1_ADAPTER": "gemini-native",
        }, scene_name="recording_cover_16x9")
        self.assertEqual(profiles, ())


if __name__ == "__main__":
    unittest.main()
