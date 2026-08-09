"""Where the notes actually land against the bar grid.

The SongSpec asks for a feel -- ``swing``, ``humanize`` -- and nothing checked
whether the render delivered it. The existing envelope cannot: it is sampled
every 20 ms, and the displacements involved are smaller than that. At 110 BPM the
composer's swing of 0.54 moves an offbeat by 7.6 ms and humanize jitters by
±1.7 ms, so measuring this on the tempo envelope would be reading noise.

So onsets are picked from a 1 ms envelope instead, and the result carries the
resolution it was measured at. Whether that is enough to hear the difference
between two swing settings is a question about the material, not about the
arithmetic, and ``tests/test_groove.py`` answers it for synthetic material of
known timing rather than assuming.

Pure and stdlib-only.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Any, Sequence

GROOVE_VERSION = "0.1"

HOP_SECONDS = 0.001
"""Envelope hop. 1 ms, because the quantities being measured are single-digit
milliseconds -- the 20 ms hop the tempo estimate uses cannot see them at all."""

WINDOW_SECONDS = 0.004
WINDOW_MULTIPLE = 1.6
"""An onset has to be this much louder than the recent past to count."""

MIN_ONSET_GAP_SECONDS = 0.040
"""Two peaks closer than a 32nd note at 180 BPM are one event."""

UNRELIABLE_DEVIATION_MS = 20.0
"""Above this, the onsets are not tracking the grid and the timing figures mean
nothing.

Measured rather than guessed: on isolated clicks this recovers a planted 7.6 ms
delay to within 0.3 ms, but every real render in the corpus comes back with a
mean absolute deviation near 35 ms -- a quarter of a sixteenth at 110 BPM -- and
an offbeat figure scattered between -9 and +4 ms where the SongSpec asked for
+7.6. Dub delay tails and overlapping parts mean a detected onset is often not a
note starting. Groove is verified on the MIDI instead, where it is exact.
"""


def _envelope(audio_path: Path) -> tuple[list[float], float]:
    with wave.open(str(audio_path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.getnframes()
        if width != 2:
            raise ValueError("only 16-bit PCM WAV is supported")
        raw = source.readframes(frames)

    hop = max(1, int(round(HOP_SECONDS * rate)))
    window = max(hop, int(round(WINDOW_SECONDS * rate)))
    step = channels * 2
    values: list[float] = []
    for start in range(0, frames - window + 1, hop):
        total = 0.0
        for frame in range(start, start + window, 2):  # every other frame is plenty
            offset = frame * step
            sample = int.from_bytes(raw[offset : offset + 2], "little", signed=True)
            total += sample * sample
        values.append(math.sqrt(total / (window / 2)) / 32768.0)
    return values, hop / rate


def _onsets(envelope: Sequence[float], hop_seconds: float) -> list[float]:
    """Times, in seconds, where the level rises sharply out of its recent past."""

    if len(envelope) < 8:
        return []
    lookback = max(1, int(round(0.030 / hop_seconds)))
    gap = max(1, int(round(MIN_ONSET_GAP_SECONDS / hop_seconds)))
    floor = max(envelope) * 0.05
    onsets: list[float] = []
    last = -gap
    for index in range(lookback, len(envelope) - 1):
        value = envelope[index]
        if value < floor or index - last < gap:
            continue
        past = sum(envelope[index - lookback : index]) / lookback
        if value > past * WINDOW_MULTIPLE and value >= envelope[index + 1]:
            onsets.append(index * hop_seconds)
            last = index
    return onsets


def grid_timing(
    audio_path: Path,
    *,
    bpm: float,
    beats_per_bar: float = 4.0,
    subdivision: int = 4,
) -> dict[str, Any]:
    """How far detected onsets sit from the nearest grid position.

    ``subdivision`` is per beat: 4 gives a sixteenth-note grid. Onsets are
    matched to the nearest grid line, so the figures describe the material's own
    relationship to the tempo, not an absolute time reference.
    """

    if bpm <= 0:
        raise ValueError("bpm must be positive")
    envelope, hop_seconds = _envelope(Path(audio_path))
    onsets = _onsets(envelope, hop_seconds)
    beat_seconds = 60.0 / bpm
    step_seconds = beat_seconds / subdivision

    deviations: list[float] = []
    offbeat: list[float] = []
    for time in onsets:
        position = time / step_seconds
        nearest = round(position)
        deviation = (position - nearest) * step_seconds
        deviations.append(deviation)
        # Odd eighths are where swing lives: the composer delays them and leaves
        # the downbeats alone.
        if subdivision % 2 == 0 and nearest % (subdivision // 2) == subdivision // 4:
            offbeat.append(deviation)

    if not deviations:
        return {
            "groove_version": GROOVE_VERSION,
            "method": f"{int(HOP_SECONDS * 1000)}ms-envelope-peak-onsets",
            "resolution_ms": HOP_SECONDS * 1000.0,
            "onsets": 0,
            "mean_abs_deviation_ms": None,
            "offbeat_delay_ms": None,
            "swing_ratio": None,
            "reliable": False,
            "reliability_note": "no onsets detected",
        }

    mean_abs = sum(abs(value) for value in deviations) / len(deviations)
    reliable = mean_abs * 1000.0 <= UNRELIABLE_DEVIATION_MS
    offbeat_delay = sum(offbeat) / len(offbeat) if offbeat else None
    # A swing ratio of 0.5 is straight; 0.667 is triplet swing. Derived from how
    # late the offbeats are relative to the eighth they belong to.
    eighth = beat_seconds / 2.0
    swing = 0.5 + (offbeat_delay / eighth) if offbeat_delay is not None else None
    return {
        "groove_version": GROOVE_VERSION,
        "method": f"{int(HOP_SECONDS * 1000)}ms-envelope-peak-onsets",
        "resolution_ms": HOP_SECONDS * 1000.0,
        "onsets": len(onsets),
        "mean_abs_deviation_ms": round(mean_abs * 1000.0, 2),
        "offbeat_delay_ms": round(offbeat_delay * 1000.0, 2) if offbeat_delay is not None else None,
        "offbeat_onsets": len(offbeat),
        "swing_ratio": round(swing, 4) if swing is not None else None,
        "reliable": reliable,
        "reliability_note": (
            None
            if reliable
            else (
                "onsets are not tracking the grid (mean deviation over "
                f"{UNRELIABLE_DEVIATION_MS:g} ms); read the MIDI groove instead"
            )
        ),
    }
