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
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import NotFound, Store, build_digest, digest_text

# What the stub server replies with, and what it saw, set per test.
REPLY = {"body": json.dumps({"choices": [{"message": {"content": "stub reply"}}]}),
         "status": 200}
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
        for key in ("MITSS_LLM_PROVIDER", "TEAM_KEY_FOR_TEST"):
            os.environ.pop(key, None)
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("MITSS_ROOT", None)
        os.environ.pop("TEAM_KEY_FOR_TEST", None)
        self.tmp.cleanup()

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
        # The endpoint was asked for the registered body-name, with its key.
        self.assertEqual(RECEIVED["body"]["model"], "team-7b-q4")
        self.assertEqual(RECEIVED["auth"], "Bearer secret-value-9")

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
