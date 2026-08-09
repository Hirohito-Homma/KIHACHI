"""Integrated loudness, ITU-R BS.1770-4.

The analyzer measured peak and RMS, and neither is how loud something sounds.
Peak says nothing about level -- a single sample decides it -- and RMS weights
40 Hz the same as 3 kHz, where the ear is roughly 20 dB more sensitive. So the
Critic could not answer "is this take too quiet to sit next to the others", let
alone compare against any published target.

BS.1770 is the answer everyone else uses: filter the way the ear responds
(K-weighting), take mean square over 400 ms blocks, and drop the blocks that are
too quiet to count before averaging -- so a fade-out or a silent bar cannot drag
the number down.

Not implemented: true peak. It needs oversampling to catch inter-sample peaks a
converter would clip on, and that is a mastering question this project does not
reach yet. ``peak_dbfs`` in the analysis remains sample peak, and is labelled so.

Pure and stdlib-only.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Any, Sequence

LOUDNESS_VERSION = "0.1"

BLOCK_SECONDS = 0.400
"""BS.1770 gating block length."""

BLOCK_OVERLAP = 0.75
"""75% overlap, so blocks step by 100 ms."""

ABSOLUTE_GATE_LUFS = -70.0
"""Blocks below this never count -- silence must not pull the mean down."""

RELATIVE_GATE_LU = -10.0
"""And blocks more than 10 LU under the ungated mean drop out too, which is what
stops a quiet intro from being averaged in with a chorus."""

# Per-channel weights. Stereo is 1.0/1.0; surround gives the rear channels
# +1.5 dB, which does not arise here but is the reason the sum is weighted.
CHANNEL_WEIGHTS = (1.0, 1.0)


def _k_weighting(rate: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The two BS.1770 biquads, as (b0,b1,b2,a0,a1,a2), derived for this rate.

    Stage 1 is a high shelf standing in for the head's response, stage 2 a
    high-pass. The standard publishes coefficients for 48 kHz only, so these are
    derived through the bilinear transform with pre-warping; at 48 kHz they
    reproduce the published values to within floating-point error, which is the
    test that keeps the derivation honest for the rates the standard does not
    print. A 44.1 kHz render measured with 48 kHz coefficients would have both
    corners 8% off.

    Deriving this from the usual audio-EQ cookbook does *not* work: that
    parameterisation misses the published coefficients and reads a -23 LUFS
    reference tone as -23.26, outside the +/-0.1 the standard allows.
    """

    shelf_frequency = 1681.974450955533
    shelf_gain_db = 3.999843853973347
    shelf_q = 0.7071752369554196
    k = math.tan(math.pi * shelf_frequency / rate)
    high_gain = 10.0 ** (shelf_gain_db / 20.0)
    band_gain = high_gain ** 0.4996667741545416
    denominator = 1.0 + k / shelf_q + k * k
    shelf = (
        (high_gain + band_gain * k / shelf_q + k * k) / denominator,
        2.0 * (k * k - high_gain) / denominator,
        (high_gain - band_gain * k / shelf_q + k * k) / denominator,
        1.0,
        2.0 * (k * k - 1.0) / denominator,
        (1.0 - k / shelf_q + k * k) / denominator,
    )

    highpass_frequency = 38.13547087602444
    highpass_q = 0.5003270373238773
    k = math.tan(math.pi * highpass_frequency / rate)
    denominator = 1.0 + k / highpass_q + k * k
    highpass = (
        1.0,
        -2.0,
        1.0,
        1.0,
        2.0 * (k * k - 1.0) / denominator,
        (1.0 - k / highpass_q + k * k) / denominator,
    )
    return shelf, highpass


def _biquad(samples: list[float], coefficients: Sequence[float]) -> list[float]:
    b0, b1, b2, _a0, a1, a2 = coefficients
    x1 = x2 = y1 = y2 = 0.0
    out = [0.0] * len(samples)
    for index, x0 in enumerate(samples):
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[index] = y0
        x2, x1 = x1, x0
        y2, y1 = y1, y0
    return out


def _channels(audio_path: Path) -> tuple[list[list[float]], int]:
    with wave.open(str(audio_path), "rb") as source:
        count = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.getnframes()
        if width != 2:
            raise ValueError("only 16-bit PCM WAV is supported")
        raw = source.readframes(frames)
    tracks: list[list[float]] = [[] for _ in range(count)]
    step = count * 2
    for offset in range(0, len(raw) - step + 1, step):
        for channel in range(count):
            start = offset + channel * 2
            tracks[channel].append(
                int.from_bytes(raw[start : start + 2], "little", signed=True) / 32768.0
            )
    return tracks, rate


def integrated_loudness(audio_path: Path) -> dict[str, Any]:
    """Gated integrated loudness in LUFS, plus the ungated and range figures."""

    tracks, rate = _channels(Path(audio_path))
    if not tracks or not tracks[0]:
        raise ValueError("no audio to measure")

    shelf, highpass = _k_weighting(rate)
    weighted = [_biquad(_biquad(track, shelf), highpass) for track in tracks]

    block = int(round(BLOCK_SECONDS * rate))
    hop = max(1, int(round(block * (1.0 - BLOCK_OVERLAP))))
    if len(weighted[0]) < block:
        raise ValueError("audio is shorter than one 400 ms gating block")

    powers: list[float] = []
    for start in range(0, len(weighted[0]) - block + 1, hop):
        total = 0.0
        for index, channel in enumerate(weighted):
            weight = CHANNEL_WEIGHTS[index] if index < len(CHANNEL_WEIGHTS) else 1.0
            segment = channel[start : start + block]
            total += weight * sum(value * value for value in segment) / block
        powers.append(total)

    loudnesses = [
        -0.691 + 10.0 * math.log10(power) if power > 0 else float("-inf")
        for power in powers
    ]
    above_absolute = [
        power for power, level in zip(powers, loudnesses) if level > ABSOLUTE_GATE_LUFS
    ]
    if not above_absolute:
        return {
            "loudness_version": LOUDNESS_VERSION,
            "method": "itu-r-bs.1770-4-gated",
            "integrated_lufs": None,
            "ungated_lufs": None,
            "loudness_range_lu": None,
            "gated_blocks": 0,
            "total_blocks": len(powers),
        }

    ungated = -0.691 + 10.0 * math.log10(sum(above_absolute) / len(above_absolute))
    threshold = ungated + RELATIVE_GATE_LU
    kept = [
        power
        for power, level in zip(powers, loudnesses)
        if level > ABSOLUTE_GATE_LUFS and level > threshold
    ]
    integrated = (
        -0.691 + 10.0 * math.log10(sum(kept) / len(kept)) if kept else ungated
    )

    # Loudness range: the spread of the middle of the distribution, so one loud
    # drop or one quiet intro does not describe the whole take.
    levels = sorted(
        level for level in loudnesses if level > ABSOLUTE_GATE_LUFS and level > threshold
    )
    if len(levels) >= 4:
        low = levels[int(0.10 * (len(levels) - 1))]
        high = levels[int(0.95 * (len(levels) - 1))]
        loudness_range = round(high - low, 2)
    else:
        loudness_range = None

    return {
        "loudness_version": LOUDNESS_VERSION,
        "method": "itu-r-bs.1770-4-gated",
        "integrated_lufs": round(integrated, 2),
        "ungated_lufs": round(ungated, 2),
        "loudness_range_lu": loudness_range,
        "gated_blocks": len(kept),
        "total_blocks": len(powers),
    }
