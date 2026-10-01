"""Stream guards: loop detection and the thinking budget.

The detector is checked on text directly, then end to end through the
streaming stub on 127.0.0.1 and the service, so a stopped run is recorded
the way a live one would be.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_folders import use_fake_models_dir
from test_llm import DONE, SCRIPT, SEEN, StreamServer, _delta, _sse

from app import service
from mitss.llm import HttpProvider, LLMConfigError, normalize_settings
from mitss.stream_guard import MIN_LOOP_CHARS, StreamWatch, find_loop
from pipeline.transcript import format_usage

USAGE = _sse({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 99}})


def csv_rows(count, same=False):
    """Plan rows like the prompt's SECTION 3 CSV: 25 fields each."""
    rows = []
    for n in range(count):
        tag = 0 if same else n
        rows.append(",".join([f"LINK-{tag:02d}", "NODE-A", "NODE-B", "RELAY",
                              "WAVEFORM-1", "RADIO-A", "NET-1"] + ["N/A"] * 18) + "\n")
    return rows


def stream(pieces, kind="content", finish="stop", tail=0.0):
    """A stream script: each piece as one delta, then finish, usage, DONE.
    `tail` pauses before the finish, so a stop that does not happen shows."""
    events = [(0, _delta(**{kind: piece})) for piece in pieces]
    return events + [(tail, _delta(content="", finish=finish)), (0, USAGE), (0, DONE)]


class FindLoop(unittest.TestCase):
    def test_a_short_unit_past_the_threshold_is_found(self):
        self.assertEqual(find_loop("SECTION 3\n" + "N/A," * 60, 40), ("N/A,", 60))

    def test_below_the_threshold_is_not(self):
        self.assertIsNone(find_loop("x" + "N/A," * 39, 40))

    def test_the_shortest_unit_is_reported(self):
        unit, _ = find_loop("ab" * 200, 40)
        self.assertEqual(unit, "ab")

    def test_a_ruler_line_is_not_a_loop(self):
        # 72 '=' is a unit of 1 repeated 72 times; it needs MIN_LOOP_CHARS.
        self.assertIsNone(find_loop("=" * 72 + "\n" + "-" * 72, 40))
        self.assertIsNotNone(find_loop("=" * MIN_LOOP_CHARS, 40))

    def test_twenty_four_csv_rows_are_not_a_loop(self):
        self.assertIsNone(find_loop("".join(csv_rows(24)), 40))
        # Even 24 identical rows stay under 40 repeats.
        self.assertIsNone(find_loop("".join(csv_rows(24, same=True)), 40))

    def test_a_long_unit_repeated_is_found(self):
        unit = "row: the same sixty characters every single time, padded!!!\n"
        self.assertEqual(len(unit), 60)
        self.assertEqual(find_loop(unit * 40, 40), (unit, 40))

    def test_zero_turns_it_off(self):
        # 1 is refused as a setting (Settings below); 0 is the off switch.
        self.assertIsNone(find_loop("a" * 1000, 0))
        self.assertIsNone(StreamWatch(loop_repeats=0).answer_loop.add("a" * 1000))


class Settings(unittest.TestCase):
    def test_new_settings_are_validated(self):
        clean = normalize_settings({"thinking_budget": 8000, "loop_repeats": 0,
                                    "repetition_penalty": 1.1, "frequency_penalty": -0.5})
        self.assertEqual(clean["loop_repeats"], 0)
        for bad in ({"thinking_budget": -1}, {"thinking_budget": 1.5},
                    {"loop_repeats": "forty"}, {"loop_repeats": 1},
                    {"repetition_penalty": 0.9},
                    {"frequency_penalty": float("nan")}):
            with self.assertRaises(LLMConfigError, msg=bad):
                normalize_settings(bad)

    def test_guards_stay_out_of_the_body_and_penalties_go_in(self):
        provider = HttpProvider(url="http://127.0.0.1:9/v1", settings={
            "thinking_budget": 8000, "loop_repeats": 30, "repetition_penalty": 1.1,
            "frequency_penalty": 0.2, "timeout": 60})
        sent = provider.request_settings()
        self.assertNotIn("thinking_budget", sent)
        self.assertNotIn("loop_repeats", sent)
        self.assertNotIn("timeout", sent)
        self.assertEqual(sent["repetition_penalty"], 1.1)
        self.assertEqual(sent["frequency_penalty"], 0.2)


