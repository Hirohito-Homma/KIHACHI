from __future__ import annotations

import math
import random
import unittest

from kihachi_music_ai.pitch import (
    MAX_HZ,
    MIN_HZ,
    PitchFrame,
    estimate_root,
    hz_to_midi,
    track_pitch,
)

RATE = 48000.0
SECONDS = 1.2
"""Long enough for several analysis frames, short enough to keep the suite quick."""


def tone(
    hz: float,
    *,
    seconds: float = SECONDS,
    harmonics: tuple[tuple[int, float], ...] = ((1, 1.0), (2, 0.5), (3, 0.25)),
    noise: float = 0.0,
    amplitude: float = 0.3,
) -> list[float]:
    generator = random.Random(8)
    frames = int(seconds * RATE)
    return [
        amplitude
        * sum(level * math.sin(2 * math.pi * hz * partial * n / RATE) for partial, level in harmonics)
        + noise * (generator.random() * 2 - 1)
        for n in range(frames)
    ]


def average_hz(frames: list[PitchFrame]) -> float:
    voiced = [frame.hz for frame in frames if frame.hz is not None]
    return sum(voiced) / len(voiced) if voiced else 0.0


def cents(estimated: float, true: float) -> float:
    return abs(1200.0 * math.log2(estimated / true)) if estimated > 0 else 9999.0


class AccuracyTests(unittest.TestCase):
    def test_bass_notes_land_within_a_few_cents(self) -> None:
        for hz in (55.0, 77.782, 82.407, 110.0):
            with self.subTest(hz=hz):
                frames = track_pitch(tone(hz), RATE)

                self.assertTrue(all(frame.voiced for frame in frames))
                self.assertLess(cents(average_hz(frames), hz), 10.0)

    def test_a_weak_fundamental_does_not_become_an_octave_error(self) -> None:
        """Plain autocorrelation picks the second harmonic here; YIN's step 3 is why."""

        weak = ((1, 0.2), (2, 1.0), (3, 0.6))

        frames = track_pitch(tone(77.782, harmonics=weak), RATE)

        self.assertLess(cents(average_hz(frames), 77.782), 15.0)

    def test_noise_does_not_move_the_estimate(self) -> None:
        frames = track_pitch(tone(77.782, noise=0.1), RATE)

        self.assertLess(cents(average_hz(frames), 77.782), 15.0)


class VoicingTests(unittest.TestCase):
    def test_silence_is_unvoiced_rather_than_confidently_wrong(self) -> None:
        """The global minimum of a silent frame is noise, and would read as a note."""

        frames = track_pitch([0.0] * int(SECONDS * RATE), RATE)

        self.assertTrue(frames)
        self.assertFalse(any(frame.voiced for frame in frames))
        self.assertEqual(estimate_root(frames)["root"], None)

    def test_a_pitch_outside_the_bass_range_is_not_reported(self) -> None:
        frames = track_pitch(tone(MAX_HZ * 3, harmonics=((1, 1.0),)), RATE)

        self.assertTrue(all(MIN_HZ <= frame.hz <= MAX_HZ for frame in frames if frame.voiced))

    def test_too_short_a_signal_reports_nothing_rather_than_guessing(self) -> None:
        self.assertEqual(track_pitch(tone(110.0, seconds=0.05), RATE), [])


class RootTests(unittest.TestCase):
    def test_a_steady_note_agrees_with_itself(self) -> None:
        frames = track_pitch(tone(77.782), RATE)  # D#2

        root = estimate_root(frames)

        self.assertEqual(root["root"], "D#")
        self.assertGreater(root["agreement"], 0.9)
        self.assertEqual(root["voiced_fraction"], 1.0)

    def test_a_line_that_splits_its_time_reports_low_agreement(self) -> None:
        """Which is the answer: there is no single root to transpose from."""

        half = tone(77.782, seconds=SECONDS) + tone(110.0, seconds=SECONDS)  # D#2 then A2

        root = estimate_root(track_pitch(half, RATE))

        self.assertLess(root["agreement"], 0.75)
        self.assertIn(root["root"], {"D#", "A"})

    def test_the_root_does_not_claim_to_be_a_key(self) -> None:
        root = estimate_root(track_pitch(tone(77.782), RATE))

        self.assertIn("not a key", root["note"])
        self.assertIn("major", root["note"])


class ConversionTests(unittest.TestCase):
    def test_midi_numbers_match_the_standard_reference(self) -> None:
        self.assertAlmostEqual(hz_to_midi(440.0), 69.0, places=6)
        self.assertAlmostEqual(hz_to_midi(220.0), 57.0, places=6)
        self.assertAlmostEqual(hz_to_midi(77.782), 39.0, places=2)


if __name__ == "__main__":
    unittest.main()
