"""Tests for the matrix runner's Step 1: matrix files, plan, check, wrapper.

Everything runs against throwaway data roots and model folders. No model is
called: `check` gets a fake command runner and a fake port probe (plus one
real probe against a socket this test opens on 127.0.0.1), so no sysctl,
osascript or mlx process is started.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_matrix as rm
from app import service
from pipeline import Store
from pipeline.store import text_sha256

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(os.path.dirname(BACKEND), "scripts", "run_matrix.sh")
CLOSED = "http://127.0.0.1:9/v1/chat/completions"


def _matrix(**overrides):
    raw = {"name": "baseline-v1", "prompt_id": "p", "versions": [1],
           "inputs": ["a"], "models": ["m"], "repeats": 1}
    raw.update(overrides)
    return raw


class MatrixFileTests(unittest.TestCase):
    def problems(self, raw):
        with self.assertRaises(rm.MatrixError) as caught:
            rm.parse_matrix(raw)
        return " | ".join(caught.exception.problems)

    def test_a_valid_file_parses(self):
        matrix = rm.parse_matrix(_matrix(versions=[3, 4], repeats=2))
        self.assertEqual(matrix.versions, [3, 4])
        self.assertEqual(matrix.repeats, 2)

    def test_repeats_defaults_to_one(self):
        raw = _matrix()
        del raw["repeats"]
        self.assertEqual(rm.parse_matrix(raw).repeats, 1)

    def test_bad_repeats_are_refused(self):
        for bad in (0, 11, -1, True, "2", 1.5, None):
            with self.subTest(repeats=bad):
                self.assertIn("repeats", self.problems(_matrix(repeats=bad)))

    def test_unsafe_names_are_refused(self):
        for bad in ("../escape", "Baseline", "has space", "-leading", "", None,
                    "a/b", "x" * (rm.MAX_NAME + 1)):
            with self.subTest(name=bad):
                self.assertIn("name", self.problems(_matrix(name=bad)))

    def test_unsafe_ids_are_refused_before_any_lookup(self):
        text = self.problems(_matrix(prompt_id="../p", inputs=["ok", "../x"],
                                     models=["M"]))
        self.assertIn("prompt_id", text)
        self.assertIn("inputs: '../x'", text)
        self.assertIn("models: 'M'", text)

    def test_unknown_keys_and_duplicates_are_refused(self):
        text = self.problems(_matrix(model=["m"], versions=[1, 1], inputs=["a", "a"]))
        self.assertIn("unknown key(s): model", text)
        self.assertIn("versions lists the same version twice", text)
        self.assertIn("inputs lists the same id twice", text)

    def test_versions_must_be_positive_integers(self):
        for bad in ([], ["1"], [0], [True], [1.0], "1"):
            with self.subTest(versions=bad):
                self.assertIn("version", self.problems(_matrix(versions=bad)))

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(rm.MatrixError) as caught:
            rm.parse_matrix(_matrix(name="Bad", repeats=0, models=[]))
        self.assertEqual(len(caught.exception.problems), 3)

    def test_a_file_that_is_not_json_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(rm.MatrixError) as caught:
                rm.load_matrix(path)
            self.assertIn("not valid JSON", caught.exception.problems[0])
            with self.assertRaises(rm.MatrixError):
                rm.load_matrix(os.path.join(tmp, "missing.json"))
            with self.assertRaises(rm.MatrixError):
                rm.parse_matrix([1, 2])


class _DataRoot(unittest.TestCase):
    """A throwaway store with one prompt (v1, v2), two inputs, three models."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.prompt = service.create_prompt("Plan", "Plan this: {input}",
                                            root=self.root)["id"]
        service.add_version(self.prompt, "Plan this well: {input}", root=self.root)
        self.input_a = service.create_input("Case A", "alpha", root=self.root)["id"]
        self.input_b = service.create_input("Case B", "beta", root=self.root)["id"]
        self.fast = service.register_model("fast-8b", url=CLOSED, root=self.root)["id"]
        self.slow = service.register_model("slow-27b", url=CLOSED, root=self.root)["id"]
        self.paste = service.register_model("paste-only", root=self.root)["id"]

    def matrix(self, **overrides):
        raw = {"name": "t", "prompt_id": self.prompt, "versions": [1, 2],
               "inputs": [self.input_a, self.input_b],
               "models": [self.fast, self.slow], "repeats": 1}
        raw.update(overrides)
        return rm.parse_matrix(raw)


