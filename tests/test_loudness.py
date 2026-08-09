from __future__ import annotations

import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.loudness import _k_weighting, integrated_loudness

# The coefficients ITU-R BS.1770-4 prints, for 48 kHz only.
PUBLISHED_SHELF = (1.53512485958697, -2.69169618940638, 1.19839281085285,
                   1.0, -1.69065929318241, 0.73248077421585)
PUBLISHED_HIGHPASS = (1.0, -2.0, 1.0,
                      1.0, -1.99004745483398, 0.99007225036621)


def write_sine(path: Path, dbfs: float, *, rate: int = 48000, freq: float = 1000.0,
               seconds: float = 4.0, channels: int = 2) -> None:
    amplitude = 10.0 ** (dbfs / 20.0)
    samples = array("h")
    for frame in range(int(seconds * rate)):
        value = int(amplitude * math.sin(2 * math.pi * freq * frame / rate) * 32767)
        samples.extend((value,) * channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(channels)
        sink.setsampwidth(2)
        sink.setframerate(rate)
        sink.writeframes(samples.tobytes())


class FilterTests(unittest.TestCase):
    def test_the_derivation_reproduces_the_published_coefficients(self) -> None:
        """The standard prints 48 kHz only; everything else is derived from this.

        Getting this wrong is not loud: an audio-EQ-cookbook shelf looks
        reasonable and reads a -23 LUFS reference tone as -23.26, which is
        outside tolerance but not obviously broken.
        """

        shelf, highpass = _k_weighting(48000.0)

        for derived, published in zip(shelf, PUBLISHED_SHELF):
            self.assertAlmostEqual(derived, published, places=12)
        for derived, published in zip(highpass, PUBLISHED_HIGHPASS):
            self.assertAlmostEqual(derived, published, places=12)

    def test_other_rates_get_their_own_coefficients(self) -> None:
        """44.1 kHz filtered with 48 kHz coefficients has both corners 8% off."""

        self.assertNotEqual(_k_weighting(44100.0), _k_weighting(48000.0))


class ReferenceToneTests(unittest.TestCase):
    """EBU Tech 3341: a stereo 1 kHz tone reads its own level, +/-0.1 LU."""

    def test_a_reference_tone_reads_its_own_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for dbfs in (-23.0, -20.0, -30.0):
                path = Path(temp) / "tone.wav"
                write_sine(path, dbfs)

                measured = integrated_loudness(path)["integrated_lufs"]

                self.assertAlmostEqual(measured, dbfs, delta=0.1, msg=f"{dbfs} dBFS")

    def test_it_holds_at_44100_as_well(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tone.wav"
            write_sine(path, -23.0, rate=44100)

            self.assertAlmostEqual(
                integrated_loudness(path)["integrated_lufs"], -23.0, delta=0.1
            )


class GatingTests(unittest.TestCase):
    def test_silence_between_tones_does_not_drag_the_level_down(self) -> None:
        """The gate is the whole reason this is not just a weighted RMS."""

        with tempfile.TemporaryDirectory() as temp:
            plain = Path(temp) / "plain.wav"
            padded = Path(temp) / "padded.wav"
            write_sine(plain, -23.0, seconds=4.0)

            # the same tone, with four seconds of silence appended
            with wave.open(str(plain), "rb") as source:
                frames = source.readframes(source.getnframes())
            with wave.open(str(padded), "wb") as sink:
                sink.setnchannels(2)
                sink.setsampwidth(2)
                sink.setframerate(48000)
                sink.writeframes(frames + b"\x00" * len(frames))

            padded_lufs = integrated_loudness(padded)["integrated_lufs"]
            plain_lufs = integrated_loudness(plain)["integrated_lufs"]

            # Not identical, and correctly so: the blocks straddling the join are
            # half tone and half silence, land near -26 LUFS, and clear the -10 LU
            # relative gate, so the standard keeps them. What matters is that the
            # four seconds of silence itself is dropped -- averaging it in would
            # cost about 3 dB.
            self.assertLess(abs(padded_lufs - plain_lufs), 0.5)
            self.assertGreater(padded_lufs, plain_lufs - 1.0)

    def test_a_take_that_is_entirely_silent_reports_nothing_rather_than_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "silent.wav"
            with wave.open(str(path), "wb") as sink:
                sink.setnchannels(2)
                sink.setsampwidth(2)
                sink.setframerate(48000)
                sink.writeframes(b"\x00" * (48000 * 2 * 2 * 2))

            report = integrated_loudness(path)

            self.assertIsNone(report["integrated_lufs"])
            self.assertEqual(report["gated_blocks"], 0)

    def test_audio_shorter_than_a_gating_block_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tiny.wav"
            write_sine(path, -23.0, seconds=0.2)

            with self.assertRaises(ValueError):
                integrated_loudness(path)


class LevelTests(unittest.TestCase):
    def test_doubling_the_amplitude_adds_six_decibels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            quiet = Path(temp) / "quiet.wav"
            loud = Path(temp) / "loud.wav"
            write_sine(quiet, -26.0)
            write_sine(loud, -20.0)

            difference = (
                integrated_loudness(loud)["integrated_lufs"]
                - integrated_loudness(quiet)["integrated_lufs"]
            )

            self.assertAlmostEqual(difference, 6.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
