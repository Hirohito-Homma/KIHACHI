from __future__ import annotations

import unittest

from kihachi_music_ai import edit
from kihachi_music_ai.intent import (
    LARGE_STRENGTH,
    PLAIN_STRENGTH,
    SMALL_STRENGTH,
    SMALL_WORDS,
    blend,
    read,
)

SEED_PROMPT = (
    "Mutation Funk、DUB、Tech House。110 BPM、D#m。ファンキーなスラップベース。"
    "前半ミニマル、後半サイケデリック。Vocoderを使用。"
)


class NegationTests(unittest.TestCase):
    """The reason this module exists: a stated refusal used to read as a request."""

    def test_japanese_negation_follows_what_it_refuses(self) -> None:
        traits = read("スラップじゃなくて指弾きで")

        self.assertTrue(traits.refused("slap"))
        self.assertEqual(traits.strength_of("slap"), 0.0)

    def test_an_adjective_negates_differently_from_a_noun(self) -> None:
        """Every trait before `dark` was a noun, so `くない` was never needed.

        `暗くない` contains none of the noun negators, so it read as a plain
        request for darkness -- the same inversion `スラップじゃない` used to
        produce, still present in the corner no trait had reached yet.
        """

        for text, name in (
            ("暗くないテクノ", "dark"),
            ("明るくないアンビエント", "bright"),
            ("サイケくない", "psychedelic"),
        ):
            with self.subTest(text=text):
                traits = read(text)
                self.assertTrue(traits.refused(name))
                self.assertEqual(traits.strength_of(name), 0.0)

    def test_english_negation_precedes_what_it_refuses(self) -> None:
        for text in ("without slap", "no slap", "not slap"):
            with self.subTest(text=text):
                self.assertTrue(read(text).refused("slap"))

    def test_a_refusal_does_not_reach_into_the_next_clause(self) -> None:
        traits = read("スラップじゃなくて指弾き。サイケに。")

        self.assertTrue(traits.refused("slap"))
        self.assertEqual(traits.strength_of("psychedelic"), PLAIN_STRENGTH)

    def test_one_refusal_covers_a_list(self) -> None:
        traits = read("スラップとサイケはなし")

        self.assertTrue(traits.refused("slap"))
        self.assertTrue(traits.refused("psychedelic"))

    def test_but_not_two_separate_statements(self) -> None:
        traits = read("ミニマルにしてサイケは無し")

        self.assertFalse(traits.refused("minimal"))
        self.assertTrue(traits.refused("psychedelic"))

    def test_an_english_list_is_covered_too(self) -> None:
        traits = read("no slap or psychedelic")

        self.assertTrue(traits.refused("slap"))
        self.assertTrue(traits.refused("psychedelic"))

    def test_a_refusal_lands_on_the_low_pole_not_beyond_it(self) -> None:
        """Refusing is worth exactly what not mentioning is worth, and no more."""

        self.assertEqual(read("スラップなし").strength_of("slap"), 0.0)
        self.assertEqual(read("ダブで").strength_of("slap"), 0.0)


class DegreeTests(unittest.TestCase):
    def test_a_plain_mention_is_worth_one(self) -> None:
        self.assertEqual(read("サイケに").strength_of("psychedelic"), PLAIN_STRENGTH)

    def test_hedged_and_insisted_land_on_either_side(self) -> None:
        self.assertEqual(read("少しサイケ").strength_of("psychedelic"), SMALL_STRENGTH)
        self.assertEqual(read("かなりサイケ").strength_of("psychedelic"), LARGE_STRENGTH)
        self.assertEqual(read("very psychedelic").strength_of("psychedelic"), LARGE_STRENGTH)
        self.assertEqual(read("slightly psychedelic").strength_of("psychedelic"), SMALL_STRENGTH)

    def test_a_degree_belongs_to_the_mention_it_precedes(self) -> None:
        traits = read("かなりダブ、サイケも")

        self.assertEqual(traits.strength_of("dub"), LARGE_STRENGTH)
        self.assertEqual(traits.strength_of("psychedelic"), PLAIN_STRENGTH)

    def test_an_unmentioned_trait_is_zero(self) -> None:
        self.assertEqual(read("Tech House。").strength_of("slap"), 0.0)


class BlendTests(unittest.TestCase):
    """Why introducing this layer cannot move an existing song."""

    def test_a_plain_mention_reproduces_the_old_constant_exactly(self) -> None:
        self.assertEqual(blend(0.58, 0.82, PLAIN_STRENGTH), 0.82)

    def test_silence_reproduces_the_old_else_branch_exactly(self) -> None:
        self.assertEqual(blend(0.58, 0.82, 0.0), 0.58)

    def test_hedging_lands_between_the_two(self) -> None:
        value = blend(0.58, 0.82, SMALL_STRENGTH)

        self.assertGreater(value, 0.58)
        self.assertLess(value, 0.82)

    def test_insisting_never_leaves_the_valid_range(self) -> None:
        self.assertLessEqual(blend(0.58, 0.95, LARGE_STRENGTH), 1.0)
        self.assertGreaterEqual(blend(0.9, 0.1, LARGE_STRENGTH), 0.0)


class RecognitionTests(unittest.TestCase):
    def test_the_seed_brief_states_every_trait_plainly(self) -> None:
        """The pinned prompt hedges and refuses nothing, so nothing about it moves."""

        traits = read(SEED_PROMPT)

        self.assertEqual(
            set(traits.names()),
            {"mutation", "dub", "slap", "minimal", "psychedelic", "vocoder"},
        )
        for name in traits.names():
            self.assertEqual(traits.strength_of(name), PLAIN_STRENGTH, name)

    def test_an_ascii_word_has_to_start_a_word(self) -> None:
        self.assertEqual(read("overloaded tone").strength_of("synth"), 0.0)

    def test_japanese_is_matched_as_a_substring(self) -> None:
        self.assertEqual(read("ファンキーなスラップベース").strength_of("slap"), PLAIN_STRENGTH)


class SharedVocabularyTests(unittest.TestCase):
    """`edit` and the brief must agree on what "少し" means."""

    def test_edit_uses_the_words_this_module_owns(self) -> None:
        self.assertIs(edit.SMALL_WORDS, SMALL_WORDS)

    def test_edit_magnitudes_are_unchanged_by_the_move(self) -> None:
        self.assertEqual(edit.SMALL_MAGNITUDE, 0.1)
        self.assertEqual(edit.DEFAULT_MAGNITUDE, 0.2)
        self.assertEqual(edit.LARGE_MAGNITUDE, 0.35)


if __name__ == "__main__":
    unittest.main()