class PlanTests(_DataRoot):
    def refusal(self, matrix, only=None):
        with self.assertRaises(rm.MatrixError) as caught:
            rm.build_plan(matrix, only, root=self.root)
        return " | ".join(caught.exception.problems)

    def test_cells_are_model_major(self):
        plan = rm.build_plan(self.matrix(repeats=2), root=self.root)
        self.assertEqual(len(plan.cells), 2 * 2 * 2 * 2)
        order = [(c.model_id, c.version, c.input_id, c.repeat) for c in plan.cells]
        self.assertEqual(order, sorted(order, key=lambda o: (
            [self.fast, self.slow].index(o[0]), o[1],
            [self.input_a, self.input_b].index(o[2]), o[3])))
        # Every cell of the first model comes before any of the second.
        first = [c.model_id for c in plan.cells]
        self.assertEqual(first, [self.fast] * 8 + [self.slow] * 8)
        self.assertEqual([c.index for c in plan.cells], list(range(1, 17)))
        self.assertEqual(plan.cells[0].model, "fast-8b")

    def test_matrix_order_of_models_is_kept(self):
        plan = rm.build_plan(self.matrix(models=[self.slow, self.fast]), root=self.root)
        self.assertEqual(plan.cells[0].model_id, self.slow)

    def test_input_hashes_match_the_store(self):
        plan = rm.build_plan(self.matrix(), root=self.root)
        self.assertEqual(plan.inputs[self.input_a],
                         {"name": "Case A", "sha256": text_sha256("alpha")})

    def test_unknown_ids_are_all_reported(self):
        text = self.refusal(self.matrix(versions=[1, 9], inputs=["nope"],
                                        models=[self.fast, "ghost"]))
        self.assertIn("has no version 9", text)
        self.assertIn("nope", text)
        self.assertIn("ghost", text)
        self.assertIn("prompt", self.refusal(self.matrix(prompt_id="missing")))

    def test_a_quarantined_model_is_refused_not_skipped(self):
        service.update_model(self.slow, quarantine="bad tokenizer", root=self.root)
        text = self.refusal(self.matrix())
        self.assertIn("quarantined: bad tokenizer", text)

    def test_a_paste_only_model_is_refused(self):
        self.assertIn("paste-only", self.refusal(self.matrix(models=[self.paste])))

    def test_models_option_narrows_and_checks(self):
        plan = rm.build_plan(self.matrix(), [self.slow], root=self.root)
        self.assertEqual({c.model_id for c in plan.cells}, {self.slow})
        self.assertIn("not in this matrix", self.refusal(self.matrix(), ["other"]))

    def test_estimate_uses_only_real_token_rates(self):
        entry = service.get_model(self.fast, root=self.root)
        self.assertIsNone(rm.estimate_cell_seconds(entry, root=self.root))
        shelf = Store(self.root)
        shelf.create_run(self.prompt, 1, "fast-8b", "x", source="provider",
                         usage={"completion_tokens": 400, "tokens_per_second": 40.0})
        shelf.create_run(self.prompt, 1, "fast-8b", "y", source="provider",
                         usage={"completion_tokens": 100})      # no rate: ignored
        guess = rm.estimate_cell_seconds(entry, root=self.root)
        self.assertEqual(guess["runs"], 1)
        self.assertAlmostEqual(guess["seconds"], 10.0)

    def test_estimate_is_capped_at_max_tokens(self):
        service.update_model(self.fast, settings={"max_tokens": 200}, root=self.root)
        Store(self.root).create_run(
            self.prompt, 1, "fast-8b", "x", source="provider",
            usage={"completion_tokens": 800, "tokens_per_second": 20.0})
        entry = service.get_model(self.fast, root=self.root)
        self.assertAlmostEqual(rm.estimate_cell_seconds(entry, root=self.root)["seconds"], 10.0)

    def test_plan_output(self):
        plan = rm.build_plan(self.matrix(), root=self.root)
        text = rm.format_plan(plan, {self.fast: {"seconds": 90, "rate": 30.0, "runs": 4},
                                     self.slow: None})
        self.assertIn("1. fast-8b ", text)
        self.assertIn("4 cells  ~6m", text)
        self.assertIn("no estimate", text)
        self.assertIn("Total: 8 cells, ~6m for the models with an estimate", text)

    def test_durations(self):
        self.assertEqual(rm.format_duration(1), "~1m")
        self.assertEqual(rm.format_duration(3540), "~59m")
        self.assertEqual(rm.format_duration(3600), "~1h00m")
        self.assertEqual(rm.format_duration(3661), "~1h02m")


