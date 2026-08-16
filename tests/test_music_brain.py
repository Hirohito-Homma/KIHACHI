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


class RefusalTests(unittest.TestCase):
    """A refused trait used to be read as a request for it."""

    def test_a_refused_technique_is_not_the_technique_chosen(self) -> None:
        spec = MusicBrain().analyze("Tech House。スラップじゃなくて指弾きで。")

        self.assertEqual(spec.bass.technique, "fingered")

    def test_refusing_slap_lands_on_every_value_slap_would_have_raised(self) -> None:
        refused = MusicBrain().analyze("Tech House。スラップじゃなくて指弾きで。")
        silent = MusicBrain().analyze("Tech House。")

        self.assertEqual(refused.bass.syncopation, silent.bass.syncopation)
        self.assertEqual(refused.bass.ghost_note_probability, silent.bass.ghost_note_probability)
        self.assertEqual(
            refused.bass.octave_jump_probability, silent.bass.octave_jump_probability
        )
        self.assertEqual(refused.groove.syncopation, silent.groove.syncopation)

    def test_a_refused_part_is_not_written(self) -> None:
        spec = MusicBrain().analyze("Tech House。アルペジオは無しで。")

        self.assertIsNone(spec.instruments)

    def test_a_refused_vocoder_leaves_the_song_instrumental(self) -> None:
        spec = MusicBrain().analyze("Tech House。vocoderなしで。")

        self.assertFalse(spec.vocal.enabled)
        self.assertFalse(spec.vocal.vocoder)


class DegreeTests(unittest.TestCase):
    """"少し" and "かなり" now land somewhere, and a plain mention lands where it did."""

    def test_a_plain_mention_still_gives_the_old_constant(self) -> None:
        spec = MusicBrain().analyze("Tech House。サイケに。")

        self.assertEqual(spec.style.psychedelic, 0.82)

    def test_hedging_lands_below_a_plain_mention(self) -> None:
        hedged = MusicBrain().analyze("Tech House。少しサイケ。")
        plain = MusicBrain().analyze("Tech House。サイケに。")
        silent = MusicBrain().analyze("Tech House。")

        self.assertLess(hedged.style.psychedelic, plain.style.psychedelic)
        self.assertGreater(hedged.style.psychedelic, silent.style.psychedelic)

    def test_insisting_lands_above_it(self) -> None:
        insisted = MusicBrain().analyze("Tech House。かなりサイケ。")
        plain = MusicBrain().analyze("Tech House。サイケに。")

        self.assertGreater(insisted.style.psychedelic, plain.style.psychedelic)
        self.assertLessEqual(insisted.style.psychedelic, 1.0)

    def test_degree_does_not_leak_into_an_unmodified_brief(self) -> None:
        """The pinned brief hedges nothing, so this whole layer is a no-op on it."""

        spec = MusicBrain(seed=8).analyze(EXAMPLE)

        self.assertEqual(spec.bass.mutation, 0.78)
        self.assertEqual(spec.bass.syncopation, 0.86)
        self.assertEqual(spec.groove.syncopation, 0.82)
        self.assertEqual(spec.style.psychedelic, 0.82)
        self.assertEqual(spec.style.darkness, 0.72)
        self.assertEqual(spec.drums.dub_space, 0.62)
        self.assertEqual(spec.chords.dub_delay, 0.74)


class StatedDarknessTests(unittest.TestCase):
    """`style.darkness` was reachable only through the genre until 2026-08-17."""

    @staticmethod
    def darkness(prompt: str) -> float:
        return MusicBrain(seed=1).analyze(prompt).style.darkness

    def test_a_brief_that_says_nothing_keeps_the_genre_reading(self) -> None:
        self.assertEqual(self.darkness("アンビエント。"), 0.48)

    def test_saying_it_moves_it_and_the_degree_decides_how_far(self) -> None:
        self.assertEqual(self.darkness("少し暗いアンビエント。"), 0.62)
        self.assertEqual(self.darkness("暗いアンビエント。"), 0.76)
        self.assertEqual(self.darkness("かなり暗いアンビエント。"), 0.9)

    def test_brightness_is_its_own_trait_and_moves_the_other_way(self) -> None:
        self.assertEqual(self.darkness("明るいアンビエント。"), 0.226667)
        self.assertEqual(self.darkness("かなり明るいアンビエント。"), 0.1)

    def test_a_genre_already_past_the_pole_is_left_alone(self) -> None:
        """Reading the pole as a target made agreeing with the brief undo it.

        Techno's own darkness is 1.0, and the first draft answered 「暗いテクノ」
        with 0.93 -- less dark than 「テクノ」 said on its own.
        """

        self.assertEqual(self.darkness("テクノ。"), 1.0)
        self.assertEqual(self.darkness("暗いテクノ。"), 1.0)
        self.assertEqual(self.darkness("かなり暗いテクノ。"), 1.0)

    def test_refusing_darkness_is_not_asking_for_brightness(self) -> None:
        """The genre's own reading beats either pole when the brief only says no."""

        self.assertEqual(self.darkness("暗くないテクノ。"), 1.0)
        self.assertEqual(self.darkness("明るくないアンビエント。"), 0.48)

    def test_the_brief_the_coverage_module_opens_with_now_moves(self) -> None:
        ambient = (
            "アンビエント。110 BPM、D#m。2分程度。きらびやかで高域中心、繊細。"
            "ベースは控えめで薄い。パーカッションは軽く、シェイカーとハイハット中心。"
        )

        self.assertEqual(self.darkness(ambient), 0.226667)


if __name__ == "__main__":
    unittest.main()

