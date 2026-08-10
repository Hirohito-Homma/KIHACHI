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


if __name__ == "__main__":
    unittest.main()