class CommandTests(unittest.TestCase):
    def test_commands_are_argument_lists(self):
        self.assertEqual(rm.sysctl_command("hw.memsize"), ["sysctl", "-n", "hw.memsize"])
        probe = rm.metal_command()
        self.assertTrue(probe[0].endswith(os.path.join("models-env", "bin", "python"))
                        or os.environ.get("MITSS_MODELS_ENV"))
        self.assertEqual(probe[1], "-c")

    def test_notification_text_is_passed_as_arguments(self):
        title = 'He said "hi" & left'
        body = "end run\" -- do shell script \"rm"
        command = rm.notification_command(title, body)
        self.assertEqual(command[0], "osascript")
        self.assertEqual(command[-2:], [title, body])
        script = " ".join(command[1:-2])
        self.assertNotIn(title, script)
        self.assertNotIn(body, script)
        self.assertIn('sound name "Glass"', script)

    def test_run_command_never_uses_a_shell(self):
        with mock.patch.object(rm.subprocess, "run") as fake:
            fake.return_value = subprocess.CompletedProcess([], 0, "42\n", "")
            self.assertEqual(rm.run_command(["sysctl", "-n", "x"]), "42\n")
            args, kwargs = fake.call_args
            self.assertEqual(args[0], ["sysctl", "-n", "x"])
            self.assertNotIn("shell", kwargs)
            fake.return_value = subprocess.CompletedProcess([], 1, "", "no")
            self.assertIsNone(rm.run_command(["sysctl"]))
            fake.side_effect = FileNotFoundError()
            self.assertIsNone(rm.run_command(["nothing"]))


def _runner(answers):
    """A fake command runner. sysctl answers by key name, the Metal probe by
    "metal", osascript by "osascript". Records every command it was given."""
    asked = []

    def run(command):
        asked.append(command)
        if command[0] == "sysctl":
            return answers.get(command[-1])
        if command[0] == "osascript":
            return answers.get("osascript", "")
        if command[1:2] == ["-c"]:
            return answers.get("metal")
        return None

    run.asked = asked
    return run


