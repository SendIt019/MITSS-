"""Tests for the matrix runner's Step 2: run, resume, status, the manifest,
results.jsonl, the copy folder, notifications and the summary.

Most tests drive the engine with a fake model call that records real runs
in a throwaway store, so interruptions can be placed exactly. One test goes
through the real service.generate_run against a stub server on 127.0.0.1.
No command is run: notifications and sysctl go to a fake runner.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_matrix as rm
from app import service
from pipeline import Store

URL = "http://127.0.0.1:8080/v1/chat/completions"
OUTPUT = "SECRET-OUTPUT-TEXT"


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


class FakeRunner:
    """Stands in for run_command: answers sysctl, the Metal probe and osascript."""

    def __init__(self):
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        if command[0] == "sysctl":
            return str(24 * rm.GIB) if command[-1] == "hw.memsize" else "0"
        if command[0] == "osascript":
            return ""
        return str(20 * rm.GIB)     # the Metal probe

    @property
    def notifications(self):
        return [c[-2:] for c in self.commands if c[0] == "osascript"]


class FakeModel:
    """A model call that records a real run, with scripted misbehaviour.

    `script` maps a call number (from 1) to a function run before the run is
    recorded; it may raise to fail the cell, or signal to interrupt it.
    """

    def __init__(self, root, script=None, finish="stop"):
        self.root = root
        self.script = script or {}
        self.finish = finish
        self.calls = []

    def __call__(self, prompt_id, version, input_id="", model_id="", root=None):
        self.calls.append((model_id, version, input_id))
        effect = self.script.get(len(self.calls))
        if effect:
            effect()
        return self.record(prompt_id, version, input_id, model_id)

    def record(self, prompt_id, version, input_id, model_id):
        shelf = Store(self.root)
        name = shelf.get_model(model_id).name
        run = shelf.create_run(prompt_id, version, name, OUTPUT, source="provider",
                               input_id=input_id,
                               usage={"finish_reason": self.finish,
                                      "completion_tokens": 10})
        return run.to_dict()


class _Base(unittest.TestCase):
    """A throwaway store: one prompt (v1, v2), two inputs, two local models."""

    def setUp(self):
        for name in ("root", "models", "copies"):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            setattr(self, name, tmp.name)
        self.prompt = service.create_prompt("Plan", "Plan: {input}", root=self.root)["id"]
        service.add_version(self.prompt, "Plan well: {input}", root=self.root)
        self.a = service.create_input("Case A", "alpha", root=self.root)["id"]
        self.b = service.create_input("Case B", "beta", root=self.root)["id"]
        self.fast = self.register("fast-8b")
        self.slow = self.register("slow-27b")
        self.runner = FakeRunner()
        self.lines = []

    def register(self, name, url=URL):
        folder = os.path.join(self.models, name)
        os.makedirs(folder)
        with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as handle:
            json.dump({"quantization": {"bits": 4}}, handle)
        return service.register_model(name, url=url, model=folder, root=self.root)["id"]

    def matrix_file(self, **overrides):
        raw = {"name": "t", "prompt_id": self.prompt, "versions": [1, 2],
               "inputs": [self.a, self.b], "models": [self.fast, self.slow]}
        raw.update(overrides)
        path = os.path.join(self.root, "matrix.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        return path

    def start(self, model, only=None, listening=lambda url: True, answer=None, **raw):
        return rm.start_matrix(
            self.matrix_file(**raw), only, yes=answer is None, root=self.root,
            copy_root=self.copies, call=model, run=self.runner,
            listening=listening, ask=lambda prompt: answer or "", out=self.lines.append)

    def resume(self, model, matrix_run_id=None, **kwargs):
        return rm.resume_matrix(matrix_run_id or self.only_run_id(), root=self.root,
                                call=model, run=self.runner,
                                listening=lambda url: True, out=self.lines.append,
                                **kwargs)

    def only_run_id(self):
        names = os.listdir(rm.matrices_dir(self.root))
        self.assertEqual(len(names), 1)
        return names[0]

    def folder(self):
        return rm.matrix_dir(self.only_run_id(), self.root)

    def results(self):
        return rm.read_results(self.folder())

    def events(self, kind):
        return [r for r in self.results() if r.get("event") == kind]

    def summary(self):
        with open(os.path.join(self.folder(), rm.SUMMARY), encoding="utf-8") as handle:
            return handle.read()

    def run_count(self):
        return len(Store(self.root).list_runs())


class RunTests(_Base):
    def test_a_full_run_records_every_cell_model_major(self):
        model = FakeModel(self.root)
        self.assertEqual(self.start(model), rm.EXIT_OK)
        order = [(self.fast, 1, self.a), (self.fast, 1, self.b), (self.fast, 2, self.a),
                 (self.fast, 2, self.b), (self.slow, 1, self.a), (self.slow, 1, self.b),
                 (self.slow, 2, self.a), (self.slow, 2, self.b)]
        self.assertEqual(model.calls, order)
        self.assertEqual(self.run_count(), 8)
        # Every call is bracketed by a started line and a result line.
        kinds = [r["event"] for r in self.results()]
        self.assertEqual(kinds, ["session"] + ["started", "recorded"] * 8 + ["session_end"])
        self.assertIn("recorded 8/8", self.summary())
        self.assertIn("[fast-8b 1/4] v1 × " + self.a + " … recorded", "\n".join(self.lines))
        title, body = self.runner.notifications[-1]
        self.assertEqual(title, "MITSS matrix finished")
        self.assertTrue(body.startswith("8 recorded · 0 failed · 0 truncated · "))

    def test_the_copy_folder_holds_the_files_and_every_run(self):
        self.start(FakeModel(self.root))
        copy = os.path.join(self.copies, self.only_run_id())
        for name in (rm.MANIFEST, rm.RESULTS, rm.SUMMARY):
            self.assertTrue(os.path.isfile(os.path.join(copy, name)), name)
        copied = sorted(os.listdir(os.path.join(copy, "runs")))
        self.assertEqual(copied, sorted(r["run_id"] for r in self.events("recorded")))
        with open(os.path.join(copy, "runs", copied[0], "output.txt"), encoding="utf-8") as h:
            self.assertEqual(h.read(), OUTPUT)

    def test_the_manifest_comes_first_and_lists_every_cell(self):
        seen = {}

        def look():
            manifest = rm.read_manifest(self.folder())
            seen["cells"] = len(manifest["cells"])
            seen["keys"] = set(manifest["models"][0])

        self.start(FakeModel(self.root, {1: look}))
        self.assertEqual(seen["cells"], 8)
        self.assertNotIn("key_env", seen["keys"])
        manifest = rm.read_manifest(self.folder())
        self.assertEqual(manifest["inputs"][self.a]["sha256"],
                         Store(self.root).get_run(self.events("recorded")[0]["run_id"]).input_sha256)

    def test_notifications_carry_counts_never_text(self):
        self.start(FakeModel(self.root))
        for title, body in self.runner.notifications:
            self.assertNotIn(OUTPUT, title + body)
            self.assertNotIn("alpha", title + body)

    def test_truncated_answers_are_flagged(self):
        self.start(FakeModel(self.root, finish="length"), versions=[1], inputs=[self.a])
        self.assertTrue(all(r["truncated"] for r in self.events("recorded")))
        self.assertIn("TRUNCATED", "\n".join(self.lines))
        self.assertIn("2 truncated", self.runner.notifications[-1][1])

    def test_models_option_narrows_the_matrix(self):
        model = FakeModel(self.root)
        self.assertEqual(self.start(model, only=[self.slow]), rm.EXIT_OK)
        self.assertEqual({c[0] for c in model.calls}, {self.slow})
        self.assertEqual(len(rm.read_manifest(self.folder())["cells"]), 4)

    def test_an_unavailable_model_skips_its_remaining_cells(self):
        def gone():
            raise service.ServiceError("model folder not found", 409, unavailable=True)

        model = FakeModel(self.root, {1: gone})
        self.assertEqual(self.start(model), rm.EXIT_FAILED_CELLS)
        self.assertEqual([c[0] for c in model.calls].count(self.fast), 1)
        self.assertEqual(len(self.events("skipped")), 3)
        self.assertEqual(len(self.events("recorded")), 4)
        self.assertIn("skipped 3", self.summary())
        self.assertIn("model folder not found", self.summary())

    def test_a_failed_cell_does_not_stop_the_matrix(self):
        def boom():
            raise service.ServiceError("server said 500", 502)

        self.assertEqual(self.start(FakeModel(self.root, {3: boom})), rm.EXIT_FAILED_CELLS)
        self.assertEqual(len(self.events("recorded")), 7)
        self.assertEqual(self.events("failed")[0]["error"], "server said 500")

    def test_a_stuck_server_stops_the_matrix(self):
        def stuck():
            raise service.ServiceError("model server is stuck", 503)

        self.assertEqual(self.start(FakeModel(self.root, {2: stuck})), rm.EXIT_STOPPED)
        self.assertEqual(len(self.events("started")), 2)
        self.assertIn("stopped - the model server is stuck", self.summary())
        title, body = self.runner.notifications[-1]
        self.assertEqual(title, "MITSS matrix stopped")
        self.assertIn(f"scripts/run_matrix.sh resume {self.only_run_id()}", body)

    def test_first_ctrl_c_finishes_the_current_cell(self):
        def ctrl_c():
            os.kill(os.getpid(), signal.SIGINT)

        self.assertEqual(self.start(FakeModel(self.root, {2: ctrl_c})), rm.EXIT_STOPPED)
        self.assertEqual(len(self.events("recorded")), 2)
        self.assertEqual(len(self.events("started")), 2)
        self.assertIn("not run 6", self.summary())
        self.assertIn("Resume:", self.summary())
        # The handler is put back afterwards.
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def test_second_ctrl_c_stops_at_once_and_still_writes_the_summary(self):
        def ctrl_c_twice():
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGINT)

        self.assertEqual(self.start(FakeModel(self.root, {2: ctrl_c_twice})),
                         rm.EXIT_STOPPED)
        self.assertEqual(len(self.events("recorded")), 1)
        self.assertEqual(len(self.events("started")), 2)     # cell 2 left started
        self.assertEqual(self.run_count(), 1)
        self.assertIn("stopped at once by a second Ctrl-C", self.summary())
        self.assertEqual(self.runner.notifications[-1][0], "MITSS matrix stopped")

    def test_an_unexpected_error_still_writes_the_summary(self):
        def bug():
            raise RuntimeError("disk on fire")

        with self.assertLogs("run_matrix", level="ERROR"):
            code = self.start(FakeModel(self.root, {1: bug}))
        self.assertEqual(code, rm.EXIT_STOPPED)
        self.assertIn("unexpected error: RuntimeError('disk on fire')", self.summary())

    def test_declining_the_prompt_creates_nothing(self):
        model = FakeModel(self.root)
        self.assertEqual(self.start(model, answer="n"), rm.EXIT_STOPPED)
        self.assertEqual(model.calls, [])
        self.assertFalse(os.path.exists(rm.matrices_dir(self.root)))

    def test_yes_at_the_prompt_runs(self):
        self.assertEqual(self.start(FakeModel(self.root), answer="y", versions=[1],
                                    inputs=[self.a]), rm.EXIT_OK)

    def test_a_server_that_is_not_listening_stops_before_anything_is_written(self):
        model = FakeModel(self.root)
        code = self.start(model, listening=lambda url: False)
        self.assertEqual(code, rm.EXIT_INVALID)
        self.assertEqual(model.calls, [])
        self.assertFalse(os.path.exists(rm.matrices_dir(self.root)))
        self.assertIn("scripts/start_model_server.sh", "\n".join(self.lines))

    def test_an_invalid_matrix_exits_three(self):
        self.assertEqual(self.start(FakeModel(self.root), versions=[9]), rm.EXIT_INVALID)

    def test_a_copy_failure_is_noted_not_fatal(self):
        blocker = os.path.join(self.copies, "not-a-folder")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        self.copies = blocker
        code = self.start(FakeModel(self.root), versions=[1], inputs=[self.a])
        self.assertEqual(code, rm.EXIT_OK)
        self.assertEqual(len(self.events("recorded")), 2)
        self.assertIn("copy failed", self.summary())
        self.assertIn("the copy folder was not updated", "\n".join(self.lines))


class ResumeTests(_Base):
    def interrupt_at(self, call_number, effect=None):
        """Run the matrix and stop it hard during the given call."""
        def ctrl_c_twice():
            if effect:
                effect()
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGINT)

        model = FakeModel(self.root, {call_number: ctrl_c_twice})
        self.assertEqual(self.start(model), rm.EXIT_STOPPED)
        return model

    def test_resume_runs_only_unrecorded_cells(self):
        def ctrl_c():
            os.kill(os.getpid(), signal.SIGINT)

        self.start(FakeModel(self.root, {3: ctrl_c}))
        manifest_before = read_bytes(os.path.join(self.folder(), rm.MANIFEST))
        model = FakeModel(self.root)
        self.assertEqual(self.resume(model), rm.EXIT_OK)
        self.assertEqual(len(model.calls), 5)
        self.assertEqual(self.run_count(), 8)
        self.assertIn("recorded 8/8", self.summary().split("=" * 72)[-1])
        # The manifest is never rewritten; the summary is appended to.
        self.assertEqual(read_bytes(os.path.join(self.folder(), rm.MANIFEST)),
                         manifest_before)
        self.assertEqual(self.summary().count("MITSS matrix"), 2)

    def test_nothing_to_resume(self):
        self.start(FakeModel(self.root), versions=[1], inputs=[self.a])
        model = FakeModel(self.root)
        self.assertEqual(self.resume(model), rm.EXIT_OK)
        self.assertEqual(model.calls, [])

    def test_resume_adopts_a_run_recorded_just_before_the_interrupt(self):
        holder = {}

        def record_first():
            holder["run"] = FakeModel(self.root).record(self.prompt, 1, self.b, self.fast)

        self.interrupt_at(2, record_first)
        model = FakeModel(self.root)
        self.assertEqual(self.resume(model), rm.EXIT_OK)
        self.assertNotIn((self.fast, 1, self.b), model.calls)
        adopted = [r for r in self.events("recorded") if r.get("adopted")]
        self.assertEqual([r["run_id"] for r in adopted], [holder["run"]["id"]])
        self.assertTrue(adopted[0]["index_event"])
        self.assertEqual(self.run_count(), 8)

    def test_resume_adopts_a_run_whose_index_event_was_never_written(self):
        # The second Ctrl-C lands inside create_run, after the run folder and
        # transcript are written but before the index.jsonl event.
        def record_without_event():
            with mock.patch.object(Store, "append_event", side_effect=KeyboardInterrupt):
                FakeModel(self.root).record(self.prompt, 1, self.b, self.fast)

        model = FakeModel(self.root, {2: record_without_event})
        self.assertEqual(self.start(model), rm.EXIT_STOPPED)
        self.assertEqual(self.run_count(), 2)
        resumed = FakeModel(self.root)
        self.assertEqual(self.resume(resumed), rm.EXIT_OK)
        self.assertEqual(len(resumed.calls), 6)
        adopted = [r for r in self.events("recorded") if r.get("adopted")]
        self.assertEqual(len(adopted), 1)
        self.assertFalse(adopted[0]["index_event"])
        self.assertIn("no run_recorded event in index.jsonl (not repaired)",
                      self.summary())
        self.assertEqual(self.run_count(), 8)

    def test_a_run_another_cell_claimed_is_never_adopted(self):
        # Two repeats of one cell: repeat 1 records, repeat 2 is cut off
        # before anything is recorded. Repeat 1's run matches repeat 2's cell
        # and may share its second; it must not be adopted.
        self.start(FakeModel(self.root, {2: lambda: (os.kill(os.getpid(), signal.SIGINT),
                                                     os.kill(os.getpid(), signal.SIGINT))}),
                   models=[self.fast], versions=[1], inputs=[self.a], repeats=2)
        resumed = FakeModel(self.root)
        self.assertEqual(self.resume(resumed), rm.EXIT_OK)
        self.assertEqual(len(resumed.calls), 1)
        self.assertEqual(self.run_count(), 2)
        self.assertFalse([r for r in self.events("recorded") if r.get("adopted")])

    def adoption_at_offset(self, seconds):
        """Interrupt cell 2 before it records, then plant a matching run and
        a started line `seconds` after that run's created_at."""
        self.interrupt_at(2)
        planted = Store(self.root).get_run(
            FakeModel(self.root).record(self.prompt, 1, self.b, self.fast)["id"])
        moment = datetime.fromisoformat(planted.created_at) + timedelta(seconds=seconds)
        rm.append_result(self.folder(), {"event": "started", "cell": 2,
                                         "at": moment.isoformat(timespec="seconds")})
        resumed = FakeModel(self.root)
        self.resume(resumed)
        return planted.id, resumed

    def test_a_run_from_the_same_second_is_adopted(self):
        planted, resumed = self.adoption_at_offset(0)
        self.assertIn(planted, [r["run_id"] for r in self.events("recorded")])
        self.assertEqual(len(resumed.calls), 6)

    def test_a_run_from_the_second_before_is_not_adopted(self):
        planted, resumed = self.adoption_at_offset(1)
        self.assertNotIn(planted, [r["run_id"] for r in self.events("recorded")])
        self.assertEqual(len(resumed.calls), 7)

    def test_a_time_zone_aware_started_time_is_compared_in_local_time(self):
        self.interrupt_at(2)
        planted = FakeModel(self.root).record(self.prompt, 1, self.b, self.fast)["id"]
        local = datetime.fromisoformat(Store(self.root).get_run(planted).created_at)
        aware = local.astimezone()          # same instant, with an offset
        rm.append_result(self.folder(), {"event": "started", "cell": 2,
                                         "at": aware.isoformat(timespec="seconds")})
        self.resume(FakeModel(self.root))
        self.assertIn(planted, [r["run_id"] for r in self.events("recorded")])

    def test_a_changed_input_is_refused_unless_allowed(self):
        self.interrupt_at(1)
        service.update_input(self.a, text="alpha, edited", root=self.root)
        model = FakeModel(self.root)
        self.assertEqual(self.resume(model), rm.EXIT_INVALID)
        self.assertEqual(model.calls, [])
        self.assertIn(f"input '{self.a}' changed", "\n".join(self.lines))
        self.assertEqual(self.resume(model, allow_changed_inputs=True), rm.EXIT_OK)

    def test_a_deleted_input_is_refused_even_when_changes_are_allowed(self):
        self.interrupt_at(1)
        service.delete_input(self.a, root=self.root)
        self.assertEqual(self.resume(FakeModel(self.root), allow_changed_inputs=True),
                         rm.EXIT_INVALID)
        self.assertIn("no longer exists", "\n".join(self.lines))

    def test_a_model_quarantined_since_is_refused(self):
        self.interrupt_at(1)
        service.update_model(self.slow, quarantine="broken", root=self.root)
        self.assertEqual(self.resume(FakeModel(self.root)), rm.EXIT_INVALID)
        self.assertIn("is quarantined: broken", "\n".join(self.lines))

    def test_an_unsafe_id_is_refused(self):
        self.assertEqual(rm.resume_matrix("../escape", root=self.root,
                                          out=self.lines.append), rm.EXIT_INVALID)
        self.assertEqual(rm.resume_matrix("no-such-run", root=self.root,
                                          out=self.lines.append), rm.EXIT_INVALID)


