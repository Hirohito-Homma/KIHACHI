"""Measure a cut sample as material, which is not what `shortlist` measures.

`shortlist` ranks takes on duration, tempo, section boundaries and the energy
curve. Every one of those is an arrangement measure, and inside four bars every
one of them is dead: the duration is whatever the cut asked for, the tempo and
the bar grid come from the SongSpec, and a section boundary does not exist.
Ranking material on them would be the `key`-at-0.350 failure again -- weight
spent on numbers that cannot move.

So this measures candidates chosen for material, and the measurements came
first. Across eight samples cut from real renders:

    on_grid_fraction   0.073 - 0.895      <- the one that decides
    onsets_per_bar     1.25  - 12.25
    low_to_high        8.5   - 148.9
    rms_dbfs          -23.2  - -16.5

**Grid agreement is the scored one, and it is not a matter of taste.** A sample
cut on the bar grid whose transients do not land on that grid is a sample whose
content disagrees with the metadata it carries. That is wrong in a way "sparse
or busy" is not.

It is also the one measurement that can lie by having too little to say. A
sustained bass stem gave `on_grid_fraction` 1.000 off nine onsets in four bars.
Below `MIN_ONSETS_FOR_ALIGNMENT` the answer is `undetermined`, not perfect.

Everything else is reported and not scored, for the reason stem balance is
(ADR-0010, PR #31): density and brightness have no defensible direction. A
sparse loop is not worse than a busy one.

Pure and stdlib-only.
"""

from __future__ import annotations

import math
import statistics as st
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .defects import scan_material
from .spectrum import band_energies

MATERIAL_REVIEW_VERSION = "0.1"

ENVELOPE_HOP_SEC = 0.010
ENVELOPE_WINDOW_SEC = 0.020

ONSET_FLUX_FRACTION = 0.25
"""A rise counts as an onset at a quarter of the loudest rise in the sample.

Relative rather than absolute so a quiet sample is not read as having no
transients; the question is where this sample's own hits are.
"""

MIN_ONSET_GAP_SEC = 0.05
"""Two detections closer than this are one hit seen twice."""

GRID_DIVISION = 4
"""Sixteenth notes. Finer than this and ordinary swing reads as misalignment."""

ON_GRID_TOLERANCE = 0.25
"""A hit within a quarter of a sixteenth of the line counts as on it."""

MIN_ONSETS_FOR_ALIGNMENT = 12
"""Three per bar over four bars. Below this the fraction is too easily 1.000."""

STEM_MARKER = "/stems/"
"""Spectral ratios are calibrated on mixes and diverge on a single stem.

Measured here: bass stems returned low/high of 720 to 612,993, against 8.5 to
149 for the mixes they came from. The README says not to apply the thresholds to
a stem; this refuses to report the number at all rather than trusting that.
"""


@dataclass(frozen=True)
class SampleReview:
    path: Path
    review: dict[str, Any]

    @property
    def usable(self) -> bool:
        return not self.review["defects"]["blocking"]

    @property
    def agreement(self) -> float | None:
        grid = self.review["grid_agreement"]
        return grid["on_grid_fraction"] if grid["confident"] else None


def _mono(path: Path) -> tuple[list[float], float]:
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        rate = float(source.getframerate())
        if source.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit audio: {path}")
        raw = source.readframes(source.getnframes())
    data = array("h")
    data.frombytes(raw)
    if channels == 1:
        return [value / 32768.0 for value in data], rate
    return [
        sum(data[index : index + channels]) / channels / 32768.0
        for index in range(0, len(data) - channels + 1, channels)
    ], rate


def _envelope(samples: Sequence[float], rate: float) -> tuple[list[float], float]:
    hop = max(1, int(ENVELOPE_HOP_SEC * rate))
    window = max(hop, int(ENVELOPE_WINDOW_SEC * rate))
    values = [
        math.sqrt(sum(value * value for value in samples[start : start + window]) / window)
        for start in range(0, len(samples) - window + 1, hop)
    ]
    return values, rate / hop


def detect_onsets(samples: Sequence[float], rate: float) -> list[float]:
    """Rising-energy peaks, in seconds from the start of the sample."""

    values, envelope_rate = _envelope(samples, rate)
    if len(values) < 3:
        return []
    # Rise from silence, so the frame at time zero has something to rise from.
    # Without this the downbeat of a cut loop is missed every time -- and the
    # downbeat is the hit that decides whether the sample sits on the grid.
    previous = [0.0, *values[:-1]]
    flux = [max(0.0, now - before) for now, before in zip(values, previous)]
    loudest = max(flux) if flux else 0.0
    if loudest <= 0.0:
        return []
    threshold = loudest * ONSET_FLUX_FRACTION
    found: list[float] = []
    for index, rise in enumerate(flux):
        if rise < threshold:
            continue
        if index > 0 and rise < flux[index - 1]:
            continue
        if index + 1 < len(flux) and rise < flux[index + 1]:
            continue
        at = index / envelope_rate
        if found and at - found[-1] < MIN_ONSET_GAP_SEC:
            continue
        found.append(at)
    return found


