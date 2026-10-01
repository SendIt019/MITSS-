"""Tests for the custom-model harness.

The HTTP provider is exercised against a throwaway local server rather than
mocked, so the request shape and response parsing are actually verified.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_folders import use_fake_models_dir

from mitss.llm import (
    HttpProvider,
    LLMConfigError,
    LLMError,
    LLMModelUnavailable,
    LLMProvider,
    LLMServerError,
    LLMStuck,
    LLMTimeout,
    LLMUnreachable,
    ManualProvider,
    ProviderUnavailable,
    available_providers,
    describe_settings,
    get_provider,
    is_local_url,
    models_dir,
    normalize_settings,
    parse_preflight_timeout,
    parse_timeout,
    redact,
    register_provider,
    resolve_model_path,
)

# What the stub server should reply with, set per test. `delay` (seconds)
# makes it sit on the request first, to provoke a client timeout.
REPLY = {"body": "{}", "status": 200, "delay": 0}
RECEIVED = {}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        RECEIVED["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
        RECEIVED.setdefault("bodies", []).append(RECEIVED["body"])
        RECEIVED["auth"] = self.headers.get("Authorization")
        if REPLY.get("delay"):
            time.sleep(REPLY["delay"])
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
                    "MITSS_LLM_MODEL", "MITSS_LLM_MODELS", "MITSS_LLM_TIMEOUT"):
            os.environ.pop(key, None)
        os.environ.pop("MITSS_LLM_PREFLIGHT_TIMEOUT", None)
        RECEIVED.clear()
        REPLY["delay"] = 0
        self.models_dir = use_fake_models_dir(self)

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

        self.assertEqual(RECEIVED["body"]["model"],
                         os.path.join(self.models_dir, "my-model"))
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
        with StubServer() as url, self.assertRaises(LLMError):
            HttpProvider(url=url).complete("x")

    def test_reasoning_is_captured_beside_the_answer(self):
        # mlx_lm.server puts a thinking model's chain of thought in its own
        # field; the harness keeps it, verbatim, apart from the output.
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"choices": [{"message": {
            "role": "assistant", "reasoning": "Let me think...",
            "content": "The answer.",
        }}]})
        with StubServer() as url:
            completion = HttpProvider(url=url).generate("x")
        self.assertEqual(completion.text, "The answer.")
        self.assertEqual(completion.reasoning, "Let me think...")

    def test_reasoning_without_an_answer_is_kept_with_an_empty_output(self):
        # The shape when a thinking model runs out of tokens mid-think: no
        # content at all. That is a result (the cap was too low), not an error.
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"choices": [{
            "finish_reason": "length",
            "message": {"role": "assistant", "reasoning": "Let me think..."},
        }]})
        with StubServer() as url:
            completion = HttpProvider(url=url).generate("x")
        self.assertEqual(completion.text, "")
        self.assertEqual(completion.reasoning, "Let me think...")

    def test_reasoning_content_alias_is_understood(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"choices": [{"message": {
            "reasoning_content": "hmm", "content": "ok",
        }}]})
        with StubServer() as url:
            completion = HttpProvider(url=url).generate("x")
        self.assertEqual((completion.text, completion.reasoning), ("ok", "hmm"))

    # -- what the server reported -------------------------------------------

    def test_usage_is_captured_exactly_as_reported(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({
            "model": "served-model-name",
            "choices": [{"finish_reason": "length",
                         "message": {"content": "out"}}],
            "usage": {"prompt_tokens": 3664, "completion_tokens": 8000,
                      "total_tokens": 11664},
        })
        with StubServer() as url:
            usage = HttpProvider(url=url, preflight=0).generate("x").usage
        self.assertEqual(usage["prompt_tokens"], 3664)
        self.assertEqual(usage["completion_tokens"], 8000)
        self.assertEqual(usage["total_tokens"], 11664)
        self.assertEqual(usage["finish_reason"], "length")
        self.assertEqual(usage["model_reported"], "served-model-name")
        # The provider does not invent a rate; that needs the measured time.
        self.assertNotIn("tokens_per_second", usage)

    def test_usage_is_none_when_the_server_reports_nothing(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "just text"})
        with StubServer() as url:
            self.assertIsNone(HttpProvider(url=url, preflight=0).generate("x").usage)
        REPLY["body"] = "not json at all"
        with StubServer() as url:
            self.assertIsNone(HttpProvider(url=url, preflight=0).generate("x").usage)

    def test_partial_usage_keeps_only_what_was_reported(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({
            "choices": [{"message": {"content": "out"}}],
            "usage": {"completion_tokens": 12, "prompt_tokens": None},
        })
        with StubServer() as url:
            usage = HttpProvider(url=url, preflight=0).generate("x").usage
        self.assertEqual(usage, {"completion_tokens": 12})

    # -- preflight ----------------------------------------------------------

    def test_preflight_sends_a_one_token_probe_for_the_same_model_first(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "ok"})
        with StubServer() as url:
            HttpProvider(url=url, model="m", settings={"max_tokens": 500}).complete("real")
        probe, real = RECEIVED["bodies"]
        self.assertEqual(probe["model"], os.path.join(self.models_dir, "m"))
        self.assertEqual(probe["max_tokens"], 1)
        self.assertEqual(probe["messages"][0]["content"], "ping")
        self.assertEqual(real["messages"][0]["content"], "real")
        self.assertEqual(real["max_tokens"], 500)

    def test_preflight_can_be_disabled(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "ok"})
        with StubServer() as url:
            HttpProvider(url=url, preflight=0).complete("real")
        self.assertEqual(len(RECEIVED["bodies"]), 1)
        os.environ["MITSS_LLM_PREFLIGHT_TIMEOUT"] = "0"
        with StubServer() as url:
            RECEIVED.clear()
            HttpProvider(url=url).complete("real")
        self.assertEqual(len(RECEIVED["bodies"]), 1)

    def test_preflight_skips_raw_bodies(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "ok"})
        with StubServer() as url:
            HttpProvider(url=url, fmt="raw").complete("real")
        self.assertEqual(len(RECEIVED["bodies"]), 1)

    def test_stuck_server_fails_fast_with_the_restart_message(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "late"})
        REPLY["delay"] = 1.5
        started = time.monotonic()
        with StubServer() as url, self.assertRaises(LLMStuck) as caught:
            HttpProvider(url=url, timeout=30, preflight=0.3).complete("real")
        self.assertLess(time.monotonic() - started, 5)
        message = str(caught.exception)
        self.assertIn("model server is stuck, restart mlx_lm.server", message)
        self.assertIn("prompt was not sent", message)
        # Only the probe went out.
        self.assertEqual(len(RECEIVED["bodies"]), 1)

    def test_preflight_timeout_env_validated_like_the_timeout_but_allows_zero(self):
        self.assertEqual(parse_preflight_timeout("0"), 0.0)
        self.assertEqual(parse_preflight_timeout(45), 45.0)
        for bad in ("-1", "inf", "nan", "soon"):
            with self.assertRaises(LLMConfigError, msg=bad):
                parse_preflight_timeout(bad)
        os.environ["MITSS_LLM_PREFLIGHT_TIMEOUT"] = "-1"
        with self.assertRaises(LLMConfigError):
            HttpProvider(url="http://x")
        os.environ.pop("MITSS_LLM_PREFLIGHT_TIMEOUT", None)
        self.assertEqual(HttpProvider(url="http://x").preflight_timeout, 90.0)
        self.assertEqual(HttpProvider(url="http://x").describe()["preflight_timeout"], 90.0)

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

    def test_a_server_that_echoes_the_key_never_leaks_it(self):
        # Regression: error bodies are quoted back to the operator, and a
        # server or proxy can echo the Authorization header it was sent.
        REPLY["status"] = 401
        REPLY["body"] = json.dumps(
            {"error": "Invalid authorization: Bearer sk-super-secret-value-9"})
        os.environ["MITSS_LLM_API_KEY"] = "sk-super-secret-value-9"
        try:
            with StubServer() as url, self.assertRaises(LLMServerError) as caught:
                HttpProvider(url=url, preflight=0).complete("x")
        finally:
            os.environ.pop("MITSS_LLM_API_KEY", None)
        message = str(caught.exception)
        self.assertNotIn("sk-super-secret-value-9", message)
        self.assertIn("[REDACTED]", message)
        self.assertIn("401", message)

    def test_redact_blanks_the_value_and_the_bearer_form(self):
        self.assertEqual(redact("saw Bearer sk-abcdef here", "sk-abcdef"),
                         "saw [REDACTED] here")
        self.assertEqual(redact("plain sk-abcdef", "sk-abcdef"), "plain [REDACTED]")
        # Nothing configured, or something too short to be a credential.
        self.assertEqual(redact("untouched", None, "", "ab"), "untouched")

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
        self.assertEqual(RECEIVED["body"]["model"],
                         os.path.join(self.models_dir, "team-70b"))

    def test_model_argument_falls_back_to_the_configured_default(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "ok"})
        with StubServer() as url:
            HttpProvider(url=url, fmt="raw", model="default-model").complete("x")
        self.assertEqual(RECEIVED["body"]["model"],
                         os.path.join(self.models_dir, "default-model"))

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

    # -- what is sent, by default and with settings -----------------------

    def test_default_body_is_temperature_zero_and_the_default_token_cap(self):
        # Every request carries a cap now (MITSS_MAX_TOKENS, default 1024);
        # sampling fields stay absent unless a model's settings add them.
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "ok"})
        with StubServer() as url:
            HttpProvider(url=url).complete("x")
        body = RECEIVED["body"]
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 1024)
        for absent in ("top_p", "top_k", "seed",
                       "chat_template_kwargs", "timeout"):
            self.assertNotIn(absent, body)

    def test_settings_reach_the_body_and_timeout_stays_client_side(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "ok"})
        settings = {"temperature": 1.0, "top_p": 0.95, "top_k": 20,
                    "min_p": 0.0, "presence_penalty": 1.5, "max_tokens": 4096,
                    "seed": 7, "timeout": 30,
                    "chat_template_kwargs": {"enable_thinking": False}}
        with StubServer() as url:
            completion = HttpProvider(url=url, settings=settings).generate("x")
        body = RECEIVED["body"]
        self.assertEqual(body["temperature"], 1.0)
        self.assertEqual(body["top_p"], 0.95)
        self.assertEqual(body["top_k"], 20)
        self.assertEqual(body["presence_penalty"], 1.5)
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["seed"], 7)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("timeout", body)
        # The snapshot the run will keep: everything sent, plus the timeout.
        self.assertEqual(completion.text, "ok")
        self.assertEqual(completion.settings["temperature"], 1.0)
        self.assertEqual(completion.settings["timeout"], 30.0)
        self.assertEqual(completion.settings["chat_template_kwargs"],
                         {"enable_thinking": False})

    def test_settings_are_not_added_to_raw_bodies(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "ok"})
        with StubServer() as url:
            completion = HttpProvider(url=url, fmt="raw",
                                      settings={"temperature": 0.7}).generate("x")
        self.assertNotIn("temperature", RECEIVED["body"])
        self.assertEqual(list(completion.settings), ["timeout"])

    def test_blank_settings_mean_the_defaults(self):
        self.assertEqual(normalize_settings(None), {})
        self.assertEqual(normalize_settings({"temperature": "", "top_p": None,
                                             "chat_template_kwargs": {}}), {})

    def test_bad_settings_are_refused_with_the_field_named(self):
        for bad, needle in [
            ({"temprature": 1}, "unknown setting 'temprature'"),
            ({"temperature": -1}, "temperature must be at least 0"),
            ({"temperature": "hot"}, "temperature must be a number"),
            ({"top_p": 1.5}, "top_p must be at most 1"),
            ({"top_k": 2.5}, "top_k must be an integer"),
            ({"top_k": True}, "top_k must be an integer"),
            ({"max_tokens": 0}, "max_tokens must be at least 1"),
            ({"seed": -3}, "seed must be at least 0"),
            ({"timeout": 0}, "timeout must be a positive finite number"),
            ({"chat_template_kwargs": "no"}, "chat_template_kwargs must be a JSON object"),
            ("not a dict", "settings must be a JSON object"),
        ]:
            with self.assertRaises(LLMConfigError, msg=repr(bad)) as caught:
                normalize_settings(bad)
            self.assertIn(needle, str(caught.exception))

    def test_describe_settings_reads_as_one_line(self):
        line = describe_settings({"temperature": 0, "timeout": 120.0,
                                  "chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(line, "temperature=0  timeout=120s  enable_thinking=false")
        self.assertEqual(describe_settings(None), "")

    # -- timeout validation -----------------------------------------------

    def test_timeout_rejects_zero_negative_inf_nan_and_junk(self):
        for bad in (0, -1, "0", "-5", float("inf"), "inf", float("nan"), "nan",
                    "", "soon", None, True):
            with self.assertRaises(LLMConfigError, msg=repr(bad)) as caught:
                parse_timeout(bad, "MITSS_LLM_TIMEOUT")
            self.assertIn("MITSS_LLM_TIMEOUT must be a positive finite number",
                          str(caught.exception))
        self.assertEqual(parse_timeout("1800"), 1800.0)
        self.assertEqual(parse_timeout(0.5), 0.5)

    def test_bad_timeout_env_is_refused_at_construction_not_at_call_time(self):
        os.environ["MITSS_LLM_TIMEOUT"] = "0"
        try:
            with self.assertRaises(LLMConfigError) as caught:
                HttpProvider(url="http://127.0.0.1:9/never")
        finally:
            os.environ.pop("MITSS_LLM_TIMEOUT", None)
        self.assertIn("MITSS_LLM_TIMEOUT", str(caught.exception))
        self.assertIn("'0'", str(caught.exception))

    def test_timeout_precedence_setting_then_argument_then_env(self):
        os.environ["MITSS_LLM_TIMEOUT"] = "45"
        try:
            self.assertEqual(HttpProvider(url="http://x").timeout, 45.0)
            self.assertEqual(HttpProvider(url="http://x", timeout=9).timeout, 9.0)
            self.assertEqual(HttpProvider(url="http://x", timeout=9,
                                          settings={"timeout": 3}).timeout, 3.0)
        finally:
            os.environ.pop("MITSS_LLM_TIMEOUT", None)
        self.assertEqual(HttpProvider(url="http://x").timeout, 120.0)

    # -- one error per cause ---------------------------------------------

    def test_timeout_is_its_own_error_and_says_what_to_do(self):
        REPLY["status"] = 200
        REPLY["body"] = json.dumps({"completion": "late"})
        REPLY["delay"] = 1.5
        with StubServer() as url, self.assertRaises(LLMTimeout) as caught:
            HttpProvider(url=url, timeout=0.3).complete("x")
        message = str(caught.exception)
        self.assertIn("did not answer within 0.3s", message)
        self.assertIn("restart", message)

    def test_refused_connection_is_its_own_error(self):
        with self.assertRaises(LLMUnreachable) as caught:
            HttpProvider(url="http://127.0.0.1:9/never", timeout=2).complete("x")
        message = str(caught.exception)
        self.assertIn("connection refused", message)
        self.assertIn("mlx_lm.server running", message)

    def test_server_error_carries_status_and_the_servers_own_words(self):
        REPLY["status"] = 500
        REPLY["body"] = json.dumps({"error": "[metal::malloc] Resource limit exceeded"})
        with StubServer() as url:
            os.environ["MITSS_LLM_API_KEY"] = "super-secret-value"
            try:
                with self.assertRaises(LLMServerError) as caught:
                    HttpProvider(url=url).complete("x")
            finally:
                os.environ.pop("MITSS_LLM_API_KEY", None)
        self.assertEqual(caught.exception.status_code, 500)
        message = str(caught.exception)
        self.assertIn("HTTP 500", message)
        self.assertIn("Resource limit exceeded", message)
        self.assertNotIn("super-secret-value", message)


# -- streaming ------------------------------------------------------------

# Per-attempt scripts for the streaming stub: a list of (pause, bytes) to write
# after the headers. SCRIPT["attempts"][n] is used for the n-th request (the
# last one repeats). "status" other than 200 sends that status and no stream.
SCRIPT = {"attempts": [], "status": 200}
SEEN = {"count": 0, "bodies": []}


def _sse(chunk):
    return f"data: {json.dumps(chunk)}\n\n".encode()


def _delta(content=None, reasoning=None, finish=None):
    delta = {"role": "assistant"}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning"] = reasoning
    return _sse({"model": "/served/path", "choices": [
        {"index": 0, "delta": delta, "finish_reason": finish}]})


DONE = b"data: [DONE]\n\n"


class _StreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        SEEN["bodies"].append(json.loads(self.rfile.read(length).decode("utf-8")))
        attempt = SEEN["count"]
        SEEN["count"] += 1
        if SCRIPT["status"] != 200:
            self.send_response(SCRIPT["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "No such file or directory"}')
            return
        attempts = SCRIPT["attempts"]
        events = attempts[min(attempt, len(attempts) - 1)]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.flush()
        try:
            for pause, data in events:
                if pause:
                    time.sleep(pause)
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up, as a timed-out client should

    def log_message(self, *args):
        pass


class StreamServer:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class Streaming(unittest.TestCase):
    """Streaming, the idle-gap timeout, retries, token caps and model paths."""

    def setUp(self):
        for key in ("MITSS_LLM_URL", "MITSS_LLM_API_KEY", "MITSS_LLM_TIMEOUT",
                    "MITSS_MAX_TOKENS"):
            os.environ.pop(key, None)
        os.environ["MITSS_LLM_PREFLIGHT_TIMEOUT"] = "0"
        self.addCleanup(os.environ.pop, "MITSS_LLM_PREFLIGHT_TIMEOUT", None)
        self.addCleanup(os.environ.pop, "MITSS_MAX_TOKENS", None)
        self.models_dir = use_fake_models_dir(self)
        SCRIPT["attempts"] = []
        SCRIPT["status"] = 200
        SEEN["count"] = 0
        SEEN["bodies"] = []
        self._delay = HttpProvider.retry_delay
        HttpProvider.retry_delay = 0
        self.addCleanup(setattr, HttpProvider, "retry_delay", self._delay)

    def _whole_answer(self, pause=0.0):
        return [
            (0, b": keepalive 2048/3922\n\n"),
            (pause, _delta(reasoning="Think ")),
            (pause, _delta(reasoning="first.")),
            (pause, _delta(content="The ")),
            (pause, _delta(content="answer.")),
            (pause, _delta(content="", finish="stop")),
            (0, _sse({"choices": [], "usage": {"prompt_tokens": 3922,
                                               "completion_tokens": 4,
                                               "total_tokens": 3926}})),
            (0, DONE),
        ]

    def test_stream_deltas_accumulate_into_one_completion(self):
        SCRIPT["attempts"] = [self._whole_answer()]
        with StreamServer() as url:
            completion = HttpProvider(url=url, model="my-model").generate("q")
        self.assertEqual(completion.text, "The answer.")
        self.assertEqual(completion.reasoning, "Think first.")
        self.assertEqual(completion.usage, {
            "prompt_tokens": 3922, "completion_tokens": 4, "total_tokens": 3926,
            "finish_reason": "stop", "model_reported": "/served/path"})
        body = SEEN["bodies"][0]
        self.assertIs(body["stream"], True)
        self.assertEqual(body["stream_options"], {"include_usage": True})
        # Transport fields are not generation settings; the snapshot a run
        # keeps is unchanged in shape.
        # The loop guard is on by default (40), so its limit is part of the
        # snapshot (2026-10-01, thinking fix).
        self.assertEqual(completion.settings,
                         {"temperature": 0, "max_tokens": 1024, "timeout": 120.0,
                          "loop_repeats": 40})

    def test_idle_timeout_measures_the_gap_not_the_total(self):
        # Six chunks 0.3 s apart take ~1.8 s in total - far past a 0.6 s
        # limit - but no single gap reaches it, so nothing is cut off.
        SCRIPT["attempts"] = [self._whole_answer(pause=0.3)]
        with StreamServer() as url:
            started = time.monotonic()
            completion = HttpProvider(url=url, timeout=0.6).generate("q")
            took = time.monotonic() - started
        self.assertEqual(completion.text, "The answer.")
        self.assertGreater(took, 0.6)
        self.assertEqual(SEEN["count"], 1)

    def test_stalled_stream_is_an_idle_timeout_retried_once(self):
        SCRIPT["attempts"] = [[(0, _delta(content="The ")), (2.0, DONE)]]
        with StreamServer() as url, self.assertRaises(LLMTimeout) as caught:
            HttpProvider(url=url, timeout=0.3).generate("q")
        self.assertEqual(SEEN["count"], 2)   # the first try and one retry
        self.assertIn("did not answer within 0.3s of its last output",
                      str(caught.exception))

    def test_one_retry_recovers_from_a_timeout(self):
        SCRIPT["attempts"] = [[(2.0, DONE)], self._whole_answer()]
        with StreamServer() as url:
            completion = HttpProvider(url=url, timeout=0.3).generate("q")
        self.assertEqual(completion.text, "The answer.")
        self.assertEqual(SEEN["count"], 2)

    def test_a_stream_that_ends_early_is_a_dropped_connection_retried_once(self):
        SCRIPT["attempts"] = [[(0, _delta(content="The "))]]   # no finish, no DONE
        with StreamServer() as url, self.assertRaises(LLMUnreachable):
            HttpProvider(url=url).generate("q")
        self.assertEqual(SEEN["count"], 2)

    def test_stream_marks_first_text_first_answer_and_last_text(self):
        # Pauses before the first delta and between deltas, as prompt
        # processing and decoding would make them.
        SCRIPT["attempts"] = [self._whole_answer(pause=0.15)]
        with StreamServer() as url:
            completion = HttpProvider(url=url, model="my-model").generate("q")
        timing = completion.timing
        self.assertAlmostEqual(timing["first_token"], 0.15, delta=0.1)   # "Think "
        self.assertAlmostEqual(timing["first_answer"], 0.45, delta=0.1)  # "The "
        # "answer." is the last text; the empty finish delta after it is not.
        self.assertAlmostEqual(timing["last_token"], 0.60, delta=0.1)
        self.assertEqual(completion.model_folder,
                         os.path.join(self.models_dir, "my-model"))

    def test_marks_are_measured_from_the_attempt_that_answered(self):
        SCRIPT["attempts"] = [[(2.0, DONE)], self._whole_answer()]
        with StreamServer() as url:
            completion = HttpProvider(url=url, timeout=0.3).generate("q")
        self.assertLess(completion.timing["first_token"], 0.2)

    def test_a_non_streamed_reply_has_no_marks(self):
        REPLY.update(body=json.dumps({"choices": [{"message": {"content": "x"}}]}),
                     status=200, delay=0)
        with StubServer() as url:
            completion = HttpProvider(url=url).generate("q")
        self.assertEqual(completion.timing, {})

    def test_404_is_not_retried(self):
        SCRIPT["status"] = 404
        with StreamServer() as url, self.assertRaises(LLMServerError) as caught:
            HttpProvider(url=url).generate("q")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("No such file or directory", str(caught.exception))
        self.assertEqual(SEEN["count"], 1)

    def test_404_on_the_preflight_is_not_retried_either(self):
        os.environ["MITSS_LLM_PREFLIGHT_TIMEOUT"] = "5"
        SCRIPT["status"] = 404
        with StreamServer() as url, self.assertRaises(LLMServerError):
            HttpProvider(url=url).generate("q")
        self.assertEqual(SEEN["count"], 1)   # the probe only; the prompt never went

    def test_refused_connection_is_retried_then_reported(self):
        provider = HttpProvider(url="http://127.0.0.1:9/v1/chat/completions")
        with self.assertRaises(LLMUnreachable) as caught:
            provider.generate("q")
        self.assertIn("connection refused", str(caught.exception))

    def test_connect_has_its_own_ten_second_limit(self):
        provider = HttpProvider(url="http://127.0.0.1:9/v1", timeout=600)
        self.assertEqual(provider.connect_timeout, 10.0)
        self.assertEqual(provider.timeout, 600.0)

    # -- max_tokens ---------------------------------------------------------

    def test_max_tokens_env_applies_and_a_models_own_setting_wins(self):
        os.environ["MITSS_MAX_TOKENS"] = "2048"
        os.environ["MITSS_LLM_PREFLIGHT_TIMEOUT"] = "5"
        SCRIPT["attempts"] = [self._whole_answer()]
        with StreamServer() as url:
            HttpProvider(url=url).generate("q")
            HttpProvider(url=url, settings={"max_tokens": 32768}).generate("q")
        probe, env_run, probe2, own_run = SEEN["bodies"]
        self.assertEqual(env_run["max_tokens"], 2048)
        self.assertEqual(own_run["max_tokens"], 32768)
        # The preflight probe stays a one-token request either way.
        self.assertEqual(probe["max_tokens"], 1)
        self.assertEqual(probe2["max_tokens"], 1)

    def test_bad_max_tokens_env_is_refused_at_construction(self):
        for bad in ("0", "-5", "lots", "1.5", ""):
            os.environ["MITSS_MAX_TOKENS"] = bad
            with self.assertRaises(LLMConfigError, msg=bad):
                HttpProvider(url="http://127.0.0.1:9/v1")

    def test_truncation_at_the_cap_is_reported(self):
        SCRIPT["attempts"] = [[(0, _delta(content="cut off mid")),
                               (0, _delta(content="", finish="length")), (0, DONE)]]
        with StreamServer() as url:
            completion = HttpProvider(url=url).generate("q")
        self.assertEqual(completion.text, "cut off mid")
        self.assertEqual(completion.usage["finish_reason"], "length")

    # -- model paths and the folder preflight -------------------------------

    def test_missing_folder_is_refused_before_anything_is_sent(self):
        with StreamServer() as url, self.assertRaises(LLMModelUnavailable) as caught:
            HttpProvider(url=url, model="not-downloaded").generate("q")
        self.assertIn("model folder not found", str(caught.exception))
        self.assertEqual(SEEN["count"], 0)

    def test_folder_without_a_readable_config_is_refused(self):
        os.makedirs(os.path.join(self.models_dir, "no-config"))
        os.makedirs(os.path.join(self.models_dir, "bad-config"))
        with open(os.path.join(self.models_dir, "bad-config", "config.json"),
                  "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with StreamServer() as url, self.assertRaisesRegex(LLMModelUnavailable, "no config.json"):
            HttpProvider(url=url, model="no-config").generate("q")
            with self.assertRaisesRegex(LLMModelUnavailable, "not readable"):
                HttpProvider(url=url, model="bad-config").generate("q")
        self.assertEqual(SEEN["count"], 0)

    def test_an_absolute_path_is_sent_as_given(self):
        path = os.path.join(self.models_dir, "my-model")
        SCRIPT["attempts"] = [self._whole_answer()]
        with StreamServer() as url:
            HttpProvider(url=url, model=path).generate("q")
        self.assertEqual(SEEN["bodies"][0]["model"], path)

    def test_only_local_endpoints_are_resolved(self):
        self.assertTrue(is_local_url("http://127.0.0.1:8080/v1/chat/completions"))
        self.assertTrue(is_local_url("http://localhost:8080/v1"))
        self.assertTrue(is_local_url("http://[::1]:8080/v1"))
        self.assertFalse(is_local_url("http://10.0.0.5:8080/v1/chat/completions"))
        self.assertFalse(is_local_url("https://api.example.com/v1"))

    def test_models_dir_comes_from_the_environment_and_defaults_to_the_home(self):
        self.assertEqual(resolve_model_path("qwen3-14b"),
                         os.path.join(self.models_dir, "qwen3-14b"))
        os.environ.pop("MITSS_MODELS_DIR")
        self.assertEqual(models_dir(),
                         os.path.expanduser(os.path.join("~", "Desktop", "models")))
        os.environ["MITSS_MODELS_DIR"] = self.models_dir


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
