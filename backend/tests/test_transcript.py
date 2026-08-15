"""Tests for the rolling plain-text transcript."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import Store
from pipeline.transcript import read, transcript_path


class Transcript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.prompt = self.store.create_prompt("Entity extraction",
                                               "Extract entities from:\n{input}")
        self.input = self.store.create_input("Acme incident", "Acme Corp, 4 March 2026.")

    def tearDown(self):
        self.tmp.cleanup()

    def text(self):
        return read(self.store.data_dir)

    def test_no_transcript_until_a_run_exists(self):
        self.assertEqual(self.text(), "")
        self.assertFalse(os.path.exists(transcript_path(self.store.data_dir)))

    def test_run_block_carries_prompt_input_and_output(self):
        self.store.create_run(self.prompt.id, 1, "team-70b",
                              "Acme Corp - organisation", input_id=self.input.id)
        body = self.text()
        self.assertIn("team-70b", body)
        self.assertIn("Acme incident", body)
        self.assertIn("PROMPT:", body)
        self.assertIn("Extract entities from:", body)
        # the rendered prompt, with the input substituted in
        self.assertIn("Acme Corp, 4 March 2026.", body)
        self.assertIn("OUTPUT:", body)
        self.assertIn("Acme Corp - organisation", body)
        self.assertIn("VERDICT: unrated", body)

    def test_provider_runs_record_source_and_duration(self):
        self.store.create_run(self.prompt.id, 1, "team-70b", "out",
                              input_id=self.input.id, source="provider",
                              duration_ms=1840)
        body = self.text()
        self.assertIn("source: provider", body)
        self.assertIn("1840ms", body)

    def test_verdict_is_appended_not_rewritten(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out")
        before = self.text()
        self.store.review_run(run.id, "accurate", "clean")

        after = self.text()
        # The original block is untouched — the file is a history, not a table.
        self.assertTrue(after.startswith(before))
        self.assertIn("---- verdict set", after)
        self.assertIn("accurate", after)
        self.assertIn("clean", after)
        self.assertIn(run.id, after)

    def test_changing_a_verdict_appends_again(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out")
        self.store.review_run(run.id, "accurate")
        self.store.review_run(run.id, "inaccurate", "changed my mind")
        body = self.text()
        # Match the entry prefix, not the phrase — the file header also
        # contains the words "verdict set" in its explanation.
        self.assertEqual(body.count("---- verdict set"), 2)
        self.assertIn("changed my mind", body)

    def test_runs_accumulate_in_order(self):
        self.store.create_run(self.prompt.id, 1, "model-a", "first output")
        self.store.create_run(self.prompt.id, 1, "model-b", "second output")
        body = self.text()
        self.assertLess(body.index("first output"), body.index("second output"))
        self.assertEqual(body.count("PROMPT:"), 2)

    def test_header_is_written_once(self):
        self.store.create_run(self.prompt.id, 1, "m", "a")
        self.store.create_run(self.prompt.id, 1, "m", "b")
        self.assertEqual(self.text().count("MITSS transcript"), 1)

    def test_deleting_a_run_leaves_the_transcript_intact(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "keep this in history")
        self.store.delete_run(run.id)
        self.assertIn("keep this in history", self.text())

    def test_reading_with_a_limit_trims_the_front(self):
        for index in range(5):
            self.store.create_run(self.prompt.id, 1, "m", f"output number {index}")
        trimmed = read(self.store.data_dir, limit=200)
        self.assertIn("earlier entries trimmed", trimmed)
        self.assertIn("output number 4", trimmed)
        self.assertNotIn("output number 0", trimmed)

    def test_transcript_is_a_plain_readable_file(self):
        self.store.create_run(self.prompt.id, 1, "m", "out")
        with open(transcript_path(self.store.data_dir), encoding="utf-8") as handle:
            self.assertIn("MITSS transcript", handle.read())

    def test_empty_output_and_prompt_do_not_break_formatting(self):
        blank = self.store.create_prompt("Blank", "   ")
        run = self.store.create_run(blank.id, 1, "", "   ")
        self.assertIn("(empty)", self.text())
        self.assertIn("unnamed model", self.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
