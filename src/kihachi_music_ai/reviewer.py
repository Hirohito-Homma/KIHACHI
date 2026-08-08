from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .midi import read_midi
from .midi_review import TRACK_FILES, review_midi_tracks
from .models import SongSpec
from .repaint_planner import build_repaint_plan
from .tail_guard import DEFAULT_TAIL_GUARD_BARS

REVIEW_VERSION = "0.3"
ALIGNMENT_WEIGHTS = {
    "duration": 0.10,
    "tempo": 0.15,
    "key": 0.20,
    "chords": 0.25,
    "section_boundaries": 0.15,
    "section_energy": 0.15,
}


@dataclass(frozen=True)
class GenerationReviewManifest:
    project_dir: Path
    review_file: Path
    revision_prompt_file: Path
    repaint_plan_file: Path
    review: dict[str, Any]


def review_project(
    project_dir: Path,
    *,
    against: Path | None = None,
    overwrite: bool = False,
    preserve_revision_prompt: bool = False,
    tail_guard_bars: float = DEFAULT_TAIL_GUARD_BARS,
    prefer_bar_level: bool = False,
) -> GenerationReviewManifest:
    project_dir = Path(project_dir)
    spec, analysis = _load_project(project_dir)
    review_file = project_dir / "generation_review.json"
    revision_prompt_file = project_dir / "revision_prompt.txt"
    repaint_plan_file = project_dir / "repaint_plan.json"
    protected_paths = [review_file, repaint_plan_file]
    if not preserve_revision_prompt:
        protected_paths.append(revision_prompt_file)
    for path in protected_paths:
        if path.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite generation review artifact: {path}")

    alignment = _alignment(analysis)
    midi_review = _midi_review(project_dir, spec)
    findings = _findings(spec, analysis)
    findings.extend(_midi_findings(analysis, midi_review))
    revision_prompt = _revision_prompt(spec, findings)
    repaint_plan = build_repaint_plan(
        spec,
        analysis,
        findings,
        tail_guard_bars=tail_guard_bars,
        prefer_bar_level=prefer_bar_level,
    )
    review: dict[str, Any] = {
        "review_version": REVIEW_VERSION,
        "scope": "song_spec_alignment_not_audio_quality",
        "project": project_dir.name,
        "analysis_version": analysis.get("analysis_version"),
        "analysis_audio_sha256": analysis.get("sha256"),
        "alignment": alignment,
        "audio_alignment_note": (
            "Derived from the finished mix, so harmony scores are limited by what a "
            "detector can hear through the mix. See midi_alignment for the exact check."
        ),
        "findings": findings,
        "revision_prompt": revision_prompt,
        "repaint_candidate": repaint_plan,
    }
    if midi_review is not None:
        review["midi_alignment"] = midi_review

    if against is not None:
        baseline_dir = Path(against)
        baseline_spec, baseline_analysis = _load_project(baseline_dir)
        if baseline_spec != spec:
            raise ValueError("comparison requires projects with identical SongSpec")
        baseline_alignment = _alignment(baseline_analysis)
        delta = alignment["score"] - baseline_alignment["score"]
        if delta > 0.05:
            preferred = "target"
        elif delta < -0.05:
            preferred = "baseline"
        else:
            preferred = "tie"
        review["comparison"] = {
            "baseline_project": baseline_dir.name,
            "target_project": project_dir.name,
            "baseline_alignment_score": baseline_alignment["score"],
            "target_alignment_score": alignment["score"],
            "score_delta": round(delta, 2),
            "preferred_song_spec_alignment": preferred,
            "note": "This compares SongSpec alignment only, not musical or audio quality.",
        }

    _atomic_write_text(review_file, json.dumps(review, ensure_ascii=False, indent=2) + "\n")
    _atomic_write_text(
        repaint_plan_file,
        json.dumps(repaint_plan, ensure_ascii=False, indent=2) + "\n",
    )
    if not preserve_revision_prompt:
        _atomic_write_text(revision_prompt_file, revision_prompt + "\n")
    return GenerationReviewManifest(
        project_dir=project_dir,
        review_file=review_file,
        revision_prompt_file=revision_prompt_file,
        repaint_plan_file=repaint_plan_file,
        review=review,
    )


def _load_project(project_dir: Path) -> tuple[SongSpec, dict[str, Any]]:
    spec_path = project_dir / "song_spec.json"
    analysis_path = project_dir / "audio_analysis.json"
    if not spec_path.is_file():
        raise FileNotFoundError(f"SongSpec not found: {spec_path}")
    if not analysis_path.is_file():
        raise FileNotFoundError(f"audio analysis not found: {analysis_path}")
    spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    if not isinstance(analysis, dict):
        raise ValueError(f"audio analysis root must be an object: {analysis_path}")
    return spec, analysis


