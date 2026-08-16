from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.transcribe import ONSET_SNAP_SEC, transcribe

RATE = 48000.0
BPM = 120.0
"""One beat is 0.5 s, so a note per half second is a note per beat."""


def note(hz: float, seconds: float) -> list[float]:
    frames = int(seconds * RATE)
    out = []
    for index in range(frames):
        attack = min(1.0, index / (0.005 * RATE))
        decay = math.exp(-index / (0.6 * RATE))
        out.append(
            0.5
            * attack
            * decay
            * (
                math.sin(2 * math.pi * hz * index / RATE)
                + 0.5 * math.sin(4 * math.pi * hz * index / RATE)
            )
        )
    return out


def line(pitches: list[float], seconds: float = 0.5) -> list[float]:
    played: list[float] = []
    for hz in pitches:
        played.extend(note(hz, seconds))
    return played


def silence(seconds: float) -> list[float]:
    return [0.0] * int(seconds * RATE)


D_SHARP_2 = 77.782
F_SHARP_2 = 92.499
G_SHARP_2 = 103.826


class PitchTests(unittest.TestCase):
    def test_each_note_comes_back_at_its_own_pitch(self) -> None:
        result = transcribe(line([D_SHARP_2, F_SHARP_2, G_SHARP_2, D_SHARP_2]), RATE, bpm=BPM)

        self.assertEqual([item.pitch for item in result.notes], [39, 42, 44, 39])

    def test_a_repeated_pitch_is_not_merged_into_one_note(self) -> None:
        """Two hits on one note are two notes; the gap between them says so."""

        played = line([D_SHARP_2]) + silence(0.3) + line([D_SHARP_2])

        result = transcribe(played, RATE, bpm=BPM)

        self.assertEqual(len(result.notes), 2)
        self.assertEqual({item.pitch for item in result.notes}, {39})


class TimingTests(unittest.TestCase):
    def test_note_starts_land_on_the_onset_not_the_tracker_hop(self) -> None:
        """The tracker's 128 ms hop is a quarter beat here -- unusable alone."""

        result = transcribe(line([D_SHARP_2, F_SHARP_2, G_SHARP_2, D_SHARP_2]), RATE, bpm=BPM)

        for index, item in enumerate(result.notes):
            self.assertAlmostEqual(item.start_beats, float(index), delta=0.05)
        self.assertEqual(result.coverage["starts_snapped_to_onsets"], 4)

    def test_notes_never_overlap_after_snapping(self) -> None:
        result = transcribe(line([D_SHARP_2, F_SHARP_2, G_SHARP_2]), RATE, bpm=BPM)

        for earlier, later in zip(result.notes, result.notes[1:]):
            self.assertLessEqual(
                earlier.start_beats + earlier.duration_beats, later.start_beats + 1e-6
            )

    def test_an_onset_too_far_away_is_left_alone(self) -> None:
        """A snap window wider than this would drag notes onto somebody else's hit."""

        self.assertLess(ONSET_SNAP_SEC, 60.0 / BPM / 2)


class CoverageTests(unittest.TestCase):
    def test_silence_transcribes_to_nothing(self) -> None:
        result = transcribe(silence(2.0), RATE, bpm=BPM)

        self.assertEqual(result.notes, ())
        self.assertEqual(result.coverage["voiced_fraction"], 0.0)

    def test_a_sample_shorter_than_a_window_says_so(self) -> None:
        result = transcribe(note(D_SHARP_2, 0.05), RATE, bpm=BPM)

        self.assertEqual(result.notes, ())
        self.assertIn("shorter than one analysis window", result.coverage["note"])

    def test_the_coverage_does_not_let_a_note_count_imply_completeness(self) -> None:
        result = transcribe(line([D_SHARP_2, F_SHARP_2]), RATE, bpm=BPM)

        self.assertIn("61%", result.coverage["note"])
        self.assertIn("monophonic_only", result.coverage["scope"])
        self.assertLessEqual(result.coverage["voiced_frames"], result.coverage["frames"])


class MixTests(unittest.TestCase):
    def test_a_dense_mix_returns_nothing_rather_than_inventing_notes(self) -> None:
        """Measured on the real cut: a full mix reads 1% voiced and yields none."""

        mixed = [
            a + b + c
            for a, b, c in zip(
                line([D_SHARP_2] * 4),
                line([F_SHARP_2 * 2.02] * 4),
                line([G_SHARP_2 * 3.03] * 4),
            )
        ]

        result = transcribe(mixed, RATE, bpm=BPM)

        self.assertLess(result.coverage["voiced_fraction"], 0.5)


if __name__ == "__main__":
    unittest.main()
