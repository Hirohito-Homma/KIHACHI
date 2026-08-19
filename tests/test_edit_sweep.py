"""Everything the edit vocabulary can say, planned once, on every run.

The brief reader has a saved corpus and a second reader to disagree with it
(`tests/test_sweep.py`). This side has neither -- but it has a vocabulary that
can generate its own instruction space, and a Spec Diff whose shape can be
checked without knowing which answer is musically right:

* nothing crashes; `EditInstructionError` is the only refusal;
* an increase only moves up, a decrease only down, a refusal lands on the
  bottom of its parameter's range;
* a named place is the only place touched, or the plan says why not;
* a named track is the only density touched;
* every section is accounted for exactly once, and the plan says it has not run;
* every plan can be applied to the spec it was planned against.

**Both bugs found on this side on 2026-08-19 are in that list.** #98 was a crash
-- 「密度を上げて」 raised `KeyError: 'sub'` from inside `build_spec_edit` -- and
#99 was a refusal that moved the value *up*. Neither needed a model to find and
neither was subtle; they needed the whole space to be tried once.

**The corpus is generated from the vocabulary rather than stored beside it**, so
a word added to `QUALITY_WORDS` tomorrow is swept tomorrow. What it cannot do is
invent a phrasing nobody listed: the movements and places below are written out
by hand, and a shape missing from them is a shape this sweep says nothing about.

Every word appears in some combinations, and every combination appears with some
words -- the full cross product is 10 times the size and finds the same things.
"""

from __future__ import annotations

import itertools
import unittest

from kihachi_music_ai.edit import (
    EditInstructionError,
    PARAMETER_RANGE,
    QUALITY_WORDS,
    apply_spec_edit,
    build_spec_edit,
    parse_edit_instruction,
    song_spec_sha256,
)
from kihachi_music_ai.models import DENSITY_FIELDS
from kihachi_music_ai.music_brain import MusicBrain
from test_music_brain import EXAMPLE

MOVES = ("をもっと", "を下げて", "を少し上げて", "をかなり上げて", "は無しで")
TRACKS = ("", "ドラムの", "ベースの", "コードの")
#: The three parts a brief can name, and the density each one carries. The four
#: in `EXTRA_TRACKS` have none -- which is what #98 was about.
NAMED_DENSITY = {"ドラムの": "drum_density", "ベースの": "bass_density", "コードの": "chord_density"}


