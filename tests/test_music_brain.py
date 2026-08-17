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

    def test_swing_was_reachable_by_one_genre_of_a_thousand(self) -> None:
        """`groove.swing` drives the composer's timing, so this reaches the MIDI.

        Only `mutation_funk` ever set it. Every family including Jazz left it at
        0.5, so 「シャッフルで」 and 「ジャズ」 both composed straight eighths.
        """

        def swing(prompt: str) -> float:
            return MusicBrain(seed=1).analyze(prompt).groove.swing

        self.assertEqual(swing("テクノ。"), 0.5)
        self.assertEqual(swing("少し跳ねるテクノ。"), 0.553333)
        self.assertEqual(swing("シャッフルで、テクノ。"), 0.606667)
        self.assertEqual(swing("かなりスウィングさせて、テクノ。"), 0.66)
        self.assertEqual(swing("スウィングしないテクノ。"), 0.5)
        self.assertEqual(swing("ストレートなテクノ。"), 0.5)

    def test_syncopation_was_reachable_by_no_genre_at_all(self) -> None:
        """Swing had one genre of a thousand; this had none.

        `derive.Profile` has no syncopation field, so all 1021 genres leave both
        values at the constant, and the only thing that ever moved them was the
        `slap` trait. Both reach the composed notes.
        """

        def groove(prompt: str) -> float:
            return MusicBrain(seed=1).analyze(prompt).groove.syncopation

        self.assertEqual(groove("テクノ。"), 0.58)
        self.assertEqual(groove("少しだけシンコペを効かせたテクノ。"), 0.68)
        self.assertEqual(groove("シンコペを効かせたテクノ。"), 0.78)
        self.assertEqual(groove("かなりうねるテクノ。"), 0.88)
        self.assertEqual(groove("オンビートで、テクノ。"), 0.326667)
        self.assertEqual(groove("かなり表打ちのテクノ。"), 0.2)
        # A refusal is not a request for the other pole, here as everywhere.
        self.assertEqual(groove("シンコペ無しのテクノ。"), 0.58)

    def test_the_bass_hears_the_same_word_as_the_groove(self) -> None:
        """`bass.syncopation` is the twin field, and slap starts it higher."""

        spec = MusicBrain(seed=1).analyze("スラップベースのファンク。オンビートで。")
        self.assertEqual(spec.groove.syncopation, 0.406667)
        self.assertEqual(spec.bass.syncopation, 0.42)

    def test_humanize_moves_from_whatever_the_family_stated(self) -> None:
        """Every family states this one, and no brief could disagree with it.

        Hardcore Electronic sits at 0.04 and Jazz at 0.45, so the loose pole is
        above all of them: a brief that agrees with Jazz still has somewhere to
        go, and the tight pole is 0.02 rather than 0.0 because a quantiser is
        not a preference.
        """

        def humanize(prompt: str) -> float:
            return MusicBrain(seed=1).analyze(prompt).groove.humanize

        self.assertEqual(humanize("テクノ。"), 0.06)
        self.assertEqual(humanize("少しヨレたテクノ。"), 0.273333)
        self.assertEqual(humanize("手弾きっぽいテクノ。"), 0.486667)
        self.assertEqual(humanize("ジャズ。"), 0.45)
        self.assertEqual(humanize("かなり人間っぽいジャズ。"), 0.7)
        self.assertEqual(humanize("タイトなジャズ。"), 0.163333)
        self.assertEqual(humanize("かっちりしたテクノ。"), 0.033333)
        self.assertEqual(humanize("ヨレないテクノ。"), 0.06)

    def test_drum_density_is_sayable_and_minimal_is_a_different_word(self) -> None:
        def kit(prompt: str) -> tuple[float, float]:
            drums = MusicBrain(seed=1).analyze(prompt).drums
            return drums.kick_density, drums.hat_density

        self.assertEqual(kit("テクノ。"), (0.85, 0.92))
        self.assertEqual(kit("少しスカスカなテクノ。"), (0.666667, 0.713333))
        self.assertEqual(kit("かなり余白のあるダブ。"), (0.3, 0.3))
        self.assertEqual(kit("ダブ。"), (0.38, 0.45))
        # `minimal` gates the arrangement's opening sections and nothing here.
        self.assertEqual(kit("ミニマルなテクノ。"), (0.85, 0.92))
        self.assertEqual(kit("スカスカじゃないテクノ。"), (0.85, 0.92))

    def test_asking_a_saturated_kit_for_more_changes_the_number_and_not_the_notes(self) -> None:
        """The honest half of this: upwards, the pattern hits its own ceiling.

        Techno already sits at 0.85/0.92 and `build_pattern` is already at
        `groove.kick_steps[1]`, so 「かなり手数の多いテクノ」 raises the spec to
        0.95 and composes the same 381 drum notes. Downwards it works from the
        same starting point, and upwards it works from a genre with room.
        """

        from kihachi_music_ai.composer import COMPOSERS

        def drums(prompt: str) -> int:
            spec = MusicBrain(seed=8).analyze(prompt)
            return len(COMPOSERS["drums"](spec))

        self.assertEqual(drums("テクノ。8小節。"), 381)
        self.assertEqual(drums("かなり手数の多いテクノ。8小節。"), 381)
        self.assertEqual(drums("スカスカなテクノ。8小節。"), 320)
        self.assertEqual(drums("ダブ。8小節。"), 240)
        self.assertEqual(drums("手数の多いダブ。8小節。"), 272)

    def test_the_brief_the_coverage_module_opens_with_now_moves(self) -> None:
        ambient = (
            "アンビエント。110 BPM、D#m。2分程度。きらびやかで高域中心、繊細。"
            "ベースは控えめで薄い。パーカッションは軽く、シェイカーとハイハット中心。"
        )

        self.assertEqual(self.darkness(ambient), 0.226667)


if __name__ == "__main__":
    unittest.main()

