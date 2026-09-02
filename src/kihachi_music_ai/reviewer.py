from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .alignment import audio_alignment, compare_audio_alignments
from .critic import critique_evidence, repaint_revision_prompt
from .midi import read_midi
from .midi_review import review_midi_tracks
from .repaint_planner import build_repaint_plan
from .review_contract import (
    ReviewPhase,
    collect_generation_review_evidence,
    collect_midi_review_evidence,
    load_material_defects,
    load_song_spec,
    require_audio_analysis,
)
from .tail_guard import DEFAULT_TAIL_GUARD_BARS
from .tail_trim import diagnose_tail_silence

REVIEW_VERSION = "0.4"


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
    evidence = collect_generation_review_evidence(project_dir)
    review_file = project_dir / "generation_review.json"
    revision_prompt_file = project_dir / "revision_prompt.txt"
    repaint_plan_file = project_dir / "repaint_plan.json"
    protected_paths = [review_file, repaint_plan_file]
    if not preserve_revision_prompt:
        protected_paths.append(revision_prompt_file)
    for path in protected_paths:
        if path.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite generation review artifact: {path}")

    spec = evidence.spec
    analysis = evidence.analysis
    assert analysis is not None

    alignment = audio_alignment(analysis)
    midi_review = _midi_review_from_evidence(evidence)
    defects = evidence.defects
    critique = critique_evidence(
        spec,
        phase=evidence.phase,
        analysis=analysis,
        audio_alignment=alignment,
        midi_review=midi_review,
        defects=defects,
        audio_analysis_status=evidence.audio_analysis_status,
        midi_status=evidence.midi_status,
        defects_status=evidence.defects_status,
    )
    findings = critique["findings"]
    revision_prompt = critique["revision_prompt"]
    repaint_plan = build_repaint_plan(
        spec,
        analysis,
        findings,
        material_defects=defects,
        tail_guard_bars=tail_guard_bars,
        prefer_bar_level=prefer_bar_level,
    )
    review: dict[str, Any] = {
        "review_version": REVIEW_VERSION,
        "scope": "song_spec_alignment_not_audio_quality",
        "review_phase": evidence.phase.value,
        "project": project_dir.name,
        "analysis_version": analysis.get("analysis_version"),
        "analysis_audio_sha256": analysis.get("sha256"),
        "alignment": alignment,
        "audio_alignment_note": (
            "Audio-only: duration, tempo, section boundaries and the energy curve. "
            "Harmony moved to midi_alignment, where it is compared rather than "
            "detected. Heard-harmony numbers are still reported under "
            "detected_harmony for diagnosis, but no longer scored."
        ),
        "detected_harmony": {
            "note": (
                "what a detector hears in the finished mix; a low value here means "
                "the harmony is masked, not that it was composed wrongly"
            ),
            "key_status": analysis.get("song_spec_comparison", {}).get("key_status"),
            "key_confidence": _number(
                analysis.get("harmony", {}).get("key", {}).get("confidence")
            ),
            "progression_match_ratio": _number(
                analysis.get("song_spec_comparison", {}).get("progression_match_ratio")
            ),
            "confident_bar_coverage": _number(
                analysis.get("harmony", {}).get("chords", {}).get("confident_bar_coverage")
            ),
        },
        "critic": {
            "critic_version": critique["critic_version"],
            "evidence_status": critique["evidence_status"],
        },
        "findings": findings,
        "revision_prompt": revision_prompt,
        "repaint_candidate": repaint_plan,
    }
    if midi_review is not None:
        review["midi_alignment"] = midi_review
    if defects is not None:
        review["material_defects"] = defects
        tail_silence = diagnose_tail_silence(defects)
        if tail_silence is not None:
            review["tail_silence"] = tail_silence
    if analysis.get("spectrum") is not None:
        review["spectral_balance"] = analysis["spectrum"]

    if against is not None:
        baseline_dir = Path(against)
        baseline_spec = load_song_spec(baseline_dir)
        if baseline_spec != spec:
            raise ValueError("comparison requires projects with identical SongSpec")
        baseline_analysis = require_audio_analysis(
            baseline_dir,
            context="generation review comparison baseline",
        )
        baseline_alignment = audio_alignment(baseline_analysis)
        review["comparison"] = compare_audio_alignments(
            alignment,
            baseline_alignment,
            baseline_project=baseline_dir.name,
            target_project=project_dir.name,
        )

    _atomic_write_text(review_file, json.dumps(review, ensure_ascii=False, indent=2) + "\n")
    _atomic_write_text(
        repaint_plan_file,
        json.dumps(repaint_plan, ensure_ascii=False, indent=2) + "\n",
    )
    if not preserve_revision_prompt:
        _atomic_write_text(revision_prompt_file, repaint_revision_prompt(revision_prompt) + "\n")
    return GenerationReviewManifest(
        project_dir=project_dir,
        review_file=review_file,
        revision_prompt_file=revision_prompt_file,
        repaint_plan_file=repaint_plan_file,
        review=review,
    )


