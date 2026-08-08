from __future__ import annotations

import unittest

from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.music_brain import MusicBrain

EXAMPLE = (
    "Mutation Funk、DUB、Tech House。110 BPM、D#m。ファンキーなスラップベース。"
    "前半ミニマル、後半サイケデリック。Vocoderを使用。"
)


class MusicBrainTests(unittest.TestCase):
    def test_example_is_interpreted_as_expected(self) -> None:
        spec = MusicBrain(seed=8).analyze(EXAMPLE)
        self.assertEqual(spec.song.title, "Mutation Signal")
        self.assertEqual(spec.song.bpm, 110.0)
        self.assertEqual(spec.song.key, "D# minor")
        self.assertEqual(spec.song.total_bars, 32)
        self.assertEqual(spec.harmony.progression, ("D#m", "B", "F#", "C#"))
        self.assertEqual(spec.bass.technique, "slap")
        self.assertTrue(spec.vocal.vocoder)

    def test_genre_weights_and_section_arc(self) -> None:
        spec = MusicBrain().analyze(EXAMPLE)
        weights = {item.name: item.weight for item in spec.style.genres}
        self.assertEqual(weights, {"mutation_funk": 0.4, "dub": 0.3, "tech_house": 0.3})
        self.assertTrue(all(section.minimal for section in spec.arrangement[:2]))
        self.assertTrue(all(not section.minimal for section in spec.arrangement[2:]))
        self.assertGreater(spec.arrangement[-1].psychedelic, spec.arrangement[0].psychedelic)

    def test_song_spec_round_trip(self) -> None:
        original = MusicBrain().analyze(EXAMPLE)
        restored = SongSpec.from_json(original.to_json())
        self.assertEqual(restored, original)

    def test_duration_request_rounds_to_eight_bar_boundary(self) -> None:
        spec = MusicBrain().analyze("Tech House、120 BPM、A minor、5分")
        self.assertEqual(spec.song.total_bars % 8, 0)
        self.assertGreater(spec.song.total_bars, 100)

    def test_empty_prompt_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MusicBrain().analyze("  ")


if __name__ == "__main__":
    unittest.main()

