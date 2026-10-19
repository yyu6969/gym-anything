from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

from agents.shared import llm_clients


class VLMClientOptionsTests(unittest.TestCase):
    def _response(self, *, content: str = "ok", reasoning: str | None = None):
        message = SimpleNamespace(content=content)
        if reasoning is not None:
            message.reasoning = reasoning
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def test_call_llm_uses_optional_vlm_api_key_and_preserves_model(self) -> None:
        for configured_key in (None, "", "test-dashscope-key"):
            with self.subTest(configured_key=configured_key):
                create = mock.Mock(return_value=self._response())
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                settings = {
                    "VLM_BASE_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                    "OPENAI_API_KEY": "unrelated-provider-key",
                    "VLM_MODEL": "unrelated-global-model",
                }
                if configured_key is not None:
                    settings["VLM_API_KEY"] = configured_key
                with mock.patch.dict(os.environ, settings, clear=True), \
                     mock.patch.object(llm_clients.openai, "OpenAI", return_value=client) as constructor:
                    result = llm_clients.call_llm(
                        [{"role": "user", "content": "hi"}], "qwen3.5-plus", 1.0, 0.95,
                    )
                constructor.assert_called_once_with(
                    base_url=settings["VLM_BASE_URL"], api_key=configured_key or "EMPTY",
                )
                self.assertEqual(create.call_args.kwargs["model"], "qwen3.5-plus")
                self.assertEqual(result, "ok")

    def test_call_llm_preserves_unauthenticated_local_defaults(self) -> None:
        create = mock.Mock(return_value=self._response())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(llm_clients.openai, "OpenAI", return_value=client) as constructor:
            llm_clients.call_llm([{"role": "user", "content": "hi"}], "local-model", 1.0, 0.95)
        constructor.assert_called_once_with(base_url="http://localhost:8080/v1", api_key="EMPTY")

    def test_call_llm_passes_disable_thinking_chat_template_kwargs(self) -> None:
        create = mock.Mock(return_value=self._response())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with mock.patch.object(llm_clients.openai, "OpenAI", return_value=client), \
             mock.patch.dict("os.environ", {"VLM_DISABLE_THINKING": "1"}, clear=False):
            result = llm_clients.call_llm(
                [{"role": "user", "content": "hi"}],
                "Qwen/Qwen3.5-2B",
                1.0,
                0.95,
                top_k=20,
                max_tokens=32,
            )

        self.assertEqual(result, "ok")
        extra_body = create.call_args.kwargs["extra_body"]
        self.assertEqual(extra_body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(extra_body["top_k"], 20)

    def test_call_llm_passes_session_id_in_extra_body(self) -> None:
        create = mock.Mock(return_value=self._response())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with mock.patch.object(llm_clients.openai, "OpenAI", return_value=client):
            result = llm_clients.call_llm(
                [{"role": "user", "content": "hi"}],
                "qwen3.5-dsl",
                1.0,
                0.95,
                top_k=20,
                max_tokens=32,
                session_id="session-123",
            )

        self.assertEqual(result, "ok")
        self.assertEqual(create.call_args.kwargs["extra_body"]["session_id"], "session-123")

    def test_call_llm_fails_if_disable_thinking_response_contains_reasoning(self) -> None:
        create = mock.Mock(return_value=self._response(reasoning="hidden"))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with mock.patch.object(llm_clients.openai, "OpenAI", return_value=client), \
             mock.patch.dict("os.environ", {"VLM_DISABLE_THINKING": "1"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "VLM_DISABLE_THINKING"):
                llm_clients.call_llm(
                    [{"role": "user", "content": "hi"}],
                    "Qwen/Qwen3.5-2B",
                    1.0,
                    0.95,
                    max_tokens=32,
                )

    def test_call_llm_does_not_retry_bad_request(self) -> None:
        response = httpx.Response(
            400,
            request=httpx.Request("POST", "http://localhost/v1/chat/completions"),
        )
        error = llm_clients.openai.BadRequestError(
            "context length exceeded",
            response=response,
            body={"error": "context length exceeded"},
        )
        create = mock.Mock(side_effect=error)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with mock.patch.object(llm_clients.openai, "OpenAI", return_value=client), \
             mock.patch.object(llm_clients.time, "sleep") as sleep:
            with self.assertRaises(llm_clients.openai.BadRequestError):
                llm_clients.call_llm(
                    [{"role": "user", "content": "hi"}],
                    "Qwen/Qwen3.5-2B",
                    1.0,
                    0.95,
                    max_tokens=32,
                )

        create.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