class EditSweepTests(unittest.TestCase):
    """One pass over the instruction space, shared by every check below."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = MusicBrain(seed=8).analyze(EXAMPLE + "5分程度。")
        cls.section_names = [section.name for section in cls.spec.arrangement]
        cls.instructions = cls._instructions(
            ("", "後半の", "前半の", f"{cls.section_names[4]}の")
        )
        cls.planned: list[tuple[str, object, dict]] = []
        cls.unplannable: list[str] = []
        cls.crashed: list[str] = []
        for text in cls.instructions:
            try:
                intent = parse_edit_instruction(text, cls.spec)
                edit = build_spec_edit(cls.spec, text)
            except EditInstructionError:
                cls.unplannable.append(text)
            except Exception as error:  # noqa: BLE001 - collected, not raised
                # Held rather than raised, so the crash is *one* failing test
                # with the instructions listed, and every other check still runs
                # over what did plan.
                cls.crashed.append(f"{text}: {type(error).__name__}: {error}")
            else:
                cls.planned.append((text, intent, edit))

    @classmethod
    def _instructions(cls, places: tuple[str, ...]) -> list[str]:
        """Every word in some combinations; every combination with some words."""

        texts: list[str] = []
        for words in QUALITY_WORDS.values():
            for word in words:
                for move, track in itertools.product(MOVES, TRACKS[:2]):
                    texts.append(f"{places[1] if track else ''}{track}{word}{move}")
            first = words[0]
            for move, track, place in itertools.product(MOVES, TRACKS, places):
                texts.append(f"{place}{track}{first}{move}")
        return texts

    def test_the_sweep_is_big_enough_to_mean_something(self) -> None:
        """A guard nothing reaches is worth nothing, so the shape is pinned.

        Every instruction is either planned or refused in the one way the module
        documents, and each kind of reading below is reached by hundreds of
        them. If a rewrite makes most of the space unplannable, the checks would
        all pass over an empty sweep and this is what says so.
        """

        self.assertEqual(
            len(self.planned) + len(self.unplannable) + len(self.crashed),
            len(self.instructions),
        )
        self.assertGreater(len(self.planned), 800)
        for kind, found in (
            ("refusals", [i for _, i, _ in self.planned if i.refusal]),
            ("increases", [i for _, i, _ in self.planned if not i.refusal and i.direction > 0]),
            ("decreases", [i for _, i, _ in self.planned if not i.refusal and i.direction < 0]),
            ("placed", [i for _, i, _ in self.planned if i.sections]),
        ):
            with self.subTest(kind=kind):
                self.assertGreater(len(found), 100)

    def test_nothing_in_the_vocabulary_crashes_the_planner(self) -> None:
        """`EditInstructionError` is the only way an instruction may fail.

        Anything else is a crash reaching the CLI, which is what #98 was: an
        instruction naming no track targeted all seven, and four of them have
        no density field to move.
        """

        self.assertEqual(self.crashed, [])

    def test_the_direction_of_every_plan_follows_the_words(self) -> None:
        """Up, down, or the low pole -- and never the other way.

        #99 was here: 「ゴーストノートは無しで」 raised the ghost notes, because a
        refusal named no decrease word and the direction defaults to up.

        The low pole is the bottom of the *parameter's* range and not 0.0.
        This test said 0.0 until `groove.swing` arrived, whose range is 0.5 to
        0.66 -- and it failed on the day it arrived, which is what it is for.
        """

        for text, intent, edit in self.planned:
            with self.subTest(text=text):
                for change in edit["changes"]:
                    before, after = float(change["from"]), float(change["to"])
                    floor = PARAMETER_RANGE.get(change["path"], (0.0, 1.0))[0]
                    if intent.refusal:
                        self.assertEqual(after, floor)
                    elif intent.direction > 0:
                        self.assertGreater(after, before)
                    else:
                        self.assertLess(after, before)

    def test_a_named_place_is_the_only_place_touched(self) -> None:
        """Or the plan says, in `scope_warnings`, that it could not be."""

        for text, intent, edit in self.planned:
            if not intent.sections:
                continue
            with self.subTest(text=text):
                touched = {
                    change["section"]
                    for change in edit["changes"]
                    if change["scope"] == "section"
                }
                self.assertEqual(touched - set(intent.sections), set())
                if any(change["scope"] == "song" for change in edit["changes"]):
                    self.assertTrue(edit["scope_warnings"])

    def test_a_named_track_is_the_only_density_touched(self) -> None:
        """An instruction that names one part does not move the other two."""

        for text, _, edit in self.planned:
            named = [prefix for prefix in NAMED_DENSITY if prefix in text]
            if len(named) != 1:
                continue
            with self.subTest(text=text):
                moved = {
                    change["path"]
                    for change in edit["changes"]
                    if change["path"] in DENSITY_FIELDS.values()
                }
                self.assertLessEqual(moved, {NAMED_DENSITY[named[0]]})

    def test_every_section_is_accounted_for_exactly_once(self) -> None:
        for text, _, edit in self.planned:
            with self.subTest(text=text):
                touched = set(edit["sections_touched"])
                untouched = set(edit["sections_untouched"])
                self.assertEqual(touched & untouched, set())
                self.assertEqual(touched | untouched, set(self.section_names))

    def test_every_plan_says_it_has_not_run_and_can_be_applied(self) -> None:
        """Planning is read-only, and a plan is applicable to what it was
        planned against -- checked over the whole space rather than on one
        instruction.
        """

        before = song_spec_sha256(self.spec)
        for text, _, edit in self.planned:
            with self.subTest(text=text):
                self.assertEqual(edit["execution_state"], "planned_not_applied")
                self.assertFalse(edit["safety"]["song_spec_mutated"])
                apply_spec_edit(self.spec, edit)
        self.assertEqual(song_spec_sha256(self.spec), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
