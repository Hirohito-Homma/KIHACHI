"""Alignment scoring: compare realization against SongSpec intent.

Alignment consumes structured analysis or MIDI review output. It does not
re-read artifacts, reimplement density diagnostics, or guess project phase.
"""

from __future__ import annotations

from typing import Any

# Audio-only weights. Harmony is scored in ``midi_review`` instead of here.
ALIGNMENT_WEIGHTS = {
    "duration": 0.15,
    "tempo": 0.30,
    "section_boundaries": 0.25,
    "section_energy": 0.30,
}

AUDIO_ALIGNMENT_SCORE_MEANING = (
    "how far the *audio* follows the SongSpec, from what audio alone can "
    "establish. Harmony is scored exactly in midi_alignment instead. Not an "
    "audio-quality score, and it moves a lot with the render seed -- one "
    "spec across three seeds spanned 28.03 to 61.21, and a second spec "
    "across five seeds spanned 37.32 to 77.52 with nothing but the seed "
    "changed, so differences of a few points between settings are not "
    "evidence"
)


def audio_alignment(analysis: dict[str, Any]) -> dict[str, Any]:
    """Score audio realization against SongSpec comparison fields."""

    comparison = analysis.get("song_spec_comparison", {})
    duration_delta = _number(comparison.get("duration_delta_sec"))
    tempo_delta = _number(comparison.get("tempo_delta_bpm"))
    boundary_recall = _number(comparison.get("section_boundary_recall"))
    energy_correlation = _number(comparison.get("section_energy_correlation"))

    component_scores = {
        "duration": _clamp(1.0 - abs(duration_delta or 0.0) / 2.0) if duration_delta is not None else 0.0,
        "tempo": _clamp(1.0 - abs(tempo_delta or 0.0) / 3.0) if tempo_delta is not None else 0.0,
        "section_boundaries": _clamp(boundary_recall or 0.0),
        "section_energy": _clamp(((energy_correlation or 0.0) + 1.0) / 2.0)
        if energy_correlation is not None
        else 0.0,
    }
    weighted = sum(component_scores[name] * ALIGNMENT_WEIGHTS[name] for name in ALIGNMENT_WEIGHTS)
    total_score = round(weighted * 100.0, 2)
    grade = _grade(total_score)
    return {
        "score": total_score,
        "grade": grade,
        "score_meaning": AUDIO_ALIGNMENT_SCORE_MEANING,
        "components": {
            name: {
                "score": round(component_scores[name], 4),
                "weight": weight,
                "weighted_points": round(component_scores[name] * weight * 100.0, 2),
            }
            for name, weight in ALIGNMENT_WEIGHTS.items()
        },
    }


def compare_audio_alignments(
    target: dict[str, Any],
    baseline: dict[str, Any],
    *,
    baseline_project: str,
    target_project: str,
) -> dict[str, Any]:
    """Compare two audio alignment scores."""

    delta = target["score"] - baseline["score"]
    if delta > 0.05:
        preferred = "target"
    elif delta < -0.05:
        preferred = "baseline"
    else:
        preferred = "tie"
    return {
        "baseline_project": baseline_project,
        "target_project": target_project,
        "baseline_alignment_score": baseline["score"],
        "target_alignment_score": target["score"],
        "score_delta": round(delta, 2),
        "preferred_song_spec_alignment": preferred,
        "note": "This compares SongSpec alignment only, not musical or audio quality.",
    }


def _grade(total_score: float) -> str:
    if total_score >= 80.0:
        return "aligned"
    if total_score >= 55.0:
        return "partial"
    return "needs_revision"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