def _alignment(analysis: dict[str, Any]) -> dict[str, Any]:
    comparison = analysis.get("song_spec_comparison", {})
    harmony = analysis.get("harmony", {})
    chords = harmony.get("chords", {})

    duration_delta = _number(comparison.get("duration_delta_sec"))
    tempo_delta = _number(comparison.get("tempo_delta_bpm"))
    key_status = comparison.get("key_status")
    chord_match = _number(comparison.get("progression_match_ratio"))
    chord_coverage = _number(chords.get("confident_bar_coverage"))
    boundary_recall = _number(comparison.get("section_boundary_recall"))
    energy_correlation = _number(comparison.get("section_energy_correlation"))

    component_scores = {
        "duration": _clamp(1.0 - abs(duration_delta or 0.0) / 2.0) if duration_delta is not None else 0.0,
        "tempo": _clamp(1.0 - abs(tempo_delta or 0.0) / 3.0) if tempo_delta is not None else 0.0,
        "key": {"match": 1.0, "low_confidence": 0.35, "not_detected": 0.20, "mismatch": 0.0}.get(
            str(key_status), 0.0
        ),
        "chords": _clamp(chord_match or 0.0) * (0.5 + 0.5 * _clamp(chord_coverage or 0.0)),
        "section_boundaries": _clamp(boundary_recall or 0.0),
        "section_energy": _clamp(((energy_correlation or 0.0) + 1.0) / 2.0)
        if energy_correlation is not None
        else 0.0,
    }
    weighted = sum(component_scores[name] * ALIGNMENT_WEIGHTS[name] for name in ALIGNMENT_WEIGHTS)
    total_score = round(weighted * 100.0, 2)
    grade = "aligned" if total_score >= 80.0 else "partial" if total_score >= 55.0 else "needs_revision"
    return {
        "score": total_score,
        "grade": grade,
        "score_meaning": "deterministic SongSpec alignment heuristic; not an audio-quality score",
        "components": {
            name: {
                "score": round(component_scores[name], 4),
                "weight": weight,
                "weighted_points": round(component_scores[name] * weight * 100.0, 2),
            }
            for name, weight in ALIGNMENT_WEIGHTS.items()
        },
    }


def _midi_review(project_dir: Path, spec: SongSpec) -> dict[str, Any] | None:
    """Exact MIDI check, when the project actually carries MIDI."""

    paths = {name: project_dir / f"{name}.mid" for name in TRACK_FILES}
    if not all(path.is_file() for path in paths.values()):
        return None
    return review_midi_tracks(
        spec, {name: read_midi(path).notes for name, path in paths.items()}
    )


