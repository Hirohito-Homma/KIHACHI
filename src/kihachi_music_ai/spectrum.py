"""How a take spends its energy across the spectrum.

The Critic was written to say things like "the bass is weak" and could not: the
analyzer measured level broadband only, and its harmony path downsamples to
4 kHz, which throws away everything above 2 kHz before anything could look at
it. So the one judgement the architecture asks the Critic for -- is this mix
balanced the way the song intends -- had no measurement behind it.

This measures it, at the file's own sample rate, with a radix-2 FFT written here
because the standard library has none. Bands are the ones a mix is actually
discussed in rather than octaves: a complaint is "no low end" or "it is harsh",
not "band 7 is down 3 dB".

Pure and stdlib-only. Reads audio, returns numbers, judges nothing -- the
thresholds live with the Critic, calibrated from real renders.
"""

from __future__ import annotations

import cmath
import math
import sys
import wave
from array import array
from pathlib import Path
from typing import Any

SPECTRUM_VERSION = "0.1"

# Calibrated from 21 real renders rather than chosen. Across all of them 63% of
# the energy sits in 60-250 Hz, so "bass heavy" is this generator's normal and
# flagging it would flag everything -- these thresholds find takes that fall
# outside what the corpus does, not takes that miss an absolute mix target.
#
#            sub    bass   low_mid  mid    high_mid  high    low/high
#   median   0.168  0.627  0.104    0.066  0.023     0.016   19.8
#   range    .05-.22 .48-.84 .04-.20 .03-.10 .01-.04  .001-.021  13-64
DULL_LOW_TO_HIGH = 40.0
"""Twice the median. Catches the pre-LoRA baseline (64.4) and the chunked
render (51.2) and nothing else -- both takes with almost no top end."""

MASKING_BASS_SHARE = 0.80
"""The corpus tops out at 0.837, which is the pre-LoRA baseline alone."""

BANDS: tuple[tuple[str, float, float], ...] = (
    ("sub", 20.0, 60.0),
    ("bass", 60.0, 250.0),
    ("low_mid", 250.0, 800.0),
    ("mid", 800.0, 2500.0),
    ("high_mid", 2500.0, 6000.0),
    ("high", 6000.0, 16000.0),
)

WINDOW = 2048
"""Points per FFT. At 44.1 kHz this is 21.5 Hz per bin -- fine enough to put a
bass note in the bass band, coarse enough to stay cheap in pure Python."""

MAX_WINDOWS = 200
"""Windows sampled across the whole take, however long it is.

A five-minute render is 13 M samples; transforming all of it in Python would
take minutes to answer a question about the average balance of a mix, which does
not change quickly enough to need every window.
"""


_TWIDDLES: dict[int, list[list[complex]]] = {}


def _twiddles(count: int) -> list[list[complex]]:
    """Roots of unity per stage, computed once and reused.

    Recomputing ``exp(-2j*pi/size)`` inside the butterfly meant 200 windows of a
    single take rebuilt the same few thousand constants 200 times.
    """

    cached = _TWIDDLES.get(count)
    if cached is None:
        cached = []
        size = 2
        while size <= count:
            step = cmath.exp(-2j * math.pi / size)
            factors = [1 + 0j]
            for _ in range(size // 2 - 1):
                factors.append(factors[-1] * step)
            cached.append(factors)
            size *= 2
        _TWIDDLES[count] = cached
    return cached


def _fft(values: list[complex]) -> list[complex]:
    """Iterative radix-2 Cooley-Tukey. ``len(values)`` must be a power of two."""

    count = len(values)
    if count & (count - 1):
        raise ValueError("FFT length must be a power of two")
    # bit-reversal permutation
    output = list(values)
    bits = count.bit_length() - 1
    for index in range(count):
        mirrored = int(f"{index:0{bits}b}"[::-1], 2) if bits else 0
        if mirrored > index:
            output[index], output[mirrored] = output[mirrored], output[index]
    stages = _twiddles(count)
    size = 2
    for factors in stages:
        half = size // 2
        for start in range(0, count, size):
            for offset in range(half):
                index = start + offset
                partner = index + half
                a = output[index]
                b = output[partner] * factors[offset]
                output[index] = a + b
                output[partner] = a - b
        size *= 2
    return output


def _hann(size: int) -> list[float]:
    if size < 2:
        return [1.0] * size
    scale = 2.0 * math.pi / (size - 1)
    return [0.5 - 0.5 * math.cos(scale * index) for index in range(size)]


def band_energies(audio_path: Path) -> dict[str, Any]:
    """Energy per band, as a share of the total and as dBFS.

    Shares are what the Critic reads: absolute level is a mastering decision and
    moves with the render's output gain, while the balance between bands is what
    "the bass is weak" is actually about.
    """

    audio_path = Path(audio_path)
    with wave.open(str(audio_path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.getnframes()
        if width != 2:
            raise ValueError("only 16-bit PCM WAV is supported")
        if frames < WINDOW:
            raise ValueError("audio is shorter than one analysis window")

        hop = max(WINDOW, frames // MAX_WINDOWS)
        window = _hann(WINDOW)
        # Which band each bin belongs to, worked out once rather than by scanning
        # the band list for all 1023 bins of all 200 windows.
        band_of: list[int | None] = []
        for bin_index in range(WINDOW // 2):
            frequency = bin_index * rate / WINDOW
            band_of.append(
                next(
                    (i for i, (_n, low, high) in enumerate(BANDS) if low <= frequency < high),
                    None,
                )
            )
        names = [name for name, _low, _high in BANDS]
        totals = {name: 0.0 for name, _low, _high in BANDS}
        grand = 0.0
        counted = 0
        position = 0
        while position + WINDOW <= frames:
            source.setpos(position)
            raw = source.readframes(WINDOW)
            # array() decodes the whole window in C; int.from_bytes per sample
            # was most of the cost of measuring a spectrum.
            decoded = array("h")
            decoded.frombytes(raw[: WINDOW * channels * 2])
            if sys.byteorder != "little":
                decoded.byteswap()
            # left channel only: the balance of a mix is not a stereo question
            block = [
                complex(decoded[index * channels] / 32768.0 * window[index], 0.0)
                for index in range(WINDOW)
            ]
            spectrum = _fft(block)
            for bin_index in range(1, WINDOW // 2):
                value = spectrum[bin_index]
                power = value.real * value.real + value.imag * value.imag
                grand += power
                band = band_of[bin_index]
                if band is not None:
                    totals[names[band]] += power
            counted += 1
            position += hop

    if not counted or grand <= 0.0:
        raise ValueError("no measurable audio")

    shares = {name: totals[name] / grand for name in totals}
    return {
        "spectrum_version": SPECTRUM_VERSION,
        "method": f"hann-{WINDOW}-fft-{counted}-windows",
        "sample_rate_hz": rate,
        "windows": counted,
        "bands": {
            name: {
                "low_hz": low,
                "high_hz": high,
                "share": round(shares[name], 6),
                "dbfs": (
                    round(10.0 * math.log10(totals[name] / counted / (WINDOW / 2)), 3)
                    if totals[name] > 0
                    else None
                ),
            }
            for name, low, high in BANDS
        },
        "low_to_high_ratio": (
            round((shares["sub"] + shares["bass"]) / max(shares["high_mid"] + shares["high"], 1e-9), 3)
        ),
        "centroid_hz": round(_centroid(shares), 1),
    }


def _centroid(shares: dict[str, float]) -> float:
    """Where the energy sits, as one number, using each band's geometric centre."""

    weighted = 0.0
    for name, low, high in BANDS:
        weighted += shares[name] * math.sqrt(low * high)
    return weighted
