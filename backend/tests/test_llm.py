"""Tests for the custom-model harness.

The HTTP provider is exercised against a throwaway local server rather than
mocked, so the request shape and response parsing are actually verified.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mitss.llm import (
    HttpProvider,
    LLMError,
    LLMProvider,
    ManualProvider,
    ProviderUnavailable,
    available_providers,
    get_provider,
    register_provider,
)

# What the stub server should reply with, set per test.
REPLY = {"body": "{}", "status": 200}
RECEIVED = {}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        RECEIVED["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
        RECEIVED["auth"] = self.headers.get("Authorization")
        self.send_response(REPLY["status"])
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(REPLY["body"].encode("utf-8"))

    def log_message(self, *args):
        pass  # keep test output clean


class StubServer:
    def __enter__(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class Manual(unittest.TestCase):
    def test_manual_is_not_available_and_explains_itself(self):
        provider = ManualProvider()
        self.assertFalse(provider.available)
        with self.assertRaises(ProviderUnavailable) as caught:
            provider.complete("anything")
        self.assertIn("paste", str(caught.exception))

    def test_manual_is_the_default(self):
        os.environ.pop("MITSS_LLM_PROVIDER", None)
        self.assertIsInstance(get_provider(), ManualProvider)

    def test_unknown_provider_name_falls_back_to_manual(self):
        self.assertIsInstance(get_provider("does-not-exist"), ManualProvider)


class Http(unittest.TestCase):
    def setUp(self):
        for key in ("MITSS_LLM_URL", "MITSS_LLM_API_KEY", "MITSS_LLM_FORMAT",
                    "MITSS_LLM_MODEL", "MITSS_LLM_MODELS"):
            os.environ.pop(key, None)
        RECEIVED.clear()

    def test_unavailable_without_a_url(self):
        provider = HttpProvider()
        self.assertFalse(provider.available)
        with self.assertRaises(ProviderUnavailable):
            provider.complete("hello")

    def test_openai_shape_round_trip(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "the schedule"}}]}
        )
        with StubServer() as url:
            provider = HttpProvider(url=url, fmt="openai", model="my-model")
            self.assertTrue(provider.available)
            self.assertEqual(provider.complete("packet text"), "the schedule")

        self.assertEqual(RECEIVED["body"]["model"], "my-model")
        self.assertEqual(RECEIVED["body"]["messages"][0]["content"], "packet text")

    def test_raw_shape_round_trip(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "raw style reply"})
        with StubServer() as url:
            provider = HttpProvider(url=url, fmt="raw")
            self.assertEqual(provider.complete("packet text"), "raw style reply")
        self.assertEqual(RECEIVED["body"]["prompt"], "packet text")

    def test_plain_text_response_is_accepted(self):
        REPLY["status"] = 200
        REPLY["body"] = "not json, just the answer"
        with StubServer() as url:
            provider = HttpProvider(url=url)
            self.assertEqual(provider.complete("x"), "not json, just the answer")

    def test_unrecognised_json_shape_is_an_error(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"unexpected": {"nested": "thing"}})
        with StubServer() as url:
            with self.assertRaises(LLMError):
                HttpProvider(url=url).complete("x")

    def test_http_error_is_reported_without_echoing_the_request(self):
        REPLY["status"] = 401
        REPLY["body"] = json.dumps({"error": "bad key"})
        with StubServer() as url:
            os.environ["MITSS_LLM_API_KEY"] = "super-secret-value"
            try:
                with self.assertRaises(LLMError) as caught:
                    HttpProvider(url=url).complete("x")
            finally:
                os.environ.pop("MITSS_LLM_API_KEY", None)
        message = str(caught.exception)
        self.assertIn("401", message)
        self.assertNotIn("super-secret-value", message)

    def test_api_key_is_sent_but_never_described(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "ok"})
        os.environ["MITSS_LLM_API_KEY"] = "super-secret-value"
        try:
            with StubServer() as url:
                provider = HttpProvider(url=url, fmt="raw")
                provider.complete("x")
                described = json.dumps(provider.describe())
            self.assertEqual(RECEIVED["auth"], "Bearer super-secret-value")
            self.assertNotIn("super-secret-value", described)
            self.assertTrue(json.loads(described)["api_key_set"])
        finally:
            os.environ.pop("MITSS_LLM_API_KEY", None)

    def test_model_argument_reaches_the_request_body(self):
        # Regression: the chosen model used to label the run without ever
        # being sent, so the endpoint answered as a different model.
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "ok"})
        with StubServer() as url:
            HttpProvider(url=url, fmt="raw", model="default-model").complete(
                "x", model="team-70b")
        self.assertEqual(RECEIVED["body"]["model"], "team-70b")

    def test_model_argument_falls_back_to_the_configured_default(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "ok"})
        with StubServer() as url:
            HttpProvider(url=url, fmt="raw", model="default-model").complete("x")
        self.assertEqual(RECEIVED["body"]["model"], "default-model")

    def test_models_list_is_parsed_in_order_without_duplicates(self):
        os.environ["MITSS_LLM_MODELS"] = " team-7b , team-70b ,team-7b, "
        provider = HttpProvider(url="http://x")
        self.assertEqual(provider.models, ["team-7b", "team-70b"])
        self.assertEqual(provider.describe()["models"], ["team-7b", "team-70b"])

    def test_models_falls_back_to_the_single_model(self):
        provider = HttpProvider(url="http://x", model="only-one")
        self.assertEqual(provider.models, ["only-one"])

    def test_manual_provider_offers_no_models(self):
        self.assertEqual(ManualProvider().describe()["models"], [])

    def test_unreachable_endpoint_is_a_clean_error(self):
        provider = HttpProvider(url="http://127.0.0.1:9/never", timeout=2)
        with self.assertRaises(LLMError):
            provider.complete("x")


class Registry(unittest.TestCase):
    def test_custom_provider_can_be_registered_and_selected(self):
        class EchoProvider(LLMProvider):
            """Echo provider used in tests."""

            name = "echo"

            @property
            def available(self):
                return True

            def complete(self, prompt, model=None):
                return f"echo: {prompt}"

        register_provider("echo", EchoProvider)
        self.assertIn("echo", available_providers())
        provider = get_provider("echo")
        self.assertEqual(provider.complete("hi"), "echo: hi")

    def test_registering_a_non_provider_is_rejected(self):
        with self.assertRaises(TypeError):
            register_provider("bad", dict)

    def test_builtin_providers_listed(self):
        names = available_providers()
        self.assertIn("manual", names)
        self.assertIn("http", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
