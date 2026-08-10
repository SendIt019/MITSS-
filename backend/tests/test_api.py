"""End-to-end tests of the HTTP layer using FastAPI's test client.

Each test runs against a throwaway root directory so runs never leak between
tests or into the real runs/ folder.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - exercised only on a bare install
    HAVE_FASTAPI = False

if HAVE_FASTAPI:
    from app.main import app

STRUCTURED = """SESSION: api-001
HORIZON: 2026-08-11 08:00 -> 18:00
GRID: 15min

RESOURCE: alpha | Team Alpha | cap 1

TASK: t1 | Survey  | 120min
TASK: t2 | Setup   | 90min | after t1
"""

PROSE = "Survey the north site, then set up the gear, then calibrate everything."

SCHEDULE_REPLY = """Here is the plan.

```json
{
  "session": "api-001",
  "assignments": [
    {"task_id": "t1", "resource_id": "alpha",
     "start": "2026-08-11T08:00:00", "end": "2026-08-11T10:00:00"},
    {"task_id": "t2", "resource_id": "alpha",
     "start": "2026-08-11T10:00:00", "end": "2026-08-11T11:30:00"}
  ],
  "unscheduled": [],
  "rationale": "Sequential."
}
```
"""

PLAN_REPLY = """```json
{
  "session": "from-prose",
  "horizon": {"start": "2026-08-11T08:00:00", "end": "2026-08-11T18:00:00"},
  "granularity_minutes": 30,
  "resources": [{"id": "crew", "name": "Crew", "capacity": 1}],
  "tasks": [
    {"id": "survey", "name": "Survey", "duration_minutes": 120},
    {"id": "setup", "name": "Setup", "duration_minutes": 60, "depends_on": ["survey"]}
  ]
}
```
"""


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

    def _upload(self, text, name="plan.txt"):
        return self.client.post(
            "/api/uploads",
            files={"file": (name, io.BytesIO(text.encode("utf-8")), "text/plain")},
        )

    # -- basics ---------------------------------------------------------

    def test_health(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_llm_status_defaults_to_manual(self):
        body = self.client.get("/api/llm").json()
        self.assertEqual(body["provider"], "manual")
        self.assertFalse(body["available"])

    # -- upload ---------------------------------------------------------

    def test_structured_upload_is_ready_with_a_packet(self):
        body = self._upload(STRUCTURED).json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["session"], "api-001")
        self.assertEqual(len(body["plan"]["tasks"]), 2)
        self.assertIn("```json", body["packet"])
        self.assertIsNone(body["structuring_packet"])

    def test_prose_upload_falls_back_to_the_model(self):
        body = self._upload(PROSE).json()
        self.assertEqual(body["status"], "needs_llm")
        self.assertIsNone(body["plan"])
        self.assertIn("structuring request", body["structuring_packet"])
        self.assertIn(PROSE, body["structuring_packet"])

    def test_broken_grammar_falls_back_and_reports_line_numbers(self):
        broken = STRUCTURED.replace("TASK: t1 | Survey  | 120min", "TASK: t1 | Survey")
        body = self._upload(broken).json()
        self.assertEqual(body["status"], "needs_llm")
        codes = {i["code"] for i in body["issues"]}
        self.assertIn("missing_duration", codes)
        self.assertTrue(any(i["where"].startswith("line") for i in body["issues"]))

    def test_non_txt_rejected(self):
        response = self.client.post(
            "/api/uploads",
            files={"file": ("plan.pdf", io.BytesIO(b"x"), "application/pdf")},
        )
        self.assertEqual(response.status_code, 400)

    def test_empty_file_rejected(self):
        self.assertEqual(self._upload("   ").status_code, 400)

    def test_non_utf8_rejected(self):
        response = self.client.post(
            "/api/uploads",
            files={"file": ("plan.txt", io.BytesIO(b"\xff\xfe\x00bad"), "text/plain")},
        )
        self.assertEqual(response.status_code, 400)

    # -- structuring fallback -------------------------------------------

    def test_attaching_a_model_plan_makes_the_run_ready(self):
        run_id = self._upload(PROSE).json()["run_id"]
        body = self.client.post(
            f"/api/runs/{run_id}/plan", json={"raw": PLAN_REPLY}
        ).json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(len(body["plan"]["tasks"]), 2)
        self.assertIn("```json", body["packet"])

    def test_unparseable_model_plan_keeps_the_run_waiting(self):
        run_id = self._upload(PROSE).json()["run_id"]
        body = self.client.post(
            f"/api/runs/{run_id}/plan", json={"raw": "I could not do it"}
        ).json()
        self.assertEqual(body["status"], "needs_llm")
        self.assertIn("unparseable_output", {i["code"] for i in body["issues"]})

    # -- scheduling round trip ------------------------------------------

    def test_legal_schedule_is_accepted_and_recorded(self):
        run_id = self._upload(STRUCTURED).json()["run_id"]
        body = self.client.post(
            f"/api/runs/{run_id}/ingest",
            json={"raw": SCHEDULE_REPLY, "model": "custom-llm-1", "note": "first"},
        ).json()
        self.assertTrue(body["legal"])
        self.assertEqual(body["status"], "ingested")
        self.assertEqual(body["model"], "custom-llm-1")
        self.assertEqual(len(body["schedule"]["assignments"]), 2)
        self.assertEqual(body["summary"]["tasks_scheduled"], 2)

    def test_illegal_schedule_is_rejected_with_reasons(self):
        run_id = self._upload(STRUCTURED).json()["run_id"]
        broken = SCHEDULE_REPLY.replace('"end": "2026-08-11T10:00:00"',
                                        '"end": "2026-08-11T09:00:00"')
        body = self.client.post(
            f"/api/runs/{run_id}/ingest", json={"raw": broken, "model": "custom-llm-1"}
        ).json()
        self.assertFalse(body["legal"])
        self.assertEqual(body["status"], "rejected")
        codes = {i["code"] for i in body["issues"]}
        self.assertIn("duration_mismatch", codes)

    def test_ingest_before_a_plan_exists_is_a_conflict(self):
        run_id = self._upload(PROSE).json()["run_id"]
        response = self.client.post(
            f"/api/runs/{run_id}/ingest", json={"raw": SCHEDULE_REPLY}
        )
        self.assertEqual(response.status_code, 409)

    def test_solve_with_manual_provider_reports_conflict(self):
        run_id = self._upload(STRUCTURED).json()["run_id"]
        response = self.client.post(f"/api/runs/{run_id}/solve")
        self.assertEqual(response.status_code, 409)
        self.assertIn("paste", response.json()["detail"])

    # -- reads ----------------------------------------------------------

    def test_run_listing_and_detail(self):
        run_id = self._upload(STRUCTURED).json()["run_id"]
        self.client.post(
            f"/api/runs/{run_id}/ingest",
            json={"raw": SCHEDULE_REPLY, "model": "custom-llm-1"},
        )

        listing = self.client.get("/api/runs").json()["runs"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["status"], "ingested")
        self.assertEqual(listing[0]["model"], "custom-llm-1")
        self.assertEqual(listing[0]["assignments"], 2)

        detail = self.client.get(f"/api/runs/{run_id}").json()
        self.assertEqual(detail["run_id"], run_id)
        self.assertIn("SESSION: api-001", detail["source_text"])
        self.assertIsNotNone(detail["schedule"])

    def test_packet_endpoint_returns_markdown(self):
        run_id = self._upload(STRUCTURED).json()["run_id"]
        response = self.client.get(f"/api/runs/{run_id}/packet")
        self.assertEqual(response.status_code, 200)
        self.assertIn("MITSS scheduling request", response.text)

    def test_csv_export(self):
        run_id = self._upload(STRUCTURED).json()["run_id"]
        self.client.post(f"/api/runs/{run_id}/ingest", json={"raw": SCHEDULE_REPLY})
        response = self.client.get(f"/api/runs/{run_id}/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.text.startswith("task_id,"))
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_csv_export_before_a_schedule_is_a_conflict(self):
        run_id = self._upload(STRUCTURED).json()["run_id"]
        self.assertEqual(
            self.client.get(f"/api/runs/{run_id}/export.csv").status_code, 409
        )

    def test_unknown_run_is_404(self):
        self.assertEqual(self.client.get("/api/runs/nope").status_code, 404)

    def test_diff_two_runs_from_different_models(self):
        first = self._upload(STRUCTURED).json()["run_id"]
        self.client.post(f"/api/runs/{first}/ingest",
                         json={"raw": SCHEDULE_REPLY, "model": "model-a"})

        second = self._upload(STRUCTURED).json()["run_id"]
        shifted = SCHEDULE_REPLY.replace("T10:00:00", "T10:30:00")
        self.client.post(f"/api/runs/{second}/ingest",
                         json={"raw": shifted, "model": "model-b"})

        body = self.client.get(f"/api/diff?a={first}&b={second}").json()
        self.assertEqual(body["a"]["model"], "model-a")
        self.assertEqual(body["b"]["model"], "model-b")
        self.assertFalse(body["identical"])
        self.assertTrue(body["changes"])

    def test_diff_of_identical_runs_reports_agreement(self):
        first = self._upload(STRUCTURED).json()["run_id"]
        self.client.post(f"/api/runs/{first}/ingest",
                         json={"raw": SCHEDULE_REPLY, "model": "model-a"})
        second = self._upload(STRUCTURED).json()["run_id"]
        self.client.post(f"/api/runs/{second}/ingest",
                         json={"raw": SCHEDULE_REPLY, "model": "model-b"})
        body = self.client.get(f"/api/diff?a={first}&b={second}").json()
        self.assertTrue(body["identical"])
        self.assertEqual(body["changes"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
