import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from modules.ai_request_ledger import (
    AiRequestLedger,
    ai_task_context,
    input_fingerprint,
    logical_ai_request,
    response_usage,
    estimate_ai_request_cost,
    resolve_ai_model_pricing,
    summarize_task_ai_usage,
)


@contextmanager
def sqlite_connection(path):
    """Use a real closing context; sqlite3.Connection.__exit__ only commits."""
    connection = sqlite3.connect(path)
    try:
        yield connection
    finally:
        connection.close()


class AiRequestLedgerTests(unittest.TestCase):
    def test_extracts_openai_usage_without_storing_content(self):
        response = SimpleNamespace(
            id="req-123",
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                prompt_tokens_details=SimpleNamespace(cached_tokens=60),
            ),
        )
        self.assertEqual(response_usage(response), {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cached_tokens": 60,
        })
        self.assertEqual(len(input_fingerprint("secret prompt")), 64)

    def test_configured_pricing_is_snapshotted_and_costed(self):
        pricing_json = '{"test-model":{"input_per_million":2,"cached_input_per_million":0.5,"output_per_million":8}}'
        usage = {
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "cached_tokens": 400,
        }
        with patch.dict(
            "os.environ", {"POTATOFLOW_AI_PRICING_JSON": pricing_json}, clear=False
        ):
            pricing = resolve_ai_model_pricing("provider", "test-model")
            self.assertEqual(estimate_ai_request_cost(usage, pricing), 0.0054)
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "ledger.sqlite3"
                ledger = AiRequestLedger(path)
                run_id = ledger.begin_run(
                    kind="llm", scene_name="priced", provider="provider",
                    model="test-model", fingerprint="hash", task_id="priced-task",
                )
                request_id, started = ledger.begin_request(run_id, mode="json")
                ledger.finish_request(
                    request_id,
                    started,
                    response=SimpleNamespace(usage={
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "total_tokens": 1500,
                        "input_tokens_details": {"cached_tokens": 400},
                    }),
                )
                ledger.finish_run(run_id, status="completed")
                with sqlite_connection(path) as connection:
                    cost, metadata_json = connection.execute(
                        "SELECT cost_usd, metadata_json FROM ai_requests"
                    ).fetchone()
                with patch.dict(
                    "os.environ",
                    {"POTATOFLOW_AI_LEDGER_PATH": str(path)},
                    clear=False,
                ):
                    summary = summarize_task_ai_usage("priced-task")

        self.assertEqual(cost, 0.0054)
        self.assertEqual(summary["cost_usd"], 0.0054)
        self.assertEqual(
            __import__("json").loads(metadata_json)["pricing_snapshot"], pricing
        )

    def test_records_logical_run_and_physical_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.sqlite3"
            ledger = AiRequestLedger(path)
            run_id = ledger.begin_run(
                kind="llm",
                scene_name="metadata",
                provider="openai_compatible",
                model="test-model",
                fingerprint=input_fingerprint("private content"),
            )
            first_id, first_started = ledger.begin_request(run_id, mode="json")
            ledger.finish_request(
                first_id,
                first_started,
                error=TimeoutError("provider timeout"),
            )
            second_id, second_started = ledger.begin_request(run_id, mode="text")
            ledger.finish_request(
                second_id,
                second_started,
                response=SimpleNamespace(
                    id="provider-2",
                    usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                ),
            )
            ledger.finish_run(run_id, status="completed")

            with sqlite_connection(path) as connection:
                run = connection.execute(
                    "SELECT status, physical_request_count, input_fingerprint FROM ai_runs"
                ).fetchone()
                requests = connection.execute(
                    "SELECT attempt, status, mode, provider_request_id FROM ai_requests ORDER BY attempt"
                ).fetchall()
                schema = " ".join(
                    row[0] for row in connection.execute(
                        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
                    )
                )

        self.assertEqual(run[:2], ("completed", 2))
        self.assertEqual(len(run[2]), 64)
        self.assertEqual(requests, [
            (1, "failed", "json", ""),
            (2, "completed", "text", "provider-2"),
        ])
        self.assertNotIn("private content", schema)

    def test_retention_prunes_only_terminal_expired_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.sqlite3"
            ledger = AiRequestLedger(path)
            expired = ledger.begin_run(
                kind="llm", scene_name="expired", provider="test",
                model="test", fingerprint="old",
            )
            active = ledger.begin_run(
                kind="llm", scene_name="active", provider="test",
                model="test", fingerprint="active",
            )
            ledger.finish_run(expired, status="completed")
            with sqlite_connection(path) as connection:
                connection.execute(
                    "UPDATE ai_runs SET created_at=1, completed_at=2 WHERE id=?",
                    (expired,),
                )
                connection.execute(
                    "UPDATE ai_runs SET created_at=1 WHERE id=?", (active,)
                )
            removed = ledger.prune_expired(30, now=100 * 86400)
            with sqlite_connection(path) as connection:
                remaining = connection.execute(
                    "SELECT id, status FROM ai_runs ORDER BY id"
                ).fetchall()

        self.assertEqual(removed, 1)
        self.assertEqual(remaining, [(active, "running")])

    def test_assigns_unique_attempts_to_parallel_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = AiRequestLedger(Path(temporary) / "ledger.sqlite3")
            run_id = ledger.begin_run(
                kind="llm",
                scene_name="parallel_batches",
                provider="test",
                model="test",
                fingerprint="fingerprint",
            )
            barrier = threading.Barrier(6)
            errors = []

            def create_request():
                try:
                    barrier.wait()
                    request_id, started = ledger.begin_request(run_id, mode="json")
                    ledger.finish_request(request_id, started, response={"id": request_id})
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=create_request) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            ledger.finish_run(run_id, status="completed")
            with sqlite_connection(ledger.path) as connection:
                attempts = [
                    row[0] for row in connection.execute(
                        "SELECT attempt FROM ai_requests ORDER BY attempt"
                    )
                ]

        self.assertEqual(errors, [])
        self.assertEqual(attempts, [1, 2, 3, 4, 5, 6])

    def test_recreates_schema_when_ledger_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.sqlite3"
            ledger = AiRequestLedger(path)
            ledger.begin_run(
                kind="llm",
                scene_name="before_replace",
                provider="test",
                model="test",
                fingerprint="first",
            )
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                candidate.unlink(missing_ok=True)

            replacement_run = ledger.begin_run(
                kind="llm",
                scene_name="after_replace",
                provider="test",
                model="test",
                fingerprint="second",
            )
            with sqlite_connection(path) as connection:
                rows = connection.execute(
                    "SELECT id, scene_name FROM ai_runs"
                ).fetchall()

        self.assertEqual(rows, [(replacement_run, "after_replace")])

    def test_json_fallback_shares_one_logical_run(self):
        fake_openai = types.ModuleType("openai")
        previous_openai = sys.modules.get("openai")
        previous_enhancer = sys.modules.pop("modules.ai_enhancer", None)
        sys.modules["openai"] = fake_openai
        try:
            from modules import ai_enhancer

            response = SimpleNamespace(
                id="provider-fallback",
                usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content='{"description":"ok"}', parsed=None)
                )],
            )
            with tempfile.TemporaryDirectory() as temporary, patch.dict(
                "os.environ",
                {"POTATOFLOW_AI_LEDGER_PATH": str(Path(temporary) / "ledger.sqlite3")},
            ), patch.object(
                ai_enhancer,
                "openai_chat_create_with_thinking_control",
                side_effect=[TimeoutError("json timeout"), response],
            ):
                parsed = ai_enhancer._request_json_object(
                    object(),
                    "test-model",
                    "system",
                    {"private": "payload"},
                    temperature=0.2,
                    thinking_enabled=False,
                    logger_obj=None,
                    scene_name="metadata_test",
                )
                with sqlite_connection(Path(temporary) / "ledger.sqlite3") as connection:
                    runs = connection.execute(
                        "SELECT scene_name, status, physical_request_count FROM ai_runs"
                    ).fetchall()
                    requests = connection.execute(
                        "SELECT attempt, status, mode FROM ai_requests ORDER BY attempt"
                    ).fetchall()
        finally:
            sys.modules.pop("modules.ai_enhancer", None)
            if previous_enhancer is not None:
                sys.modules["modules.ai_enhancer"] = previous_enhancer
            if previous_openai is None:
                sys.modules.pop("openai", None)
            else:
                sys.modules["openai"] = previous_openai

        self.assertEqual(parsed, {"description": "ok"})
        self.assertEqual(runs, [("metadata_test", "completed", 2)])
        self.assertEqual(requests, [
            (1, "failed", "json"),
            (2, "completed", "text"),
        ])

    def test_asr_sdk_request_is_recorded_with_audio_fingerprint(self):
        fake_requests = types.ModuleType("requests")
        fake_requests.post = Mock()
        previous_requests = sys.modules.get("requests")
        previous_asr = sys.modules.pop("modules.asr_api_client", None)
        sys.modules["requests"] = fake_requests
        try:
            from modules import asr_api_client as asr

            with tempfile.TemporaryDirectory() as temporary, patch.dict(
                "os.environ",
                {"POTATOFLOW_AI_LEDGER_PATH": str(Path(temporary) / "ledger.sqlite3")},
            ):
                wav = Path(temporary) / "sample.wav"
                wav.write_bytes(b"audio sample")
                client = asr.AsrApiClient.__new__(asr.AsrApiClient)
                client.config = asr.AsrConfig(
                    provider="whisper",
                    api_key="test",
                    model_name="whisper-test",
                    max_retries=1,
                )
                client.logger = Mock()
                client.task_id = "asr-task-1"
                client.client = SimpleNamespace(audio=SimpleNamespace(
                    transcriptions=SimpleNamespace(create=Mock(return_value=SimpleNamespace(
                        id="asr-provider-1",
                        text="1\n00:00:00,000 --> 00:00:01,000\n测试\n",
                    ))),
                    translations=SimpleNamespace(create=Mock()),
                ))
                client._language_hint = ""
                client._capability_cache = asr._AsrCapabilityCache(
                    transcription_format="srt"
                )
                probe = asr._AsrCapabilityProbeResult(transcription_format="srt")
                with patch.object(client, "_get_or_probe_capabilities", return_value=probe):
                    result = client.transcribe_window(str(wav))

                with sqlite_connection(Path(temporary) / "ledger.sqlite3") as connection:
                    run = connection.execute(
                        "SELECT kind, provider, model, physical_request_count, input_fingerprint, task_id FROM ai_runs"
                    ).fetchone()
                    request = connection.execute(
                        "SELECT mode, provider_request_id, status FROM ai_requests"
                    ).fetchone()
        finally:
            sys.modules.pop("modules.asr_api_client", None)
            if previous_asr is not None:
                sys.modules["modules.asr_api_client"] = previous_asr
            if previous_requests is None:
                sys.modules.pop("requests", None)
            else:
                sys.modules["requests"] = previous_requests

        self.assertEqual(result.timestamp_mode, "srt")
        self.assertEqual(run[:4], ("asr", "whisper", "whisper-test", 1))
        self.assertEqual(len(run[4]), 64)
        self.assertEqual(run[5], "asr-task-1")
        self.assertEqual(request, ("asr_sdk", "asr-provider-1", "completed"))

    def test_task_context_produces_aggregate_usage_summary(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ",
            {"POTATOFLOW_AI_LEDGER_PATH": str(Path(temporary) / "ledger.sqlite3")},
        ):
            with ai_task_context("recording-123"):
                with logical_ai_request(
                    kind="llm",
                    scene_name="summary",
                    provider="test",
                    model="test",
                    fingerprint="hash",
                ) as (ledger, run_id):
                    request_id, started = ledger.begin_request(run_id, mode="json")
                    ledger.finish_request(
                        request_id,
                        started,
                        response=SimpleNamespace(
                            usage={
                                "input_tokens": 20,
                                "output_tokens": 10,
                                "total_tokens": 30,
                                "input_tokens_details": {"cached_tokens": 12},
                            }
                        ),
                    )
            summary = summarize_task_ai_usage("recording-123")

        self.assertEqual(summary["logical_request_count"], 1)
        self.assertEqual(summary["physical_request_count"], 1)
        self.assertEqual(summary["total_tokens"], 30)
        self.assertEqual(summary["cached_tokens"], 12)

    def test_llm_request_continues_when_ledger_is_unavailable(self):
        fake_openai = types.ModuleType("openai")
        previous_openai = sys.modules.get("openai")
        previous_enhancer = sys.modules.pop("modules.ai_enhancer", None)
        sys.modules["openai"] = fake_openai

        @contextmanager
        def unavailable_ledger(**_kwargs):
            yield None, ""

        try:
            from modules import ai_enhancer

            response = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content='{"ok":true}', parsed=None
                ))],
            )
            with patch.object(
                ai_enhancer,
                "logical_ai_request",
                side_effect=unavailable_ledger,
            ), patch.object(
                ai_enhancer,
                "openai_chat_create_with_thinking_control",
                return_value=response,
            ) as request:
                result = ai_enhancer._request_json_object(
                    object(),
                    "model",
                    "system",
                    {"input": True},
                    temperature=0.1,
                    thinking_enabled=False,
                    logger_obj=None,
                    scene_name="ledger_failure",
                )
        finally:
            sys.modules.pop("modules.ai_enhancer", None)
            if previous_enhancer is not None:
                sys.modules["modules.ai_enhancer"] = previous_enhancer
            if previous_openai is None:
                sys.modules.pop("openai", None)
            else:
                sys.modules["openai"] = previous_openai

        self.assertEqual(result, {"ok": True})
        request.assert_called_once()

    def test_openai_clients_are_reused_only_for_matching_configuration(self):
        fake_openai = types.ModuleType("openai")
        created = []

        def create_client(**kwargs):
            client = SimpleNamespace(kwargs=kwargs, close=Mock())
            created.append(client)
            return client

        fake_openai.OpenAI = Mock(side_effect=create_client)
        previous_openai = sys.modules.get("openai")
        previous_enhancer = sys.modules.pop("modules.ai_enhancer", None)
        sys.modules["openai"] = fake_openai
        previous_module_openai = None
        try:
            from modules import ai_enhancer
            previous_module_openai = getattr(ai_enhancer, "openai", None)
            ai_enhancer.openai = fake_openai

            first = ai_enhancer.get_openai_client({
                "OPENAI_API_KEY": "secret-a",
                "OPENAI_BASE_URL": "https://api.example/v1/",
                "OPENAI_TIMEOUT_SECONDS": "60",
            })
            same = ai_enhancer.get_openai_client({
                "OPENAI_API_KEY": "secret-a",
                "OPENAI_BASE_URL": "https://api.example/v1",
                "OPENAI_TIMEOUT_SECONDS": 60,
            })
            different = ai_enhancer.get_openai_client({
                "OPENAI_API_KEY": "secret-b",
                "OPENAI_BASE_URL": "https://api.example/v1",
                "OPENAI_TIMEOUT_SECONDS": 60,
            })
            for index in range(7):
                ai_enhancer.get_openai_client({
                    "OPENAI_API_KEY": f"extra-{index}",
                    "OPENAI_BASE_URL": "https://api.example/v1",
                    "OPENAI_TIMEOUT_SECONDS": 60,
                })
            evicted_closed_before_clear = first.close.called
            ai_enhancer._clear_openai_client_pool()
        finally:
            if previous_module_openai is not None:
                ai_enhancer.openai = previous_module_openai
            sys.modules.pop("modules.ai_enhancer", None)
            if previous_enhancer is not None:
                sys.modules["modules.ai_enhancer"] = previous_enhancer
            if previous_openai is None:
                sys.modules.pop("openai", None)
            else:
                sys.modules["openai"] = previous_openai

        self.assertIs(first, same)
        self.assertIsNot(first, different)
        self.assertEqual(fake_openai.OpenAI.call_count, 9)
        self.assertFalse(evicted_closed_before_clear)
        self.assertFalse(first.close.called)
        self.assertTrue(all(client.close.called for client in created[1:]))


if __name__ == "__main__":
    unittest.main()
