"""Stop a streamed reply that will never finish usefully.

Two guards, both on the MITSS side because mlx_lm.server has neither:

- **Loop detection.** A model stuck repeating a short unit back to back
  (Gemma 4 writing `N/A,` 448 times) is stopped once the unit has repeated
  `loop_repeats` times, and the run records `finish_reason: "repetition"`
  with the unit. Answer and thinking text are watched separately.
- **Thinking budget.** Reasoning deltas are counted while no answer text has
  arrived; past `thinking_budget` the run stops with
  `finish_reason: "thinking_budget"`. mlx_lm.server sends about one token per
  delta, so the count is approximate. This fails fast: it keeps the thinking
  text but does not try to salvage an answer.

Everything received before the stop is kept. The reader closes the
connection, which is how the server learns to stop generating.

Standard library only.
"""

from __future__ import annotations

import time
from typing import Any

DEFAULT_LOOP_REPEATS = 40
MAX_UNIT = 60
# A repeated run must also cover this many characters, so a ruler line of 72
# '=' or '-' (a unit of 1, repeated 72 times) is never taken for a loop. For
# units of 5 characters or more, the repeat count alone decides.
MIN_LOOP_CHARS = 200

REPETITION = "repetition"
THINKING_BUDGET = "thinking_budget"


def find_loop(text: str, repeats: int) -> tuple[str, int] | None:
    """(unit, count) when `text` ends with a unit of up to MAX_UNIT
    characters repeated back to back at least `repeats` times and over at
    least MIN_LOOP_CHARS characters; the shortest such unit wins."""
    if repeats < 2:
        return None
    for size in range(1, MAX_UNIT + 1):
        needed = max(repeats, -(-MIN_LOOP_CHARS // size))
        if len(text) < size * needed:
            break
        unit = text[-size:]
        if text.endswith(unit * needed):
            count = needed
            while text.endswith(unit * (count + 1)):
                count += 1
            return unit, count
    return None


class LoopWatch:
    """The tail of one text stream, checked for a loop after every piece."""

    def __init__(self, repeats: int):
        self.repeats = repeats
        # Enough tail for the longest unit at the threshold, or the minimum span.
        self.keep = max(MAX_UNIT * repeats, MIN_LOOP_CHARS * 2)
        self.tail = ""

    def add(self, piece: str) -> tuple[str, int] | None:
        if self.repeats < 2 or not piece:
            return None
        self.tail = (self.tail + piece)[-self.keep:]
        return find_loop(self.tail, self.repeats)


class StreamWatch:
    """Per-attempt state the streaming reader fills in.

    `marks`: monotonic times of the first delta with any text, the first
    with answer text and the last with any text. `stop`: set when a guard
    ends the reply early - its finish_reason plus details for `usage`.
    """

    def __init__(self, loop_repeats: int = DEFAULT_LOOP_REPEATS,
                 thinking_budget: int = 0):
        self.marks: dict[str, float] = {}
        self.stop: dict[str, Any] | None = None
        self.thinking_budget = thinking_budget
        self.thinking_deltas = 0
        self.answer_loop = LoopWatch(loop_repeats)
        self.thinking_loop = LoopWatch(loop_repeats)

    def see(self, answer: str | None, thinking: str | None) -> bool:
        """Note one delta's text. True means stop reading now."""
        answer = answer if isinstance(answer, str) else ""
        thinking = thinking if isinstance(thinking, str) else ""
        if not (answer or thinking):
            return False
        moment = time.monotonic()
        self.marks.setdefault("first_token", moment)
        if answer:
            self.marks.setdefault("first_answer", moment)
        self.marks["last_token"] = moment

        if thinking and "first_answer" not in self.marks:
            self.thinking_deltas += 1
            if self.thinking_budget and self.thinking_deltas > self.thinking_budget:
                self.stop = {"finish_reason": THINKING_BUDGET,
                             "thinking_deltas": self.thinking_deltas}
                return True
        for text, watch, where in ((answer, self.answer_loop, "answer"),
                                   (thinking, self.thinking_loop, "thinking")):
            found = watch.add(text)
            if found:
                unit, count = found
                self.stop = {"finish_reason": REPETITION, "repetition_unit": unit,
                             "repetition_count": count, "repetition_in": where}
                return True
        return False