class MemoryTests(unittest.TestCase):
    RAM = str(24 * rm.GIB)

    def test_wired_limit_wins_when_set(self):
        memory = rm.read_memory(_runner({"hw.memsize": self.RAM,
                                         "iogpu.wired_limit_mb": "20480",
                                         "metal": "999"}))
        self.assertEqual(memory.limit, 20480 * rm.MIB)
        self.assertEqual(memory.source, "iogpu.wired_limit_mb")

    def test_metal_working_set_when_wired_limit_is_zero(self):
        run = _runner({"hw.memsize": self.RAM, "iogpu.wired_limit_mb": "0",
                       "metal": "19069665280\n"})
        memory = rm.read_memory(run)
        self.assertEqual(memory.limit, 19069665280)
        self.assertIn("Metal", memory.source)

    def test_fraction_of_ram_when_nothing_is_readable(self):
        self.assertEqual(rm.read_memory(_runner({"hw.memsize": self.RAM})).limit,
                         16 * rm.GIB)
        big = str(64 * rm.GIB)
        self.assertEqual(rm.read_memory(_runner({"hw.memsize": big})).limit, 48 * rm.GIB)
        self.assertIsNone(rm.read_memory(_runner({})).limit)

    def test_warning_threshold_and_command(self):
        limit = 16 * rm.GIB
        self.assertIsNone(rm.memory_warning(14 * rm.GIB, limit))       # exactly fits
        warning = rm.memory_warning(14 * rm.GIB + 1, limit)
        self.assertIn("sudo sysctl iogpu.wired_limit_mb=20480", warning)
        self.assertIsNone(rm.memory_warning(99 * rm.GIB, None))
        # Too big for 20 GiB: a figure that would actually fit, rounded up
        # to a whole GiB, unless that would starve macOS.
        self.assertIn("=23552", rm.memory_warning(21 * rm.GIB, limit))
        self.assertIn("=23552", rm.memory_warning(21 * rm.GIB, limit, 32 * rm.GIB))
        refused = rm.memory_warning(21 * rm.GIB, limit, 24 * rm.GIB)
        self.assertIn("does not fit on this Mac", refused)
        self.assertNotIn("sudo", refused)
        # The lineup's case: 14.3 GiB on a 24 GiB Mac still gets 20480.
        self.assertIn("=20480", rm.memory_warning(15341205776, limit, 24 * rm.GIB))

    def test_quantization(self):
        self.assertEqual(rm.quantization({"quantization": {"bits": 4, "group_size": 64}}),
                         "4-bit")
        mixed = {"quantization": {"bits": 4, "group_size": 64,
                                  "layers.0.router": {"bits": 8, "group_size": 64}}}
        self.assertEqual(rm.quantization(mixed), "4-bit, mixed (8-bit on some layers)")
        self.assertIn("not quantized", rm.quantization({}))


