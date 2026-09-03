"""VS5 — Adopted take → Ableton handoff.

Consumes an explicit human adoption (VS4) and builds a durable, provenance-checked
handoff into the existing ArrangementPlan boundary.  Never adopts a take, never
talks to Ableton Live, and never infers a winner from ranking or preference memory.

Architectural contract preserved:

    MIDI carries editable structure into Live.
    Generated audio is production / reference / source material.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .ableton import ArrangementPlanManifest, build_arrangement_plan
from .midi import read_midi
from .models import SongSpec
from .project_artifacts import managed_midi_names, require_managed_midi
from .repaint_planner import song_spec_sha256
from .revision import (
    Adoption,
    RevisionLog,
    Round,
    _resolve_round_project,
    _round_by_index,
    load_revision_log,
    revision_log_path,
)

ABLETON_HANDOFF_VERSION = "0.1"
ABLETON_HANDOFF_NAME = "ableton_handoff.json"
ARRANGEMENT_PLAN_NAME = "arrangement_plan.json"


class AbletonHandoffError(ValueError):
    """Actionable refusal when the adopted take cannot be handed off."""


@dataclass(frozen=True)
class MidiArtifact:
    part: str
    path: Path
    sha256: str

    def to_dict(self, *, base_dir: Path | None = None) -> dict[str, Any]:
        return {
            "part": self.part,
            "path": _relpath(self.path, base_dir),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class AdoptedTake:
    """Canonical resolution of the explicitly human-adopted production state."""

    root_project: Path
    adopted_round: int
    adopted_project: Path
    audio_file: Path
    audio_sha256: str
    song_spec_file: Path
    song_spec_sha256: str
    midi: tuple[MidiArtifact, ...]
    revision_log: RevisionLog
    adoption: Adoption
    preference_memory_file: Path | None

    @property
    def midi_files(self) -> tuple[Path, ...]:
        return tuple(item.path for item in self.midi)


@dataclass(frozen=True)
class AbletonHandoffManifest:
    root_project: Path
    handoff_file: Path
    handoff: dict[str, Any]
    adopted_take: AdoptedTake
    arrangement: ArrangementPlanManifest
    unchanged: bool


def ableton_handoff_path(project_dir: Path) -> Path:
    return Path(project_dir) / ABLETON_HANDOFF_NAME


def resolve_adopted_take(project_dir: Path) -> AdoptedTake:
    """Resolve the explicitly human-adopted take for ``project_dir``.

    Refuses when no adoption exists, the adopted round is missing, or provenance
    no longer describes the on-disk production state.  Never falls back to
    ranking, newest files, or preference memory.
    """

    root = Path(project_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"project not found: {root}")

    log_path = revision_log_path(root)
    if not log_path.is_file():
        raise AbletonHandoffError(
            "No human-adopted take exists for this project. "
            f"Missing revision log: {log_path}. "
            "Run `kihachi revise ...` then `kihachi adopt ... --round N` first."
        )

    log = load_revision_log(root)
    if log.adopted is None:
        raise AbletonHandoffError(
            "No human-adopted take exists for this project. "
            "Run `kihachi revisions ...` and explicitly adopt a round first "
            "with `kihachi adopt PROJECT --round N`."
        )

    adoption = log.adopted
    try:
        round_ = _round_by_index(log, adoption.round)
    except ValueError as error:
        raise AbletonHandoffError(
            f"Adopted round {adoption.round} is recorded but no longer exists "
            f"in revision_log.json: {error}"
        ) from error

    try:
        adopted_project = _resolve_round_project(root, round_)
    except FileNotFoundError as error:
        raise AbletonHandoffError(
            f"Adopted project for round {adoption.round} is missing: {error}"
        ) from error

    _assert_project_identity(root, adoption, round_, adopted_project)
    audio_file, audio_sha = _resolve_and_verify_audio(root, adoption, round_, adopted_project)
    song_spec_file, spec, spec_sha = _resolve_and_verify_song_spec(root, round_, adopted_project)
    midi = _resolve_and_verify_midi(adopted_project, spec)

    preference_path = root / "preference_memory.json"
    return AdoptedTake(
        root_project=root,
        adopted_round=adoption.round,
        adopted_project=adopted_project,
        audio_file=audio_file,
        audio_sha256=audio_sha,
        song_spec_file=song_spec_file,
        song_spec_sha256=spec_sha,
        midi=midi,
        revision_log=log,
        adoption=adoption,
        preference_memory_file=preference_path if preference_path.is_file() else None,
    )


def build_ableton_handoff(
    project_dir: Path,
    *,
    overwrite: bool = False,
    first_track_index: int = 0,
    session_slot: int = 0,
    automation: Sequence[Mapping[str, Any]] = (),
    split_drums: bool = False,
    sends: Sequence[Mapping[str, Any]] = (),
) -> AbletonHandoffManifest:
    """Validate the adopted take, build ArrangementPlan, write ``ableton_handoff.json``.

    Does not adopt, does not mutate audio / MIDI / SongSpec / preference memory,
    and does not require Ableton Live, GPU, or ACE-Step.
    """

    take = resolve_adopted_take(project_dir)
    destination = ableton_handoff_path(take.root_project)

    arrangement = _plan_adopted_arrangement(
        take,
        overwrite=overwrite,
        first_track_index=first_track_index,
        session_slot=session_slot,
        automation=automation,
        split_drums=split_drums,
        sends=sends,
    )

    handoff = _build_handoff_document(take, arrangement)
    unchanged = False
    if destination.exists() and not overwrite:
        existing = _load_handoff_document(destination)
        if _handoff_equivalent(existing, handoff):
            unchanged = True
            handoff = existing
        else:
            raise FileExistsError(
                f"refusing to overwrite ableton handoff with different provenance: "
                f"{destination} (use --overwrite to regenerate)"
            )
    elif not unchanged:
        _atomic_write_text(
            destination,
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
        )

    return AbletonHandoffManifest(
        root_project=take.root_project,
        handoff_file=destination,
        handoff=handoff,
        adopted_take=take,
        arrangement=arrangement,
        unchanged=unchanged,
    )


def describe_ableton_handoff(manifest: AbletonHandoffManifest) -> list[str]:
    """Concise summary lines for the CLI."""

    take = manifest.adopted_take
    lines = [
        f"Adopted round: {take.adopted_round}",
        f"Project: {take.adopted_project.name}",
        f"Audio: {_relpath(take.audio_file, take.root_project)}",
        f"Managed MIDI: {len(take.midi)} files",
        f"Arrangement plan: {_relpath(manifest.arrangement.plan_file, take.root_project)}",
        f"Handoff: {_relpath(manifest.handoff_file, take.root_project)}",
    ]
    if manifest.unchanged:
        lines.append("Handoff unchanged (identical provenance already on disk)")
    return lines


def _assert_project_identity(
    root: Path,
    adoption: Adoption,
    round_: Round,
    adopted_project: Path,
) -> None:
    expected_name = root.name if round_.index == 0 else f"{root.name}-rev{round_.index:02d}"
    if adopted_project.name != expected_name:
        raise AbletonHandoffError(
            f"Adopted project {adopted_project.name!r} does not match expected "
            f"revision identity {expected_name!r}"
        )
    if adoption.project and adoption.project != adopted_project.name:
        raise AbletonHandoffError(
            f"Adoption metadata project {adoption.project!r} does not match "
            f"resolved project {adopted_project.name!r}"
        )
    # Prevent path traversal / unrelated external substitution.
    try:
        adopted_project.relative_to(root.parent.resolve())
    except ValueError as error:
        raise AbletonHandoffError(
            f"Adopted project is outside the source project parent: {adopted_project}"
        ) from error
    if round_.index == 0 and adopted_project.resolve() != root:
        raise AbletonHandoffError("Adopted round 0 must be the source project itself")


def _resolve_and_verify_audio(
    root: Path,
    adoption: Adoption,
    round_: Round,
    adopted_project: Path,
) -> tuple[Path, str]:
    audio = Path(round_.audio_file)
    if not audio.is_file() and adoption.audio_file:
        audio = _resolve_path_candidate(adoption.audio_file, root=root, project=adopted_project)
    if not audio.is_file():
        audio = _resolve_path_candidate(str(round_.audio_file), root=root, project=adopted_project)
    if not audio.is_file():
        analysis_path = adopted_project / "audio_analysis.json"
        if analysis_path.is_file():
            try:
                analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
                candidate = adopted_project / analysis["audio_file"]
                if candidate.is_file():
                    audio = candidate
            except (OSError, ValueError, KeyError, TypeError):
                pass
    if not audio.is_file():
        raise AbletonHandoffError(
            f"Adopted WAV is missing for round {adoption.round}: {round_.audio_file}"
        )

    # Keep the adopted audio inside the adopted project tree.
    try:
        audio.resolve().relative_to(adopted_project.resolve())
    except ValueError as error:
        raise AbletonHandoffError(
            f"Adopted audio escapes the adopted project: {audio}"
        ) from error

    actual_sha = _file_sha256(audio)
    recorded = adoption.audio_sha256
    if recorded is None:
        raise AbletonHandoffError(
            "Adoption metadata lacks audio_sha256; re-run "
            "`kihachi adopt PROJECT --round N` to record immutable audio provenance."
        )
    if actual_sha != recorded:
        raise AbletonHandoffError(
            f"Adopted audio SHA-256 mismatch for round {adoption.round}: "
            "the WAV changed after human adoption (or does not match the recorded digest)."
        )
    return audio.resolve(), actual_sha


def _resolve_and_verify_song_spec(
    root: Path,
    round_: Round,
    adopted_project: Path,
) -> tuple[Path, SongSpec, str]:
    spec_path = adopted_project / "song_spec.json"
    if not spec_path.is_file():
        raise AbletonHandoffError(
            f"Adopted project is missing song_spec.json: {spec_path}"
        )
    try:
        spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise AbletonHandoffError(
            f"Adopted song_spec.json is invalid: {spec_path}"
        ) from error

    actual_sha = song_spec_sha256(spec)

    if round_.index == 0:
        root_spec_path = root / "song_spec.json"
        if root_spec_path.is_file() and root_spec_path.resolve() != spec_path.resolve():
            root_sha = song_spec_sha256(
                SongSpec.from_json(root_spec_path.read_text(encoding="utf-8"))
            )
            if root_sha != actual_sha:
                raise AbletonHandoffError(
                    "Adopted round 0 SongSpec does not match the source project SongSpec"
                )
        return spec_path.resolve(), spec, actual_sha

    stage_path = adopted_project / "repaint_stage.json"
    if not stage_path.is_file():
        raise AbletonHandoffError(
            f"Adopted revision is missing repaint provenance: {stage_path}"
        )
    try:
        stage = json.loads(stage_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise AbletonHandoffError(
            f"Adopted revision has invalid provenance: {stage_path}"
        ) from error

    source_name = stage.get("source_project")
    if not isinstance(source_name, str) or not source_name:
        raise AbletonHandoffError(
            f"Adopted revision provenance lacks source_project: {stage_path}"
        )
    allowed = {root.name}
    for index in range(1, round_.index):
        allowed.add(f"{root.name}-rev{index:02d}")
    if source_name not in allowed:
        raise AbletonHandoffError(
            f"Adopted SongSpec lineage is outside the revision chain "
            f"(source_project={source_name!r})"
        )

    source_sha = stage.get("source_song_spec_sha256")
    if isinstance(source_sha, str) and source_sha and source_sha != actual_sha:
        raise AbletonHandoffError(
            "Adopted SongSpec SHA does not match repaint provenance "
            "(SongSpec lineage mismatch)."
        )

    return spec_path.resolve(), spec, actual_sha


def _resolve_and_verify_midi(
    adopted_project: Path,
    spec: SongSpec,
) -> tuple[MidiArtifact, ...]:
    try:
        paths = require_managed_midi(
            adopted_project,
            spec,
            context="Ableton handoff adopted project",
        )
    except FileNotFoundError as error:
        raise AbletonHandoffError(str(error)) from error

    artifacts: list[MidiArtifact] = []
    for part, path in zip(spec.parts(), paths, strict=True):
        artifacts.append(
            MidiArtifact(part=part, path=path.resolve(), sha256=_file_sha256(path))
        )
    # Sanity: names from the managed resolver stay authoritative.
    expected = set(managed_midi_names(spec))
    actual = {f"{item.part}.mid" for item in artifacts}
    if expected != actual:
        raise AbletonHandoffError(
            f"Managed MIDI set mismatch: expected {sorted(expected)}, got {sorted(actual)}"
        )
    return tuple(artifacts)


def _plan_adopted_arrangement(
    take: AdoptedTake,
    *,
    overwrite: bool,
    first_track_index: int,
    session_slot: int,
    automation: Sequence[Mapping[str, Any]],
    split_drums: bool,
    sends: Sequence[Mapping[str, Any]],
) -> ArrangementPlanManifest:
    """Build ArrangementPlan from the adopted project (MIDI = structure).

    Adopted audio is recorded in the handoff manifest as production/reference
    material and is not injected via ``import_vocal_take``.
    """

    project_dir = take.adopted_project
    spec = SongSpec.from_json(take.song_spec_file.read_text(encoding="utf-8"))
    files = require_managed_midi(project_dir, spec, context="Ableton handoff")
    tracks = {
        name: read_midi(path).notes
        for name, path in zip(spec.parts(), files, strict=True)
    }
    plan = build_arrangement_plan(
        spec,
        tracks,
        first_track_index=first_track_index,
        session_slot=session_slot,
        automation=automation,
        split_drums=split_drums,
        sends=sends,
    )

    destination = project_dir / ARRANGEMENT_PLAN_NAME
    if destination.exists():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise FileExistsError(
                f"refusing to overwrite non-arrangement plan: {destination}"
            ) from error
        if _arrangement_identity(existing) == _arrangement_identity(plan):
            return ArrangementPlanManifest(project_dir, files, destination, existing)
        if not overwrite:
            raise FileExistsError(
                f"refusing to overwrite arrangement plan with different content: "
                f"{destination} (use --overwrite)"
            )

    _atomic_write_text(
        destination,
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
    )
    return ArrangementPlanManifest(project_dir, files, destination, plan)


def _arrangement_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Compare ArrangementPlan substance without depending on write order noise."""

    return {
        "arrangement_plan_version": plan.get("arrangement_plan_version"),
        "song": plan.get("song"),
        "tracks": plan.get("tracks"),
        "structure": plan.get("structure"),
        "operations": plan.get("operations"),
        "drums_split": plan.get("drums_split"),
    }


