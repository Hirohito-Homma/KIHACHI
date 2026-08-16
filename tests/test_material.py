from __future__ import annotations

import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.material import (
    MIN_ONSETS_FOR_ALIGNMENT,
    describe,
    detect_onsets,
    grid_agreement,
    rank_samples,
    review_sample,
)

RATE = 8000
BPM = 120.0
"""120 BPM puts a sixteenth at exactly 125 ms, so hits can be placed by hand."""


def write_hits(path: Path, times: list[float], *, seconds: float = 4.0) -> None:
    """Short decaying blips at the given seconds, over near-silence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * RATE)
    samples = [0.0] * frames
    for at in times:
        start = int(at * RATE)
        for offset in range(int(0.04 * RATE)):
            index = start + offset
            if index >= frames:
                break
            decay = math.exp(-offset / (0.008 * RATE))
            samples[index] += 0.8 * decay * math.sin(2 * math.pi * 180 * offset / RATE)
    data = array("h")
    for value in samples:
        clamped = max(-0.99, min(0.99, value))
        data.extend((int(clamped * 32767), int(clamped * 32767)))
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(2)
        sink.setsampwidth(2)
        sink.setframerate(RATE)
        sink.writeframes(data.tobytes())


def on_grid_times(count: int) -> list[float]:
    """`count` hits exactly on the eighth-note grid at 120 BPM."""

    return [index * 0.25 for index in range(count)]


class OnsetTests(unittest.TestCase):
    def test_hits_are_found_where_they_were_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hits.wav"
            times = [0.0, 0.5, 1.0, 1.5, 2.0]
            write_hits(path, times)

            from kihachi_music_ai.material import _mono

            samples, rate = _mono(path)
            found = detect_onsets(samples, rate)

            self.assertEqual(len(found), len(times))
            for detected, expected in zip(found, times):
                self.assertAlmostEqual(detected, expected, delta=0.03)

    def test_silence_has_no_onsets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "quiet.wav"
            write_hits(path, [])

            from kihachi_music_ai.material import _mono

            samples, rate = _mono(path)

            self.assertEqual(detect_onsets(samples, rate), [])


class GridTests(unittest.TestCase):
    def test_hits_on_the_grid_agree_with_it(self) -> None:
        agreement = grid_agreement(on_grid_times(16), BPM)

        self.assertEqual(agreement["on_grid_fraction"], 1.0)
        self.assertTrue(agreement["confident"])

    def test_hits_between_the_lines_do_not(self) -> None:
        """Half a sixteenth off is as far from the grid as a hit can be."""

        drifted = [index * 0.25 + 0.0625 for index in range(16)]

        agreement = grid_agreement(drifted, BPM)

        self.assertEqual(agreement["on_grid_fraction"], 0.0)
        self.assertAlmostEqual(agreement["mean_abs_deviation"], 0.5, places=3)

    def test_too_few_onsets_is_undetermined_rather_than_perfect(self) -> None:
        """A sustained bass stem read 1.000 off nine onsets. That is not alignment."""

        agreement = grid_agreement(on_grid_times(MIN_ONSETS_FOR_ALIGNMENT - 1), BPM)

        self.assertEqual(agreement["on_grid_fraction"], 1.0)
        self.assertFalse(agreement["confident"])
        self.assertIn("too few onsets", agreement["note"])


class ReviewTests(unittest.TestCase):
    def sample(self, root: Path, name: str, times: list[float]) -> Path:
        path = root / f"{name}.wav"
        write_hits(path, times)
        return path

    def test_grid_agreement_is_scored_and_the_rest_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.sample(Path(temp), "loop", on_grid_times(16))

            review = review_sample(path, bpm=BPM).review

            self.assertTrue(review["grid_agreement"]["confident"])
            self.assertIn("not scored", review["density"]["note"])
            self.assertIn("not scored", review["level"]["note"])
            self.assertIn("not scored", review["spectral"]["note"])

    def test_a_sample_cut_from_a_stem_gets_no_spectral_ratio(self) -> None:
        """The ratio is calibrated on mixes and diverges on one stem."""

        with tempfile.TemporaryDirectory() as temp:
            path = self.sample(Path(temp), "bass", on_grid_times(16))

            review = review_sample(
                path, bpm=BPM, source_audio="audio/stems/htdemucs/bass.wav"
            ).review

            self.assertFalse(review["spectral"]["measured"])
            self.assertIn("diverges", review["spectral"]["reason"])
            self.assertTrue(review["from_stem"])

    def test_a_sample_cut_from_a_mix_gets_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.sample(Path(temp), "mix", on_grid_times(16))

            review = review_sample(
                path, bpm=BPM, source_audio="audio/ace-step-01.wav"
            ).review

            self.assertTrue(review["spectral"]["measured"])
            self.assertIn("low_to_high_ratio", review["spectral"])


class RankingTests(unittest.TestCase):
    def build(self, root: Path, name: str, times: list[float], **kwargs):
        path = root / f"{name}.wav"
        write_hits(path, times)
        return review_sample(path, bpm=BPM, label=name, **kwargs)

    def test_better_alignment_ranks_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tight = self.build(root, "tight", on_grid_times(16))
            loose = self.build(
                root, "loose", [index * 0.25 + 0.0625 for index in range(16)]
            )

            ordered = rank_samples([loose, tight])

            self.assertEqual([item.review["sample"] for item in ordered], ["tight", "loose"])

    def test_an_undetermined_sample_does_not_outrank_a_measured_one(self) -> None:
        """It is not worse -- a pad has no transients -- but this cannot speak to it."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            measured = self.build(
                root, "measured", [index * 0.25 + 0.0625 for index in range(16)]
            )
            sparse = self.build(root, "sparse", on_grid_times(4))

            ordered = rank_samples([sparse, measured])

            self.assertEqual(
                [item.review["sample"] for item in ordered], ["measured", "sparse"]
            )
            self.assertIsNone(sparse.agreement)

    def test_the_output_says_what_it_did_not_judge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            review = self.build(root, "loop", on_grid_times(16))

            lines = "\n".join(describe([review]))

            self.assertIn("ranked on grid agreement only", lines)
            self.assertIn("musical interest", lines)


if __name__ == "__main__":
    unittest.main()
