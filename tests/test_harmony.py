"""The harmony has to belong to the genre too.

Every one of the 1020 genres used to play four triads on degrees i-VI-III-VII.
Jazz got a swung ride, a comping articulation, and Am-Dm-C-G underneath it.
"""

from __future__ import annotations

import unittest

from kihachi_music_ai.composer import compose_chords, compose_tracks
from kihachi_music_ai.derive import FAMILY_PROFILES
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.theory import (
    CHORD_QUALITIES,
    DEFAULT_PROGRESSION,
    PROGRESSIONS,
    chord_is_minor,
    chord_pitches,
    parse_key,
    progression_for_key,
    split_chord,
)


class ChordSymbolTests(unittest.TestCase):
    def test_a_major_seventh_is_not_a_minor_chord(self) -> None:
        """``startswith("m")`` said it was, in three modules."""

        self.assertFalse(chord_is_minor("Cmaj7"))
        self.assertTrue(chord_is_minor("Cm7"))
        self.assertTrue(chord_is_minor("Cm7b5"))
        self.assertFalse(chord_is_minor("C7"))

    def test_a_power_chord_really_has_no_third(self) -> None:
        # Writing the third anyway is what made every metal brief sound like a
        # rock brief.
        self.assertEqual(len(chord_pitches("A5")), 2)

    def test_the_root_comes_first_whatever_the_quality(self) -> None:
        for suffix, intervals in CHORD_QUALITIES.items():
            with self.subTest(quality=suffix or "major"):
                self.assertEqual(intervals[0], 0)
                pitches = chord_pitches("C" + suffix, octave=3)
                self.assertEqual(pitches[0], min(pitches))

    def test_an_unwritable_quality_is_refused_rather_than_guessed(self) -> None:
        with self.assertRaises(ValueError):
            split_chord("Cwhatever")

    def test_every_chord_a_progression_writes_can_be_played(self) -> None:
        for name, shape in PROGRESSIONS.items():
            for mode in ("minor", "major"):
                with self.subTest(progression=name, mode=mode):
                    for chord in progression_for_key(9, mode, shape=name):
                        self.assertTrue(chord_pitches(chord))


class ProgressionTests(unittest.TestCase):
    def _progression(self, prompt: str) -> tuple[str, ...]:
        return MusicBrain(seed=8).analyze(prompt + "。Am。").harmony.progression

    def test_every_progression_a_genre_can_ask_for_is_defined(self) -> None:
        named = {p.progression for p in FAMILY_PROFILES.values() if p.progression}

        self.assertEqual(named - set(PROGRESSIONS), set())

    def test_jazz_plays_a_two_five_one_instead_of_the_default_four(self) -> None:
        self.assertEqual(self._progression("ジャズ"), ("Bm7b5", "E7", "Am7", "Am7"))

    def test_metal_plays_power_chords(self) -> None:
        for chord in self._progression("Death Metal"):
            self.assertTrue(chord.endswith("5"), chord)

    def test_techno_does_not_move_much_and_jazz_does(self) -> None:
        techno = len(set(self._progression("テクノ")))
        jazz = len(set(self._progression("ジャズ")))

        self.assertLess(techno, jazz)

    def test_the_shape_is_stated_once_and_works_in_every_key(self) -> None:
        # Flat keys spell their roots flat, as they always did.
        self.assertEqual(
            MusicBrain(seed=8).analyze("ジャズ。Ebm。").harmony.progression,
            ("Fm7b5", "Bb7", "Ebm7", "Ebm7"),
        )

    def test_the_default_is_still_the_progression_every_song_had(self) -> None:
        self.assertEqual(
            progression_for_key(3, "minor", shape=DEFAULT_PROGRESSION),
            ("D#m", "B", "F#", "C#"),
        )
        self.assertEqual(progression_for_key(0, "major"), ("C", "Am", "F", "G"))

    def test_an_unknown_shape_falls_back_rather_than_raising(self) -> None:
        self.assertEqual(
            progression_for_key(9, "minor", shape="no_such_shape"),
            progression_for_key(9, "minor"),
        )


class EveryPartSurvivesTheNewChordsTests(unittest.TestCase):
    """Sevenths and power chords change how many notes a chord has.

    ``compose_synth`` unpacked exactly three, so the first genre to ask for a
    major seventh would have raised ValueError rather than composing.
    """

    PROMPT = "。Am。5分程度。シンセスタブ、アルペジオ、ボコーダー。"

    def test_every_family_composes_all_six_parts(self) -> None:
        for prompt in ("ジャズ", "Death Metal", "ヒップホップ", "ボサノヴァ", "テクノ"):
            with self.subTest(genre=prompt):
                spec = MusicBrain(seed=8).analyze(prompt + self.PROMPT)
                tracks = compose_tracks(spec)
                self.assertEqual(len(tracks), 6)
                for part, notes in tracks.items():
                    self.assertTrue(notes, f"{prompt}: {part} wrote nothing")

    def test_a_seventh_chord_reaches_the_notes(self) -> None:
        spec = MusicBrain(seed=8).analyze("ジャズ。Am。")
        pitch_classes = {note.pitch % 12 for note in compose_chords(spec)}

        # G is the seventh of Am7 and is not in the plain A minor triad.
        self.assertIn(7, pitch_classes)  # E, from the triad
        self.assertIn(11, pitch_classes)  # B, the root of the iiø7