def grid_agreement(onsets: Sequence[float], bpm: float) -> dict[str, Any]:
    """How much of this sample's content sits on the grid it claims to be on."""

    step = 60.0 / bpm / GRID_DIVISION
    deviations = [abs(at - round(at / step) * step) / step for at in onsets]
    confident = len(onsets) >= MIN_ONSETS_FOR_ALIGNMENT
    return {
        "onsets": len(onsets),
        "grid": f"1/{GRID_DIVISION * 4}",
        "on_grid_fraction": (
            round(sum(1 for value in deviations if value <= ON_GRID_TOLERANCE) / len(deviations), 4)
            if deviations
            else 0.0
        ),
        "mean_abs_deviation": round(st.mean(deviations), 4) if deviations else None,
        "confident": confident,
        "minimum_onsets": MIN_ONSETS_FOR_ALIGNMENT,
        "note": (
            "transients against the sample's own bar grid; scored, because a cut "
            "whose content disagrees with its metadata is wrong rather than "
            "merely different"
            if confident
            else "too few onsets to judge alignment: a sustained sample reads 1.000 "
            "off a handful of hits, which is not the same as being on the grid"
        ),
    }


STEM_CALIBRATED_CODES = frozenset({"mono_collapse", "narrow_stereo", "dull_high_end"})
"""Findings whose thresholds were calibrated on a mix, not on one stem.

A bass stem is very nearly mono because bass is; the scan calls that
`mono_collapse` and it is not a defect of the material. Reported with the reason
rather than dropped, because the measurement is still true -- it is the
interpretation that does not carry over.
"""


def review_sample(
    path: Path,
    *,
    bpm: float,
    source_audio: str | None = None,
    label: str | None = None,
) -> SampleReview:
    """Measure one sample. Reads only."""

    path = Path(path)
    samples, rate = _mono(path)
    scan = scan_material(path)
    measured = scan["measurements"]
    onsets = detect_onsets(samples, rate)
    duration = float(measured["duration_sec"]) or 1.0
    bars = duration / (60.0 / bpm * 4.0)

    from_stem = bool(source_audio and STEM_MARKER in source_audio.replace("\\", "/"))
    spectral: dict[str, Any]
    if from_stem:
        spectral = {
            "measured": False,
            "reason": (
                "cut from a stem; the low/high ratio is calibrated on mixes and "
                "diverges on one -- bass stems measured 720 to 612,993 here"
            ),
        }
    else:
        bands = band_energies(path)
        spectral = {
            "measured": True,
            "low_to_high_ratio": bands["low_to_high_ratio"],
            "centroid_hz": bands["centroid_hz"],
            "note": "reported, not scored: bright is not better than dark",
        }

    return SampleReview(
        path=path,
        review={
            "material_review_version": MATERIAL_REVIEW_VERSION,
            "scope": "one_cut_sample_as_material_not_as_a_song",
            "sample": label or path.name,
            "from_stem": from_stem,
            "bpm": bpm,
            "bars": round(bars, 3),
            "grid_agreement": grid_agreement(onsets, bpm),
            "density": {
                "onsets_per_bar": round(len(onsets) / bars, 3) if bars else 0.0,
                "note": "reported, not scored: sparse is not worse than busy",
            },
            "level": {
                "rms_dbfs": measured["rms_dbfs"],
                "crest_db": measured["crest_db"],
                "note": "reported, not scored: level is set in the mix, not chosen here",
            },
            "spectral": spectral,
            "defects": {
                "blocking": scan["blocking"],
                "warnings": scan["warnings"],
                "findings": [
                    {
                        "code": item["code"],
                        "severity": item["severity"],
                        # A bass stem is nearly mono because bass is. The number
                        # is right; reading it as a defect of the material is not.
                        "calibrated_for_a_mix": from_stem
                        and item["code"] in STEM_CALIBRATED_CODES,
                    }
                    for item in scan["findings"]
                ],
            },
            "not_judged": [
                "whether the sound is the one this track wants",
                "whether it sits with the other material",
                "musical interest",
            ],
        },
    )


def rank_samples(reviews: Sequence[SampleReview]) -> tuple[SampleReview, ...]:
    """Usable first, then by grid agreement. Undetermined alignment ranks last.

    Not because such a sample is bad -- a pad has no transients to align -- but
    because this cannot speak to it, and a number it could not establish must not
    outrank one it did.
    """

    def key(item: SampleReview) -> tuple[int, float, str]:
        if not item.usable:
            return (2, 0.0, item.path.name)
        agreement = item.agreement
        if agreement is None:
            return (1, 0.0, item.path.name)
        return (0, -agreement, item.path.name)

    return tuple(sorted(reviews, key=key))


def describe(reviews: Sequence[SampleReview]) -> list[str]:
    """The ranking as lines to print, with what it could not judge."""

    if not reviews:
        return ["No samples to review."]
    lines = ["KIHACHI material review:"]
    for position, item in enumerate(rank_samples(reviews), start=1):
        grid = item.review["grid_agreement"]
        alignment = (
            f"{grid['on_grid_fraction']:.3f} on grid"
            if grid["confident"]
            else f"undetermined ({grid['onsets']} onsets)"
        )
        defects = (
            ", ".join(
                sorted(
                    finding["code"] + ("*" if finding["calibrated_for_a_mix"] else "")
                    for finding in item.review["defects"]["findings"]
                )
            )
            or "clean"
        )
        lines.append(
            f"  #{position} {alignment:<26} {item.review['density']['onsets_per_bar']:5.2f}/bar  "
            f"{defects:<26} {item.review['sample']}"
        )
    lines.append(
        "- ranked on grid agreement only. Density, level and brightness are "
        "reported because they have no better and worse"
    )
    if any(
        finding["calibrated_for_a_mix"]
        for item in reviews
        for finding in item.review["defects"]["findings"]
    ):
        lines.append(
            "- * marks a finding whose threshold was calibrated on a mix, on a "
            "sample cut from a single stem: the measurement holds, reading it as "
            "a defect of the material does not"
        )
    lines.append("- not judged here: " + "; ".join(reviews[0].review["not_judged"]))
    return lines