class StatusTests(_Base):
    def test_status_lists_and_shows_runs(self):
        self.assertEqual(rm.status_lines(root=self.root), ["No matrix runs yet."])
        self.start(FakeModel(self.root), versions=[1], inputs=[self.a])
        listing = rm.status_lines(root=self.root)
        self.assertEqual(len(listing), 1)
        self.assertIn("2/2 recorded", listing[0])
        self.assertIn("(finished)", listing[0])
        detail = "\n".join(rm.status_lines(self.only_run_id(), root=self.root))
        self.assertIn("recorded 2/2", detail)
        with self.assertRaises(rm.MatrixError):
            rm.status_lines("../x", root=self.root)

    def test_status_through_the_command_line(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MITSS_ROOT": self.root}), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(rm.main(["status"]), rm.EXIT_OK)
            self.assertEqual(rm.main(["status", "missing-run"]), rm.EXIT_INVALID)
        self.assertIn("No matrix runs yet.", out.getvalue())


class FileTests(unittest.TestCase):
    def test_a_line_cut_short_is_skipped_and_closed_off(self):
        with tempfile.TemporaryDirectory() as folder:
            rm.append_result(folder, {"event": "started", "cell": 1})
            with open(os.path.join(folder, rm.RESULTS), "a", encoding="utf-8") as handle:
                handle.write('{"event": "recor')           # a hard stop mid-line
            rm.append_result(folder, {"event": "failed", "cell": 1, "error": "x"})
            self.assertEqual([r["event"] for r in rm.read_results(folder)],
                             ["started", "failed"])

    def test_a_recorded_cell_stays_recorded(self):
        states = rm.cell_states([
            {"event": "started", "cell": 1, "at": "2026-09-29T10:00:00"},
            {"event": "recorded", "cell": 1, "run_id": "r1"},
            {"event": "started", "cell": 1, "at": "2026-09-29T10:05:00"},
            {"event": "started", "cell": 2, "at": "2026-09-29T10:06:00"},
        ])
        self.assertEqual(states[1]["event"], "recorded")
        self.assertEqual(states[2]["started_at"], "2026-09-29T10:06:00")

    def test_formats(self):
        self.assertEqual(rm.format_elapsed(41.2), "41s")
        self.assertEqual(rm.format_elapsed(2880), "48m00s")
        self.assertEqual(rm.format_elapsed(3720), "1h02m")
        total = {"recorded": 24, "failed": 0, "skipped": 0, "truncated": 2}
        self.assertEqual(rm.notification_body(total, 2880),
                         "24 recorded · 0 failed · 2 truncated · 48m00s")

    def test_the_manifest_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            rm.write_manifest(folder, {"a": 1})
            with self.assertRaises(FileExistsError):
                rm.write_manifest(folder, {"a": 2})


# The model each request to the stub server named, in order.
MODELS_ASKED = []


class _StubModelServer(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        MODELS_ASKED.append(body.get("model"))
        reply = json.dumps({
            "choices": [{"message": {"content": "stub reply"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


class ThroughTheServiceTests(_Base):
    """The real recording path: service.generate_run against a stub server."""

    def setUp(self):
        super().setUp()
        MODELS_ASKED.clear()
        server = HTTPServer(("127.0.0.1", 0), _StubModelServer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        for model_id in (self.fast, self.slow):
            service.update_model(model_id, url=url, root=self.root)

    def test_runs_are_ordinary_mitss_runs_and_data_only_grows(self):
        shelf = Store(self.root)
        index_before = read_bytes(shelf.index_path)
        code = rm.start_matrix(self.matrix_file(), yes=True, root=self.root,
                               copy_root=self.copies, run=self.runner,
                               out=self.lines.append)       # real call, real probe
        self.assertEqual(code, rm.EXIT_OK, "\n".join(self.lines))
        runs = shelf.list_runs()
        self.assertEqual(len(runs), 8)
        self.assertEqual(sorted({r.model for r in runs}), ["fast-8b", "slow-27b"])
        self.assertEqual({r.source for r in runs}, {"provider"})
        # Model-major: the server is asked for each model folder in one stretch.
        switches = [m for i, m in enumerate(MODELS_ASKED)
                    if i == 0 or m != MODELS_ASKED[i - 1]]
        self.assertEqual(switches, [os.path.join(self.models, "fast-8b"),
                                    os.path.join(self.models, "slow-27b")])
        # index.jsonl and the transcript were appended to, never rewritten.
        index_after = read_bytes(shelf.index_path)
        self.assertTrue(index_after.startswith(index_before))
        transcript = service.transcript(root=self.root)
        self.assertEqual(transcript.count("stub reply"), 8)


if __name__ == "__main__":
    unittest.main()
