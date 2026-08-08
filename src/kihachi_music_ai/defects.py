"""Material defects: what is wrong with the audio, regardless of the design.

The SongSpec alignment score answers "did the render follow the plan". It cannot
answer "is this usable material", and it is a poor proxy: measured across three
seeds of one identical spec it swung 33 points (28.03 to 61.21), so a few points
of difference between two settings says nothing at all.

This module measures absolutes instead. A silent gap, a clipped peak, a mono
collapse, a splice click -- these are true or false about the file itself, do not
move with the seed, and are the things that actually stop a take from being
usable. Nothing here reads the SongSpec.

Deliberately reports **findings, not a score**. A single number invites the same
over-optimisation that made the alignment score misleading; a producer wants to
know *which* defect to fix, and severities do that without pretending the defects
are commensurable.

Pure and stdlib-only, one streaming pass over the file.
"""

from __future__ import annotations

import math
import sys
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Window used for level-based checks. 20 ms is short enough to catch a dropout
# and long enough that a single zero crossing is not mistaken for silence.
WINDOW_SECONDS = 0.02

SILENCE_DBFS = -50.0
SILENCE_WARN_SECONDS = 0.5
SILENCE_BLOCK_SECONDS = 2.0
CLIP_CEILING = 0.999
CLIP_RUN_SAMPLES = 3
# Real ACE-Step renders sit at 0.0009-0.0016 (about -57 dBFS), which is inaudible
# and harmless. A limit of 0.001 flagged every single take; 1% is where DC starts
# actually costing headroom.
DC_OFFSET_LIMIT = 0.01
# Measured across seven real renders: 14.4-18.6 dB. A heavily limited master sits
# near 8; a steady sine is only 3.01 dB by construction, so this threshold is about
# *music* being squashed, not about any signal with a low crest.
CREST_CRUSHED_DB = 8.0
CREST_SPARSE_DB = 30.0
MONO_CORRELATION = 0.99
PHASE_CORRELATION = 0.0
# An absolute jump threshold cannot tell a splice click from a legitimate kick
# transient: real renders reach 0.40-0.74 between consecutive samples with no
# audible click. A click is what stands far above the material's own typical slew,
# so both conditions must hold.
# Note this check is sample-rate dependent: one sample of a 900 Hz tone at 0.85
# steps 0.60 at 8 kHz but only 0.10 at 48 kHz, so the same music looks far more
# discontinuous when sampled slowly. These numbers are calibrated for the 48 kHz
# that renders arrive at.
DISCONTINUITY_JUMP = 0.5
DISCONTINUITY_RATIO = 8.0

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"


@dataclass(frozen=True)
class DefectFinding:
    code: str
    severity: str
    detail: str
    value: float
    threshold: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def scan_material(audio_path: Path) -> dict[str, Any]:
    """Measure a rendered WAV for defects that make it hard to use.

    Returns the measurements alongside the findings so a caller can see what a
    check was based on, not just its verdict.
    """

    measured = _measure(Path(audio_path))
    findings: list[DefectFinding] = []
    findings.extend(_silence_findings(measured))
    findings.extend(_clipping_findings(measured))
    findings.extend(_dc_findings(measured))
    findings.extend(_crest_findings(measured))
    findings.extend(_stereo_findings(measured))
    findings.extend(_discontinuity_findings(measured))

    return {
        "defect_scan_version": "0.1",
        "scope": "absolute_audio_defects_not_song_spec_conformance",
        "audio_file": str(Path(audio_path).name),
        "measurements": measured,
        "findings": [finding.to_dict() for finding in findings],
        "blocking": sum(1 for item in findings if item.severity == BLOCKING),
        "warnings": sum(1 for item in findings if item.severity == WARNING),
        "clean": not any(item.severity in (BLOCKING, WARNING) for item in findings),
    }


