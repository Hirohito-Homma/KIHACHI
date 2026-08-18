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

        japanese = (
            "{w}は無しで。", "{w}は要らない。", "{w}抜きで。", "{w}は不要。", "{w}じゃなくて。",
            # The axis this sweep was missing. Every template above refuses by
            # *naming* the thing; a brief refuses by naming the action just as
            # often, and 「{w}は入れないで」 read as a request for {w} until the
            # verbs went in. Sweeping one axis says nothing about the other.
            "{w}は入れないで。", "{w}は使わないで。", "{w}は足さないで。", "{w}は避けて。",
            # And the same verbs behind the other particles, because the object
            # rule reads the gap between the two and 「は」 is not the only thing
            # that can be in it.
            "{w}を省いて。", "{w}も排除して。", "{w}を入れないで。",
            # A brief refuses an adjective by describing the thing it does not
            # want, and half of this vocabulary is adjectives. The ending was
            # read as a particle, so the noun after it looked like the verb's
            # own object: every one of these asked for the trait it refuses.
            "{w}な音は避けて。", "{w}い音は避けて。",
            # The compound tail #87 fixed against a hand-written list, saying
            # this sweep could not generate one. It can -- a tail is a noun
            # appended -- and this template fails on the rule #84 shipped.
            "{w}サウンドは避けて。",
            # The tails a brief adds to a trait word. Same misreading as the
            # two templates above, one derivation out: the noun after the tail
            # read as the verb's own object and the refusal was dropped.
            "{w}すぎる音は避けて。", "{w}っぽい処理は避けて。", "{w}めの音は避けて。",
        )
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

    def test_a_refusal_can_name_the_action_instead_of_the_thing(self) -> None:
        """`使わな` was the only verb in the list, and it was there by accident.

        Every other entry refuses by naming the thing -- 「無し」「不要」「抜き」 --
        or negates the trait word itself. A brief that says what *not to do*
        with it was read as asking for it: 「ボコーダーは入れないで」 came back
        +1. The bare `ない` does not reach it either, because a suffix negator
        counts only where it touches the mention and 「は入れ」 is in the way.
        """

        for text, name in (
            ("ボコーダーは入れないで。", "vocoder"),
            ("ボコーダーは足さないで。", "vocoder"),
            ("サイケは加えないで。", "psychedelic"),
            ("サイケは混ぜずに。", "psychedelic"),
            ("ダブは乗せないで。", "dub"),
            ("スラップは弾かないで。", "slap"),
            ("ボコーダーは鳴らさないで。", "vocoder"),
            ("スラップは使用しないで。", "slap"),
            ("ボコーダーは使いません。", "vocoder"),
            ("ボコーダーはいらん。", "vocoder"),
            ("ダブは取り除いて。", "dub"),
            ("スラップは抜いて。", "slap"),
        ):
            with self.subTest(text=text):
                self.assertTrue(read(text).refused(name))

    def test_a_verb_refusing_its_own_object_leaves_the_mention_alone(self) -> None:
        """The bug that came free with #64's five verbs, and shipped with them.

        A negator attaches to the nearest mention earlier in the clause. That
        is right for a noun form, which sits where the thing was named, and
        wrong for a verb, which brings an object of its own: 「ミニマルにして
        無駄を省いて」 asks for minimalism and refused it, because `省い` looked
        back past 「無駄」 and found 「ミニマル」. Same for 排除 and 避け, all
        three on `main` before this.

        A verb refusal now needs particles only between it and the mention.
        Other negators may sit in the gap -- 「スラップ抜きじゃない」 depends on
        it -- but another noun means the verb is talking about that noun.
        """

        for text, name in (
            ("ミニマルにして無駄を省いて。", "minimal"),
            ("ミニマルにして無駄を排除。", "minimal"),
            ("ミニマルにして無駄を取り除いて。", "minimal"),
            ("ルーズにして固さを避けて。", "loose"),
            ("サイケにして濁りを省く。", "psychedelic"),
            ("ルーズに力を抜いて。", "loose"),
        ):
            with self.subTest(text=text):
                self.assertFalse(read(text).refused(name))

    def test_the_rest_of_a_compound_is_not_another_noun(self) -> None:
        """The object rule read the vocabulary's own words as someone else's.

        A mention is the vocabulary's spelling and a brief writes longer words
        around it: 「アルペジ|オ」, 「ダブ|ディレイ」, 「シンセ|リード」, 「ダブ|処理」.
        The first version of the object rule allowed hiragana in the gap and
        nothing else, so every one of those tails read as a separate noun and
        the refusal was dropped from phrases this reader had understood
        completely.

        Fixed twice. Allowing the vocabulary's own words covered 「シンセリード」
        alone -- 「オ」 is not a word, 「ディレイ」 is not in the vocabulary -- and
        that is the whole lesson: **the boundary between two nouns is the
        particle, not the script**. Content, then particles; content appearing
        after a particle is the verb's own object. `の` joins rather than
        separates, so 「シンセのリードは省いて」 is about the synth.

        Both rounds were caught by `compare-readings` re-run over the same
        sweep, one commit after the rules were right.
        """

        for text, name in (
            ("派手なシンセリードは避けて。", "synth"),
            ("シンセリードは入れないで。", "synth"),
            ("シンセのリードは省いて。", "synth"),
            # Neither of these is fixed by knowing the vocabulary: 「オ」 is not
            # a word and 「ディレイ」 is not in it. The boundary between one noun
            # and the next is the particle, not the script.
            ("アルペジオは省いて。", "arp"),
            ("ダブディレイも排除して。", "dub"),
            ("ダブ処理は避けて。", "dub"),
            ("サブベースは入れないで。", "sub"),
        ):
            with self.subTest(text=text):
                self.assertTrue(read(text).refused(name))

    def test_an_adjective_ending_is_not_a_boundary_between_two_nouns(self) -> None:
        """#87 one part of speech over, and the wider half of the vocabulary.

        The object rule reads the gap between a mention and the verb refusing
        it: content, then particles, because a particle is what separates one
        noun from the next. An adjective's ending is not that separator -- it
        attaches the adjective to the noun that follows -- but 「い」 and 「な」
        are kana, so 曲 and パッド read as the verb's own object and the brief
        came back asking for the trait it refuses. A polarity flip, on 380 of
        the 950 briefs this shape can build.

        `の` was already carved out for the same reason in #87. These are the
        other two joining kana, and the argument is one argument: the boundary
        is the particle, and な, い and の join rather than separate.

        The model reader agrees on all of these (`compare-readings`, 2026-08-19)
        and on the controls below, where the verb really does bring its own
        object.
        """

        for text, name in (
            ("暗い曲は避けて。", "dark"),
            ("明るい感じは避けて。", "bright"),
            ("サイケデリックなパッドは避けて。", "psychedelic"),
            ("ストレートなビートは避けて。", "straight"),
            ("タイトな演奏は避けて。", "tight"),
            ("ミニマルな構成は避けて。", "minimal"),
            ("スカスカな感じは入れないで。", "sparse"),
            # の, then い: 「歯切れのいい」 is one description, not three nouns.
            ("歯切れのいいキックは避けて。", "staccato"),
        ):
            with self.subTest(text=text):
                traits = read(text)
                self.assertTrue(traits.refused(name))
                self.assertEqual(traits.strength_of(name), 0.0)

    def test_an_adjective_before_a_second_object_still_leaves_it_alone(self) -> None:
        """The control the widening had to keep: 「にして」 is still a boundary.

        Adding な and い to the gap could have re-opened #84's bug, where a verb
        looked past its own object to the nearest mention. It does not: the
        joining kana attach a noun to what precedes them, and everything here
        puts a particle between the trait and the verb's object instead.
        """

        for text, name in (
            ("ミニマルな感じにして無駄を省いて。", "minimal"),
            ("暗い感じにして低音を足さないで。", "dark"),
            ("明るい曲にして、リバーブは足さないで。", "bright"),
            ("タイトにしてタイミングを外して。", "tight"),
        ):
            with self.subTest(text=text):
                self.assertFalse(read(text).refused(name))

    def test_a_tail_the_brief_adds_is_still_the_same_word(self) -> None:
        """#88 covered the endings a trait word has; these are the added ones.

        A brief describes what it does not want, and it grows the word to do
        it: 「暗すぎる音」, 「暗そうな曲」, 「暗めの音」, 「ダブっぽい処理」,
        「暗くなる音」. Every one of them read as a **request** for the trait,
        because the noun after the tail looked like the verb's own object --
        the same polarity flip #88 fixed, one derivation further out.

        The model reader refuses all five (`compare-readings`, 2026-08-19),
        and agrees with the repaired rules on every brief probed here.
        """

        for text, name in (
            ("暗すぎる音は避けて。", "dark"),
            ("タイトすぎる感じは避けて。", "tight"),
            ("明るすぎるパッドは省いて。", "bright"),
            ("ミニマルすぎる展開は避けて。", "minimal"),
            ("暗そうな曲は避けて。", "dark"),
            ("暗めの音は避けて。", "dark"),
            ("ダブっぽい処理は避けて。", "dub"),
            ("サイケっぽいフレーズは入れないで。", "psychedelic"),
            ("暗くなる音は避けて。", "dark"),
        ):
            with self.subTest(text=text):
                traits = read(text)
                self.assertTrue(traits.refused(name))
                self.assertEqual(traits.strength_of(name), 0.0)

    def test_the_same_tail_conjugated_hands_the_verb_its_own_object(self) -> None:
        """Why the tails are listed in the attributive form and not as stems.

        「ダブっぽい処理は避けて」 refuses `dub` and 「ダブっぽくして低音を足さ
        ないで」 asks for it. The two differ by one inflection, so a list of
        stems -- 「っぽ」, 「め」 without its ending -- would refuse both, and the
        second is an ordinary brief.

        The last two are why the boundary was not named directly instead. A
        rule listing the case particles and connectives has to hold 「から」 and
        「ので」 as well, and each one it misses is a false refusal of a trait
        the brief asked for.
        """

        for text, name in (
            ("ダブっぽくして低音を足さないで。", "dub"),
            ("暗めにして低音を足さないで。", "dark"),
            ("暗くしてこもる音は避けて。", "dark"),
            ("暗いから低音は足さないで。", "dark"),
            ("暗いので無駄は省いて。", "dark"),
        ):
            with self.subTest(text=text):
                self.assertFalse(read(text).refused(name))

    def test_the_noun_forms_keep_the_reach_they_had(self) -> None:
        """Known-wrong, and pinned rather than fixed.

        The same shape one part of speech over: 「ミニマルにして無駄は不要」 is a
        false refusal for exactly the reason the verbs were. The rule is not
        applied to the noun forms because they earn that reach honestly --
        「手数が多すぎない」 crosses an adjective and 「サイケ感の無い」 crosses a
        suffix, and both are real refusals of the mention. Narrowing them to
        catch 「無駄は不要」 would lose those.
        """

        self.assertTrue(read("ミニマルにして無駄は不要。").refused("minimal"))
        self.assertTrue(read("手数が多すぎない").refused("busy"))

    def test_the_verbs_left_out_do_not_refuse_an_ordinary_brief(self) -> None:
        """Candidates that read as refusals and are not.

        `やめ`, `以外`, `カット` and `NG` were declined for the same reason one
        list down; these four are the verb-shaped members of that family. Each
        describes doing something *to* the music rather than leaving it out.
        """

        for text, name, why in (
            ("タイトにしてタイミングを外して。", "tight", "タイミングを外す is a groove, not a removal"),
            ("暗くして輪郭を消して。", "dark", "消す names an effect on the sound"),
            ("サイケは控えめに。", "psychedelic", "控えめ is a degree word and already hedges"),
            ("手数を減らして。", "busy", "減らす asks for less, which is not none"),
        ):
            with self.subTest(text=text, why=why):
                self.assertFalse(read(text).refused(name), msg=why)

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

    def test_a_second_refusal_cancels_the_first(self) -> None:
        """Refusals were a set of indices, so two of them marked a mention once.

        Both of these read as flat refusals of the thing they ask for.
        """

        for text, name in (("スラップ抜きじゃない。", "slap"), ("暗くなくはない。", "dark")):
            with self.subTest(text=text):
                self.assertFalse(read(text).refused(name))
                self.assertGreater(read(text).strength_of(name), 0.0)

    def test_a_double_negative_asks_for_less_than_the_plain_word(self) -> None:
        """Litotes. 「暗くなくはない」 is *somewhat* dark, not dark.

        Cancelling the refusal was only half of it: reading the result as a
        plain request overshoots in the same way the refusal did, by stating
        something the brief withheld. This was left open by the change that
        cancelled them, on the theory that 「スラップ抜きじゃない」 was plain and
        only 「暗くなくはない」 hedged, and that some rule could tell them apart.
        Both are litotes; there was no rule to find.
        """

        for text, name in (("スラップ抜きじゃない。", "slap"), ("暗くなくはない。", "dark")):
            with self.subTest(text=text):
                self.assertEqual(read(text).strength_of(name), SMALL_STRENGTH)

    def test_a_degree_word_still_outranks_the_double_negative(self) -> None:
        """Someone who writes 「かなり」 has said how much, whatever the shape."""

        self.assertEqual(read("かなり暗くなくはない。").strength_of("dark"), LARGE_STRENGTH)
        self.assertEqual(read("少しだけ暗くなくはない。").strength_of("dark"), SMALL_STRENGTH)

    def test_a_single_negation_is_untouched_by_the_hedge(self) -> None:
        """Only an even count softens; one refusal is still a refusal at 0.0."""

        for text, name in (
            ("スラップではない。", "slap"),
            ("暗くない。", "dark"),
            ("スラップは避けて。", "slap"),
        ):
            with self.subTest(text=text):
                self.assertTrue(read(text).refused(name))
                self.assertEqual(read(text).strength_of(name), 0.0)

    def test_a_brief_with_no_negation_keeps_its_strength(self) -> None:
        """The softening must not reach a sentence that never doubled back."""

        self.assertEqual(read("スラップで。").strength_of("slap"), PLAIN_STRENGTH)
        self.assertEqual(read("かなりサイケ。").strength_of("psychedelic"), LARGE_STRENGTH)
        self.assertEqual(read("少しだけサイケ。").strength_of("psychedelic"), SMALL_STRENGTH)

    def test_one_refusal_spelled_two_ways_is_still_one_refusal(self) -> None:
        """The reason the fix counts spans and not matches.

        ``ではない`` is found by both ``ではない`` and ``はない`` at overlapping
        positions. Counting matches would have made every 「〜ではない」 in the
        vocabulary come out affirmed -- the ordinary case, broken by the fix for
        the rare one. Touching is not overlapping, which is what still separates
        「なくはない」 into two.
        """

        for text, name in (
            ("スラップではない。", "slap"),
            ("サイケではないやつ。", "psychedelic"),
            ("メリハリの無いテクノ。", "contrast"),
            ("暗くない。", "dark"),
            ("跳ねすぎない。", "swung"),
        ):
            with self.subTest(text=text):
                self.assertTrue(read(text).refused(name))
                self.assertEqual(read(text).strength_of(name), 0.0)

    def test_a_negation_on_an_antonym_is_still_misread(self) -> None:
        """Pinned as known-wrong, so the next reader is not surprised twice.

        「手数は少なくない」 fires one negator, ``くない``, and it belongs to
        「少なく」 rather than to the only mention in the clause. Reading it right
        means knowing 少ない is the opposite of 手数, which this module does not
        know. Recorded rather than guessed at -- change this test when it is
        fixed, and do not treat it as a licence to invent the antonym table.
        """

        self.assertTrue(read("手数は少なくない。").refused("busy"))

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
