"""Tests for input sets, prompt rendering, and the input dimension of the matrix."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import NotFound, Store, build_matrix
from pipeline.models import ACCURATE, INACCURATE
from pipeline.render import has_placeholder, preview, render_prompt


class Rendering(unittest.TestCase):
    def test_placeholder_is_substituted_in_place(self):
        rendered, notes = render_prompt("Summarise {input} in one line.", "the passage")
        self.assertEqual(rendered, "Summarise the passage in one line.")
        self.assertEqual(notes, [])

    def test_placeholder_can_sit_anywhere(self):
        rendered, _ = render_prompt("PASSAGE:\n{input}\n\nNow list the entities.", "abc")
        self.assertEqual(rendered, "PASSAGE:\nabc\n\nNow list the entities.")

    def test_repeated_placeholder_substitutes_everywhere(self):
        rendered, notes = render_prompt("{input} ... and again: {input}", "X")
        self.assertEqual(rendered, "X ... and again: X")
        self.assertIn("repeated_placeholder", [n["code"] for n in notes])

    def test_input_without_placeholder_is_appended_with_a_warning(self):
        rendered, notes = render_prompt("Summarise the passage.", "the passage")
        self.assertEqual(rendered, "Summarise the passage.\n\nthe passage")
        self.assertIn("no_placeholder", [n["code"] for n in notes])

    def test_no_input_leaves_a_plain_prompt_untouched(self):
        rendered, notes = render_prompt("Just do the thing.", "")
        self.assertEqual(rendered, "Just do the thing.")
        self.assertEqual(notes, [])

    def test_placeholder_with_no_input_warns_and_empties_it(self):
        rendered, notes = render_prompt("Summarise {input}.", "")
        self.assertEqual(rendered, "Summarise .")
        self.assertIn("empty_input", [n["code"] for n in notes])

    def test_has_placeholder(self):
        self.assertTrue(has_placeholder("a {input} b"))
        self.assertFalse(has_placeholder("a b"))

    def test_preview_reports_counts_and_truncates(self):
        body = preview("{input}", "word " * 100, limit=50)
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["rendered"]), 50)
        self.assertEqual(body["words"], 100)
        self.assertTrue(body["has_placeholder"])


class Inputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_read(self):
        created = self.store.create_input("Passage A", "Acme Corp announced...")
        self.assertEqual(created.id, "passage-a")
        self.assertEqual(self.store.get_input("passage-a").text, "Acme Corp announced...")

    def test_input_text_is_a_plain_file(self):
        self.store.create_input("P", "line one\nline two")
        path = os.path.join(self.store.input_dir("p"), "input.txt")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "line one\nline two")

    def test_ids_do_not_collide(self):
        a = self.store.create_input("Same", "x")
        b = self.store.create_input("Same", "y")
        self.assertNotEqual(a.id, b.id)

    def test_update_is_allowed(self):
        created = self.store.create_input("P", "old text")
        updated = self.store.update_input(created.id, name="Renamed", text="new text")
        self.assertEqual(updated.name, "Renamed")
        self.assertEqual(updated.text, "new text")

    def test_delete(self):
        created = self.store.create_input("P", "x")
        self.store.delete_input(created.id)
        with self.assertRaises(NotFound):
            self.store.get_input(created.id)

    def test_missing_input_raises(self):
        with self.assertRaises(NotFound):
            self.store.get_input("nope")


class RunsWithInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.prompt = self.store.create_prompt("P", "Extract entities from {input}")
        self.input_a = self.store.create_input("Passage A", "Acme Corp, Lisbon")
        self.input_b = self.store.create_input("Passage B", "Globex, Berlin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_stores_rendered_template_and_input_separately(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out",
                                    input_id=self.input_a.id)
        self.assertEqual(run.prompt_text, "Extract entities from Acme Corp, Lisbon")
        self.assertEqual(run.template_text, "Extract entities from {input}")
        self.assertEqual(run.input_text, "Acme Corp, Lisbon")
        self.assertEqual(run.input_name, "Passage A")

    def test_all_three_texts_survive_a_reload(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out",
                                    input_id=self.input_a.id)
        again = self.store.get_run(run.id)
        self.assertEqual(again.prompt_text, run.prompt_text)
        self.assertEqual(again.template_text, run.template_text)
        self.assertEqual(again.input_text, run.input_text)

    def test_editing_an_input_does_not_rewrite_past_runs(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out",
                                    input_id=self.input_a.id)
        self.store.update_input(self.input_a.id, text="completely different now")
        frozen = self.store.get_run(run.id)
        self.assertEqual(frozen.input_text, "Acme Corp, Lisbon")
        self.assertEqual(frozen.prompt_text, "Extract entities from Acme Corp, Lisbon")

    def test_deleting_an_input_does_not_break_past_runs(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out",
                                    input_id=self.input_a.id)
        self.store.delete_input(self.input_a.id)
        frozen = self.store.get_run(run.id)
        self.assertEqual(frozen.input_text, "Acme Corp, Lisbon")
        self.assertEqual(frozen.input_name, "Passage A")

    def test_run_without_an_input_is_still_allowed(self):
        run = self.store.create_run(self.prompt.id, 1, "m", "out")
        self.assertEqual(run.input_id, "")
        self.assertEqual(run.prompt_text, "Extract entities from ")

    def test_unknown_input_is_rejected(self):
        with self.assertRaises(NotFound):
            self.store.create_run(self.prompt.id, 1, "m", "out", input_id="ghost")

    def test_filtering_runs_by_input(self):
        self.store.create_run(self.prompt.id, 1, "m", "a", input_id=self.input_a.id)
        self.store.create_run(self.prompt.id, 1, "m", "b", input_id=self.input_b.id)
        self.assertEqual(
            len(self.store.list_runs(prompt_id=self.prompt.id, input_id=self.input_a.id)), 1)
        self.assertEqual(len(self.store.list_runs(prompt_id=self.prompt.id)), 2)

    def test_inputs_used_lists_what_appears_in_runs(self):
        self.store.create_run(self.prompt.id, 1, "m", "a", input_id=self.input_a.id)
        names = {i["name"] for i in self.store.inputs_used(self.prompt.id)}
        self.assertEqual(names, {"Passage A"})


class MatrixWithInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.prompt = self.store.create_prompt("P", "Do the thing with {input}")
        self.store.add_version(self.prompt.id, "Do the thing carefully with {input}", "v2")
        self.a = self.store.create_input("A", "material a")
        self.b = self.store.create_input("B", "material b")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cell_reports_how_many_inputs_it_covers(self):
        r1 = self.store.create_run(self.prompt.id, 1, "m", "x", input_id=self.a.id)
        r2 = self.store.create_run(self.prompt.id, 1, "m", "y", input_id=self.b.id)
        self.store.review_run(r1.id, ACCURATE)
        self.store.review_run(r2.id, INACCURATE)

        grid = build_matrix(self.store.list_runs(prompt_id=self.prompt.id), [1, 2])
        cell = grid["cells"]["1|m"]
        self.assertEqual(cell["count"], 2)
        self.assertEqual(cell["inputs_covered"], 2)
        # worst verdict still wins when aggregating across inputs
        self.assertEqual(cell["verdict"], INACCURATE)

    def test_filtering_to_one_input_gives_a_like_for_like_grid(self):
        r1 = self.store.create_run(self.prompt.id, 1, "m", "x", input_id=self.a.id)
        r2 = self.store.create_run(self.prompt.id, 1, "m", "y", input_id=self.b.id)
        self.store.review_run(r1.id, ACCURATE)
        self.store.review_run(r2.id, INACCURATE)

        only_a = self.store.list_runs(prompt_id=self.prompt.id, input_id=self.a.id)
        grid = build_matrix(only_a, [1, 2])
        self.assertEqual(grid["cells"]["1|m"]["verdict"], ACCURATE)
        self.assertEqual(grid["cells"]["1|m"]["inputs_covered"], 1)

    def test_missing_combinations_are_reported(self):
        self.store.create_run(self.prompt.id, 1, "m", "x", input_id=self.a.id)
        grid = build_matrix(self.store.list_runs(prompt_id=self.prompt.id), [1, 2])
        self.assertEqual(grid["missing"], [{"version": 2, "model": "m"}])

    def test_inputs_in_view(self):
        self.store.create_run(self.prompt.id, 1, "m", "x", input_id=self.a.id)
        self.store.create_run(self.prompt.id, 2, "m", "y", input_id=self.b.id)
        grid = build_matrix(self.store.list_runs(prompt_id=self.prompt.id), [1, 2])
        self.assertEqual(grid["inputs_in_view"], sorted([self.a.id, self.b.id]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