def _build_handoff_document(
    take: AdoptedTake,
    arrangement: ArrangementPlanManifest,
) -> dict[str, Any]:
    root = take.root_project
    return {
        "ableton_handoff_version": ABLETON_HANDOFF_VERSION,
        "execution_state": "handoff_ready_not_applied",
        "source_project": root.name,
        "adopted_round": take.adopted_round,
        "adopted_project": take.adopted_project.name,
        "adoption": {
            "round": take.adoption.round,
            "project": take.adoption.project,
            "selected_at": take.adoption.selected_at,
            "selection_mode": take.adoption.selection_mode,
            "reason": take.adoption.reason,
            "tags": list(take.adoption.tags),
            "audio_file": take.adoption.audio_file,
            "audio_sha256": take.adoption.audio_sha256,
        },
        "audio": {
            "path": _relpath(take.audio_file, root),
            "sha256": take.audio_sha256,
            "role": "production_reference",
            "authoritative_for_structure": False,
        },
        "song_spec": {
            "path": _relpath(take.song_spec_file, root),
            "sha256": take.song_spec_sha256,
        },
        "midi": [item.to_dict(base_dir=root) for item in take.midi],
        "arrangement_plan": {
            "path": _relpath(arrangement.plan_file, root),
            "sha256": _file_sha256(arrangement.plan_file),
        },
        "revision_log": {
            "path": _relpath(revision_log_path(root), root),
        },
        "preference_memory": (
            {
                "path": _relpath(take.preference_memory_file, root),
                "affects_selection": False,
            }
            if take.preference_memory_file is not None
            else None
        ),
        "boundary": {
            "kihachi": "decides what to make",
            "abletongpt": "operates Ableton Live",
            "live_execution": False,
            "auto_adoption": False,
        },
    }


