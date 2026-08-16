"""Track the fundamental of a monophonic line, and read a root off it.

ADR-0010 turns renders into material, and then measured that the key of a cut
sample cannot be established: ten estimates, from the mix and from the separated
bass alike, all under the analyzer's 0.25 confidence threshold, on one design
that asked for D# minor throughout. So "transpose the sample into key" was
withdrawn and pitched material left out of scope.

The reason is not that the audio is unreadable. It is that the estimator there
is built for triads -- weighted chroma over a full mix -- and a bass stem is one
note at a time. A monophonic line wants a monophonic method.

This is the cumulative-mean-normalised difference function from YIN (de
Cheveigné & Kawahara, 2002), which is autocorrelation with the two failures that
matter here already handled: the octave error that plain autocorrelation makes
on a strong second harmonic, and the amplitude dependence that makes a threshold
untunable across takes.

It reports per-frame f0 with a confidence, and never collapses that to a single
answer without saying how much of the sample agreed. A bass line moves; a root
that 30% of frames support is not a root, and the caller has to be able to see
that rather than being handed a note name.

Pure and stdlib-only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

ANALYSIS_RATE_HZ = 4000.0
"""Bass fundamentals live under 250 Hz, so 4 kHz is eight times what is needed.

The same rate the chroma path downsamples to, by box average, for the same
reason: the arithmetic below is O(window x lag) per frame in Python.
"""

MIN_HZ = 30.0
MAX_HZ = 400.0
"""Low B on a five-string is 30.87 Hz; 400 Hz covers a bass playing high."""

WINDOW_SEC = 0.256
HOP_SEC = 0.128

YIN_THRESHOLD = 0.15
"""Below this the dip is accepted as the period, per the paper's own default.

A frame that never gets there is not silently given its global minimum: it is
reported unvoiced, because on a drum hit or a gap the global minimum is noise
and would otherwise arrive as a confident wrong note.
"""

PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


@dataclass(frozen=True)
class PitchFrame:
    at_sec: float
    hz: float | None
    confidence: float
    midi: float | None

    @property
    def voiced(self) -> bool:
        return self.hz is not None


def downsample(samples: Sequence[float], sample_rate: float) -> tuple[list[float], float]:
    """Box-average down to about 4 kHz, matching the chroma path."""

    factor = max(1, round(sample_rate / ANALYSIS_RATE_HZ))
    if factor == 1:
        return list(samples), sample_rate
    reduced: list[float] = []
    total = 0.0
    count = 0
    for value in samples:
        total += value
        count += 1
        if count == factor:
            reduced.append(total / factor)
            total = 0.0
            count = 0
    if count:
        reduced.append(total / count)
    return reduced, sample_rate / factor


def hz_to_midi(hz: float) -> float:
    return 69.0 + 12.0 * math.log2(hz / 440.0)


def _difference(window: Sequence[float], max_lag: int) -> list[float]:
    """YIN's step 2: squared difference against the signal shifted by each lag."""

    size = len(window) - max_lag
    result = [0.0] * (max_lag + 1)
    for lag in range(1, max_lag + 1):
        total = 0.0
        for index in range(size):
            delta = window[index] - window[index + lag]
            total += delta * delta
        result[lag] = total
    return result


def _cumulative_mean_normalised(difference: Sequence[float]) -> list[float]:
    """YIN's step 3. This is what removes the octave error and the amplitude term."""

    result = [1.0] * len(difference)
    running = 0.0
    for lag in range(1, len(difference)):
        running += difference[lag]
        result[lag] = difference[lag] * lag / running if running > 0 else 1.0
    return result


def _parabolic(values: Sequence[float], lag: int) -> float:
    """Refine the dip between samples; a whole-lag period is up to a semitone off."""

    if lag <= 0 or lag >= len(values) - 1:
        return float(lag)
    left, centre, right = values[lag - 1], values[lag], values[lag + 1]
    denominator = 2.0 * (2.0 * centre - left - right)
    if denominator == 0.0:
        return float(lag)
    return lag + (right - left) / denominator


def track_pitch(
    samples: Sequence[float],
    sample_rate: float,
    *,
    threshold: float = YIN_THRESHOLD,
) -> list[PitchFrame]:
    """Per-frame fundamental of a monophonic signal."""

    audio, rate = downsample(samples, sample_rate)
    window_size = int(WINDOW_SEC * rate)
    hop = max(1, int(HOP_SEC * rate))
    min_lag = max(2, int(rate / MAX_HZ))
    max_lag = min(int(rate / MIN_HZ), window_size // 2)
    if max_lag <= min_lag or len(audio) < window_size:
        return []

    frames: list[PitchFrame] = []
    for start in range(0, len(audio) - window_size + 1, hop):
        window = audio[start : start + window_size]
        difference = _difference(window, max_lag)
        normalised = _cumulative_mean_normalised(difference)

        chosen = None
        for lag in range(min_lag, max_lag + 1):
            if normalised[lag] < threshold:
                # Walk down to the bottom of this dip rather than taking its
                # first edge, which reads a few cents sharp.
                while lag + 1 <= max_lag and normalised[lag + 1] < normalised[lag]:
                    lag += 1
                chosen = lag
                break

        at_sec = start / rate
        if chosen is None:
            frames.append(PitchFrame(at_sec=at_sec, hz=None, confidence=0.0, midi=None))
            continue
        period = _parabolic(normalised, chosen)
        hz = rate / period if period > 0 else 0.0
        if not MIN_HZ <= hz <= MAX_HZ:
            frames.append(PitchFrame(at_sec=at_sec, hz=None, confidence=0.0, midi=None))
            continue
        frames.append(
            PitchFrame(
                at_sec=round(at_sec, 4),
                hz=round(hz, 3),
                confidence=round(max(0.0, 1.0 - normalised[chosen]), 4),
                midi=round(hz_to_midi(hz), 3),
            )
        )
    return frames


def estimate_root(frames: Sequence[PitchFrame]) -> dict[str, object]:
    """The pitch class the voiced frames agree on, and how much they agree.

    Agreement is reported rather than folded into the answer. A bass line that
    walks spends real time off the root, and a sample whose top pitch class
    holds 30% of the voiced time has not told you its key -- it has told you it
    does not have one you can transpose from.
    """

    voiced = [frame for frame in frames if frame.voiced]
    if not voiced:
        return {
            "root": None,
            "voiced_fraction": 0.0,
            "agreement": 0.0,
            "note": "no voiced frame; nothing monophonic to read",
        }

    weights = [0.0] * 12
    for frame in voiced:
        assert frame.midi is not None
        weights[int(round(frame.midi)) % 12] += frame.confidence
    total = sum(weights)
    if total <= 0:
        return {
            "root": None,
            "voiced_fraction": round(len(voiced) / len(frames), 4),
            "agreement": 0.0,
            "note": "voiced frames carried no confidence",
        }
    best = max(range(12), key=lambda index: weights[index])
    return {
        "root": PITCH_CLASSES[best],
        "root_pitch_class": best,
        "agreement": round(weights[best] / total, 4),
        "voiced_fraction": round(len(voiced) / len(frames), 4),
        "weights": {
            PITCH_CLASSES[index]: round(value / total, 4)
            for index, value in enumerate(weights)
        },
        "note": (
            "root of a monophonic line, not a key: it says nothing about major "
            "or minor, and agreement below about 0.5 means the line did not sit "
            "anywhere long enough to transpose from"
        ),
    }