class KeyReadingTests(unittest.TestCase):
    """What a bare letter means depends on what is written next to it.

    `_KEY_RE` guards against a Latin letter on either side, which is what makes
    `key of G` work and `Gm` a minor key. Nothing guarded the Japanese side, so
    every 「Aメロ」 -- the standard word for a verse -- stated a key of A major,
    and so did 「Eギター」, 「Gベース」 and 「Bパート」. None of those clauses is
    about key, and the key they set was the one thing about the song a brief
    is most likely to state deliberately somewhere else.
    """

    def test_the_japanese_word_for_a_verse_is_not_a_key(self) -> None:
        for brief in ("Aメロは静かに", "Bメロで盛り上げて", "Aメロからサビへ", "Bパートを長く"):
            with self.subTest(brief=brief):
                self.assertEqual(parse_key(brief)[0], "C minor")

    def test_an_instrument_named_by_its_letter_is_not_a_key(self) -> None:
        for brief in ("Eギターを重ねて", "Gベースを太く"):
            with self.subTest(brief=brief):
                self.assertEqual(parse_key(brief)[0], "C minor")

    def test_a_key_stated_in_japanese_still_reads(self) -> None:
        """The quality word is inside the match, so it is the boundary.

        These are the shapes the refusal must not touch: with a quality there
        is no ambiguity about what the letter was doing, whichever script the
        quality is written in.
        """

        self.assertEqual(parse_key("D#マイナーのテクノ")[0], "D# minor")
        self.assertEqual(parse_key("キーはEマイナー")[0], "E minor")
        self.assertEqual(parse_key("Cメジャーで")[0], "C major")
        self.assertEqual(parse_key("A♭マイナー")[0], "Ab minor")

    def test_a_particle_after_a_bare_letter_still_reads(self) -> None:
        """Hiragana is not refused, and it is why the rule names katakana.

        Every particle that can follow a stated key is hiragana, so 「キーはAで」
        has to keep working. Katakana after a bare letter is the opposite
        signal: it is a word, and the letter was its first syllable.
        """

        self.assertEqual(parse_key("キーはAで")[0], "A major")
        self.assertEqual(parse_key("キーはGにして")[0], "G major")

    def test_the_key_is_read_from_the_clause_that_states_one(self) -> None:
        """`parse_key` takes the first match, and 「Aメロ」 used to be first.

        A brief that says both -- which is the normal way to write one -- lost
        the key it stated to the section it named before it.
        """

        self.assertEqual(parse_key("Aメロは静かに、キーはDマイナー")[0], "D minor")

    def test_the_ascii_shapes_are_untouched(self) -> None:
        self.assertEqual(parse_key("key of G")[0], "G major")
        self.assertEqual(parse_key("Gm")[0], "G minor")
        self.assertEqual(parse_key("C minor")[0], "C minor")

    def test_a_genre_written_in_latin_script_is_not_a_key(self) -> None:
        """The Latin guard reads a letter, and these are joined by marks.

        `(?![A-Za-z])` was written to stop `G` matching inside `Groove`, and a
        hyphen and an ampersand are not letters -- so `G-Funk`, a genre this
        database answers to by name, composed in G major, and `D&B` in D. The
        second half is refused too: with only the first rule `D&B` moved from
        D major to B major rather than to no key at all.
        """

        for brief in ("G-Funk", "g-funk track", "D&B", "B-Boy", "make a G-Funk beat"):
            with self.subTest(brief=brief):
                self.assertEqual(parse_key(brief)[0], "C minor")

    def test_an_uppercase_b_is_not_a_flat(self) -> None:
        """`re.IGNORECASE` was folding the accidental as well as the letter.

        So `EBM` -- Electronic Body Music, a row in this database -- read as
        E, flat, and `M` for the quality: E flat minor, from three letters
        that name a genre. The accidental is the one group that has to stay
        case-sensitive, and `Ab`, `Bb` and `Eb` still read as flats.
        """

        self.assertEqual(parse_key("EBM")[0], "C minor")
        self.assertEqual(parse_key("Ab major")[0], "Ab major")
        self.assertEqual(parse_key("key of Bb")[0], "Bb major")

    def test_an_english_word_after_the_letter_makes_it_a_name(self) -> None:
        """`A Cappella` is the one left in the database, and `a` is the article.

        A key written in English without a quality word ends its clause --
        `in A`, `key of G`, `the key is C` -- so a letter followed by another
        English word is part of a name. The lowercase article is refused
        outright: `make a G-Funk beat` still composed in A major once the
        hyphen rule had refused the G.
        """

        self.assertEqual(parse_key("A Cappella")[0], "C minor")
        self.assertEqual(parse_key("in A")[0], "A major")
        self.assertEqual(parse_key("the key is C")[0], "C major")
        self.assertEqual(parse_key("key of G")[0], "G major")

    def test_a_mode_this_project_cannot_write_is_left_alone(self) -> None:
        """Known limit, stated so the refusal is not mistaken for a reading.

        「Dドリアン」 names D as a tonic and this reader has no modes beyond
        major and minor, so the refusal loses the D. Composing D major from it
        would have been a different wrong answer, not a right one.
        """

        self.assertEqual(parse_key("Dドリアン")[0], "C minor")


if __name__ == "__main__":
    unittest.main()