def _load_handoff_document(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise FileExistsError(
            f"refusing to overwrite non-handoff file: {path}"
        ) from error
    if not isinstance(payload, dict) or payload.get("ableton_handoff_version") != ABLETON_HANDOFF_VERSION:
        raise FileExistsError(f"refusing to overwrite non-handoff file: {path}")
    return payload


def _handoff_equivalent(existing: Mapping[str, Any], proposed: Mapping[str, Any]) -> bool:
    keys = (
        "ableton_handoff_version",
        "source_project",
        "adopted_round",
        "adopted_project",
        "adoption",
        "audio",
        "song_spec",
        "midi",
        "arrangement_plan",
    )
    return all(existing.get(key) == proposed.get(key) for key in keys)


def _resolve_path_candidate(stored: str, *, root: Path, project: Path) -> Path:
    path = Path(stored)
    if path.is_file():
        return path
    under_project = project / path.name
    if "audio/" in stored.replace("\\", "/"):
        relative = stored.replace("\\", "/").split("audio/", 1)[-1]
        under_project = project / "audio" / relative
        if under_project.is_file():
            return under_project
    if under_project.is_file():
        return under_project
    by_name = root.parent / path.name
    if by_name.is_file():
        return by_name
    return path


def _relpath(path: Path, base_dir: Path | None) -> str:
    if base_dir is None:
        return str(path)
    resolved = Path(path).resolve()
    root = Path(base_dir).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        pass
    # Adopted revision projects live beside the root (song-rev01/...), so prefer
    # a path relative to the shared parent when the file is outside the root.
    try:
        return resolved.relative_to(root.parent).as_posix()
    except ValueError:
        return str(path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise
