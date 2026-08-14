"""End-to-end tests of the HTTP layer using FastAPI's test client.

Each test runs against a throwaway root so nothing leaks between tests or into
real data.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - only on a bare install
    HAVE_FASTAPI = False

if HAVE_FASTAPI:
    from app.main import app


@unittest.skipUnless(HAVE_FASTAPI, "fastapi is not installed")
class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MITSS_ROOT"] = self.tmp.name
        os.environ.pop("MITSS_LLM_PROVIDER", None)
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("MITSS_ROOT", None)
        self.tmp.cleanup()

    def _prompt(self, name="Summarise", text="Summarise the passage."):
        return self.client.post(
            "/api/prompts", json={"name": name, "text": text}
        ).json()

    def _run(self, prompt_id, model="model-a", output="a summary", version=None,
             verdict="unrated"):
        return self.client.post("/api/runs", json={
            "prompt_id": prompt_id, "version": version, "model": model,
            "output": output, "verdict": verdict,
        }).json()

    # -- basics ---------------------------------------------------------

    def test_health(self):
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")

    def test_llm_defaults_to_manual(self):
        body = self.client.get("/api/llm").json()
        self.assertEqual(body["provider"], "manual")
        self.assertFalse(body["available"])

    def test_verdict_options(self):
        verdicts = self.client.get("/api/verdicts").json()["verdicts"]
        self.assertEqual([v["value"] for v in verdicts],
                         ["unrated", "accurate", "partial", "inaccurate"])

    # -- prompts --------------------------------------------------------

    def test_create_and_fetch_prompt(self):
        body = self._prompt()
        self.assertEqual(body["id"], "summarise")
        self.assertEqual(body["version_count"], 1)
        self.assertEqual(body["selected_version"]["text"], "Summarise the passage.")

        again = self.client.get("/api/prompts/summarise").json()
        self.assertEqual(again["latest_version"], 1)

    def test_empty_prompt_rejected(self):
        response = self.client.post("/api/prompts", json={"name": "x", "text": "   "})
        self.assertEqual(response.status_code, 400)

    def test_add_version(self):
        prompt = self._prompt()
        body = self.client.post(
            f"/api/prompts/{prompt['id']}/versions",
            json={"text": "Summarise in one sentence.", "note": "tighter"},
        ).json()
        self.assertEqual(body["version_count"], 2)
        self.assertEqual(body["selected_version"]["version"], 2)
        self.assertEqual(body["versions"][1]["note"], "tighter")

    def test_identical_version_is_refused(self):
        prompt = self._prompt()
        response = self.client.post(
            f"/api/prompts/{prompt['id']}/versions",
            json={"text": "Summarise the passage."},
        )
        self.assertEqual(response.status_code, 409)

    def test_fetch_specific_version(self):
        prompt = self._prompt()
        self.client.post(f"/api/prompts/{prompt['id']}/versions", json={"text": "v2 text"})
        body = self.client.get(f"/api/prompts/{prompt['id']}?version=1").json()
        self.assertEqual(body["selected_version"]["text"], "Summarise the passage.")

    def test_version_text_as_plain_text(self):
        prompt = self._prompt()
        response = self.client.get(f"/api/prompts/{prompt['id']}/versions/1/text")
        self.assertEqual(response.text, "Summarise the passage.")

    def test_rename(self):
        prompt = self._prompt()
        body = self.client.patch(f"/api/prompts/{prompt['id']}",
                                 json={"name": "Summarise tightly"}).json()
        self.assertEqual(body["name"], "Summarise tightly")
        self.assertEqual(body["id"], prompt["id"])

    def test_unknown_prompt_is_404(self):
        self.assertEqual(self.client.get("/api/prompts/nope").status_code, 404)

    # -- uploads --------------------------------------------------------

    def _upload(self, text, name="my-prompt.txt", prompt_id="", note=""):
        return self.client.post(
            "/api/uploads",
            files={"file": (name, io.BytesIO(text.encode("utf-8")), "text/plain")},
            data={"prompt_id": prompt_id, "note": note},
        )

    def test_upload_creates_a_prompt(self):
        body = self._upload("Classify the sentiment of the text.").json()
        self.assertEqual(body["id"], "my-prompt")
        self.assertEqual(body["selected_version"]["text"],
                         "Classify the sentiment of the text.")

    def test_upload_to_existing_prompt_adds_a_version(self):
        prompt = self._prompt()
        body = self._upload("A revised prompt.", prompt_id=prompt["id"]).json()
        self.assertEqual(body["id"], prompt["id"])
        self.assertEqual(body["version_count"], 2)

    def test_upload_rejects_non_text(self):
        response = self.client.post(
            "/api/uploads",
            files={"file": ("x.pdf", io.BytesIO(b"x"), "application/pdf")},
            data={"prompt_id": "", "note": ""},
        )
        self.assertEqual(response.status_code, 400)

    def test_upload_rejects_empty(self):
        self.assertEqual(self._upload("   ").status_code, 400)

    def test_upload_rejects_non_utf8(self):
        response = self.client.post(
            "/api/uploads",
            files={"file": ("x.txt", io.BytesIO(b"\xff\xfe\x00"), "text/plain")},
            data={"prompt_id": "", "note": ""},
        )
        self.assertEqual(response.status_code, 400)

    # -- runs -----------------------------------------------------------

    def test_record_a_run(self):
        prompt = self._prompt()
        body = self._run(prompt["id"], model="custom-llm-1", output="the summary")
        self.assertEqual(body["model"], "custom-llm-1")
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["verdict"], "unrated")
        self.assertEqual(body["output"], "the summary")
        self.assertEqual(body["prompt_text"], "Summarise the passage.")

    def test_run_defaults_to_latest_version(self):
        prompt = self._prompt()
        self.client.post(f"/api/prompts/{prompt['id']}/versions", json={"text": "v2"})
        body = self._run(prompt["id"])
        self.assertEqual(body["version"], 2)

    def test_empty_output_rejected(self):
        prompt = self._prompt()
        response = self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "m", "output": "  "})
        self.assertEqual(response.status_code, 400)

    def test_bad_verdict_rejected(self):
        prompt = self._prompt()
        response = self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "m", "output": "x",
            "verdict": "excellent"})
        self.assertEqual(response.status_code, 400)

    def test_review_a_run(self):
        prompt = self._prompt()
        run = self._run(prompt["id"])
        body = self.client.patch(f"/api/runs/{run['id']}", json={
            "verdict": "partial", "notes": "missed the third point"}).json()
        self.assertEqual(body["verdict"], "partial")
        self.assertEqual(body["verdict_label"], "partly right")
        self.assertIn("third point", body["notes"])
        self.assertTrue(body["reviewed_at"])

    def test_list_and_filter_runs(self):
        prompt = self._prompt()
        self._run(prompt["id"], model="model-a")
        self._run(prompt["id"], model="model-b")
        self.assertEqual(len(self.client.get("/api/runs").json()["runs"]), 2)
        filtered = self.client.get("/api/runs?model=model-b").json()["runs"]
        self.assertEqual(len(filtered), 1)

    def test_delete_run(self):
        prompt = self._prompt()
        run = self._run(prompt["id"])
        self.assertEqual(self.client.delete(f"/api/runs/{run['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/runs/{run['id']}").status_code, 404)

    def test_generate_with_manual_provider_is_a_conflict(self):
        prompt = self._prompt()
        response = self.client.post("/api/generate", json={"prompt_id": prompt["id"]})
        self.assertEqual(response.status_code, 409)
        self.assertIn("paste", response.json()["detail"])

    # -- comparison -----------------------------------------------------

    def test_matrix_shape(self):
        prompt = self._prompt()
        self.client.post(f"/api/prompts/{prompt['id']}/versions", json={"text": "v2"})
        first = self._run(prompt["id"], model="model-a", version=1)
        self._run(prompt["id"], model="model-b", version=2)
        self.client.patch(f"/api/runs/{first['id']}", json={"verdict": "inaccurate"})

        grid = self.client.get(f"/api/prompts/{prompt['id']}/matrix").json()
        self.assertEqual(grid["versions"], [1, 2])
        self.assertEqual(grid["models"], ["model-a", "model-b"])
        self.assertEqual(grid["cells"]["1|model-a"]["verdict"], "inaccurate")
        self.assertEqual(grid["cells"]["2|model-b"]["verdict"], "unrated")
        self.assertEqual(grid["totals"]["total"], 2)

    def test_compare_two_outputs(self):
        prompt = self._prompt()
        a = self._run(prompt["id"], model="model-a", output="the quick brown fox")
        b = self._run(prompt["id"], model="model-b", output="the quick red fox")
        body = self.client.get(f"/api/compare?a={a['id']}&b={b['id']}").json()

        self.assertTrue(body["same_prompt_version"])
        self.assertFalse(body["same_model"])
        diff = body["output_diff"]
        self.assertEqual(diff["added_words"], 1)
        self.assertEqual(diff["removed_words"], 1)
        self.assertFalse(diff["identical"])

    def test_compare_identical_outputs(self):
        prompt = self._prompt()
        a = self._run(prompt["id"], model="model-a", output="same words here")
        b = self._run(prompt["id"], model="model-b", output="same words here")
        diff = self.client.get(f"/api/compare?a={a['id']}&b={b['id']}").json()["output_diff"]
        self.assertTrue(diff["identical"])
        self.assertEqual(diff["similarity"], 1.0)

    def test_compare_prompt_versions(self):
        prompt = self._prompt()
        self.client.post(f"/api/prompts/{prompt['id']}/versions",
                         json={"text": "Summarise the passage briefly."})
        body = self.client.get(
            f"/api/prompts/{prompt['id']}/compare-versions?a=1&b=2").json()
        diff = body["diff"]
        self.assertFalse(diff["identical"])
        # Punctuation rides along with its word, so "passage." becoming
        # "passage briefly." is one token replaced by two rather than a bare
        # insertion. What matters is that the new word surfaces as added.
        added = " ".join(s["text"] for s in diff["right"] if s["kind"] == "added")
        self.assertIn("briefly", added)
        self.assertNotIn("Summarise", added)

    def test_compare_unknown_run_is_404(self):
        self.assertEqual(self.client.get("/api/compare?a=x&b=y").status_code, 404)

    def test_activity_log(self):
        prompt = self._prompt()
        self._run(prompt["id"])
        events = self.client.get("/api/activity").json()["events"]
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[0], "prompt_created")
        self.assertIn("run_recorded", kinds)

    # -- input sets -----------------------------------------------------

    def _input(self, name="Passage A", text="Acme Corp opened in Lisbon."):
        return self.client.post("/api/inputs",
                                json={"name": name, "text": text}).json()

    def test_create_and_list_inputs(self):
        created = self._input()
        self.assertEqual(created["id"], "passage-a")
        listing = self.client.get("/api/inputs").json()["inputs"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["name"], "Passage A")

    def test_empty_input_rejected(self):
        response = self.client.post("/api/inputs", json={"name": "x", "text": "  "})
        self.assertEqual(response.status_code, 400)

    def test_update_and_delete_input(self):
        created = self._input()
        updated = self.client.patch(f"/api/inputs/{created['id']}",
                                    json={"text": "Globex opened in Berlin."}).json()
        self.assertEqual(updated["text"], "Globex opened in Berlin.")
        self.assertEqual(
            self.client.delete(f"/api/inputs/{created['id']}").status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/inputs/{created['id']}").status_code, 404)

    def test_preview_renders_prompt_with_input(self):
        prompt = self.client.post("/api/prompts", json={
            "name": "Extract", "text": "List entities in {input}"}).json()
        chosen = self._input()
        body = self.client.get(
            f"/api/prompts/{prompt['id']}/preview?input_id={chosen['id']}").json()
        self.assertEqual(body["rendered"], "List entities in Acme Corp opened in Lisbon.")
        self.assertTrue(body["has_placeholder"])
        self.assertEqual(body["notes"], [])

    def test_preview_warns_when_prompt_has_no_placeholder(self):
        prompt = self._prompt()
        chosen = self._input()
        body = self.client.get(
            f"/api/prompts/{prompt['id']}/preview?input_id={chosen['id']}").json()
        self.assertIn("no_placeholder", [n["code"] for n in body["notes"]])
        self.assertFalse(body["has_placeholder"])

    def test_run_records_the_input_it_used(self):
        prompt = self.client.post("/api/prompts", json={
            "name": "Extract", "text": "List entities in {input}"}).json()
        chosen = self._input()
        run = self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "m", "output": "Acme Corp",
            "input_id": chosen["id"]}).json()
        self.assertEqual(run["input_id"], chosen["id"])
        self.assertEqual(run["input_name"], "Passage A")
        self.assertEqual(run["prompt_text"],
                         "List entities in Acme Corp opened in Lisbon.")
        self.assertEqual(run["template_text"], "List entities in {input}")

    def test_matrix_can_be_filtered_to_one_input(self):
        prompt = self.client.post("/api/prompts", json={
            "name": "Extract", "text": "List entities in {input}"}).json()
        a = self._input("Passage A", "Acme Corp")
        b = self._input("Passage B", "Globex")

        ra = self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "m", "output": "x",
            "input_id": a["id"]}).json()
        rb = self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "m", "output": "y",
            "input_id": b["id"]}).json()
        self.client.patch(f"/api/runs/{ra['id']}", json={"verdict": "accurate"})
        self.client.patch(f"/api/runs/{rb['id']}", json={"verdict": "inaccurate"})

        both = self.client.get(f"/api/prompts/{prompt['id']}/matrix").json()
        self.assertEqual(both["cells"]["1|m"]["inputs_covered"], 2)
        self.assertEqual(both["cells"]["1|m"]["verdict"], "inaccurate")
        self.assertFalse(both["like_for_like"])

        just_a = self.client.get(
            f"/api/prompts/{prompt['id']}/matrix?input_id={a['id']}").json()
        self.assertEqual(just_a["cells"]["1|m"]["verdict"], "accurate")
        self.assertTrue(just_a["like_for_like"])

    def test_runs_can_be_filtered_by_input(self):
        prompt = self._prompt()
        a = self._input("Passage A", "one")
        self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "m", "output": "x", "input_id": a["id"]})
        self.client.post("/api/runs", json={
            "prompt_id": prompt["id"], "model": "m", "output": "y"})
        filtered = self.client.get(f"/api/runs?input_id={a['id']}").json()["runs"]
        self.assertEqual(len(filtered), 1)

    def test_upload_an_input_file(self):
        response = self.client.post(
            "/api/inputs/upload",
            files={"file": ("passage.txt", io.BytesIO(b"Some material."), "text/plain")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "passage")


if __name__ == "__main__":
    unittest.main(verbosity=2)