def _midi_findings(
    analysis: dict[str, Any],
    midi_review: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Separate "the design is wrong" from "the render did not deliver it"."""

    if midi_review is None:
        return []
    findings: list[dict[str, Any]] = []
    harmony = midi_review["harmony"]
    written = min(
        _number(harmony.get("bass_root_match_ratio")) or 0.0,
        _number(harmony.get("chord_tone_match_ratio")) or 0.0,
    )
    heard = _number(analysis.get("song_spec_comparison", {}).get("progression_match_ratio"))
    heard = 0.0 if heard is None else heard

    if written >= 0.95 and heard < 0.5:
        findings.append(
            {
                "code": "harmony_written_but_not_detected",
                "severity": "info",
                "evidence": (
                    f"The written MIDI plays the SongSpec progression exactly "
                    f"(match {written:.4f}), while the audio analysis reads "
                    f"{heard:.4f} from the finished mix."
                ),
                "recommendation": (
                    "Treat the audio chord score as a detection limit, not a "
                    "composition error. Do not repaint to 'fix' the progression; "
                    "improve separation around chord attacks if the chords should "
                    "become audible."
                ),
            }
        )
    elif written < 0.95:
        findings.append(
            {
                "code": "midi_harmony_misaligned",
                "severity": "high",
                "evidence": (
                    f"The written MIDI itself departs from the SongSpec progression "
                    f"(bass-root match {harmony['bass_root_match_ratio']:.4f}, "
                    f"chord-tone match {harmony['chord_tone_match_ratio']:.4f})."
                ),
                "recommendation": (
                    "Fix the composition before rendering again; no repaint can "
                    "correct harmony that was never written."
                ),
            }
        )

    key = midi_review["key"]
    if key["out_of_key_notes"]:
        findings.append(
            {
                "code": "midi_out_of_key_notes",
                "severity": "medium",
                "evidence": (
                    f"{key['out_of_key_notes']} of {key['pitched_notes']} pitched MIDI "
                    f"notes fall outside {key['key']}."
                ),
                "recommendation": "Constrain the composer to the SongSpec scale.",
            }
        )

    empty = midi_review["coverage"]["empty_bars"]
    if empty:
        findings.append(
            {
                "code": "midi_empty_bars",
                "severity": "medium",
                "evidence": f"Tracks with silent bars: {empty}.",
                "recommendation": (
                    "Check the arrangement densities; a silent bar in the MIDI will "
                    "read as an energy collapse downstream."
                ),
            }
        )
    return findings


def _findings(spec: SongSpec, analysis: dict[str, Any]) -> list[dict[str, Any]]:
    comparison = analysis.get("song_spec_comparison", {})
    harmony = analysis.get("harmony", {})
    chords = harmony.get("chords", {})
    sections = analysis.get("sections", {})
    findings: list[dict[str, Any]] = []

    duration_delta = _number(comparison.get("duration_delta_sec"))
    if duration_delta is None or abs(duration_delta) > 0.25:
        findings.append(
            _finding(
                "action",
                "duration_alignment",
                f"Duration delta is {duration_delta} seconds.",
                f"Keep the render at {spec.song.target_duration_sec:.3f} seconds ({spec.song.total_bars} bars).",
            )
        )

    tempo_delta = _number(comparison.get("tempo_delta_bpm"))
    if tempo_delta is None or abs(tempo_delta) > 1.0:
        findings.append(
            _finding(
                "action",
                "tempo_alignment",
                f"Tempo delta is {tempo_delta} BPM.",
                f"Lock the rhythmic pulse to {spec.song.bpm:g} BPM without half-time or double-time ambiguity.",
            )
        )

    key_status = comparison.get("key_status")
    key_confidence = _number(comparison.get("key_confidence"))
    observed_key = comparison.get("observed_key")
    if key_status != "match":
        severity = "warning" if key_status in {"low_confidence", "not_detected"} else "action"
        findings.append(
            _finding(
                severity,
                "key_alignment",
                f"Observed key candidate is {observed_key} at confidence {key_confidence}; status is {key_status}.",
                f"Anchor section openings and bass pedals on {spec.song.tonic}; make {spec.song.key} unambiguous.",
            )
        )

    chord_match = _number(comparison.get("progression_match_ratio"))
    progression = " - ".join(spec.harmony.progression)
    if chord_match is None or chord_match < 0.5:
        findings.append(
            _finding(
                "action",
                "chord_progression_alignment",
                f"Reliable-bar progression match is {chord_match}.",
                f"State one clear chord per bar in the repeating progression {progression}; keep delay tails below the next change.",
            )
        )

    coverage = _number(chords.get("confident_bar_coverage"))
    if coverage is None or coverage < 0.5:
        findings.append(
            _finding(
                "warning",
                "harmonic_readability",
                f"Confident chord coverage is {coverage}.",
                "Reduce harmonic masking from vocals, distortion, reverb, and dub delay around chord attacks.",
            )
        )

    boundary_recall = _number(comparison.get("section_boundary_recall"))
    planned_boundaries = sections.get("planned_boundaries_after_bar", [])
    if boundary_recall is None or boundary_recall < 0.67:
        findings.append(
            _finding(
                "action",
                "section_boundary_alignment",
                f"Planned-boundary recall is {boundary_recall}; planned boundaries are after bars {planned_boundaries}.",
                "Mark each planned boundary with a clear dropout, fill, riser, or density change.",
            )
        )

    energy_correlation = _number(comparison.get("section_energy_correlation"))
    if energy_correlation is None or energy_correlation < 0.5:
        target_arc = " → ".join(f"{section.name} {section.energy:.2f}" for section in spec.arrangement)
        findings.append(
            _finding(
                "action",
                "section_energy_alignment",
                f"Section-energy correlation is {energy_correlation}.",
                f"Follow a clearly rising energy arc: {target_arc}.",
            )
        )
    return findings


def _revision_prompt(spec: SongSpec, findings: list[dict[str, Any]]) -> str:
    header = (
        f"Revision pass: keep {spec.song.bpm:g} BPM, {spec.song.key}, "
        f"{spec.song.time_signature}, {spec.song.total_bars} bars."
    )
    recommendations = [item["recommendation"] for item in findings]
    if not recommendations:
        return header + " Preserve the current SongSpec alignment and refine sound quality only."
    return " ".join([header, *recommendations])


def _finding(severity: str, code: str, evidence: str, recommendation: str) -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "evidence": evidence,
        "recommendation": recommendation,
    }


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}-",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
