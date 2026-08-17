from __future__ import annotations

import dataclasses
import math
import random
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.composer import compose_tracks, swing_for_offbeat
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
        """The triplet is in this list because stopping short of it hid a bug.

        This ran to 0.75 and passed while the report was blind at 0.9762 -- the
        value `derive` gives every 12/8 row. The old matcher measured from the
        nearest eighth and dropped anything beyond a quarter of one, a window of
        0.125 beats; a triplet shuffle displaces the offbeat by 0.1667, so the
        swung notes all fell out and `written_offbeat_delay_ms` was `None`.
        """

        for swing in (0.50, 0.54, 0.62, 0.75, swing_for_offbeat(2 / 3)):
            spec = self._spec(swing=swing, humanize=0.0)

            report = groove_report(spec, compose_tracks(spec))

            self.assertIsNotNone(
                report["written_offbeat_delay_ms"], msg=f"swing {swing}: nothing measured"
            )
            self.assertAlmostEqual(
                report["written_offbeat_delay_ms"],
                report["expected_offbeat_delay_ms"],
                delta=0.01,
                msg=f"swing {swing}",
            )

    def test_no_note_falls_out_of_the_groove_measurement(self) -> None:
        """A dropped note is worse than a wrong number: it is a silent one.

        The old matcher skipped whatever sat too far from an eighth, so a groove
        it could not classify simply shrank the sample it averaged. Every note
        belongs to some slot now, and a note that drifts has to move the figure
        rather than leave it.
        """

        for swing in (0.5, 0.54, swing_for_offbeat(2 / 3)):
            for humanize in (0.0, 0.9):
                spec = self._spec(swing=swing, humanize=humanize)
                tracks = compose_tracks(spec)

                report = groove_report(spec, tracks)

                self.assertEqual(
                    report["swung_notes"] + report["straight_notes"],
                    sum(len(notes) for notes in tracks.values()),
                    msg=f"swing {swing}, humanize {humanize}",
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

    def test_a_triplet_shuffle_leaves_nothing_between_the_triplets(self) -> None:
        """The 8th-note tests above pass while the 16ths are still straight.

        `_groove` decides what to swing with `int(round(start * 2))`, which only
        names the 8th grid: 0.25 rounds to subdivision 0 and 0.75 to subdivision
        2, so both are read as beats and neither is pushed. The parts that write
        16ths then play a swung 8th at 0.667 *and* a straight 16th at 0.75, one
        twelfth of a beat apart -- a flam, not a shuffle. Every earlier test asks
        whether the 8ths moved, and they do, so this passed unheard until the
        chords were soloed in Live.

        The grid asked for is the **sextuplet**, not the triplet. A shuffled beat
        divides in three, so a 16th has no slot of its own in it; six is the
        coarsest division that holds both the swung 8th (4/6) and a 16th either
        side of it. Asking for thirds here would fail a correct fix as surely as
        the broken one.

        So this says nothing about *where* a 16th should land -- that is the open
        design question -- only that the written grid has to be one grid. 0.25
        and 0.75 are on no division of a shuffled beat at all.
        """

        spec = self._spec(swing=swing_for_offbeat(2 / 3), humanize=0.0)

        stray: dict[str, set[float]] = {}
        for part, notes in compose_tracks(spec).items():
            for note in notes:
                position = note.start_beats % 1.0
                if min(abs(position - sixth / 6) for sixth in range(7)) > 1e-4:
                    stray.setdefault(part, set()).add(round(position, 4))

        self.assertEqual(
            {part: sorted(positions) for part, positions in stray.items()},
            {},
            msg="onsets between the triplets, by part",
        )


if __name__ == "__main__":
    unittest.main()
