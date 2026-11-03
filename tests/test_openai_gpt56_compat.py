import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from modules.utils import openai_chat_create_with_thinking_control


class OfficialGPT56CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.create = Mock(return_value="response")
        self.client = SimpleNamespace(
            base_url="https://api.openai.com/v1/",
            chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)),
        )
        self.kwargs = {
            "model": "gpt-5.6-terra",
            "messages": [{"role": "user", "content": "Summarize this recording."}],
            "max_tokens": 512,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

    def test_official_policy_opt_out_still_normalizes_and_disables_reasoning(self):
        original = copy.deepcopy(self.kwargs)
        result = openai_chat_create_with_thinking_control(
            self.client, self.kwargs, thinking_enabled=False,
            attempt_disable_thinking=False,
        )
        sent = self.create.call_args.kwargs
        self.assertEqual(result, "response")
        self.assertNotIn("max_tokens", sent)
        self.assertEqual(sent["max_completion_tokens"], 512)
        self.assertEqual(sent["reasoning_effort"], "none")
        self.assertNotIn("extra_body", sent)
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertEqual(self.kwargs, original)
        self.create.assert_called_once()

    def test_custom_provider_callback_receives_normalized_request(self):
        callback = Mock(return_value="routed")
        result = openai_chat_create_with_thinking_control(
            self.client, self.kwargs, create_callable=callback,
        )
        self.assertEqual(result, "routed")
        self.assertEqual(callback.call_args.args[0]["max_completion_tokens"], 512)
        self.assertNotIn("max_tokens", callback.call_args.args[0])
        self.create.assert_not_called()

    def test_explicit_limit_and_reasoning_effort_are_preserved(self):
        kwargs = dict(self.kwargs, max_completion_tokens=1024,
                      reasoning_effort="high", top_p=0.8, logprobs=True,
                      top_logprobs=2)
        openai_chat_create_with_thinking_control(
            self.client, kwargs, thinking_enabled=True,
        )
        sent = self.create.call_args.kwargs
        self.assertEqual(sent["max_completion_tokens"], 1024)
        self.assertEqual(sent["reasoning_effort"], "high")
        for parameter in ("max_tokens", "temperature", "top_p", "logprobs", "top_logprobs"):
            self.assertNotIn(parameter, sent)

    def test_remove_vendor_thinking_without_mutating_other_extra_body_fields(self):
        kwargs = dict(self.kwargs, extra_body={
            "thinking": {"enabled": True}, "custom_option": "keep",
        })
        original = copy.deepcopy(kwargs)
        openai_chat_create_with_thinking_control(self.client, kwargs)
        self.assertEqual(self.create.call_args.kwargs["extra_body"],
                         {"custom_option": "keep"})
        self.assertEqual(kwargs, original)

    def test_third_party_endpoint_keeps_legacy_behavior(self):
        self.client.base_url = "https://compatible.example/v1"
        openai_chat_create_with_thinking_control(self.client, self.kwargs)
        sent = self.create.call_args.kwargs
        self.assertEqual(sent["max_tokens"], 512)
        self.assertNotIn("max_completion_tokens", sent)
        self.assertEqual(sent["extra_body"]["thinking"]["type"], "disabled")

    def test_other_official_models_are_unchanged(self):
        kwargs = dict(self.kwargs, model="gpt-4.1")
        openai_chat_create_with_thinking_control(
            self.client, kwargs, thinking_enabled=True,
        )
        self.create.assert_called_once_with(**kwargs)

    def test_official_auth_errors_are_not_retried_with_legacy_parameters(self):
        self.create.side_effect = RuntimeError("401 invalid_api_key")
        with self.assertRaisesRegex(RuntimeError, "401"):
            openai_chat_create_with_thinking_control(self.client, self.kwargs)
        self.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
