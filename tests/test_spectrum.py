from __future__ import annotations

import cmath
import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.spectrum import BANDS, MAX_WINDOWS, WINDOW, _fft, band_energies

RATE = 44100


def write_tone(path: Path, components, *, seconds: float = 2.0, rate: int = RATE) -> None:
    """A sum of sine components, as (frequency, amplitude) pairs."""

    samples = array("h")
    for frame in range(int(seconds * rate)):
        value = sum(
            amplitude * math.sin(2 * math.pi * frequency * frame / rate)
            for frequency, amplitude in components
        )
        sample = max(-32767, min(32767, int(value * 32767)))
        samples.extend((sample, sample))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(2)
        sink.setsampwidth(2)
        sink.setframerate(rate)
        sink.writeframes(samples.tobytes())


def loudest(report) -> str:
    return max(report["bands"], key=lambda name: report["bands"][name]["share"])


class FFTTests(unittest.TestCase):
    """The transform is written here, so it is checked here."""

    def _naive(self, values):
        count = len(values)
        return [
            sum(
                values[n] * cmath.exp(-2j * math.pi * k * n / count)
                for n in range(count)
            )
            for k in range(count)
        ]

    def test_it_agrees_with_the_definition(self) -> None:
        values = [complex(math.sin(i * 0.7) + 0.3 * math.cos(i * 2.1), 0.0) for i in range(32)]

        fast = _fft(values)
        slow = self._naive(values)

        for quick, plain in zip(fast, slow):
            self.assertAlmostEqual(quick.real, plain.real, places=8)
            self.assertAlmostEqual(quick.imag, plain.imag, places=8)

    def test_a_single_bin_holds_a_matching_sinusoid(self) -> None:
        count = 64
        values = [complex(math.sin(2 * math.pi * 8 * i / count), 0.0) for i in range(count)]

        magnitudes = [abs(item) for item in _fft(values)]

        self.assertEqual(max(range(1, count // 2), key=lambda k: magnitudes[k]), 8)

    def test_a_length_that_is_not_a_power_of_two_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _fft([complex(0.0)] * 30)


class BandTests(unittest.TestCase):
    def test_a_tone_lands_in_the_band_that_contains_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for frequency, expected in (
                (40, "sub"), (120, "bass"), (500, "low_mid"),
                (1500, "mid"), (4000, "high_mid"), (9000, "high"),
            ):
                path = Path(temp) / f"{frequency}.wav"
                write_tone(path, [(frequency, 0.5)])

                self.assertEqual(loudest(band_energies(path)), expected, f"{frequency} Hz")

    def test_shares_sum_to_one_across_the_bands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mix.wav"
            write_tone(path, [(80, 0.4), (700, 0.2), (5000, 0.1)])

            report = band_energies(path)

            total = sum(report["bands"][name]["share"] for name, _low, _high in BANDS)
            self.assertAlmostEqual(total, 1.0, places=2)

    def test_a_bass_heavy_take_reads_as_bass_heavy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            heavy = Path(temp) / "heavy.wav"
            bright = Path(temp) / "bright.wav"
            write_tone(heavy, [(90, 0.6), (8000, 0.02)])
            write_tone(bright, [(90, 0.2), (8000, 0.2)])

            self.assertGreater(
                band_energies(heavy)["low_to_high_ratio"],
                band_energies(bright)["low_to_high_ratio"],
            )

    def test_the_centroid_follows_the_energy_upwards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            low = Path(temp) / "low.wav"
            high = Path(temp) / "high.wav"
            write_tone(low, [(90, 0.4)])
            write_tone(high, [(7000, 0.4)])

            self.assertLess(
                band_energies(low)["centroid_hz"], band_energies(high)["centroid_hz"]
            )

    def test_the_cost_is_capped_however_long_the_take_is(self) -> None:
        """A five-minute render must not cost four times a seventy-second one.

        Windows are spread across the whole file rather than taken back to back,
        so the count stops rising once the take is long enough to need spreading.
        Short takes sit below the cap because the hop never goes under one window.
        """

        with tempfile.TemporaryDirectory() as temp:
            short = Path(temp) / "short.wav"
            long = Path(temp) / "long.wav"
            write_tone(short, [(200, 0.3)], seconds=2.0)
            write_tone(long, [(200, 0.3)], seconds=20.0)

            counts = (band_energies(short)["windows"], band_energies(long)["windows"])

            self.assertLessEqual(max(counts), MAX_WINDOWS)
            self.assertEqual(band_energies(long)["windows"], MAX_WINDOWS)

    def test_audio_shorter_than_a_window_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tiny.wav"
            write_tone(path, [(200, 0.3)], seconds=WINDOW / RATE / 2)

            with self.assertRaises(ValueError):
                band_energies(path)


if __name__ == "__main__":
    unittest.main()
