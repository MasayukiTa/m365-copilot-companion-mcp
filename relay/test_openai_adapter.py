"""Unit tests for relay.openai_adapter -- NO live Copilot required.

Runs with plain stdlib `unittest` (the repo has no pytest installed):
    python relay/test_openai_adapter.py

A FAKE relay is injected by monkeypatching the module's blocking completion
entry point (`_run_completion_blocking`) so no Playwright / Edge / CDP is
touched. Covers:
  * message flattening (string content AND list-of-parts content)
  * the OpenAI chat.completion response JSON shape
  * auth rejection (401 on missing/wrong bearer)
"""

import os
import sys
import unittest

# Ensure repo root is importable when run directly as `python relay/test_...py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# MCP_API_KEY must be set before anything reads it; the adapter reads it lazily
# per-request via os.environ, so setting it here is enough.
os.environ.setdefault("MCP_API_KEY", "unit-test-key")
os.environ["OPENAI_COMPAT"] = "1"  # so register_openai_routes mounts the routes

from starlette.applications import Starlette          # noqa: E402
from starlette.testclient import TestClient            # noqa: E402

import relay.openai_adapter as adapter                 # noqa: E402

API_KEY = os.environ["MCP_API_KEY"]
AUTH = {"Authorization": f"Bearer {API_KEY}"}


def _make_client(fake_reply="CANNED REPLY from fake relay"):
    """Build a Starlette app with the adapter routes and a FAKE relay.

    The fake replaces `_run_completion_blocking` so the request path never
    constructs a real driver. It records the prompt it was handed for assertions.
    """
    captured = {}

    def fake_blocking(prompt, timeout_s):
        captured["prompt"] = prompt
        captured["timeout_s"] = timeout_s
        return fake_reply

    adapter._run_completion_blocking = fake_blocking  # monkeypatch

    app = Starlette()
    app.router.routes.extend(adapter.openai_routes())
    return TestClient(app), captured


class TestFlattenMessages(unittest.TestCase):
    def test_string_content(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello there"},
        ]
        out = adapter.flatten_messages(msgs)
        self.assertIn("## System", out)
        self.assertIn("You are helpful.", out)
        self.assertIn("## User", out)
        self.assertIn("Hello there", out)
        # ends with an assistant cue so Copilot answers the latest user turn
        self.assertTrue(out.rstrip().endswith("## Assistant"))

    def test_list_of_parts_content(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "part one"},
                    {"type": "text", "text": "part two"},
                ],
            }
        ]
        out = adapter.flatten_messages(msgs)
        self.assertIn("part one", out)
        self.assertIn("part two", out)

    def test_non_text_part_is_placeholdered(self):
        msgs = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "x"}},
            {"type": "text", "text": "describe it"},
        ]}]
        out = adapter.flatten_messages(msgs)
        self.assertIn("describe it", out)
        self.assertIn("non-text content part", out)

    def test_content_to_text_none(self):
        self.assertEqual(adapter._content_to_text(None), "")


class TestResponseShape(unittest.TestCase):
    def test_chat_completion_json_shape(self):
        client, captured = _make_client(fake_reply="42 is the answer")
        resp = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "m365-copilot-opus",
                "messages": [{"role": "user", "content": "what is 6*7?"}],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        # top-level shape
        self.assertEqual(body["object"], "chat.completion")
        self.assertTrue(body["id"].startswith("chatcmpl-"))
        self.assertIn("created", body)
        self.assertEqual(body["model"], "m365-copilot-opus")
        # choices
        self.assertEqual(len(body["choices"]), 1)
        choice = body["choices"][0]
        self.assertEqual(choice["index"], 0)
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"]["role"], "assistant")
        self.assertEqual(choice["message"]["content"], "42 is the answer")
        # usage block present with integer counts
        usage = body["usage"]
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self.assertIsInstance(usage[k], int)
        self.assertEqual(
            usage["total_tokens"],
            usage["prompt_tokens"] + usage["completion_tokens"],
        )
        # the fake relay actually received the flattened prompt
        self.assertIn("## User", captured["prompt"])
        self.assertIn("what is 6*7?", captured["prompt"])

    def test_models_endpoint(self):
        client, _ = _make_client()
        resp = client.get("/v1/models", headers=AUTH)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["object"], "list")
        self.assertEqual(body["data"][0]["id"], adapter.MODEL_ID)

    def test_stream_returns_400(self):
        client, _ = _make_client()
        resp = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        self.assertEqual(resp.status_code, 400)

    def test_empty_messages_400(self):
        client, _ = _make_client()
        resp = client.post(
            "/v1/chat/completions", headers=AUTH, json={"messages": []}
        )
        self.assertEqual(resp.status_code, 400)


class TestAuth(unittest.TestCase):
    def test_missing_bearer_rejected(self):
        client, _ = _make_client()
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(resp.status_code, 401)

    def test_wrong_bearer_rejected(self):
        client, _ = _make_client()
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer WRONG"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(resp.status_code, 401)

    def test_models_requires_auth(self):
        client, _ = _make_client()
        resp = client.get("/v1/models")  # no auth header
        self.assertEqual(resp.status_code, 401)

    def test_correct_bearer_accepted(self):
        client, _ = _make_client()
        resp = client.get("/v1/models", headers=AUTH)
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
