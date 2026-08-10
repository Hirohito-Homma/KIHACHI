"""The last three SongSpec fields the composer was not really reading.

``bass.role`` reached only the audio prompt, ``hat_density`` was a switch
wearing a float's clothes, and the bar was 4.0 beats long as a literal.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from kihachi_music_ai.composer import compose_bass, compose_drums, compose_tracks
from kihachi_music_ai.groove_tables import (
    BASS_ROLES,
    CLOSED_HAT,
    DEFAULT_BASS_ROLE,
    DRUM_PATTERNS,
    bass_role,
    hat_positions,
)
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.theory import beats_per_bar

SIX_PARTS = "。Am。5分程度。シンセスタブ、アルペジオ、ボコーダー。"


def _with(spec, **fields):
    return replace(spec, **fields)


class BassRoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = MusicBrain(seed=8).analyze("テクノ。Am。")

    def _bass(self, role: str):
        spec = _with(self.spec, bass=replace(self.spec.bass, role=role))
        return compose_bass(spec)

    def test_a_supporting_bass_plays_less_than_a_dominant_one(self) -> None:
        supporting = self._bass("supporting")
        dominant = self._bass("dominant")

        self.assertLess(len(supporting), len(dominant))

    def test_a_supporting_bass_is_quieter_than_a_dominant_one(self) -> None:
        def loudest(notes):
            return max(note.velocity for note in notes)

        self.assertLess(loudest(self._bass("supporting")), loudest(self._bass("dominant")))

    def test_the_three_roles_are_the_ones_the_audio_prompt_weighs(self) -> None:
        # A fourth name would read as 0.5 in ``prompt_compiler._role_weight``.
        self.assertEqual(set(BASS_ROLES), {"supporting", "present", "dominant"})

    def test_an_unknown_role_falls_back_rather_than_raising(self) -> None:
        self.assertEqual(bass_role("lead"), BASS_ROLES[DEFAULT_BASS_ROLE])

    def test_the_incumbent_role_is_what_every_bass_used_to_play(self) -> None:
        self.assertEqual(BASS_ROLES[DEFAULT_BASS_ROLE].density_scale, 1.0)
        self.assertEqual(BASS_ROLES[DEFAULT_BASS_ROLE].velocity, 102)


class HatDensityTests(unittest.TestCase):
    """It was ``>= 0.3``, so every value above 0.3 wrote the same file."""

    def _hats(self, density: float) -> int:
        spec = MusicBrain(seed=8).analyze("テクノ。Am。")
        spec = _with(spec, drums=replace(spec.drums, hat_density=density))
        return len([n for n in compose_drums(spec) if n.pitch == CLOSED_HAT])

    def test_two_values_above_the_old_threshold_no_longer_agree(self) -> None:
        self.assertNotEqual(self._hats(0.4), self._hats(1.0))

    def test_more_density_never_means_fewer_hats(self) -> None:
        counts = [self._hats(value / 10) for value in range(11)]

        self.assertEqual(counts, sorted(counts))
        self.assertLess(counts[0], counts[-1])

    def test_thinning_keeps_the_sparse_skeleton_at_every_density(self) -> None:
        groove = DRUM_PATTERNS["four_on_floor"]
        skeleton = set(hat_positions(groove, 0.0))

        for step in range(11):
            with self.subTest(density=step / 10):
                self.assertLessEqual(skeleton, set(hat_positions(groove, step / 10)))

    def test_the_families_may_now_state_their_own(self) -> None:
        ambient = MusicBrain(seed=8).analyze("アンビエント。Am。").drums.hat_density
        techno = MusicBrain(seed=8).analyze("テクノ。Am。").drums.hat_density

        self.assertLess(ambient, techno)


class TimeSignatureTests(unittest.TestCase):
    def test_a_bar_is_as_long_as_the_signature_says(self) -> None:
        self.assertEqual(beats_per_bar("4/4"), 4.0)
        self.assertEqual(beats_per_bar("3/4"), 3.0)
        # Six eighth notes are three quarter-note beats, not six.
        self.assertEqual(beats_per_bar("6/8"), 3.0)
        self.assertEqual(beats_per_bar("5/4"), 5.0)

    def test_a_stated_meter_reaches_the_song(self) -> None:
        for prompt, expected in (
            ("ワルツ", "3/4"),
            ("3拍子のフォーク", "3/4"),
            ("6/8のブルース", "6/8"),
            ("ジャズ", "4/4"),
        ):
            with self.subTest(prompt=prompt):
                spec = MusicBrain(seed=8).analyze(prompt + "。Am。")
                self.assertEqual(spec.song.time_signature, expected)

    def test_a_three_four_song_is_three_quarters_as_long(self) -> None:
        four = MusicBrain(seed=8).analyze("フォーク。Am。120 BPM。")
        three = MusicBrain(seed=8).analyze("3拍子のフォーク。Am。120 BPM。")

        self.assertAlmostEqual(
            three.song.target_duration_sec, four.song.target_duration_sec * 0.75, places=3
        )

    def test_asking_for_a_length_in_three_four_gets_that_length(self) -> None:
        spec = MusicBrain(seed=8).analyze("3拍子。Am。3分程度。120 BPM。")

        self.assertAlmostEqual(spec.song.target_duration_sec, 180.0, places=1)

    def test_no_part_writes_outside_a_bar_of_any_meter(self) -> None:
        base = MusicBrain(seed=8).analyze("ジャズ" + SIX_PARTS)
        for signature in ("4/4", "3/4", "6/8", "5/4", "7/8"):
            spec = _with(base, song=replace(base.song, time_signature=signature))
            end = spec.song.total_bars * beats_per_bar(signature)
            with self.subTest(time_signature=signature):
                tracks = compose_tracks(spec)
                self.assertEqual(len(tracks), 6)
                for part, notes in tracks.items():
                    self.assertTrue(notes, f"{signature}: {part} wrote nothing")
                    for note in notes:
                        self.assertGreaterEqual(note.start_beats, 0.0)
                        self.assertLess(note.start_beats, end, f"{signature}: {part}")

    def test_a_shorter_bar_drops_the_slots_that_no_longer_fit(self) -> None:
        """A 3.5 slot in a 3/4 bar is the next bar's downbeat, not a late one."""

        base = MusicBrain(seed=8).analyze("テクノ。Am。")
        three = _with(base, song=replace(base.song, time_signature="3/4"))

        for note in compose_drums(three):
            self.assertLess(note.start_beats % 3.0, 3.0)
        # ...and the bar really is three beats: bar 1 starts at beat 3.
        starts = sorted({round(n.start_beats) for n in compose_drums(three)})
        self.assertIn(3, starts)


if __name__ == "__main__":
    unittest.main()
