"""Tests for the team features: the model registry, batch runs, the digest,
and the review-queue filter.

The registry's one hard rule — a credential never reaches disk or a response —
is exercised directly, and generate/batch are run against a real throwaway
HTTP server rather than mocks, matching test_llm.py.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_folders import use_fake_models_dir

from mitss.llm import HttpProvider
from pipeline import NotFound, Store, build_digest, digest_text

# What the stub server replies with, and what it saw, set per test.
REPLY = {"body": json.dumps({"choices": [{"message": {"content": "stub reply"}}]}),
         "status": 200, "delay": 0}
RECEIVED = {}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        RECEIVED["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
        RECEIVED["auth"] = self.headers.get("Authorization")
        RECEIVED["count"] = RECEIVED.get("count", 0) + 1
        if REPLY.get("delay"):
            time.sleep(REPLY["delay"])
        self.send_response(REPLY["status"])
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(REPLY["body"].encode("utf-8"))

    def log_message(self, *args):
        pass


class StubServer:
    def __enter__(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_and_read_back(self):
        entry = self.store.register_model(
            "Team 7B", owner="alex", url="http://127.0.0.1:9/v1",
            fmt="openai", model="team-7b-q4", key_env="TEAM_7B_KEY",
            notes="alex's quantised build",
        )
        self.assertEqual(entry.id, "team-7b")
        reloaded = self.store.get_model("team-7b")
        self.assertEqual(reloaded.owner, "alex")
        self.assertEqual(reloaded.model, "team-7b-q4")
        self.assertTrue(reloaded.callable)

    def test_entry_without_url_is_paste_only(self):
        entry = self.store.register_model("Hand Carried")
        self.assertFalse(entry.callable)

    def test_ids_do_not_collide(self):
        a = self.store.register_model("Same Name")
        b = self.store.register_model("Same Name")
        self.assertNotEqual(a.id, b.id)

    def test_update_changes_details_but_never_the_name(self):
        entry = self.store.register_model("Team 7B", url="http://old")
        updated = self.store.update_model(entry.id, url="http://new", owner="sam")
        self.assertEqual(updated.url, "http://new")
        self.assertEqual(updated.owner, "sam")
        self.assertEqual(updated.name, "Team 7B")

    def test_delete_removes_registration_only(self):
        prompt = self.store.create_prompt("p", "text")
        entry = self.store.register_model("Team 7B")
        self.store.create_run(prompt.id, 1, entry.name, "an output")
        self.store.delete_model(entry.id)
        with self.assertRaises(NotFound):
            self.store.get_model(entry.id)
        # The run recorded under the model's name is untouched.
        self.assertEqual(self.store.list_runs(model="Team 7B")[0].output,
                         "an output")

    def test_registration_never_writes_a_key_field(self):
        entry = self.store.register_model("Team 7B", key_env="TEAM_7B_KEY")
        with open(os.path.join(self.store.model_dir(entry.id), "model.json"),
                  encoding="utf-8") as handle:
            raw = handle.read()
        self.assertIn("TEAM_7B_KEY", raw)     # the name of the variable
        self.assertNotIn("api_key", raw)      # never a value-shaped field

    def test_settings_are_stored_and_replaced_whole(self):
        entry = self.store.register_model(
            "qwen", url="http://x", settings={"temperature": 0.7, "top_k": 20})
        self.assertEqual(self.store.get_model(entry.id).settings,
                         {"temperature": 0.7, "top_k": 20})
        # None leaves them alone; a dict replaces the block; {} clears it.
        self.store.update_model(entry.id, notes="n")
        self.assertEqual(self.store.get_model(entry.id).settings["top_k"], 20)
        self.store.update_model(entry.id, settings={"max_tokens": 10})
        self.assertEqual(self.store.get_model(entry.id).settings, {"max_tokens": 10})
        self.store.update_model(entry.id, settings={})
        self.assertEqual(self.store.get_model(entry.id).settings, {})

    def test_registration_written_before_settings_existed_still_loads(self):
        entry = self.store.register_model("old", url="http://x")
        path = os.path.join(self.store.model_dir(entry.id), "model.json")
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
        del meta["settings"]
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle)
        self.assertEqual(self.store.get_model(entry.id).settings, {})
        self.assertEqual(self.store.get_model(entry.id).summary()["settings"], {})

    def test_registry_events_are_logged(self):
        entry = self.store.register_model("Team 7B")
        self.store.update_model(entry.id, notes="tuned")
        self.store.delete_model(entry.id)
        kinds = [e["event"] for e in self.store.read_events()]
        self.assertEqual(kinds, ["model_registered", "model_updated",
                                 "model_deleted"])


class RunFilters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_verdict_filter_is_the_review_queue(self):
        prompt = self.store.create_prompt("p", "text")
        read = self.store.create_run(prompt.id, 1, "m", "one")
        self.store.create_run(prompt.id, 1, "m", "two")
        self.store.review_run(read.id, verdict="accurate")

        queue = self.store.list_runs(verdict="unrated")
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].output, "two")
        self.assertEqual(len(self.store.list_runs(verdict="accurate")), 1)
        self.assertEqual(len(self.store.list_runs()), 2)


class Digest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        prompt = self.store.create_prompt("Extract", "Find things.")
        self.store.add_version(prompt.id, "Find all the things.", "broader")
        a = self.store.create_run(prompt.id, 1, "team-7b", "out one")
        self.store.create_run(prompt.id, 2, "team-70b", "out two")
        self.store.review_run(a.id, verdict="inaccurate")
        self.store.register_model("team-7b", owner="alex")
        return prompt

    def test_build_digest_compiles_verdicts_per_version_and_model(self):
        self._seed()
        digest = build_digest(self.store.list_prompts(), self.store.list_runs(),
                              self.store.list_models())
        self.assertEqual(digest["totals"]["total"], 2)
        self.assertEqual(digest["unreviewed"], 1)

        block = digest["prompts"][0]
        self.assertEqual(block["versions"][0]["totals"]["inaccurate"], 1)
        self.assertEqual(block["versions"][1]["totals"]["unrated"], 1)
        models = {m["model"]: m["totals"] for m in block["models"]}
        self.assertEqual(models["team-7b"]["inaccurate"], 1)
        self.assertEqual(models["team-70b"]["unrated"], 1)
        self.assertEqual(digest["registered_models"][0]["owner"], "alex")

    def test_digest_text_reads_as_a_page(self):
        self._seed()
        text = digest_text(build_digest(self.store.list_prompts(),
                                        self.store.list_runs(),
                                        self.store.list_models()))
        self.assertIn("MITSS DIGEST", text)
        self.assertIn("PROMPT: Extract", text)
        self.assertIn("1 inaccurate", text)
        self.assertIn("REVIEW QUEUE: 1 output not read yet", text)
        self.assertIn("team-7b (owner: alex)", text)

    def test_empty_digest_still_renders(self):
        text = digest_text(build_digest([], [], []))
        self.assertIn("no runs", text)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - only on a bare install
    HAVE_FASTAPI = False
except RuntimeError as exc:  # pragma: no cover - only on a bare install
    if "httpx" not in str(exc):
        raise
    HAVE_FASTAPI = False

if HAVE_FASTAPI:
    from app.main import app


@unittest.skipUnless(HAVE_FASTAPI, "fastapi is not installed")
class TeamApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MITSS_ROOT"] = self.tmp.name
        for key in ("MITSS_LLM_PROVIDER", "TEAM_KEY_FOR_TEST", "MITSS_LLM_TIMEOUT"):
            os.environ.pop(key, None)
        # Tests that want the probe say so; the rest exercise the run itself.
        os.environ["MITSS_LLM_PREFLIGHT_TIMEOUT"] = "0"
        REPLY["status"] = 200
        REPLY["delay"] = 0
        REPLY["body"] = json.dumps({"choices": [{"message": {"content": "stub reply"}}]})
        self.client = TestClient(app)
        self.models_dir = use_fake_models_dir(self)
        RECEIVED.clear()
        # Retries still happen; they just do not pause in tests.
        delay = HttpProvider.retry_delay
        HttpProvider.retry_delay = 0
        self.addCleanup(setattr, HttpProvider, "retry_delay", delay)

    def tearDown(self):
        os.environ.pop("MITSS_ROOT", None)
        os.environ.pop("TEAM_KEY_FOR_TEST", None)
        os.environ.pop("MITSS_LLM_TIMEOUT", None)
        os.environ.pop("MITSS_LLM_PREFLIGHT_TIMEOUT", None)
        self.tmp.cleanup()

    def _prompt(self):
        return self.client.post("/api/prompts", json={"name": "p", "text": "t"}).json()

    def _register(self, name="team-7b", **extra):
        payload = {"name": name, **extra}
        response = self.client.post("/api/models", json=payload)
        return response

    def test_register_list_and_delete(self):
        created = self._register(owner="alex", url="http://127.0.0.1:9/v1").json()
        self.assertEqual(created["id"], "team-7b")
        self.assertTrue(created["callable"])

        listed = self.client.get("/api/models").json()["models"]
        self.assertEqual([m["id"] for m in listed], ["team-7b"])

        self.client.delete("/api/models/team-7b")
        self.assertEqual(self.client.get("/api/models").json()["models"], [])

    def test_key_env_that_looks_like_a_key_is_rejected(self):
        response = self._register(key_env="sk-abc123-not-a-name!")
        self.assertEqual(response.status_code, 400)
        self.assertIn("NAME of an environment variable",
                      response.json()["detail"])

    def test_key_presence_is_reported_but_never_the_value(self):
        self._register(key_env="TEAM_KEY_FOR_TEST")
        self.assertFalse(self.client.get("/api/models/team-7b").json()["key_set"])
        os.environ["TEAM_KEY_FOR_TEST"] = "secret-value-9"
        body = self.client.get("/api/models/team-7b")
        self.assertTrue(body.json()["key_set"])
        self.assertNotIn("secret-value-9", body.text)

    def test_bad_url_and_format_are_rejected(self):
        self.assertEqual(self._register(url="ftp://nope").status_code, 400)
        self.assertEqual(self._register(format="grpc").status_code, 400)

    def test_generate_calls_the_registered_models_own_endpoint(self):
        prompt = self.client.post("/api/prompts", json={
            "name": "Summarise", "text": "Summarise: {input}",
        }).json()
        with StubServer() as url:
            self._register(url=url, model="team-7b-q4",
                           key_env="TEAM_KEY_FOR_TEST")
            os.environ["TEAM_KEY_FOR_TEST"] = "secret-value-9"
            run = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b",
            }).json()
        self.assertEqual(run["output"], "stub reply")
        self.assertEqual(run["model"], "team-7b")
        # The endpoint was asked for the registered body-name, with its key -
        # as a folder path, because the endpoint is on this machine.
        self.assertEqual(RECEIVED["body"]["model"],
                         os.path.join(self.models_dir, "team-7b-q4"))
        self.assertEqual(RECEIVED["auth"], "Bearer secret-value-9")

    # -- per-model settings ----------------------------------------------

    def test_settings_round_trip_through_the_api(self):
        created = self._register(url="http://127.0.0.1:9/v1", settings={
            "temperature": 1.0, "top_p": 0.95, "top_k": 20, "max_tokens": 4096,
            "timeout": 600, "chat_template_kwargs": {"enable_thinking": False},
            "seed": "",  # blank box = default, dropped
        }).json()
        self.assertEqual(created["settings"]["top_k"], 20)
        self.assertEqual(created["settings"]["timeout"], 600.0)
        self.assertNotIn("seed", created["settings"])

        patched = self.client.patch("/api/models/team-7b", json={
            "settings": {"temperature": 0.2},
        }).json()
        self.assertEqual(patched["settings"], {"temperature": 0.2})
        untouched = self.client.patch("/api/models/team-7b", json={"notes": "x"}).json()
        self.assertEqual(untouched["settings"], {"temperature": 0.2})

    def test_bad_settings_are_a_400_naming_the_field(self):
        response = self._register(settings={"temperature": -1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("temperature must be at least 0", response.json()["detail"])
        response = self._register(name="b", settings={"timeout": "inf"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("timeout must be a positive finite number",
                      response.json()["detail"])
        response = self._register(name="c", settings={"tempreture": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown setting", response.json()["detail"])

    def test_run_records_a_snapshot_of_the_settings_it_used(self):
        prompt = self._prompt()
        with StubServer() as url:
            self._register(url=url, settings={
                "temperature": 0.6, "max_tokens": 2048,
                "chat_template_kwargs": {"enable_thinking": False},
            })
            run = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b",
            }).json()
            # The endpoint got the settings...
            self.assertEqual(RECEIVED["body"]["temperature"], 0.6)
            self.assertEqual(RECEIVED["body"]["max_tokens"], 2048)
            self.assertEqual(RECEIVED["body"]["chat_template_kwargs"],
                             {"enable_thinking": False})
            # ...the run kept them, with the timeout that bounded the wait...
            self.assertEqual(run["settings"]["temperature"], 0.6)
            self.assertEqual(run["settings"]["timeout"], 120.0)
            self.assertEqual(run["settings"]["chat_template_kwargs"],
                             {"enable_thinking": False})
            # ...and editing the registration afterwards changes nothing.
            self.client.patch("/api/models/team-7b", json={"settings": {}})
            again = self.client.get(f"/api/runs/{run['id']}").json()
            self.assertEqual(again["settings"]["temperature"], 0.6)
            listed = self.client.get(f"/api/runs?prompt_id={prompt['id']}").json()["runs"]
            self.assertEqual(listed[0]["settings"]["max_tokens"], 2048)

    def test_run_without_settings_still_serves_them_as_null(self):
        prompt = self._prompt()
        with StubServer() as url:
            self._register(url=url)
            run = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b",
            }).json()
        # Defaults are still recorded - temperature 0 and the default token
        # cap are facts, not absences.
        self.assertEqual(run["settings"], {"temperature": 0, "max_tokens": 1024,
                                           "timeout": 120.0})
        pasted = self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "hand", "output": "o",
        }).json()
        self.assertIsNone(pasted["settings"])

    # -- one status per cause --------------------------------------------

    def test_timeout_is_a_504_with_its_own_message(self):
        prompt = self._prompt()
        REPLY["delay"] = 1.5
        with StubServer() as url:
            self._register(url=url, settings={"timeout": 0.3})
            response = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b",
            })
        self.assertEqual(response.status_code, 504)
        self.assertIn("did not answer within 0.3s", response.json()["detail"])

    def test_stuck_server_is_a_503_before_the_prompt_is_sent(self):
        prompt = self._prompt()
        os.environ["MITSS_LLM_PREFLIGHT_TIMEOUT"] = "0.3"
        REPLY["delay"] = 1.5
        with StubServer() as url:
            self._register(url=url, settings={"timeout": 60})
            response = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b",
            })
        self.assertEqual(response.status_code, 503)
        self.assertIn("model server is stuck, restart mlx_lm.server",
                      response.json()["detail"])
        self.assertEqual(RECEIVED["body"]["max_tokens"], 1)   # only the probe
        runs = self.client.get(f"/api/runs?prompt_id={prompt['id']}").json()["runs"]
        self.assertEqual(runs, [])

    def test_reasoning_is_recorded_apart_from_the_output(self):
        prompt = self._prompt()
        REPLY["body"] = json.dumps({"choices": [{"message": {
            "reasoning": "First, consider the PACE plan...", "content": "The answer.",
        }}]})
        with StubServer() as url:
            self._register(url=url)
            run = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b",
            }).json()
        self.assertEqual(run["output"], "The answer.")
        self.assertEqual(run["reasoning"], "First, consider the PACE plan...")
        detail = self.client.get(f"/api/runs/{run['id']}").json()
        self.assertEqual(detail["reasoning"], "First, consider the PACE plan...")
        listed = self.client.get(f"/api/runs?prompt_id={prompt['id']}").json()["runs"]
        self.assertEqual(listed[0]["reasoning_characters"], 32)
        self.assertNotIn("reasoning", listed[0])   # the list stays light
        # On disk, beside the output, as its own file.
        path = os.path.join(self.tmp.name, "data", "runs", run["id"], "reasoning.txt")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "First, consider the PACE plan...")
        # A run with no reasoning has no file and an empty field.
        REPLY["body"] = json.dumps({"choices": [{"message": {"content": "plain"}}]})
        with StubServer() as url:
            self._register(name="plain", url=url)
            plain = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "plain",
            }).json()
        self.assertEqual(plain["reasoning"], "")
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp.name, "data", "runs", plain["id"], "reasoning.txt")))

    def test_run_records_what_the_server_reported_plus_a_measured_rate(self):
        prompt = self._prompt()
        REPLY["body"] = json.dumps({
            "model": "served-name",
            "choices": [{"finish_reason": "length", "message": {"content": "out"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "total_tokens": 150},
        })
        with StubServer() as url:
            self._register(url=url)
            run = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b",
            }).json()
        self.assertEqual(run["usage"]["completion_tokens"], 50)
        self.assertEqual(run["usage"]["finish_reason"], "length")
        self.assertEqual(run["usage"]["model_reported"], "served-name")
        # The rate is measured here, where the elapsed time is known.
        self.assertGreater(run["usage"]["tokens_per_second"], 0)
        detail = self.client.get(f"/api/runs/{run['id']}").json()
        self.assertEqual(detail["usage"]["total_tokens"], 150)

    def test_a_run_with_no_reported_usage_stores_null_not_a_guess(self):
        prompt = self._prompt()
        REPLY["body"] = json.dumps({"choices": [{"message": {"content": "a b c"}}]})
        with StubServer() as url:
            self._register(url=url)
            run = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b",
            }).json()
        self.assertIsNone(run["usage"])
        pasted = self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "hand", "output": "o",
        }).json()
        self.assertIsNone(pasted["usage"])

    def test_rate_excludes_the_preflight_so_a_model_load_is_not_slowness(self):
        # The probe is what absorbs a model swap. If its time counted toward
        # the rate, the first model in a batch would always look slowest.
        prompt = self._prompt()
        os.environ["MITSS_LLM_PREFLIGHT_TIMEOUT"] = "30"
        REPLY["delay"] = 0.4          # every request, probe included
        REPLY["body"] = json.dumps({
            "choices": [{"message": {"content": "out"}}],
            "usage": {"completion_tokens": 40},
        })
        with StubServer() as url:
            self._register(url=url, settings={"timeout": 60})
            run = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b",
            }).json()
        # Two round trips of 0.4s each were waited through...
        self.assertGreaterEqual(run["duration_ms"], 800)
        # ...but the rate is measured against the generation request only,
        # so it reflects ~0.4s (=100 tok/s), not ~0.8s (=50 tok/s).
        self.assertGreater(run["usage"]["tokens_per_second"], 60)

    def test_batch_results_carry_timings(self):
        prompt = self._prompt()
        with StubServer() as url:
            self._register(name="works", url=url)
            self._register(name="broken", url="http://127.0.0.1:9/nowhere")
            body = self.client.post("/api/batch", json={
                "prompt_id": prompt["id"],
            }).json()
        by_model = {r["model"]: r for r in body["results"]}
        # Both the success and the failure say how long they took.
        self.assertIsInstance(by_model["works"]["elapsed_ms"], int)
        self.assertIsInstance(by_model["broken"]["elapsed_ms"], int)

    def test_refused_connection_is_a_502_that_says_so(self):
        prompt = self._prompt()
        self._register(url="http://127.0.0.1:9/nowhere")
        response = self.client.post("/api/generate", json={
            "prompt_id": prompt["id"], "model_id": "team-7b",
        })
        self.assertEqual(response.status_code, 502)
        self.assertIn("connection refused", response.json()["detail"])

    def test_model_server_error_is_a_502_quoting_the_server(self):
        prompt = self._prompt()
        REPLY["status"] = 500
        REPLY["body"] = json.dumps({"error": "generation thread died"})
        with StubServer() as url:
            self._register(url=url)
            response = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b",
            })
        self.assertEqual(response.status_code, 502)
        self.assertIn("HTTP 500", response.json()["detail"])
        self.assertIn("generation thread died", response.json()["detail"])

    def test_bad_timeout_env_is_a_clear_500_and_llm_status_reports_it(self):
        prompt = self._prompt()
        os.environ["MITSS_LLM_PROVIDER"] = "http"
        os.environ["MITSS_LLM_URL"] = "http://127.0.0.1:9/v1"
        os.environ["MITSS_LLM_TIMEOUT"] = "nan"
        try:
            response = self.client.post("/api/generate", json={"prompt_id": prompt["id"]})
            self.assertEqual(response.status_code, 500)
            self.assertIn("MITSS_LLM_TIMEOUT must be a positive finite number",
                          response.json()["detail"])
            status = self.client.get("/api/llm").json()
            self.assertFalse(status["available"])
            self.assertIn("MITSS_LLM_TIMEOUT", status["error"])
        finally:
            for key in ("MITSS_LLM_PROVIDER", "MITSS_LLM_URL", "MITSS_LLM_TIMEOUT"):
                os.environ.pop(key, None)

    def test_generate_on_a_paste_only_model_conflicts(self):
        prompt = self.client.post("/api/prompts", json={
            "name": "p", "text": "t",
        }).json()
        self._register(name="hand-carried")
        response = self.client.post("/api/generate", json={
            "prompt_id": prompt["id"], "model_id": "hand-carried",
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn("paste", response.json()["detail"])

    def test_batch_runs_every_callable_model_and_survives_a_failure(self):
        prompt = self.client.post("/api/prompts", json={
            "name": "p", "text": "t",
        }).json()
        with StubServer() as url:
            self._register(name="works", url=url)
            self._register(name="broken", url="http://127.0.0.1:9/nowhere")
            self._register(name="paste-only")  # no url: skipped, not failed
            body = self.client.post("/api/batch", json={
                "prompt_id": prompt["id"],
            }).json()

        self.assertEqual(body["recorded"], 1)
        self.assertEqual(body["failed"], 1)
        by_model = {r["model"]: r for r in body["results"]}
        self.assertTrue(by_model["works"]["ok"])
        self.assertFalse(by_model["broken"]["ok"])
        self.assertNotIn("paste-only", by_model)

        runs = self.client.get(f"/api/runs?prompt_id={prompt['id']}").json()["runs"]
        self.assertEqual([r["model"] for r in runs], ["works"])

    # -- quarantine, unavailable models, truncation ------------------------

    def test_quarantine_round_trips_through_patch(self):
        self._register(url="http://127.0.0.1:9/v1")
        self.assertEqual(self.client.get("/api/models/team-7b").json()["quarantine"], "")
        reason = "tokenizer loads with an incorrect regex (fix_mistral_regex)"
        patched = self.client.patch("/api/models/team-7b",
                                    json={"quarantine": f"  {reason}  "}).json()
        self.assertEqual(patched["quarantine"], reason)
        on_disk = os.path.join(self.tmp.name, "data", "models", "team-7b", "model.json")
        with open(on_disk, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["quarantine"], reason)
        # Omitting it leaves it alone; "" lifts it.
        self.client.patch("/api/models/team-7b", json={"notes": "n"})
        self.assertEqual(self.client.get("/api/models/team-7b").json()["quarantine"], reason)
        lifted = self.client.patch("/api/models/team-7b", json={"quarantine": ""}).json()
        self.assertEqual(lifted["quarantine"], "")
        too_long = self.client.patch("/api/models/team-7b", json={"quarantine": "x" * 501})
        self.assertEqual(too_long.status_code, 400)

    def test_a_registration_without_the_field_loads_unquarantined(self):
        self._register(url="http://127.0.0.1:9/v1")
        on_disk = os.path.join(self.tmp.name, "data", "models", "team-7b", "model.json")
        with open(on_disk, encoding="utf-8") as handle:
            meta = json.load(handle)
        del meta["quarantine"]
        with open(on_disk, "w", encoding="utf-8") as handle:
            json.dump(meta, handle)
        self.assertEqual(self.client.get("/api/models/team-7b").json()["quarantine"], "")

    def test_quarantined_model_is_skipped_by_batch_and_refused_alone(self):
        prompt = self._prompt()
        reason = "tokenizer loads with an incorrect regex"
        with StubServer() as url:
            self._register(name="works", url=url)
            self._register(name="team-7b", url=url)
            self.client.patch("/api/models/team-7b", json={"quarantine": reason})
            body = self.client.post("/api/batch", json={"prompt_id": prompt["id"]}).json()
            alone = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "team-7b"})
        by_model = {r["model"]: r for r in body["results"]}
        self.assertTrue(by_model["works"]["ok"])
        skipped = by_model["team-7b"]
        self.assertFalse(skipped["ok"])
        self.assertTrue(skipped["skipped"])
        self.assertTrue(skipped["quarantined"])
        self.assertEqual(skipped["reason"], reason)
        self.assertIn(reason, skipped["error"])
        self.assertEqual((body["recorded"], body["failed"], body["skipped"]), (1, 1, 1))
        self.assertEqual(RECEIVED["count"], 1)   # only "works" was asked
        self.assertEqual(alone.status_code, 409)
        self.assertIn("quarantined", alone.json()["detail"])

    def test_404_marks_a_model_unavailable_for_the_rest_of_the_batch(self):
        prompt = self._prompt()
        REPLY["status"] = 404
        REPLY["body"] = json.dumps({"error": "No such file or directory"})
        with StubServer() as url:
            self._register(name="team-7b", url=url)
            # A second registration of the same endpoint and model.
            self._register(name="team-7b-again", url=url, model="team-7b")
            # A model whose folder is missing: refused before any request.
            self._register(name="ghost", url=url)
            body = self.client.post("/api/batch", json={"prompt_id": prompt["id"]}).json()
        by_model = {r["model"]: r for r in body["results"]}
        first = by_model["team-7b"]
        self.assertTrue(first["unavailable"])
        self.assertIn("HTTP 404", first["reason"])
        self.assertNotIn("skipped", first)
        again = by_model["team-7b-again"]
        self.assertTrue(again["skipped"])
        self.assertTrue(again["unavailable"])
        self.assertEqual(again["reason"], first["reason"])
        ghost = by_model["ghost"]
        self.assertTrue(ghost["unavailable"])
        self.assertIn("model folder not found", ghost["reason"])
        # One request in total: the 404 was not retried, the duplicate was
        # not asked, and the missing folder never reached the server.
        self.assertEqual(RECEIVED["count"], 1)
        self.assertEqual((body["recorded"], body["failed"], body["skipped"]), (0, 3, 1))

    def test_missing_folder_is_a_409_for_a_single_run(self):
        prompt = self._prompt()
        with StubServer() as url:
            self._register(name="ghost", url=url)
            response = self.client.post("/api/generate", json={
                "prompt_id": prompt["id"], "model_id": "ghost"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("model folder not found", response.json()["detail"])
        self.assertNotIn("count", RECEIVED)

    def test_batch_flags_an_answer_cut_off_at_the_token_cap(self):
        prompt = self._prompt()
        REPLY["body"] = json.dumps({"choices": [{
            "message": {"content": "cut off mid"}, "finish_reason": "length"}]})
        with StubServer() as url:
            self._register(name="works", url=url)
            body = self.client.post("/api/batch", json={"prompt_id": prompt["id"]}).json()
        result = body["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["finish_reason"], "length")
        self.assertTrue(result["truncated"])
        run_json = os.path.join(self.tmp.name, "data", "runs", result["run_id"], "run.json")
        with open(run_json, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["usage"]["finish_reason"], "length")

    def test_a_complete_answer_is_not_flagged(self):
        prompt = self._prompt()
        REPLY["body"] = json.dumps({"choices": [{
            "message": {"content": "done"}, "finish_reason": "stop"}]})
        with StubServer() as url:
            self._register(name="works", url=url)
            result = self.client.post("/api/batch", json={
                "prompt_id": prompt["id"]}).json()["results"][0]
        self.assertEqual(result["finish_reason"], "stop")
        self.assertFalse(result["truncated"])

    def test_batch_with_no_callable_models_conflicts(self):
        prompt = self.client.post("/api/prompts", json={
            "name": "p", "text": "t",
        }).json()
        response = self.client.post("/api/batch", json={"prompt_id": prompt["id"]})
        self.assertEqual(response.status_code, 409)

    def test_runs_verdict_filter(self):
        prompt = self.client.post("/api/prompts", json={
            "name": "p", "text": "t",
        }).json()
        first = self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "m", "output": "one",
        }).json()
        self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "m", "output": "two",
        })
        self.client.patch(f"/api/runs/{first['id']}", json={"verdict": "accurate"})

        queue = self.client.get("/api/runs?verdict=unrated").json()["runs"]
        self.assertEqual(len(queue), 1)
        bad = self.client.get("/api/runs?verdict=nonsense")
        self.assertEqual(bad.status_code, 400)

    def test_digest_json_and_text(self):
        prompt = self.client.post("/api/prompts", json={
            "name": "Extract", "text": "t",
        }).json()
        run = self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "team-7b", "output": "out",
        }).json()
        self.client.patch(f"/api/runs/{run['id']}", json={"verdict": "partial"})

        digest = self.client.get("/api/digest").json()
        self.assertEqual(digest["totals"]["partial"], 1)

        text = self.client.get("/api/digest?format=text").text
        self.assertIn("MITSS DIGEST", text)
        self.assertIn("partly right", text)

        attached = self.client.get("/api/digest?format=text&download=true")
        self.assertIn("mitss-digest.txt",
                      attached.headers["content-disposition"])


if __name__ == "__main__":
    unittest.main()
