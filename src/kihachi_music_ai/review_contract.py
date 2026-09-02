"""Phase-aware review contracts and artifact validation.

Diagnostics, alignment, reviewer, and critic each consume evidence appropriate
to the current pipeline phase. Later-phase artifacts must not fail an earlier
phase, and missing required artifacts must fail at the operation boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .models import SongSpec
from .project_artifacts import managed_midi_paths, require_managed_midi

REVIEW_CONTRACT_VERSION = "0.1"


class ReviewPhase(str, Enum):
    """Pipeline phases the review stack understands."""

    MIDI_ONLY = "midi_only"
    """SongSpec and managed MIDI are present; audio analysis is not required."""

    AUDIO_ANALYSIS = "audio_analysis"
    """``audio_analysis.json`` is present for the take under review."""

    GENERATION_REVIEW = "generation_review"
    """Full generation review: audio analysis required; MIDI and defects optional."""


class EvidenceStatus(str, Enum):
    """Whether a piece of evidence applies at the current phase."""

    EVALUATED = "evaluated"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ReviewerEvidence:
    """Structured evidence collected for alignment and critic interpretation."""

    project_dir: Path
    spec: SongSpec
    phase: ReviewPhase
    analysis: dict[str, Any] | None
    midi_paths: tuple[Path, ...] | None
    defects: dict[str, Any] | None
    audio_analysis_status: EvidenceStatus
    midi_status: EvidenceStatus
    defects_status: EvidenceStatus


def load_song_spec(project_dir: Path) -> SongSpec:
    project_dir = Path(project_dir)
    spec_path = project_dir / "song_spec.json"
    if not spec_path.is_file():
        raise FileNotFoundError(f"SongSpec not found: {spec_path}")
    return SongSpec.from_json(spec_path.read_text(encoding="utf-8"))


def detect_review_phase(project_dir: Path, spec: SongSpec | None = None) -> ReviewPhase:
    """Infer the highest review phase the project has reached."""

    project_dir = Path(project_dir)
    if spec is None:
        spec = load_song_spec(project_dir)
    if (project_dir / "audio_analysis.json").is_file():
        return ReviewPhase.GENERATION_REVIEW
    declared = managed_midi_paths(project_dir, spec)
    if any(path.is_file() for path in declared):
        return ReviewPhase.MIDI_ONLY
    return ReviewPhase.MIDI_ONLY


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} root must be an object: {path}")
    return payload


def load_material_defects(project_dir: Path) -> dict[str, Any] | None:
    path = project_dir / "material_defects.json"
    if not path.is_file():
        return None
    payload = _load_json_object(path, "material defects")
    return payload


def require_audio_analysis(project_dir: Path, *, context: str) -> dict[str, Any]:
    """Load audio analysis or fail clearly when audio review is requested."""

    project_dir = Path(project_dir)
    analysis_path = project_dir / "audio_analysis.json"
    if not analysis_path.is_file():
        raise FileNotFoundError(
            f"{context} requires audio analysis artifact: {analysis_path.name}"
        )
    return _load_json_object(analysis_path, "audio analysis")


def collect_generation_review_evidence(project_dir: Path) -> ReviewerEvidence:
    """Collect evidence for ``review_project`` (audio analysis required)."""

    project_dir = Path(project_dir)
    spec = load_song_spec(project_dir)
    analysis = require_audio_analysis(
        project_dir,
        context="generation review",
    )
    declared = managed_midi_paths(project_dir, spec)
    midi_paths: tuple[Path, ...] | None
    if any(path.is_file() for path in declared):
        midi_paths = require_managed_midi(
            project_dir,
            spec,
            context="generation review project",
        )
        midi_status = EvidenceStatus.EVALUATED
    else:
        midi_paths = None
        midi_status = EvidenceStatus.UNAVAILABLE
    defects = load_material_defects(project_dir)
    return ReviewerEvidence(
        project_dir=project_dir,
        spec=spec,
        phase=ReviewPhase.GENERATION_REVIEW,
        analysis=analysis,
        midi_paths=midi_paths,
        defects=defects,
        audio_analysis_status=EvidenceStatus.EVALUATED,
        midi_status=midi_status,
        defects_status=(
            EvidenceStatus.EVALUATED if defects is not None else EvidenceStatus.UNAVAILABLE
        ),
    )


def collect_midi_review_evidence(project_dir: Path) -> ReviewerEvidence:
    """Collect evidence for ``review_project_midi`` (managed MIDI required)."""

    project_dir = Path(project_dir)
    spec = load_song_spec(project_dir)
    midi_paths = require_managed_midi(
        project_dir,
        spec,
        context="MIDI review project",
    )
    return ReviewerEvidence(
        project_dir=project_dir,
        spec=spec,
        phase=ReviewPhase.MIDI_ONLY,
        analysis=None,
        midi_paths=midi_paths,
        defects=None,
        audio_analysis_status=EvidenceStatus.NOT_APPLICABLE,
        midi_status=EvidenceStatus.EVALUATED,
        defects_status=EvidenceStatus.NOT_APPLICABLE,
    )
