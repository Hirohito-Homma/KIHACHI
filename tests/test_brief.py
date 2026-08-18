from __future__ import annotations

import unittest

from kihachi_music_ai.brief import describe, read_coverage
from kihachi_music_ai.music_brain import MusicBrain

AMBIENT = (
    "アンビエント。110 BPM、D#m。2分程度。きらびやかで高域中心、繊細。"
    "ベースは控えめで薄い。パーカッションは軽く、シェイカーとハイハット中心。"
    "インストゥルメンタル。"
)
MUTATION = (
    "Mutation Funk、DUB、Tech House。110 BPM、D#m。2分程度。"
    "ファンキーなスラップベース。サイケデリック、インストゥルメンタル。"
)


class CoverageTests(unittest.TestCase):
    def test_what_the_readers_act_on_is_named(self) -> None:
        coverage = read_coverage(MUTATION)

        acted = {label for clause in coverage["clauses"] for label in clause["read_as"]}
        self.assertIn("bpm", acted)
        self.assertIn("key", acted)
        self.assertIn("duration", acted)
        self.assertIn("genre", acted)
        self.assertTrue(any(label.startswith("trait:") for label in acted))

    def test_the_ambient_brief_still_loses_most_of_the_sound(self) -> None:
        """The measurement this module exists for, pinned as it now stands.

        `きらびやかで高域中心` used to be here too -- it is the clause the module
        docstring opens with. The `bright` trait reads it now, and the SongSpec
        moves off its genre default of 0.48 because of it. Everything else about
        the sound is still lost, and the song is still composed anyway.
        """

        coverage = read_coverage(AMBIENT)

        self.assertNotIn("きらびやかで高域中心", coverage["unread"])
        self.assertIn("ベースは控えめで薄い", coverage["unread"])
        self.assertIn("繊細", coverage["unread"])
        # Half, up from 0.4: one more clause of ten is now heard.
        self.assertLessEqual(coverage["read_fraction"], 0.5)
        # It still composes -- silently, which is the problem being reported.
        self.assertEqual(MusicBrain(seed=8).analyze(AMBIENT).song.bpm, 110.0)

    def test_a_brief_of_nothing_but_numbers_is_fully_read(self) -> None:
        coverage = read_coverage("110 BPM、D#m")

        self.assertEqual(coverage["unread"], [])
        self.assertEqual(coverage["read_fraction"], 1.0)

    def test_a_clause_read_only_in_part_says_which(self) -> None:
        """Clause granularity alone would hide this.

        `ダブの32小節` contains a genre, so the clause counts as read -- while
        nothing anywhere reads a bar count, and `_total_bars` falls through to
        its default 32. Reporting how much of the clause was touched is what
        surfaces it, and following that thread is how the key bug below was
        found.
        """

        brief = "ダブの32小節。110BPM、D#マイナー。"

        coverage = read_coverage(brief)

        self.assertIn("ダブの32小節", coverage["partly_read"])
        self.assertNotIn("ダブの32小節", coverage["unread"])
        self.assertEqual(MusicBrain(seed=8).analyze(brief).song.total_bars, 32)

    def test_a_japanese_minor_key_is_not_composed_in_major(self) -> None:
        """Three shipped projects asked for D#マイナー and carry D# major.

        The coverage report flagged the statement as only partly read; the
        reason was that `parse_key` knew `m`, `min` and `minor` and no Japanese
        at all, so the mode fell through to its "nothing stated" default.
        """

        spec = MusicBrain(seed=8).analyze("ダブ。110BPM、D#マイナー。")

        self.assertEqual(spec.song.key, "D# minor")
        self.assertEqual(spec.song.mode, "minor")

    def test_a_verse_named_in_japanese_is_reported_as_unread(self) -> None:
        """The report and the song have to agree about 「Aメロ」.

        `brief` re-runs the brain's readers so coverage cannot drift from
        behaviour, and it imported the key *pattern* -- which matched 「Aメロ」
        and made the report say a key had been read, because the song was
        composing in A major. Filtering only `parse_key` would have kept the
        claim and dropped the behaviour, which is the same disagreement the
        other way round.
        """

        brief = "ポップ。Aメロは静かに。"

        coverage = read_coverage(brief)

        self.assertIn("Aメロは静かに", coverage["unread"])
        self.assertEqual(MusicBrain(seed=8).analyze(brief).song.key, "C minor")

    def test_the_key_a_brief_states_is_still_the_key_it_gets(self) -> None:
        brief = "Aメロは静かに、キーはDマイナー。"

        spec = MusicBrain(seed=8).analyze(brief)

        self.assertEqual(spec.song.key, "D minor")


class ReportTests(unittest.TestCase):
    def test_an_unread_clause_is_not_called_a_rejected_one(self) -> None:
        coverage = read_coverage(AMBIENT)

        self.assertIn("not a rejected one", coverage["note"])
        self.assertIn("still composes", coverage["note"])

    def test_the_lines_show_every_clause_and_what_read_it(self) -> None:
        lines = "\n".join(describe(read_coverage(AMBIENT)))

        self.assertIn("nothing acted on this", lines)
        self.assertIn("繊細", lines)
        self.assertIn("155 surface forms", lines)

    def test_a_fully_read_brief_says_nothing_about_gaps(self) -> None:
        lines = "\n".join(describe(read_coverage("110 BPM、D#m")))

        self.assertNotIn("went unread", lines)


class DriftTests(unittest.TestCase):
    def test_every_trait_word_is_reported_as_read(self) -> None:
        """A trait the brain acts on must never be reported as unread."""

        from kihachi_music_ai.intent import TRAIT_WORDS

        for name, words in TRAIT_WORDS.items():
            with self.subTest(trait=name):
                coverage = read_coverage(words[0])

                self.assertEqual(coverage["unread"], [])


if __name__ == "__main__":
    unittest.main()