class Streaming(unittest.TestCase):
    def setUp(self):
        os.environ["MITSS_LLM_PREFLIGHT_TIMEOUT"] = "0"
        self.addCleanup(os.environ.pop, "MITSS_LLM_PREFLIGHT_TIMEOUT", None)
        use_fake_models_dir(self)
        SCRIPT["attempts"], SCRIPT["status"] = [], 200
        SEEN["count"], SEEN["bodies"] = 0, []

    def generate(self, settings=None):
        with StreamServer() as url:
            started = time.monotonic()
            completion = HttpProvider(url=url, model="my-model",
                                      settings=settings).generate("q")
            return completion, time.monotonic() - started

    def test_an_answer_loop_stops_the_stream_and_keeps_what_arrived(self):
        pieces = ["SECTION 1\nok\n", "NONE AVAILABLE,"] + ["N/A,"] * 500
        # A 3 s pause before the server's own finish: if the guard did not
        # stop reading, the call would take at least that long.
        SCRIPT["attempts"] = [stream(pieces, tail=3.0)]
        completion, took = self.generate()
        self.assertLess(took, 2.0)
        usage = completion.usage
        self.assertEqual(usage["finish_reason"], "repetition")
        self.assertEqual(usage["repetition_unit"], "N/A,")
        self.assertEqual(usage["repetition_in"], "answer")
        self.assertGreaterEqual(usage["repetition_count"], 50)   # 200 chars / 4
        self.assertTrue(completion.text.startswith("SECTION 1\nok\nNONE AVAILABLE,N/A,"))
        self.assertLess(completion.text.count("N/A,"), 60)
        self.assertEqual(SEEN["count"], 1)    # a guard stop is not retried
        self.assertNotIn("prompt_tokens", usage)   # the usage chunk never came

    def test_legitimate_csv_rows_run_to_the_end(self):
        SCRIPT["attempts"] = [stream(["SECTION 3\n"] + csv_rows(24) + ["done"])]
        completion, _ = self.generate()
        self.assertEqual(completion.usage["finish_reason"], "stop")
        self.assertTrue(completion.text.endswith("done"))

    def test_a_thinking_loop_is_caught_separately(self):
        SCRIPT["attempts"] = [stream(["Let's check the 24-row constraint again. "] * 45,
                                     kind="reasoning", tail=3.0)]
        completion, took = self.generate()
        self.assertLess(took, 2.0)
        self.assertEqual(completion.usage["finish_reason"], "repetition")
        self.assertEqual(completion.usage["repetition_in"], "thinking")
        self.assertEqual(completion.text, "")
        self.assertIn("24-row constraint", completion.reasoning)

    def test_loop_repeats_zero_turns_the_guard_off(self):
        SCRIPT["attempts"] = [stream(["N/A,"] * 300)]
        completion, _ = self.generate({"loop_repeats": 0})
        self.assertEqual(completion.usage["finish_reason"], "stop")
        self.assertEqual(completion.text.count("N/A,"), 300)
        self.assertEqual(completion.settings["loop_repeats"], 0)

    def test_the_thinking_budget_stops_a_runaway_thought(self):
        thoughts = [f"thought {n}. " for n in range(200)]
        SCRIPT["attempts"] = [stream(thoughts, kind="reasoning", tail=3.0)]
        completion, took = self.generate({"thinking_budget": 50})
        self.assertLess(took, 2.0)
        self.assertEqual(completion.usage["finish_reason"], "thinking_budget")
        self.assertEqual(completion.usage["thinking_deltas"], 51)
        self.assertEqual(completion.text, "")
        self.assertTrue(completion.reasoning.endswith("thought 50. "))
        self.assertEqual(completion.settings["thinking_budget"], 50)

    def test_the_budget_stops_counting_once_the_answer_starts(self):
        events = ([(0, _delta(reasoning=f"t{n} ")) for n in range(10)]
                  + [(0, _delta(content="Answer begins. "))]
                  + [(0, _delta(reasoning=f"late {n} ")) for n in range(20)]
                  + [(0, _delta(content="", finish="stop")), (0, USAGE), (0, DONE)])
        SCRIPT["attempts"] = [events]
        completion, _ = self.generate({"thinking_budget": 15})
        self.assertEqual(completion.usage["finish_reason"], "stop")
        self.assertEqual(completion.usage["completion_tokens"], 99)


class Recorded(unittest.TestCase):
    """Through the service: the run, its transcript line and its usage."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        os.environ["MITSS_LLM_PREFLIGHT_TIMEOUT"] = "0"
        self.addCleanup(os.environ.pop, "MITSS_LLM_PREFLIGHT_TIMEOUT", None)
        self.models = use_fake_models_dir(self)
        SCRIPT["attempts"], SCRIPT["status"] = [], 200
        SEEN["count"], SEEN["bodies"] = 0, []
        self.prompt = service.create_prompt("P", "Plan: {input}", root=self.root)["id"]

    def test_a_loop_is_recorded_with_its_unit_in_usage_and_the_transcript(self):
        SCRIPT["attempts"] = [stream(["SECTION 1\n"] + ["N/A,"] * 300, tail=3.0)]
        with StreamServer() as url:
            model_id = service.register_model(
                "looper", url=url, model=os.path.join(self.models, "m"),
                settings={"thinking_budget": 100}, root=self.root)["id"]
            run = service.generate_run(self.prompt, model_id=model_id, root=self.root)
        self.assertEqual(run["usage"]["finish_reason"], "repetition")
        self.assertNotIn("thinking_budget", SEEN["bodies"][0])
        self.assertNotIn("loop_repeats", SEEN["bodies"][0])
        self.assertIn('stopped: repetition of "N/A," x50 in the answer',
                      format_usage(run["usage"]))
        transcript = service.transcript(root=self.root)
        self.assertIn('stopped: repetition of "N/A," x50 in the answer', transcript)


if __name__ == "__main__":
    unittest.main()