def _midi_review_from_evidence(evidence) -> dict[str, Any] | None:
    if evidence.midi_paths is None:
        return None
    tracks = {
        name: read_midi(path).notes
        for name, path in zip(evidence.spec.parts(), evidence.midi_paths, strict=True)
    }
    return review_midi_tracks(evidence.spec, tracks)


def review_project_midi_only(
    project_dir: Path,
    *,
    overwrite: bool = False,
) -> GenerationReviewManifest:
    """Run the local MIDI review and critic path without audio analysis."""

    project_dir = Path(project_dir)
    evidence = collect_midi_review_evidence(project_dir)
    review_file = project_dir / "generation_review.json"
    revision_prompt_file = project_dir / "revision_prompt.txt"
    for path in (review_file, revision_prompt_file):
        if path.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite generation review artifact: {path}")

    midi_review = _midi_review_from_evidence(evidence)
    assert midi_review is not None
    critique = critique_evidence(
        evidence.spec,
        phase=ReviewPhase.MIDI_ONLY,
        analysis=None,
        audio_alignment=None,
        midi_review=midi_review,
        defects=None,
        audio_analysis_status=evidence.audio_analysis_status,
        midi_status=evidence.midi_status,
        defects_status=evidence.defects_status,
    )
    review: dict[str, Any] = {
        "review_version": REVIEW_VERSION,
        "scope": "midi_only_local_slice_not_audio_quality",
        "review_phase": evidence.phase.value,
        "project": project_dir.name,
        "midi_alignment": midi_review,
        "critic": {
            "critic_version": critique["critic_version"],
            "evidence_status": critique["evidence_status"],
        },
        "findings": critique["findings"],
        "revision_prompt": critique["revision_prompt"],
    }

    _atomic_write_text(review_file, json.dumps(review, ensure_ascii=False, indent=2) + "\n")
    _atomic_write_text(
        revision_prompt_file,
        repaint_revision_prompt(critique["revision_prompt"]) + "\n",
    )
    return GenerationReviewManifest(
        project_dir=project_dir,
        review_file=review_file,
        revision_prompt_file=revision_prompt_file,
        repaint_plan_file=project_dir / "repaint_plan.json",
        review=review,
    )


# Backward-compatible aliases for tests and internal callers.
def _load_project(project_dir: Path) -> tuple[Any, dict[str, Any]]:
    spec = load_song_spec(project_dir)
    analysis = require_audio_analysis(project_dir, context="generation review")
    return spec, analysis


def _alignment(analysis: dict[str, Any]) -> dict[str, Any]:
    return audio_alignment(analysis)


def _material_defects(project_dir: Path) -> dict[str, Any] | None:
    return load_material_defects(project_dir)


def _balance_findings(spectrum: dict[str, Any] | None) -> list[dict[str, Any]]:
    from .critic import balance_findings

    return balance_findings(spectrum)


def _defect_findings(defects: dict[str, Any] | None) -> list[dict[str, Any]]:
    from .critic import defect_findings

    return defect_findings(defects)


def _midi_findings(
    analysis: dict[str, Any],
    midi_review: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    from .critic import midi_findings

    return midi_findings(analysis, midi_review)


def _findings(spec, analysis: dict[str, Any]) -> list[dict[str, Any]]:
    from .critic import audio_alignment_findings

    return audio_alignment_findings(spec, analysis)


def _revision_prompt(spec, findings: list[dict[str, Any]]) -> str:
    from .critic import revision_prompt_for_findings
    from .review_contract import ReviewPhase

    return revision_prompt_for_findings(spec, findings, phase=ReviewPhase.GENERATION_REVIEW)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


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