class CheckTests(_DataRoot):
    def setUp(self):
        super().setUp()
        folders = tempfile.TemporaryDirectory()
        self.addCleanup(folders.cleanup)
        self.folder = os.path.join(folders.name, "slow-27b")
        os.makedirs(self.folder)
        with open(os.path.join(self.folder, "config.json"), "w", encoding="utf-8") as h:
            json.dump({"quantization": {"bits": 4, "group_size": 64}}, h)
        with open(os.path.join(self.folder, "model.safetensors"), "wb") as h:
            h.truncate(3 * rm.MIB)
        service.update_model(self.slow, model=self.folder, settings={
            "temperature": 0.2, "chat_template_kwargs": {"enable_thinking": False}},
            root=self.root)
        service.update_model(self.fast, model=os.path.join(folders.name, "absent"),
                             root=self.root)
        self.run_fake = _runner({"hw.memsize": str(24 * rm.GIB),
                                 "iogpu.wired_limit_mb": "0", "metal": str(2 * rm.MIB)})

    def check(self, models, listening=lambda url: True):
        return rm.check(models, root=self.root, run=self.run_fake, listening=listening)

    def test_a_good_model_passes_and_reports_facts(self):
        report = self.check([self.slow])
        text = "\n".join(report.lines)
        self.assertIn("weights 0.0 GiB, 4-bit", text)
        self.assertIn("enable_thinking=false", text)
        self.assertIn("listening", text)
        self.assertIn("Test notification sent", text)
        self.assertEqual(report.problems, 0)
        # 3 MiB of weights + 2 GiB headroom is over the fake 2 MiB limit.
        self.assertEqual(report.warnings, 1)
        self.assertIn("sudo sysctl iogpu.wired_limit_mb=20480", text)

    def test_problems_are_counted(self):
        service.update_model(self.slow, quarantine="broken", root=self.root)
        report = self.check([self.fast, self.slow, "ghost"], listening=lambda url: False)
        text = "\n".join(report.lines)
        self.assertIn("model folder not found", text)
        self.assertIn("quarantined: broken", text)
        self.assertIn("ghost", text)
        self.assertIn("not registered", text)
        self.assertIn("start the model server first: scripts/start_model_server.sh llama-3.1-8b 9",
                      text)
        self.assertEqual(report.problems, 4)

    def test_no_real_command_is_run(self):
        self.check([self.slow])
        kinds = {c[0] for c in self.run_fake.asked}
        self.assertEqual(kinds, {"sysctl", "osascript", rm.metal_command()[0]})
        notice = next(c for c in self.run_fake.asked if c[0] == "osascript")
        self.assertEqual(notice[-2:], ["MITSS matrix check",
                                       "Notifications work. No model was called."])

    def test_only_the_named_variables_are_printed(self):
        secret = "sk-do-not-print-0000"
        with mock.patch.dict(os.environ, {"MITSS_LLM_API_KEY": secret,
                                          "MITSS_LLM_URL": "http://example.invalid",
                                          "MITSS_LLM_TIMEOUT": "77"}):
            text = "\n".join(self.check([self.slow]).lines)
        self.assertNotIn(secret, text)
        self.assertNotIn("MITSS_LLM_API_KEY", text)
        self.assertNotIn("example.invalid", text)
        self.assertIn("MITSS_LLM_TIMEOUT = 77", text)
        printed = [line.split("=")[0].strip() for line in rm.environment_lines({})]
        self.assertEqual(tuple(printed), rm.ENV_SHOWN)

    def test_dotenv_is_never_opened(self):
        opened = []
        real_open = builtins.open

        def spy(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", spy):
            self.check([self.slow, self.fast])
            rm.build_plan(self.matrix(), root=self.root)
        self.assertFalse([p for p in opened if os.path.basename(p).startswith(".env")])

    def test_real_port_probe_on_loopback(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            self.assertTrue(rm.server_listening(f"http://127.0.0.1:{port}/v1"))
        finally:
            listener.close()
        self.assertFalse(rm.server_listening(f"http://127.0.0.1:{port}/v1", timeout=0.5))
        self.assertIsNone(rm.server_listening("https://api.example.invalid/v1"))


class _EnvSpy(dict):
    """A stand-in for os.environ that records every name looked up."""

    def __init__(self, base):
        super().__init__(base)
        self.asked = set()

    def get(self, key, default=None):
        self.asked.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.asked.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.asked.add(key)
        return super().__contains__(key)


class ReviewFixTests(_DataRoot):
    """Codex's Step 1 review: key variables, remote endpoints, store writes."""

    def fake_run(self):
        return _runner({"hw.memsize": str(24 * rm.GIB), "iogpu.wired_limit_mb": "0",
                        "metal": str(20 * rm.GIB)})

    def test_the_key_variable_is_never_looked_up(self):
        keyed = service.register_model("keyed-7b", url=CLOSED, key_env="TEAM_7B_KEY",
                                       root=self.root)["id"]
        spy = _EnvSpy(os.environ)
        spy["TEAM_7B_KEY"] = "sk-never-read"
        with mock.patch.object(os, "environ", spy), \
                mock.patch.object(service, "_model_payload",
                                  side_effect=AssertionError("service.get_model used")):
            report = rm.check([keyed], root=self.root, run=self.fake_run(),
                              listening=lambda url: True)
            plan = rm.build_plan(self.matrix(models=[keyed]), root=self.root)
            rm.estimate_cell_seconds(plan.models[0], root=self.root)
        self.assertNotIn("TEAM_7B_KEY", spy.asked)
        self.assertNotIn("sk-never-read", "\n".join(report.lines))
        self.assertNotIn("key_set", plan.models[0])

    def test_remote_endpoints_are_refused(self):
        remote = service.register_model(
            "cloud-70b", url="https://api.example.invalid/v1/chat/completions",
            root=self.root)["id"]
        with self.assertRaises(rm.MatrixError) as caught:
            rm.build_plan(self.matrix(models=[self.fast, remote]), root=self.root)
        self.assertIn("not on this machine", " | ".join(caught.exception.problems))

        probed = []
        report = rm.check([remote], root=self.root, run=self.fake_run(),
                          listening=lambda url: probed.append(url) or True)
        self.assertEqual(report.problems, 1)
        self.assertIn("only calls loopback endpoints", "\n".join(report.lines))
        self.assertEqual(probed, [])

    def test_every_loopback_spelling_is_accepted(self):
        for url in ("http://127.0.0.1:8080/v1/chat/completions",
                    "http://localhost:8080/v1/chat/completions",
                    "http://[::1]:8080/v1/chat/completions"):
            with self.subTest(url=url):
                service.update_model(self.fast, url=url, root=self.root)
                plan = rm.build_plan(self.matrix(models=[self.fast]), root=self.root)
                self.assertEqual(plan.models[0]["url"], url)

    def test_nothing_is_created_under_a_missing_data_directory(self):
        with tempfile.TemporaryDirectory() as empty:
            rm.check(rm.LINEUP, root=empty, run=self.fake_run(),
                     listening=lambda url: True)
            with self.assertRaises(rm.MatrixError):
                rm.build_plan(self.matrix(), root=empty)
            rm.estimate_cell_seconds({"name": "x", "settings": {}}, root=empty)
            self.assertEqual(os.listdir(empty), [])

    def test_an_existing_data_directory_is_left_exactly_as_it_was(self):
        def snapshot():
            found = {}
            for folder, _, files in os.walk(self.root):
                for name in files:
                    path = os.path.join(folder, name)
                    with open(path, "rb") as handle:
                        found[path] = handle.read()
                found[folder] = None
            return found

        before = snapshot()
        rm.check([self.fast, self.slow, "ghost"], root=self.root,
                 run=self.fake_run(), listening=lambda url: False)
        plan = rm.build_plan(self.matrix(), root=self.root)
        rm.estimate_cell_seconds(plan.models[0], root=self.root)
        self.assertEqual(snapshot(), before)


class RoundTwoTests(_DataRoot):
    """Codex's Step 1 round 2: invalid ports, and the model-name fallback."""

    def fake_run(self):
        return _runner({"hw.memsize": str(24 * rm.GIB), "iogpu.wired_limit_mb": "0",
                        "metal": str(20 * rm.GIB)})

    def test_invalid_ports_are_refused_not_crashed_on(self):
        for url in ("http://127.0.0.1:abc/v1/chat/completions",
                    "http://127.0.0.1:70000/v1/chat/completions",
                    "http://localhost:-1/v1"):
            with self.subTest(url=url):
                service.update_model(self.fast, url=url, root=self.root)
                with self.assertRaises(rm.MatrixError) as caught:
                    rm.build_plan(self.matrix(models=[self.fast]), root=self.root)
                self.assertIn("invalid port", caught.exception.problems[0])
                probed = []
                report = rm.check([self.fast], root=self.root, run=self.fake_run(),
                                  listening=lambda u, seen=probed: seen.append(u) or True)
                self.assertEqual(report.problems, 1)
                self.assertIn("invalid port", "\n".join(report.lines))
                self.assertEqual(probed, [])
                self.assertFalse(rm.server_listening(url))

    def test_a_bad_port_through_the_command_line_exits_three(self):
        service.update_model(self.fast, url="http://127.0.0.1:abc/v1", root=self.root)
        with mock.patch.object(rm, "run_command", self.fake_run()), \
                mock.patch.dict(os.environ, {"MITSS_ROOT": self.root}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(rm.main(["check", "--models", self.fast]), rm.EXIT_INVALID)

    def test_ports_and_defaults(self):
        self.assertEqual(rm.url_port("http://127.0.0.1:8080/v1"), 8080)
        self.assertEqual(rm.url_port("http://localhost/v1"), 80)
        self.assertEqual(rm.url_port("https://[::1]/v1"), 443)
        self.assertIsNone(rm.url_port("http://127.0.0.1:99999/v1"))

    def test_an_empty_model_field_falls_back_to_the_name(self):
        self.assertEqual(rm.served_model({"model": "", "name": "llama-3.1-8b"}),
                         "llama-3.1-8b")
        self.assertEqual(rm.served_model({"model": "/m/x", "name": "x-label"}), "/m/x")
        # Through the store the summary already fills it in; check agrees.
        with tempfile.TemporaryDirectory() as models:
            os.makedirs(os.path.join(models, "fast-8b"))
            with open(os.path.join(models, "fast-8b", "config.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({}, handle)
            with mock.patch.dict(os.environ, {"MITSS_MODELS_DIR": models}):
                report = rm.check([self.fast], root=self.root, run=self.fake_run(),
                                  listening=lambda u: True)
        self.assertIn(os.path.join(models, "fast-8b"), "\n".join(report.lines))
        self.assertEqual(report.problems, 0)


class CommandLineTests(_DataRoot):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"MITSS_ROOT": self.root}), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = rm.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def write(self, raw):
        path = os.path.join(self.root, "matrix.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        return path

    def test_plan_prints_cells_and_exits_zero(self):
        path = self.write({"name": "t", "prompt_id": self.prompt, "versions": [1],
                           "inputs": [self.input_a], "models": [self.fast]})
        code, out, _ = self.main("plan", path)
        self.assertEqual(code, rm.EXIT_OK)
        self.assertIn("Total: 1 cells", out)

    def test_invalid_matrix_exits_three(self):
        path = self.write({"name": "t", "prompt_id": self.prompt, "versions": [7],
                           "inputs": [self.input_a], "models": [self.paste]})
        code, _, err = self.main("plan", path)
        self.assertEqual(code, rm.EXIT_INVALID)
        self.assertIn("2 problem(s)", err)

    def test_usage_errors_exit_three_not_two(self):
        self.assertEqual(self.main()[0], rm.EXIT_INVALID)
        self.assertEqual(self.main("run", "x.json")[0], rm.EXIT_INVALID)
        self.assertEqual(self.main("plan")[0], rm.EXIT_INVALID)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class WrapperTests(unittest.TestCase):
    """The wrapper, run against a copy of the repo layout with a fake runner."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        os.makedirs(os.path.join(self.repo, "scripts"))
        os.makedirs(os.path.join(self.repo, "backend"))
        os.makedirs(os.path.join(self.repo, "elsewhere"))
        shutil.copy(WRAPPER, os.path.join(self.repo, "scripts", "run_matrix.sh"))
        with open(os.path.join(self.repo, "backend", "run_matrix.py"), "w",
                  encoding="utf-8") as handle:
            handle.write(
                "import json, os, sys\n"
                "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
                "  'timeout': os.environ.get('MITSS_LLM_TIMEOUT'),\n"
                "  'preflight': os.environ.get('MITSS_LLM_PREFLIGHT_TIMEOUT')}))\n")

    def run_wrapper(self, *args, env_file=None, extra=None):
        if env_file is not None:
            with open(os.path.join(self.repo, "backend", ".env"), "w",
                      encoding="utf-8") as handle:
                handle.write(env_file)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("MITSS_")}
        env.update(extra or {})
        done = subprocess.run(
            ["bash", os.path.join(self.repo, "scripts", "run_matrix.sh"), *args],
            cwd=os.path.join(self.repo, "elsewhere"), env=env,
            capture_output=True, text=True, timeout=30, check=True)
        return json.loads(done.stdout)

    def test_loads_env_and_defaults_preflight_to_300(self):
        seen = self.run_wrapper("plan", "my matrix.json",
                                env_file="MITSS_LLM_TIMEOUT=77\n")
        self.assertEqual(seen["argv"], ["plan", "my matrix.json"])
        self.assertEqual(seen["timeout"], "77")
        self.assertEqual(seen["preflight"], "300")
        self.assertEqual(os.path.realpath(seen["cwd"]),
                         os.path.realpath(os.path.join(self.repo, "elsewhere")))

    def test_matrix_preflight_override_wins(self):
        seen = self.run_wrapper("check", env_file="MITSS_LLM_PREFLIGHT_TIMEOUT=5\n",
                                extra={"MITSS_MATRIX_PREFLIGHT_TIMEOUT": "450"})
        self.assertEqual(seen["preflight"], "450")

    def test_works_without_an_env_file(self):
        seen = self.run_wrapper("check")
        self.assertIsNone(seen["timeout"])
        self.assertEqual(seen["preflight"], "300")


if __name__ == "__main__":
    unittest.main()
