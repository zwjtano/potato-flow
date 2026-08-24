import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.ai_result_cache import (
    AiResultCache,
    build_result_cache_key,
    resolve_result_cache_mode,
)
from modules.prompt_registry import (
    get_prompt_spec,
    is_valid_cache_result,
    resolve_scene_request,
)


class AiResultCacheTests(unittest.TestCase):
    def test_cache_round_trip_expiry_and_no_raw_input_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "results.sqlite3"
            cache = AiResultCache(path)
            key = build_result_cache_key(
                scene_name="subtitle_qc",
                prompt_version="v1",
                provider_bucket="provider",
                model_name="model",
                system_prompt="private system",
                user_content={"subtitle": "private subtitle"},
                temperature=0.0,
                max_tokens=120,
                thinking_enabled=False,
            )
            self.assertNotIn("private", key)
            self.assertTrue(cache.put(
                key,
                {"passed": True, "score": 0.9},
                scene_name="subtitle_qc",
                prompt_version="v1",
                model_name="model",
                ttl_seconds=10,
                now=100,
            ))
            self.assertEqual(cache.get(key, now=105), {"passed": True, "score": 0.9})
            self.assertIsNone(cache.get(key, now=111))

            connection = sqlite3.connect(path)
            try:
                count = connection.execute("SELECT COUNT(*) FROM ai_result_cache").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 0)

    def test_prompt_version_and_model_invalidate_key(self):
        base = dict(
            scene_name="scene",
            prompt_version="v1",
            provider_bucket="provider",
            model_name="model-a",
            system_prompt="system",
            user_content="input",
            temperature=0.0,
            max_tokens=100,
            thinking_enabled=False,
        )
        first = build_result_cache_key(**base)
        self.assertNotEqual(first, build_result_cache_key(**{**base, "prompt_version": "v2"}))
        self.assertNotEqual(first, build_result_cache_key(**{**base, "model_name": "model-b"}))

    def test_bypass_disables_reads_and_writes(self):
        self.assertEqual(resolve_result_cache_mode({"OPENAI_RESULT_CACHE_BYPASS": True}), "off")

    def test_fill_lease_is_cross_connection_and_owner_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = AiResultCache(Path(temporary) / "results.sqlite3")
            owner = cache.acquire_fill_lease("same", lease_seconds=10, now=100)
            self.assertTrue(owner)
            self.assertEqual(cache.acquire_fill_lease("same", now=101), "")
            cache.release_fill_lease("same", "wrong-owner")
            self.assertEqual(cache.acquire_fill_lease("same", now=102), "")
            cache.release_fill_lease("same", owner)
            self.assertTrue(cache.acquire_fill_lease("same", now=103))


class PromptRegistryTests(unittest.TestCase):
    def test_registered_scene_routes_model_and_caps_budget(self):
        model, budget, spec = resolve_scene_request(
            {
                "OPENAI_CLASSIFICATION_MODEL_NAME": "small-classifier",
                "OPENAI_SCENE_TOKEN_BUDGETS": {"ai_enhancer_tags": 80},
            },
            scene_name="ai_enhancer_tags",
            model_name="expensive-default",
            max_tokens=160,
        )
        self.assertEqual(model, "small-classifier")
        self.assertEqual(budget, 80)
        self.assertTrue(spec.cacheable)

    def test_creative_retry_scene_is_not_accidentally_cacheable(self):
        spec = get_prompt_spec("recording_danmaku_title_from_description_retry_json")
        self.assertFalse(spec.cacheable)
        self.assertEqual(spec.version, "unversioned")

    def test_scene_contract_rejects_parseable_but_invalid_json(self):
        spec = get_prompt_spec("subtitle_translate_batch")
        self.assertFalse(is_valid_cache_result(spec, {"items": []}))
        self.assertFalse(is_valid_cache_result(spec, {"translations": "wrong-type"}))
        self.assertTrue(is_valid_cache_result(spec, {"translations": ["字幕"]}))