def _measure(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_rate = source.getframerate()
        sample_width = source.getsampwidth()
        frame_count = source.getnframes()
        if source.getcomptype() != "NONE":
            raise ValueError("compressed WAV is not supported")
        if channels <= 0 or sample_rate <= 0 or frame_count <= 0:
            raise ValueError("WAV must contain non-empty PCM audio")
        if sample_width not in {1, 2, 3, 4}:
            raise ValueError(f"unsupported PCM sample width: {sample_width} bytes")

        window_frames = max(1, int(round(sample_rate * WINDOW_SECONDS)))
        peak = 0.0
        square_sum = 0.0
        signed_sum = [0.0] * channels
        clip_runs = 0
        clip_run_length = 0
        max_jump = 0.0
        max_jump_time = 0.0
        jump_sum = 0.0
        jump_count = 0
        previous = None
        left_right = [0.0, 0.0, 0.0]  # sum(L*R), sum(L^2), sum(R^2)
        window_square = 0.0
        window_used = 0
        silent_run = 0
        longest_silent_run = 0
        longest_silent_at = 0
        window_index = 0
        sample_count = 0

        while data := source.readframes(8192):
            samples = _decode(data, sample_width)
            for offset in range(0, len(samples) - channels + 1, channels):
                frame = samples[offset : offset + channels]
                mono = sum(frame) / channels
                magnitude = max(abs(value) for value in frame)

                if magnitude > peak:
                    peak = magnitude
                square_sum += mono * mono
                for index, value in enumerate(frame):
                    signed_sum[index] += value
                sample_count += 1

                if magnitude >= CLIP_CEILING:
                    clip_run_length += 1
                    if clip_run_length == CLIP_RUN_SAMPLES:
                        clip_runs += 1
                else:
                    clip_run_length = 0

                if previous is not None:
                    jump = abs(mono - previous)
                    jump_sum += jump
                    jump_count += 1
                    if jump > max_jump:
                        max_jump = jump
                        max_jump_time = sample_count / sample_rate
                previous = mono

                if channels >= 2:
                    left, right = frame[0], frame[1]
                    left_right[0] += left * right
                    left_right[1] += left * left
                    left_right[2] += right * right

                window_square += mono * mono
                window_used += 1
                if window_used == window_frames:
                    rms = math.sqrt(window_square / window_used)
                    if _dbfs(rms) < SILENCE_DBFS:
                        silent_run += 1
                        if silent_run > longest_silent_run:
                            longest_silent_run = silent_run
                            longest_silent_at = (window_index + 1 - silent_run) * WINDOW_SECONDS
                    else:
                        silent_run = 0
                    window_square = 0.0
                    window_used = 0
                    window_index += 1

    duration = frame_count / sample_rate
    rms = math.sqrt(square_sum / sample_count) if sample_count else 0.0
    correlation = None
    if channels >= 2 and left_right[1] > 0 and left_right[2] > 0:
        correlation = left_right[0] / math.sqrt(left_right[1] * left_right[2])
    return {
        "duration_sec": round(duration, 4),
        "sample_rate": sample_rate,
        "channels": channels,
        "peak_dbfs": round(_dbfs(peak), 3),
        "rms_dbfs": round(_dbfs(rms), 3),
        "crest_db": round(_dbfs(peak) - _dbfs(rms), 3),
        "clipped_runs": clip_runs,
        "dc_offset": [round(value / sample_count, 6) for value in signed_sum]
        if sample_count
        else [],
        "stereo_correlation": round(correlation, 4) if correlation is not None else None,
        "longest_silence_sec": round(longest_silent_run * WINDOW_SECONDS, 3),
        "longest_silence_at_sec": round(longest_silent_at, 3),
        "max_sample_jump": round(max_jump, 4),
        "max_sample_jump_at_sec": round(max_jump_time, 3),
        "mean_sample_jump": round(jump_sum / jump_count, 6) if jump_count else 0.0,
    }


def _silence_findings(measured: dict[str, Any]) -> list[DefectFinding]:
    length = float(measured["longest_silence_sec"])
    if length < SILENCE_WARN_SECONDS:
        return []
    severity = BLOCKING if length >= SILENCE_BLOCK_SECONDS else WARNING
    return [
        DefectFinding(
            code="silent_gap",
            severity=severity,
            detail=(
                f"{length:.2f} s below {SILENCE_DBFS:g} dBFS starting at "
                f"{measured['longest_silence_at_sec']:.2f} s"
            ),
            value=length,
            threshold=SILENCE_WARN_SECONDS,
        )
    ]


def _clipping_findings(measured: dict[str, Any]) -> list[DefectFinding]:
    runs = int(measured["clipped_runs"])
    if not runs:
        return []
    return [
        DefectFinding(
            code="clipping",
            severity=BLOCKING if runs > 20 else WARNING,
            detail=f"{runs} run(s) of {CLIP_RUN_SAMPLES}+ samples at full scale",
            value=float(runs),
            threshold=0.0,
        )
    ]


def _dc_findings(measured: dict[str, Any]) -> list[DefectFinding]:
    worst = max((abs(value) for value in measured["dc_offset"]), default=0.0)
    if worst <= DC_OFFSET_LIMIT:
        return []
    return [
        DefectFinding(
            code="dc_offset",
            severity=WARNING,
            detail=f"mean sample value {worst:.5f}; wastes headroom and can click on splice",
            value=worst,
            threshold=DC_OFFSET_LIMIT,
        )
    ]


def _crest_findings(measured: dict[str, Any]) -> list[DefectFinding]:
    crest = float(measured["crest_db"])
    if crest < CREST_CRUSHED_DB:
        return [
            DefectFinding(
                code="crushed_dynamics",
                severity=WARNING,
                detail=f"crest {crest:.1f} dB; transients are squashed",
                value=crest,
                threshold=CREST_CRUSHED_DB,
            )
        ]
    if crest > CREST_SPARSE_DB:
        return [
            DefectFinding(
                code="sparse_dynamics",
                severity=INFO,
                detail=f"crest {crest:.1f} dB; mostly quiet with isolated peaks",
                value=crest,
                threshold=CREST_SPARSE_DB,
            )
        ]
    return []


def _stereo_findings(measured: dict[str, Any]) -> list[DefectFinding]:
    correlation = measured["stereo_correlation"]
    if correlation is None:
        return []
    if correlation > MONO_CORRELATION:
        return [
            DefectFinding(
                code="mono_collapse",
                severity=INFO,
                detail=f"L/R correlation {correlation:.4f}; effectively mono",
                value=float(correlation),
                threshold=MONO_CORRELATION,
            )
        ]
    if correlation < PHASE_CORRELATION:
        return [
            DefectFinding(
                code="phase_cancellation",
                severity=WARNING,
                detail=(
                    f"L/R correlation {correlation:.4f}; the channels partly cancel "
                    "and the mix will lose level in mono"
                ),
                value=float(correlation),
                threshold=PHASE_CORRELATION,
            )
        ]
    return []


def _discontinuity_findings(measured: dict[str, Any]) -> list[DefectFinding]:
    jump = float(measured["max_sample_jump"])
    mean_jump = float(measured["mean_sample_jump"])
    if jump <= DISCONTINUITY_JUMP:
        return []
    # Absolute size alone is not evidence: percussive material reaches this
    # routinely. A click is a step the surrounding signal never takes.
    ratio = jump / mean_jump if mean_jump > 0 else float("inf")
    if ratio <= DISCONTINUITY_RATIO:
        return []
    return [
        DefectFinding(
            code="discontinuity",
            severity=WARNING,
            detail=(
                f"sample-to-sample jump {jump:.3f} at "
                f"{measured['max_sample_jump_at_sec']:.2f} s, {ratio:.1f}x the "
                f"material's mean slew; likely an audible click"
            ),
            value=jump,
            threshold=DISCONTINUITY_JUMP,
        )
    ]


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0 else -120.0


def _decode(data: bytes, sample_width: int) -> list[float]:
    if sample_width == 1:
        return [(value - 128) / 128.0 for value in data]
    if sample_width == 2:
        values = array("h")
        values.frombytes(data)
        if sys.byteorder != "little":
            values.byteswap()
        return [value / 32768.0 for value in values]
    if sample_width == 3:
        return [
            int.from_bytes(data[index : index + 3], "little", signed=True) / 8388608.0
            for index in range(0, len(data) - 2, 3)
        ]
    values = array("i")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    return [value / 2147483648.0 for value in values]
