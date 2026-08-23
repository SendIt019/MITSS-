"""Tests for the prompt pipeline core: storage, versioning, verdicts, diffing."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import NotFound, Store, build_matrix, compare_runs, diff_text
from pipeline.models import ACCURATE, INACCURATE, PARTIAL, UNRATED, Run, slugify


class Prompts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_prompt_starts_at_version_one(self):
        prompt = self.store.create_prompt("Extract entities", "Find the entities.")
        self.assertEqual(prompt.id, "extract-entities")
        self.assertEqual(len(prompt.versions), 1)
        self.assertEqual(prompt.latest.version, 1)
        self.assertEqual(prompt.latest.text, "Find the entities.")

    def test_versions_are_immutable_and_accumulate(self):
        prompt = self.store.create_prompt("p", "first")
        self.store.add_version(prompt.id, "second", "reworded")
        self.store.add_version(prompt.id, "third", "tightened")

        reloaded = self.store.get_prompt(prompt.id)
        self.assertEqual([v.version for v in reloaded.versions], [1, 2, 3])
        self.assertEqual(reloaded.version(1).text, "first")
        self.assertEqual(reloaded.version(3).text, "third")
        self.assertEqual(reloaded.version(2).note, "reworded")

    def test_ids_do_not_collide(self):
        a = self.store.create_prompt("Same Name", "x")
        b = self.store.create_prompt("Same Name", "y")
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(self.store.get_prompt(a.id).latest.text, "x")
        self.assertEqual(self.store.get_prompt(b.id).latest.text, "y")

    def test_rename_keeps_id_and_versions(self):
        prompt = self.store.create_prompt("old name", "text")
        self.store.rename_prompt(prompt.id, "new name")
        reloaded = self.store.get_prompt(prompt.id)
        self.assertEqual(reloaded.name, "new name")
        self.assertEqual(reloaded.id, prompt.id)
        self.assertEqual(len(reloaded.versions), 1)

    def test_missing_prompt_raises(self):
        with self.assertRaises(NotFound):
            self.store.get_prompt("does-not-exist")

    def test_prompt_text_is_a_readable_file_on_disk(self):
        prompt = self.store.create_prompt("p", "the exact text\nwith a newline")
        path = os.path.join(self.store.prompt_dir(prompt.id), "v1.txt")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "the exact text\nwith a newline")

    def test_slugify(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertEqual(slugify("   "), "prompt")
        self.assertEqual(slugify("a" * 80), "a" * 48)


class Runs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.prompt = self.store.create_prompt("p", "version one text")
        self.store.add_version(self.prompt.id, "version two text", "v2")

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_freezes_the_prompt_text(self):
        run = self.store.create_run(self.prompt.id, 1, "model-a", "the output")
        self.assertEqual(run.prompt_text, "version one text")
        self.assertEqual(self.store.get_run(run.id).prompt_text, "version one text")

    def test_run_defaults_to_unrated(self):
        run = self.store.create_run(self.prompt.id, 1, "model-a", "out")
        self.assertEqual(run.verdict, UNRATED)
        self.assertEqual(run.reviewed_at, "")

    def test_review_records_verdict_and_timestamp(self):
        run = self.store.create_run(self.prompt.id, 1, "model-a", "out")
        reviewed = self.store.review_run(run.id, ACCURATE, "looks right")
        self.assertEqual(reviewed.verdict, ACCURATE)
        self.assertEqual(reviewed.notes, "looks right")
        self.assertTrue(reviewed.reviewed_at)
        self.assertEqual(self.store.get_run(run.id).verdict, ACCURATE)

    def test_review_can_change_only_notes(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out")
        self.store.review_run(run.id, INACCURATE, "wrong")
        again = self.store.review_run(run.id, None, "wrong, missed the last item")
        self.assertEqual(again.verdict, INACCURATE)
        self.assertIn("missed", again.notes)

    def test_unknown_version_rejected(self):
        with self.assertRaises(NotFound):
            self.store.create_run(self.prompt.id, 99, "m", "out")

    def test_filtering(self):
        self.store.create_run(self.prompt.id, 1, "model-a", "a")
        self.store.create_run(self.prompt.id, 2, "model-a", "b")
        self.store.create_run(self.prompt.id, 2, "model-b", "c")

        self.assertEqual(len(self.store.list_runs(prompt_id=self.prompt.id)), 3)
        self.assertEqual(len(self.store.list_runs(prompt_id=self.prompt.id, version=2)), 2)
        self.assertEqual(len(self.store.list_runs(prompt_id=self.prompt.id, model="model-b")), 1)

    def test_models_used(self):
        self.store.create_run(self.prompt.id, 1, "zeta", "a")
        self.store.create_run(self.prompt.id, 1, "alpha", "b")
        self.assertEqual(self.store.models_used(self.prompt.id), ["alpha", "zeta"])

    def test_delete_run(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out")
        self.store.delete_run(run.id)
        with self.assertRaises(NotFound):
            self.store.get_run(run.id)

    def test_append_only_index_records_every_action(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out")
        self.store.review_run(run.id, PARTIAL, "half")
        kinds = [e["event"] for e in self.store.read_events()]
        self.assertEqual(kinds[0], "prompt_created")
        self.assertIn("version_added", kinds)
        self.assertIn("run_recorded", kinds)
        self.assertIn("run_reviewed", kinds)

    def test_deleting_a_run_leaves_its_history_in_the_index(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out")
        self.store.delete_run(run.id)
        kinds = [e["event"] for e in self.store.read_events()]
        self.assertIn("run_recorded", kinds)
        self.assertIn("run_deleted", kinds)


class Diffing(unittest.TestCase):
    def test_identical_text(self):
        result = diff_text("the same words", "the same words")
        self.assertTrue(result["identical"])
        self.assertEqual(result["added_words"], 0)
        self.assertEqual(result["removed_words"], 0)
        self.assertEqual(result["similarity"], 1.0)

    def test_word_level_change_is_isolated(self):
        result = diff_text("the quick brown fox", "the quick red fox")
        self.assertFalse(result["identical"])
        self.assertEqual(result["added_words"], 1)
        self.assertEqual(result["removed_words"], 1)
        added = [s["text"].strip() for s in result["right"] if s["kind"] == "added"]
        removed = [s["text"].strip() for s in result["left"] if s["kind"] == "removed"]
        self.assertEqual(added, ["red"])
        self.assertEqual(removed, ["brown"])

    def test_spans_reconstruct_each_side_exactly(self):
        left_text = "alpha beta gamma\ndelta"
        right_text = "alpha GAMMA gamma delta epsilon"
        result = diff_text(left_text, right_text)
        self.assertEqual("".join(s["text"] for s in result["left"]), left_text)
        self.assertEqual("".join(s["text"] for s in result["right"]), right_text)

    def test_leading_whitespace_survives_reconstruction(self):
        # An output starting with a blank line or indentation must render
        # with it; the tokenizer once dropped anything before the first word.
        left_text = "\n\nstarts after a blank line"
        right_text = "   indented start"
        result = diff_text(left_text, right_text)
        self.assertEqual("".join(s["text"] for s in result["left"]), left_text)
        self.assertEqual("".join(s["text"] for s in result["right"]), right_text)

    def test_leading_whitespace_is_not_counted_as_a_word(self):
        # The whitespace run is a token (so spans reconstruct) but not a
        # word: identical words with different leading blank lines must not
        # report an added "word" or a depressed similarity.
        result = diff_text("foo", "\n\nfoo")
        self.assertEqual(result["added_words"], 0)
        self.assertEqual(result["removed_words"], 0)
        self.assertEqual(result["similarity"], 1.0)
        self.assertFalse(result["identical"])

    def test_empty_sides(self):
        result = diff_text("", "brand new output")
        self.assertEqual(result["removed_words"], 0)
        self.assertEqual(result["added_words"], 3)
        self.assertFalse(result["identical"])

    def test_both_empty_is_not_reported_as_identical_content(self):
        result = diff_text("", "")
        self.assertFalse(result["identical"])
        self.assertEqual(result["added_words"], 0)


class Matrix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.prompt = self.store.create_prompt("p", "v1")
        self.store.add_version(self.prompt.id, "v2", "second")

    def tearDown(self):
        self.tmp.cleanup()

    def test_grid_shape(self):
        self.store.create_run(self.prompt.id, 1, "model-a", "x")
        self.store.create_run(self.prompt.id, 2, "model-b", "y")
        grid = build_matrix(self.store.list_runs(prompt_id=self.prompt.id), [1, 2])

        self.assertEqual(grid["versions"], [1, 2])
        self.assertEqual(grid["models"], ["model-a", "model-b"])
        self.assertEqual(len(grid["cells"]), 4)
        self.assertEqual(grid["cells"]["1|model-a"]["count"], 1)
        self.assertEqual(grid["cells"]["1|model-b"]["count"], 0)
        self.assertIsNone(grid["cells"]["1|model-b"]["verdict"])

    def test_worst_verdict_wins_a_cell(self):
        first = self.store.create_run(self.prompt.id, 1, "m", "x")
        second = self.store.create_run(self.prompt.id, 1, "m", "y")
        self.store.review_run(first.id, ACCURATE)
        self.store.review_run(second.id, INACCURATE)

        grid = build_matrix(self.store.list_runs(prompt_id=self.prompt.id), [1])
        self.assertEqual(grid["cells"]["1|m"]["verdict"], INACCURATE)
        self.assertEqual(grid["cells"]["1|m"]["count"], 2)

    def test_partial_beats_accurate_but_loses_to_inaccurate(self):
        a = self.store.create_run(self.prompt.id, 1, "m", "x")
        b = self.store.create_run(self.prompt.id, 1, "m", "y")
        self.store.review_run(a.id, ACCURATE)
        self.store.review_run(b.id, PARTIAL)
        grid = build_matrix(self.store.list_runs(prompt_id=self.prompt.id), [1])
        self.assertEqual(grid["cells"]["1|m"]["verdict"], PARTIAL)

    def test_unnamed_model_gets_a_column(self):
        self.store.create_run(self.prompt.id, 1, "", "x")
        grid = build_matrix(self.store.list_runs(prompt_id=self.prompt.id), [1])
        self.assertEqual(grid["models"], ["unnamed"])

    def test_totals(self):
        a = self.store.create_run(self.prompt.id, 1, "m", "x")
        self.store.create_run(self.prompt.id, 2, "m", "y")
        self.store.review_run(a.id, ACCURATE)
        grid = build_matrix(self.store.list_runs(prompt_id=self.prompt.id), [1, 2])
        self.assertEqual(grid["totals"]["total"], 2)
        self.assertEqual(grid["totals"][ACCURATE], 1)
        self.assertEqual(grid["totals"][UNRATED], 1)

    def test_compare_runs_flags_what_is_shared(self):
        a = self.store.create_run(self.prompt.id, 1, "model-a", "hello there")
        b = self.store.create_run(self.prompt.id, 1, "model-b", "hello world")
        payload = compare_runs(self.store.get_run(a.id), self.store.get_run(b.id))
        self.assertTrue(payload["same_prompt_version"])
        self.assertFalse(payload["same_model"])
        self.assertTrue(payload["prompt_diff"]["identical"])
        self.assertFalse(payload["output_diff"]["identical"])


class HostileIds(unittest.TestCase):
    """Ids arrive from URLs and become directory names. Anything the store
    could not have minted must read as not-found, never as a path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unmintable_ids_raise_not_found_everywhere(self):
        for hostile in ("..", "../..", "a/../b", "/etc", "a\\b", ".", "", "UPPER"):
            for call in (self.store.get_prompt, self.store.get_input,
                         self.store.get_run, self.store.delete_input,
                         self.store.delete_run):
                with self.assertRaises(NotFound,
                                       msg=f"{call.__name__}({hostile!r})"):
                    call(hostile)

    def test_every_minted_id_passes_the_stores_own_guard(self):
        # The mint (slugify/stamp in models.py) and the check (_SAFE_ID in
        # store.py) are separate definitions; this is the tripwire if they
        # drift. Names are chosen to stress the slug edges: over the length
        # cap, mixed case, punctuation, non-ascii, and a collision suffix.
        for name in ("X" * 80 + " — weird / Name!!", "Ünïcode  ~ name", "?!"):
            prompt = self.store.create_prompt(name, "text {input}")
            self.store.get_prompt(prompt.id)          # raises if check rejects mint
            duplicate = self.store.create_prompt(name, "text {input}")
            self.store.get_prompt(duplicate.id)       # the -2 suffix path
            run = self.store.create_run(prompt.id, 1, "model-a", "out")
            self.store.get_run(run.id)                # stamp-slug-vN composite

    def test_traversal_delete_cannot_reach_the_append_only_files(self):
        # delete_input("..") once resolved to data/ itself and removed the
        # event index and transcript before erroring on a subdirectory.
        self.store.create_prompt("p", "text {input}")
        index = os.path.join(self.store.data_dir, "index.jsonl")
        self.assertTrue(os.path.exists(index))
        with self.assertRaises(NotFound):
            self.store.delete_input("..")
        with self.assertRaises(NotFound):
            self.store.delete_run("..")
        self.assertTrue(os.path.exists(index))


if __name__ == "__main__":
    unittest.main(verbosity=2)