class AiEnhancerResultCacheIntegrationTests(unittest.TestCase):
    def test_second_valid_json_request_skips_provider_and_marks_ledger(self):
        from modules import ai_enhancer
        from modules.ai_result_cache import AiResultCache

        response = SimpleNamespace(
            id="provider-1",
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"tags":["录播"]}', parsed=None)
            )],
        )
        create = Mock(return_value=response)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        config = {
            "OPENAI_API_KEY": "secret",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "OPENAI_MODEL_NAME": "model",
            "OPENAI_RESULT_CACHE_MODE": "auto",
            "OPENAI_RESULT_CACHE_TTL_HOURS": 24,
            "OPENAI_RPM_LIMIT": 0,
            "OPENAI_REQUEST_MAX_RETRIES": 0,
        }

        with tempfile.TemporaryDirectory() as temporary:
            result_cache = AiResultCache(Path(temporary) / "results.sqlite3")
            ledger_path = Path(temporary) / "ledger.sqlite3"
            ai_enhancer._OPENAI_CLIENT_CONFIGS[id(client)] = dict(config)
            try:
                with patch.object(ai_enhancer, "get_ai_result_cache", return_value=result_cache), patch.dict(
                    "os.environ", {"POTATOFLOW_AI_LEDGER_PATH": str(ledger_path)}, clear=False
                ):
                    first = ai_enhancer._request_json_object(
                        client,
                        "model",
                        "stable system",
                        {"title": "same"},
                        max_tokens=160,
                        temperature=0.2,
                        thinking_enabled=False,
                        logger_obj=None,
                        scene_name="ai_enhancer_tags",
                    )
                    second = ai_enhancer._request_json_object(
                        client,
                        "model",
                        "stable system",
                        {"title": "same"},
                        max_tokens=160,
                        temperature=0.2,
                        thinking_enabled=False,
                        logger_obj=None,
                        scene_name="ai_enhancer_tags",
                    )
            finally:
                ai_enhancer._OPENAI_CLIENT_CONFIGS.pop(id(client), None)
                for key in tuple(ai_enhancer._OPENAI_CLIENT_REQUEST_POLICIES):
                    if key[0] == id(client):
                        ai_enhancer._OPENAI_CLIENT_REQUEST_POLICIES.pop(key, None)

            connection = sqlite3.connect(ledger_path)
            try:
                rows = connection.execute(
                    """SELECT application_cache_hit, physical_request_count
                       FROM ai_runs ORDER BY created_at"""
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(first, {"tags": ["录播"]})
        self.assertEqual(second, first)
        create.assert_called_once()
        self.assertEqual(rows, [(0, 1), (1, 0)])

    def test_concurrent_identical_requests_are_single_flight(self):
        from modules import ai_enhancer
        from modules.ai_result_cache import AiResultCache

        response = SimpleNamespace(
            id="provider-1",
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"tags":["录播"]}', parsed=None)
            )],
        )

        def slow_create(**kwargs):
            time.sleep(0.15)
            return response

        create = Mock(side_effect=slow_create)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        config = {
            "OPENAI_API_KEY": "secret",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "OPENAI_MODEL_NAME": "model",
            "OPENAI_RESULT_CACHE_MODE": "auto",
            "OPENAI_RESULT_CACHE_TTL_HOURS": 24,
            "OPENAI_RESULT_CACHE_WAIT_SECONDS": 2,
            "OPENAI_RPM_LIMIT": 0,
            "OPENAI_REQUEST_MAX_RETRIES": 0,
        }

        with tempfile.TemporaryDirectory() as temporary:
            result_cache = AiResultCache(Path(temporary) / "results.sqlite3")
            ledger_path = Path(temporary) / "ledger.sqlite3"
            ai_enhancer._OPENAI_CLIENT_CONFIGS[id(client)] = dict(config)
            try:
                with patch.object(ai_enhancer, "get_ai_result_cache", return_value=result_cache), patch.dict(
                    "os.environ", {"POTATOFLOW_AI_LEDGER_PATH": str(ledger_path)}, clear=False
                ):
                    def request():
                        return ai_enhancer._request_json_object(
                            client,
                            "model",
                            "stable system",
                            {"title": "same"},
                            max_tokens=160,
                            temperature=0.2,
                            thinking_enabled=False,
                            logger_obj=None,
                            scene_name="ai_enhancer_tags",
                        )

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(lambda _: request(), range(2)))
            finally:
                ai_enhancer._OPENAI_CLIENT_CONFIGS.pop(id(client), None)
                for key in tuple(ai_enhancer._OPENAI_CLIENT_REQUEST_POLICIES):
                    if key[0] == id(client):
                        ai_enhancer._OPENAI_CLIENT_REQUEST_POLICIES.pop(key, None)

        self.assertEqual(results, [{"tags": ["录播"]}, {"tags": ["录播"]}])
        create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
