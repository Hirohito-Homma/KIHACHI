from __future__ import annotations

import dataclasses
import math
import random
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.composer import compose_tracks
from kihachi_music_ai.groove import UNRELIABLE_DEVIATION_MS, grid_timing
from kihachi_music_ai.midi_review import groove_report
from kihachi_music_ai.music_brain import MusicBrain

RATE = 44100
BPM = 110.0
BRIEF = "ダブとテックハウス。110 BPM、D#m。"


def write_clicks(path: Path, *, offbeat_delay_ms: float = 0.0, jitter_ms: float = 0.0,
                 bars: int = 12, seed: int = 0) -> None:
    """Sixteenth-note clicks, with the odd eighths pushed late."""

    rng = random.Random(seed)
    beat = 60.0 / BPM
    step = beat / 4
    frames = int(bars * 4 * beat * RATE)
    buffer = [0.0] * frames
    for index in range(bars * 16):
        time = index * step
        if index % 2:
            time += offbeat_delay_ms / 1000.0
        time += (rng.random() - 0.5) * 2 * jitter_ms / 1000.0
        start = int(time * RATE)
        for offset in range(int(0.02 * RATE)):
            if start + offset < frames:
                buffer[start + offset] += 0.7 * math.exp(
                    -offset / (RATE * 0.004)
                ) * math.sin(2 * math.pi * 1200 * offset / RATE)
    samples = array("h")
    for value in buffer:
        samples.extend((int(max(-1.0, min(1.0, value)) * 32767),) * 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(2)
        sink.setsampwidth(2)
        sink.setframerate(RATE)
        sink.writeframes(samples.tobytes())


class AudioGrooveTests(unittest.TestCase):
    """What the audio measurement can and cannot do, established rather than assumed."""

    def test_a_planted_delay_is_recovered_from_clean_clicks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for planted in (0.0, 7.6, 20.0, 40.0):
                path = Path(temp) / "clicks.wav"
                write_clicks(path, offbeat_delay_ms=planted)

                measured = grid_timing(path, bpm=BPM)["offbeat_delay_ms"]

                # a constant bias from the envelope window's leading edge
                self.assertAlmostEqual(measured, planted, delta=1.0, msg=f"{planted} ms")

    def test_clean_clicks_are_reported_as_reliable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clicks.wav"
            write_clicks(path, offbeat_delay_ms=7.6)

            report = grid_timing(path, bpm=BPM)

            self.assertTrue(report["reliable"])
            self.assertIsNone(report["reliability_note"])

    def test_onsets_that_do_not_track_the_grid_are_marked_unreliable(self) -> None:
        """A real mix lands ~35 ms off; the figures must not be read as timing."""

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "smeared.wav"
            write_clicks(path, offbeat_delay_ms=0.0, jitter_ms=45.0, seed=3)

            report = grid_timing(path, bpm=BPM)

            self.assertGreater(report["mean_abs_deviation_ms"], UNRELIABLE_DEVIATION_MS)
            self.assertFalse(report["reliable"])
            self.assertIn("MIDI", report["reliability_note"])

    def test_a_bad_tempo_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clicks.wav"
            write_clicks(path)

            with self.assertRaises(ValueError):
                grid_timing(path, bpm=0.0)


class MidiGrooveTests(unittest.TestCase):
    """On the written notes it is exact, which is why the Critic reads it here."""

    def _spec(self, *, swing: float, humanize: float):
        spec = MusicBrain(seed=8).analyze(BRIEF)
        return dataclasses.replace(
            spec, groove=dataclasses.replace(spec.groove, swing=swing, humanize=humanize)
        )

    def test_the_requested_swing_comes_back_out(self) -> None:
        for swing in (0.50, 0.54, 0.62, 0.75):
            spec = self._spec(swing=swing, humanize=0.0)

            report = groove_report(spec, compose_tracks(spec))

            self.assertAlmostEqual(
                report["written_offbeat_delay_ms"],
                report["expected_offbeat_delay_ms"],
                delta=0.01,
                msg=f"swing {swing}",
            )

    def test_humanize_shows_up_as_jitter_on_the_straight_positions(self) -> None:
        quiet = groove_report(
            self._spec(swing=0.5, humanize=0.0),
            compose_tracks(self._spec(swing=0.5, humanize=0.0)),
        )
        loose = groove_report(
            self._spec(swing=0.5, humanize=0.9),
            compose_tracks(self._spec(swing=0.5, humanize=0.9)),
        )

        self.assertAlmostEqual(quiet["straight_jitter_ms"], 0.0, delta=0.01)
        self.assertGreater(loose["straight_jitter_ms"], 2.0)

    def test_swing_displacement_is_not_counted_as_humanize(self) -> None:
        """Averaging the swung notes in reported 4.4 ms of jitter for a 0.9 ms setting."""

        spec = self._spec(swing=0.54, humanize=0.18)

        report = groove_report(spec, compose_tracks(spec))

        self.assertGreater(report["written_offbeat_delay_ms"], 7.0)
        self.assertLess(report["straight_jitter_ms"], 2.0)


if __name__ == "__main__":
    unittest.main()
