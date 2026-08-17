from __future__ import annotations

import unittest

from kihachi_music_ai import edit
from kihachi_music_ai.intent import (
    EARLIER_HALF_WORDS,
    FIRST_HALF,
    LARGE_STRENGTH,
    LATER_HALF_WORDS,
    PLAIN_STRENGTH,
    SMALL_STRENGTH,
    SECOND_HALF,
    SMALL_WORDS,
    TRAIT_WORDS,
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

    def test_every_word_can_be_refused_in_the_ordinary_ways(self) -> None:
        """The sweep, not the example. Four gaps were found one at a time.

        `くない`, `すぎない`, the bare `ない` and the particle forms each turned up
        by writing one brief and being surprised. Reading the negator list never
        found any of them, because what is missing from a list is exactly what
        reading it does not show. So this asks the whole vocabulary at once: 155
        surface forms against the refusals that work for any part of speech, 655
        briefs in all.
        """

        japanese = ("{w}は無しで。", "{w}は要らない。", "{w}抜きで。", "{w}は不要。", "{w}じゃなくて。")
        english = ("without {w}.", "no {w}.", "avoid {w}.")

        misread: list[str] = []
        for name, words in TRAIT_WORDS.items():
            for word in words:
                ascii_only = all(character < "\x80" for character in word)
                for template in english if ascii_only else japanese:
                    brief = template.format(w=word)
                    trait = read(brief).find(name)
                    if trait is not None and trait.polarity > 0:
                        misread.append(f"{brief} -> {name} asked for")

        self.assertEqual(misread, [], msg="refusals read as requests")

    def test_a_refusal_can_be_a_verb_rather_than_a_grammatical_negation(self) -> None:
        """`avoid` was an English negator from v0.1 and had no Japanese twin.

        Nothing in the list covered 「避ける」「排除」「厳禁」「省く」, so a brief
        that turned a trait down in the most direct words available got it.
        """

        for text, name in (
            ("スラップは避けて。", "slap"),
            ("サイケは避けたい。", "psychedelic"),
            ("スラップを排除。", "slap"),
            ("暗いのは厳禁。", "dark"),
            ("ボコーダーは省く。", "vocoder"),
            ("暗いのを省いて。", "dark"),
        ):
            with self.subTest(text=text):
                traits = read(text)
                self.assertTrue(traits.refused(name))
                self.assertEqual(traits.strength_of(name), 0.0)

    def test_the_continuative_negative_refuses_like_the_plain_one(self) -> None:
        """`ない` was in the suffix list and `なく` was not.

        So 「跳ねないで」 was refused and 「跳ねなくて」 was read as a request for
        the same thing, one inflection apart.
        """

        for text in ("跳ねないで。", "跳ねなくて。", "跳ねないように。"):
            with self.subTest(text=text):
                self.assertTrue(read(text).refused("swung"))

    def test_the_words_left_out_do_not_refuse_an_ordinary_brief(self) -> None:
        """Each of these was a candidate and each earns a false refusal.

        Kept as a test rather than a comment because the next person to sweep
        the vocabulary will find them again and they look reasonable.
        """

        for text, name, why in (
            ("暗くしてテンポをはやめて。", "dark", "はやめて is speed up, and contains やめて"),
            ("サイケ以外は暗くして。", "psychedelic", "以外 names a span, not a refusal"),
            ("ミニマル以外の要素も入れて。", "minimal", "以外 here asks for more"),
            ("サイケなsongを。", "psychedelic", "NG would fire inside song"),
        ):
            with self.subTest(text=text, why=why):
                self.assertFalse(read(text).refused(name), msg=why)

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

    def test_a_verb_negates_by_touching_what_it_follows(self) -> None:
        for text, name in (
            ("スウィングしないテクノ", "swung"),
            ("跳ねないテクノ", "swung"),
            ("スラップせず指弾きで", "slap"),
        ):
            with self.subTest(text=text):
                self.assertTrue(read(text).refused(name))

    def test_too_much_of_something_is_a_refusal_not_a_request(self) -> None:
        """Found by disagreeing with the LLM reader on the same brief.

        `intent read` returned 「暗すぎない感じで」 as a refusal while this read
        it as a plain request for darkness -- opposite answers from the two
        readers of one vocabulary, and the model's was the right one.
        """

        self.assertTrue(read("暗すぎない感じで").refused("dark"))
        self.assertTrue(read("スウィングしすぎない").refused("swung"))
        # A noun trait puts a particle and an adjective between itself and the
        # negation, so `すぎない` cannot require adjacency the way a bare `ない`
        # must. It moved to the ordinary negators for this, which is safe only
        # because `すぎない` is never an innocent word ending.
        self.assertTrue(read("手数が多すぎない").refused("busy"))
        self.assertTrue(read("スカスカすぎず").refused("sparse"))
        # 「暗すぎる」 is the opposite statement and stays a request.
        self.assertEqual(read("暗すぎる").strength_of("dark"), 1.0)

    def test_a_bare_negative_reaches_only_what_it_touches(self) -> None:
        """Why the suffix list is separate: `ない` is a common ending.

        The ordinary negators attach to the nearest mention anywhere earlier in
        the clause, so a bare `ない` there would read 「サイケで切ないやつ」 as a
        refusal of psychedelia on the strength of an unrelated adjective.
        """

        for text, name in (("サイケで切ないやつ", "psychedelic"), ("スラップと少ないノート", "slap")):
            with self.subTest(text=text):
                traits = read(text)
                self.assertFalse(traits.refused(name))
                self.assertEqual(traits.strength_of(name), 1.0)

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

    def test_a_degree_does_not_reach_past_the_mention_it_modifies(self) -> None:
        """The bound the docstring always claimed, now actually there.

        The search ran to the start of the clause, so 「かなりサイケなアルペジオ」
        gave the arpeggio the 1.5 belonging to the psychedelia. The comma in the
        older example hid it. Found by `compare-readings`: the model said 1.0.
        """

        traits = read("かなりサイケなアルペジオを主役に")

        self.assertEqual(traits.strength_of("psychedelic"), 1.5)
        self.assertEqual(traits.strength_of("arp"), 1.0)

    def test_a_trailing_hedge_is_read(self) -> None:
        """Japanese hedges after the thing too: 「サブベースは少しだけ」."""

        self.assertEqual(read("サブベースは少しだけ").strength_of("sub"), 0.5)
        self.assertEqual(read("サブベースとサイケを少し").strength_of("sub"), 1.0)
        self.assertEqual(read("サブベースとサイケを少し").strength_of("psychedelic"), 0.5)

    def test_a_degree_between_two_mentions_belongs_to_the_later_one(self) -> None:
        """Reading trailing text for every mention would double-claim it."""

        traits = read("サイケとかなりダブ")

        self.assertEqual(traits.strength_of("psychedelic"), 1.0)
        self.assertEqual(traits.strength_of("dub"), 1.5)

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


class SyncopationWordTests(unittest.TestCase):
    """The feel `edit` could already change, said in the brief instead.

    `edit.py` has carried words for syncopation since v0.1, so this was sayable
    about a finished render and not about the brief that produced it.
    """

    def test_both_poles_are_readable(self) -> None:
        self.assertEqual(read("シンコペを効かせて").strength_of("syncopated"), PLAIN_STRENGTH)
        self.assertEqual(read("裏打ち中心のハウス").strength_of("syncopated"), PLAIN_STRENGTH)
        self.assertEqual(read("オンビートで").strength_of("on_grid"), PLAIN_STRENGTH)
        self.assertEqual(read("表打ちのテクノ").strength_of("on_grid"), PLAIN_STRENGTH)

    def test_the_verb_is_matched_in_every_form_a_brief_uses(self) -> None:
        """Found by running briefs: 「うねる」 read as nothing at all.

        The word list started at the stem `うねら` alone, which catches
        「うねらせて」 and misses the plain dictionary form a person is far more
        likely to write.
        """

        for text in ("かなりうねるテクノ", "うねりのあるテクノ", "うねらせて", "うねったベース"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("syncopated"))

    def test_refusing_either_pole_reads_as_a_refusal(self) -> None:
        self.assertTrue(read("シンコペしないテクノ").refused("syncopated"))
        self.assertTrue(read("うねらないテクノ").refused("syncopated"))
        self.assertTrue(read("シンコペ無しで").refused("syncopated"))
        self.assertTrue(read("syncopated house, not on-grid").refused("on_grid"))


class HumanizeWordTests(unittest.TestCase):
    """The one every genre states and no brief could reach.

    `groove.humanize` is the mirror of syncopation: all 23 families set it,
    from Hardcore Electronic's 0.04 to Jazz's 0.45, and a brief still had no
    way to disagree with the family it landed in.
    """

    def test_both_poles_are_readable(self) -> None:
        for text in ("手弾きっぽいテクノ", "少しヨレたテクノ", "人間っぽい演奏", "loose house"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("loose"))
        for text in ("タイトなジャズ", "かっちりしたテクノ", "ジャストで", "machine tight house"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("tight"))

    def test_refusing_either_pole_reads_as_a_refusal(self) -> None:
        self.assertTrue(read("ヨレないテクノ").refused("loose"))
        self.assertTrue(read("タイトすぎない").refused("tight"))

    def test_the_two_poles_do_not_read_each_other(self) -> None:
        """`tight` and `on_grid` are different questions on the same bar.

        One is how far a note sits from the grid on purpose, the other is how
        far it sits from it by hand, so a brief may ask for either alone.
        """

        loose = read("手弾きっぽいテクノ")
        self.assertEqual(loose.strength_of("syncopated"), 0.0)
        self.assertEqual(loose.strength_of("on_grid"), 0.0)
        self.assertEqual(read("オンビートで").strength_of("tight"), 0.0)


class DrumDensityWordTests(unittest.TestCase):
    """`minimal` was the nearest word and it means something else."""

    def test_both_poles_are_readable(self) -> None:
        for text in ("手数の多いテクノ", "ぎっしり詰まったハウス", "busy drums"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("busy"))
        for text in ("スカスカなテクノ", "余白のあるダブ", "sparse techno"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("sparse"))

    def test_minimal_is_not_sparse(self) -> None:
        """One is the opening two sections, the other is the kit."""

        minimal = read("ミニマルなテクノ")
        self.assertTrue(minimal.asked_for("minimal"))
        self.assertEqual(minimal.strength_of("sparse"), 0.0)
        self.assertEqual(read("スカスカなテクノ").strength_of("minimal"), 0.0)

    def test_refusing_either_pole_reads_as_a_refusal(self) -> None:
        self.assertTrue(read("スカスカじゃないテクノ").refused("sparse"))
        self.assertTrue(read("手数が多すぎない").refused("busy"))


class HarmonicRhythmWordTests(unittest.TestCase):
    def test_both_poles_are_readable(self) -> None:
        for text in ("展開が速いテクノ", "目まぐるしく変わるハウス", "fast changes"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("fast_changes"))
        for text in ("ワンコードのテクノ", "コードを引っ張って", "one chord"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("slow_changes"))

    def test_refusing_reads_as_a_refusal(self) -> None:
        self.assertTrue(read("ワンコードじゃないテクノ").refused("slow_changes"))


class NoteLengthWordTests(unittest.TestCase):
    def test_both_poles_are_readable(self) -> None:
        for text in ("歯切れのいいテクノ", "スタッカート気味に", "短く切って", "staccato"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("staccato"))
        for text in ("レガートなアンビエント", "繋げて弾く", "伸ばして", "legato"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("legato"))

    def test_refusing_reads_as_a_refusal(self) -> None:
        self.assertTrue(read("歯切れよくないテクノ").refused("staccato"))
        self.assertTrue(read("レガートすぎない").refused("legato"))


class SectionContrastWordTests(unittest.TestCase):
    def test_both_poles_are_readable(self) -> None:
        for text in ("メリハリのあるテクノ", "起伏のある展開", "抑揚をつけて", "more contrast"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("contrast"))
        for text in ("淡々としたテクノ", "平坦に", "一定のまま", "uniform"):
            with self.subTest(text=text):
                self.assertTrue(read(text).asked_for("flat"))

    def test_refusing_reads_as_a_refusal(self) -> None:
        """A particle sits between the trait and the negation here.

        The bare `ない` in the suffix list has to touch what it negates, so
        「メリハリの無いテクノ」 read as a request until the particle forms
        joined the ordinary negators.
        """

        for text in ("メリハリの無いテクノ", "メリハリのないテクノ", "起伏は無い"):
            with self.subTest(text=text):
                self.assertTrue(read(text).refused("contrast"))
        self.assertTrue(read("スラップが無い").refused("slap"))


class ScopeTests(unittest.TestCase):
    """A brief can name a place, for the traits that have one (ADR-0013)."""

    def test_a_span_word_scopes_the_traits_in_its_clause(self) -> None:
        self.assertEqual(read("後半でメリハリを").traits[0].scope, SECOND_HALF)
        self.assertEqual(read("終盤だけスカスカに").traits[0].scope, SECOND_HALF)
        self.assertEqual(read("序盤は淡々と").traits[0].scope, FIRST_HALF)

    def test_scope_does_not_cross_a_clause_boundary(self) -> None:
        """Like negation: two places, two statements."""

        traits = read("前半は淡々と、後半でメリハリを").traits
        self.assertEqual([(t.name, t.scope) for t in traits], [("flat", FIRST_HALF), ("contrast", SECOND_HALF)])

    def test_only_traits_with_a_per_section_field_take_a_scope(self) -> None:
        """`darkness` is one number for the whole song, so 「後半は暗く」 is not
        refused -- it means what it meant before scopes existed."""

        trait = read("後半は暗く").traits[0]
        self.assertEqual(trait.name, "dark")
        self.assertIsNone(trait.scope)

    def test_the_two_views_split_the_traits(self) -> None:
        traits = read("後半は手数を多く、全体は暗く")
        self.assertEqual([t.name for t in traits.unscoped().traits], ["dark"])
        self.assertEqual([t.name for t in traits.within(SECOND_HALF).traits], ["busy"])
        self.assertEqual(traits.unscoped().strength_of("busy"), 0.0)

    def test_the_seed_brief_names_two_places_it_cannot_use(self) -> None:
        """「前半ミニマル、後半サイケデリック」 -- neither trait has a per-section
        field, so both still apply to the whole song and nothing silently moved."""

        traits = read(SEED_PROMPT)
        self.assertIsNone(traits.find("minimal").scope)
        self.assertIsNone(traits.find("psychedelic").scope)

    def test_edit_and_the_brief_share_one_list_of_span_words(self) -> None:
        self.assertIs(edit.LATER_HALF_WORDS, LATER_HALF_WORDS)
        self.assertIs(edit.EARLIER_HALF_WORDS, EARLIER_HALF_WORDS)


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
