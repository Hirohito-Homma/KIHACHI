"""The revision loop: measure a take, fix the worst thing, measure again.

Every piece of this already existed as a separate command -- `analyze` measures,
`review` decides what to fix, `ace-step stage-repaint` builds a clean project
from that decision, `ace-step render` fills it in. Driving them by hand meant
four commands per round and a five-minute wait in the middle, so in practice one
round is what ever got run.

What this does *not* do is pick a winner. The takes it produces are candidates,
and it says so: it ranks them and stops. Adopting one is a listening decision,
and the numbers here are not good enough to make it -- the audio alignment score
cannot hear whether a take is any good, only whether it followed the plan, and
across a seed sweep the same settings moved it by 33 points.

Nothing is overwritten. Each round writes a new project beside the last, and
the source project keeps every input it arrived with -- the one thing written
back into it is ``revision_log.json``, plus an optional Markdown mirror when
the caller explicitly names one.  Human adoption is a separate metadata write:
``adopt_revision`` records which candidate was chosen without replacing audio.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .analyzer import analyze_project
from .preference_memory import PreferenceEntry, record_preference
from .repaint_planner import stage_repaint_project
from .reviewer import review_project

REVISION_LOG_VERSION = "0.1"
DEFAULT_ROUNDS = 3
MIN_GAIN = 1.0
"""Points of alignment a round has to win to count as progress.

Not zero: re-rendering the same settings with a different seed moves this score
by tens of points, so a fraction of a point is noise wearing a result's clothes.
"""
SELECTION_MODE_HUMAN = "human"


class Renderer(Protocol):
    """Fills in a staged project's audio. Injected so the loop can be tested.

    ``source_audio`` is the take being repainted. A repaint is defined relative
    to existing audio and ACE-Step refuses one without it, and staging
    deliberately does not copy the source into the new project -- so the loop has
    to hand it over rather than let the renderer guess.
    """

    def __call__(self, project_dir: Path, source_audio: Path) -> None: ...


@dataclass(frozen=True)
class Round:
    index: int
    project_dir: Path
    alignment: float
    grade: str
    blocking: int
    warnings: int
    defect_codes: tuple[str, ...]
    planned_action: str | None
    audio_file: Path
    tail_silence_only: bool = False

    @property
    def usable(self) -> bool:
        return self.blocking == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "project": str(self.project_dir),
            "alignment": self.alignment,
            "grade": self.grade,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "defects": list(self.defect_codes),
            "planned_action": self.planned_action,
            "audio_file": str(self.audio_file),
            "usable": self.usable,
            "tail_silence_only": self.tail_silence_only,
        }


@dataclass(frozen=True)
class Adoption:
    """An explicit human selection of one measured revision take."""

    round: int
    project: str
    selected_at: str
    selection_mode: str = SELECTION_MODE_HUMAN
    reason: str | None = None
    tags: tuple[str, ...] = ()
    audio_file: str | None = None
    audio_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "project": self.project,
            "audio_file": self.audio_file,
            "audio_sha256": self.audio_sha256,
            "selected_at": self.selected_at,
            "selection_mode": self.selection_mode,
            "reason": self.reason,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class RevisionLog:
    rounds: tuple[Round, ...]
    stopped_because: str
    #: ``"revising"`` while the loop is still running, ``"failed"`` when a round
    #: raised. The log is written after every round, so a run that dies in the
    #: middle still leaves an account of the takes that were measured -- each of
    #: which cost a render.
    execution_state: str = "complete"
    adopted: Adoption | None = None

    def ranked(self) -> tuple[Round, ...]:
        """Best first: usable takes above unusable ones, then by alignment.

        A take with a hole in it does not win on points. That ordering is the
        whole reason defects are measured separately from conformance -- the
        baseline scores 88.69 "aligned" with 2.28 s of silence in it.
        """

        return tuple(sorted(self.rounds, key=lambda r: (not r.usable, -r.alignment)))

    def to_dict(self) -> dict[str, Any]:
        if self.adopted is None:
            adoption_note = (
                "Nothing was adopted. These are candidates: the alignment score "
                "measures whether a take followed the SongSpec, not whether it "
                "sounds good, and a seed change alone moves it by tens of points."
            )
            adopted_payload: dict[str, Any] | None = None
        else:
            adoption_note = (
                f"Round {self.adopted.round} was adopted by an explicit human "
                f"selection ({self.adopted.selection_mode})."
            )
            adopted_payload = self.adopted.to_dict()
        return {
            "revision_log_version": REVISION_LOG_VERSION,
            "execution_state": self.execution_state,
            "stopped_because": self.stopped_because,
            "rounds": [item.to_dict() for item in self.rounds],
            "ranking": [item.index for item in self.ranked()],
            "adopted": adopted_payload,
            "adoption_note": adoption_note,
        }


@dataclass(frozen=True)
class AdoptionManifest:
    project_dir: Path
    log_file: Path
    log: RevisionLog
    adoption: Adoption
    unchanged: bool
    preference_recorded: bool


def _analysis_is_current(project_dir: Path) -> bool:
    """Whether ``audio_analysis.json`` describes the audio that is there now.

    The analysis records the SHA-256 of the file it measured, so this is a fact
    rather than a guess about mtimes. It matters because the loop reuses an
    existing analysis: a project re-rendered since it was last analyzed would
    otherwise be judged, and revised, on the previous take's numbers.
    """

    analysis_path = project_dir / "audio_analysis.json"
    if not analysis_path.is_file():
        return False
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        audio_path = project_dir / analysis["audio_file"]
        recorded = str(analysis["sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not audio_path.is_file():
        return False
    digest = hashlib.sha256()
    with audio_path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == recorded


def _measure(project_dir: Path, index: int) -> Round:
    """Analyze and review a project, reusing an analysis that is still true."""

    if not _analysis_is_current(project_dir):
        analyze_project(project_dir, overwrite=True)
    manifest = review_project(project_dir, overwrite=True)
    review = manifest.review
    alignment = review["alignment"]
    defects = review.get("material_defects") or {}
    findings = [
        item for item in defects.get("findings", [])
        if item.get("severity") in {"blocking", "warning"}
    ]
    analysis = json.loads((project_dir / "audio_analysis.json").read_text(encoding="utf-8"))
    audio_file = project_dir / analysis["audio_file"]
    plan_file = manifest.repaint_plan_file
    action = None
    if plan_file.is_file():
        selection = json.loads(plan_file.read_text(encoding="utf-8")).get("selection", {})
        if selection.get("section_name"):
            action = f"repaint {selection['section_name']}"
        elif selection.get("start_bar") is not None:
            action = f"repaint bars {selection['start_bar']}:{selection['end_bar']}"
    return Round(
        index=index,
        project_dir=project_dir,
        alignment=float(alignment["score"]),
        grade=str(alignment["grade"]),
        blocking=sum(1 for item in findings if item["severity"] == "blocking"),
        warnings=sum(1 for item in findings if item["severity"] == "warning"),
        defect_codes=tuple(item["code"] for item in findings),
        planned_action=action,
        audio_file=audio_file,
        tail_silence_only=(
            review.get("tail_silence") is not None
            and sum(1 for item in findings if item["severity"] == "blocking") == 1
        ),
    )


def _has_audio(project_dir: Path) -> bool:
    audio_dir = project_dir / "audio"
    return audio_dir.is_dir() and any(item.is_file() for item in audio_dir.iterdir())


def run_revision_loop(
    project_dir: Path,
    render: Renderer,
    *,
    rounds: int = DEFAULT_ROUNDS,
    on_round: Callable[[Round], None] | None = None,
    resume: bool = False,
    log_file: Path | None = None,
    markdown_log_file: Path | None = None,
) -> RevisionLog:
    """Revise a rendered project up to ``rounds`` times, keeping every take.

    Stops early on any of: nothing left to fix, a round that did not improve on
    the one before it, or a staging step that refuses. Each stop reason is
    recorded rather than inferred from the round count.

    The log is written after every round rather than at the end. A round is a
    render, and a render is minutes: a loop that died on the third one used to
    leave two measured takes on disk with nothing saying they existed.

    ``resume`` picks up a run that stopped that way. A ``-revNN`` directory that
    already holds audio is measured rather than re-rendered; one that exists
    without audio is still refused, because a half-staged project is not a take.

    ``markdown_log_file`` is a human-readable mirror of the JSON log. It is
    updated at the same checkpoints, including the failed state, but the JSON
    log remains the machine-readable source of truth.
    """

    if rounds < 1:
        raise ValueError("rounds must be at least 1")
    project_dir = Path(project_dir)
    if not (project_dir / "song_spec.json").is_file():
        raise FileNotFoundError(f"SongSpec not found: {project_dir / 'song_spec.json'}")
    destination_log = Path(log_file) if log_file is not None else project_dir / "revision_log.json"
    _validate_json_destination(destination_log, resume=resume)
    destination_markdown = (
        Path(markdown_log_file) if markdown_log_file is not None else None
    )
    if destination_markdown is not None:
        _validate_markdown_destination(
            destination_markdown,
            json_log=destination_log,
            resume=resume,
        )

    history: list[Round] = []
    stopped = "reached the round limit"

    def save(state: str) -> None:
        current_log = RevisionLog(tuple(history), stopped, state)
        _atomic_write_text(
            destination_log,
            json.dumps(
                current_log.to_dict(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        if destination_markdown is not None:
            _atomic_write_text(destination_markdown, render_markdown(current_log))

    try:
        history.append(_measure(project_dir, 0))
        if on_round is not None:
            on_round(history[0])
        save("revising")

        for index in range(1, rounds + 1):
            current = history[-1]
            if current.planned_action is None:
                stopped = "the review found nothing worth repainting"
                break
            # Repainting cannot shorten the delivered take, so a blocking silence
            # that runs to the end survives every round. Measured: two rounds took
            # it from 4.80 s to 2.02 s and stopped there. Spend no more renders.
            if current.tail_silence_only:
                stopped = (
                    "the only blocking defect is a silent tail, which a repaint "
                    "cannot remove; run trim-tail on this take"
                )
                break

            destination = project_dir.parent / f"{project_dir.name}-rev{index:02d}"
            if destination.exists():
                if not (resume and _has_audio(destination)):
                    stopped = f"{destination.name} already exists; refusing to replace it"
                    break
            else:
                stage_repaint_project(current.project_dir, destination)
                render(destination, current.audio_file)

            outcome = _measure(destination, index)
            history.append(outcome)
            if on_round is not None:
                on_round(outcome)
            save("revising")

            # A round earns another one by fixing something a listener would notice:
            # clearing a blocking defect, or winning more alignment than seed noise.
            cleared = current.blocking > 0 and outcome.blocking == 0
            gained = outcome.alignment - current.alignment
            if outcome.blocking > 0 and not cleared:
                continue
            if not cleared and gained < MIN_GAIN:
                stopped = (
                    f"round {index} changed alignment by {gained:+.2f}, "
                    f"under the {MIN_GAIN:g}-point floor"
                )
                break
    except Exception as error:
        stopped = f"round {len(history)} failed: {type(error).__name__}: {error}"
        save("failed")
        raise

    log = RevisionLog(tuple(history), stopped)
    save("complete")
    return log


def round_summary(round_: Round) -> dict[str, Any]:
    """One round as a stable, machine-readable summary."""

    return {
        "index": round_.index,
        "alignment": round_.alignment,
        "grade": round_.grade,
        "blocking": round_.blocking,
        "warnings": round_.warnings,
        "defect_codes": list(round_.defect_codes),
        "planned_action": round_.planned_action,
        "project": str(round_.project_dir),
        "audio_file": str(round_.audio_file),
        "usable": round_.usable,
    }


def compare_rounds(before: Round, after: Round) -> dict[str, Any]:
    """Before/after deltas between two measured rounds."""

    return {
        "from_index": before.index,
        "to_index": after.index,
        "alignment": round(after.alignment - before.alignment, 2),
        "blocking": after.blocking - before.blocking,
        "warnings": after.warnings - before.warnings,
    }


def describe_comparison(log: RevisionLog) -> list[str]:
    """Round-by-round evidence with deltas between consecutive rounds."""

    if not log.rounds:
        return ["No rounds recorded."]

    lines = [f"{len(log.rounds)} take(s); stopped because {log.stopped_because}"]
    for round_ in log.rounds:
        action = round_.planned_action or "(none)"
        lines.append(f"Round {round_.index}:")
        lines.append(f"  alignment: {round_.alignment:.1f}")
        lines.append(f"  blocking defects: {round_.blocking}")
        lines.append(f"  planned action: {action}")
    for before, after in zip(log.rounds, log.rounds[1:]):
        delta = compare_rounds(before, after)
        lines.append(f"Delta (round {before.index} -> {after.index}):")
        lines.append(f"  alignment: {delta['alignment']:+.1f}")
        lines.append(f"  blocking defects: {delta['blocking']:+d}")
    if log.adopted is None:
        lines.append("Nothing adopted -- these are candidates. Listen before choosing.")
    else:
        lines.append(
            f"Adopted round {log.adopted.round} "
            f"({log.adopted.selection_mode}) at {log.adopted.selected_at}."
        )
    return lines


def describe(log: RevisionLog) -> list[str]:
    """The log as lines to print, ranked, with adoption state explicit."""

    lines = [f"{len(log.rounds)} take(s); stopped because {log.stopped_because}"]
    for item in log.ranked():
        defects = ", ".join(item.defect_codes) or "clean"
        marker = ""
        if log.adopted is not None and log.adopted.round == item.index:
            marker = " [adopted]"
        lines.append(
            f"  [{item.index}] {item.alignment:6.2f} {item.grade:<14} "
            f"{defects:<28} {item.project_dir.name}{marker}"
        )
    if log.adopted is None:
        lines.append("Nothing adopted -- these are candidates. Listen before choosing.")
    else:
        lines.append(
            f"Adopted round {log.adopted.round} "
            f"({log.adopted.selection_mode})."
        )
    return lines


def render_markdown(log: RevisionLog) -> str:
    """A shareable markdown summary of the revision log."""

    lines = [
        "# Revision Log",
        "",
        f"- state: {log.execution_state}",
        f"- stopped because: {log.stopped_because}",
        f"- rounds: {len(log.rounds)}",
        "",
        "```text",
        *describe(log),
        "```",
        "",
    ]
    return "\n".join(lines)


def export_markdown(log: RevisionLog, path: Path) -> None:
    """Write the revision log as markdown to ``path``."""

    _atomic_write_text(path, render_markdown(log))


def revision_log_path(project_dir: Path) -> Path:
    return Path(project_dir) / "revision_log.json"


def load_revision_log(project_dir: Path, *, log_file: Path | None = None) -> RevisionLog:
    """Load a durable revision log; missing or invalid files raise."""

    path = Path(log_file) if log_file is not None else revision_log_path(project_dir)
    if not path.is_file():
        raise FileNotFoundError(f"revision log not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"invalid revision log: {path}") from error
    return revision_log_from_dict(payload, base_dir=Path(project_dir))


def revision_log_from_dict(
    payload: dict[str, Any],
    *,
    base_dir: Path | None = None,
) -> RevisionLog:
    """Rehydrate a RevisionLog, including legacy logs where adopted is null."""

    if not isinstance(payload, dict):
        raise ValueError("revision log must be an object")
    if payload.get("revision_log_version") != REVISION_LOG_VERSION:
        raise ValueError(
            "unsupported revision log version: "
            f"{payload.get('revision_log_version')!r}"
        )
    rounds_payload = payload.get("rounds")
    if not isinstance(rounds_payload, list):
        raise ValueError("revision log rounds must be a list")
    rounds = tuple(
        _round_from_dict(item, base_dir=base_dir) for item in rounds_payload
    )
    adopted = _adoption_from_dict(payload.get("adopted"))
    return RevisionLog(
        rounds=rounds,
        stopped_because=str(payload.get("stopped_because", "")),
        execution_state=str(payload.get("execution_state", "complete")),
        adopted=adopted,
    )


def adopt_revision(
    project_dir: Path,
    round_number: int,
    *,
    reason: str | None = None,
    tags: Sequence[str] | None = None,
    log_file: Path | None = None,
    selected_at: str | None = None,
) -> AdoptionManifest:
    """Record an explicit human selection of one existing revision take.

    Selection is metadata only: source audio, SongSpec, managed MIDI, and every
    revision WAV stay byte-identical.  Scores never populate ``adopted``.
    """

    project_dir = Path(project_dir)
    destination = Path(log_file) if log_file is not None else revision_log_path(project_dir)
    log = load_revision_log(project_dir, log_file=destination)
    selected = _round_by_index(log, round_number)
    tag_values = _normalize_tags(tags)
    reason_value = None if reason is None else str(reason).strip() or None

    verified = _verify_candidate_for_adoption(project_dir, selected)
    audio_sha = verified["audio_sha256"]

    if (
        log.adopted is not None
        and log.adopted.round == round_number
        and log.adopted.selection_mode == SELECTION_MODE_HUMAN
        and _same_annotations(log.adopted, reason_value, tag_values)
    ):
        return AdoptionManifest(
            project_dir=project_dir,
            log_file=destination,
            log=log,
            adoption=log.adopted,
            unchanged=True,
            preference_recorded=False,
        )

    timestamp = selected_at or _utc_now()
    adoption = Adoption(
        round=selected.index,
        project=selected.project_dir.name,
        selected_at=timestamp,
        selection_mode=SELECTION_MODE_HUMAN,
        reason=reason_value,
        tags=tag_values,
        audio_file=str(selected.audio_file),
        audio_sha256=audio_sha,
    )
    updated = RevisionLog(
        rounds=log.rounds,
        stopped_because=log.stopped_because,
        execution_state=log.execution_state,
        adopted=adoption,
    )
    _atomic_write_text(
        destination,
        json.dumps(updated.to_dict(), ensure_ascii=False, indent=2) + "\n",
    )

    comparison = _preference_comparison(log, selected)
    entry = PreferenceEntry(
        source_project=project_dir.name,
        selected_round=selected.index,
        candidate_rounds=tuple(item.index for item in log.rounds),
        rejected_rounds=tuple(
            item.index for item in log.rounds if item.index != selected.index
        ),
        reason=reason_value,
        tags=tag_values,
        comparison=comparison,
        selected_at=timestamp,
        selection_mode=SELECTION_MODE_HUMAN,
        selected_project=selected.project_dir.name,
        audio_sha256=audio_sha,
    )
    record_preference(project_dir, entry)
    return AdoptionManifest(
        project_dir=project_dir,
        log_file=destination,
        log=updated,
        adoption=adoption,
        unchanged=False,
        preference_recorded=True,
    )


def describe_revisions(log: RevisionLog) -> list[str]:
    """Inspection lines for every candidate before a human chooses."""

    if not log.rounds:
        return ["No revision rounds recorded."]
    lines = [
        f"{len(log.rounds)} revision take(s); stopped because {log.stopped_because}",
        f"execution state: {log.execution_state}",
    ]
    if log.adopted is None:
        lines.append("adopted: null")
    else:
        lines.append(
            f"adopted: round {log.adopted.round} "
            f"({log.adopted.project}, {log.adopted.selection_mode})"
        )
        if log.adopted.reason:
            lines.append(f"  reason: {log.adopted.reason}")
        if log.adopted.tags:
            lines.append(f"  tags: {', '.join(log.adopted.tags)}")
    for round_ in log.rounds:
        action = round_.planned_action or "(none)"
        marker = (
            " [adopted]"
            if log.adopted is not None and log.adopted.round == round_.index
            else ""
        )
        lines.append(f"Round {round_.index}{marker}:")
        lines.append(f"  project: {round_.project_dir}")
        lines.append(f"  audio: {round_.audio_file}")
        lines.append(f"  alignment: {round_.alignment:.2f} ({round_.grade})")
        lines.append(f"  blocking: {round_.blocking}")
        lines.append(f"  planned action: {action}")
    for before, after in zip(log.rounds, log.rounds[1:]):
        delta = compare_rounds(before, after)
        lines.append(f"Delta (round {before.index} -> {after.index}):")
        lines.append(f"  alignment: {delta['alignment']:+.2f}")
        lines.append(f"  blocking: {delta['blocking']:+d}")
    return lines


def _round_from_dict(
    payload: dict[str, Any],
    *,
    base_dir: Path | None = None,
) -> Round:
    if not isinstance(payload, dict):
        raise ValueError("revision round must be an object")
    project = _resolve_stored_path(str(payload["project"]), base_dir=base_dir)
    audio = _resolve_stored_path(
        str(payload["audio_file"]),
        base_dir=base_dir,
        sibling_of=project,
    )
    defects = payload.get("defects") or ()
    return Round(
        index=int(payload["index"]),
        project_dir=project,
        alignment=float(payload["alignment"]),
        grade=str(payload["grade"]),
        blocking=int(payload["blocking"]),
        warnings=int(payload.get("warnings", 0)),
        defect_codes=tuple(str(item) for item in defects),
        planned_action=(
            None
            if payload.get("planned_action") is None
            else str(payload["planned_action"])
        ),
        audio_file=audio,
        tail_silence_only=bool(payload.get("tail_silence_only", False)),
    )


def _resolve_stored_path(
    stored: str,
    *,
    base_dir: Path | None = None,
    sibling_of: Path | None = None,
) -> Path:
    path = Path(stored)
    if path.exists():
        return path
    if sibling_of is not None:
        under_sibling = sibling_of / path.name
        if "audio/" in stored.replace("\\", "/"):
            relative = stored.replace("\\", "/").split("audio/", 1)[-1]
            under_sibling = sibling_of / "audio" / relative
        if under_sibling.exists():
            return under_sibling
    if base_dir is not None:
        by_name = base_dir.parent / path.name
        if by_name.exists():
            return by_name
        under_base = base_dir / path.name
        if under_base.exists():
            return under_base
    return path


def _adoption_from_dict(payload: Any) -> Adoption | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("revision adoption must be an object or null")
    mode = str(payload.get("selection_mode", SELECTION_MODE_HUMAN))
    if mode != SELECTION_MODE_HUMAN:
        raise ValueError(f"unsupported selection_mode: {mode!r}")
    tags = payload.get("tags") or ()
    return Adoption(
        round=int(payload["round"]),
        project=str(payload["project"]),
        selected_at=str(payload["selected_at"]),
        selection_mode=mode,
        reason=None if payload.get("reason") is None else str(payload["reason"]),
        tags=tuple(str(item) for item in tags),
        audio_file=(
            None if payload.get("audio_file") is None else str(payload["audio_file"])
        ),
        audio_sha256=(
            None
            if payload.get("audio_sha256") is None
            else str(payload["audio_sha256"])
        ),
    )


def _round_by_index(log: RevisionLog, round_number: int) -> Round:
    for round_ in log.rounds:
        if round_.index == round_number:
            return round_
    available = ", ".join(str(item.index) for item in log.rounds) or "(none)"
    raise ValueError(
        f"revision round {round_number} does not exist; available: {available}"
    )


def _normalize_tags(tags: Sequence[str] | None) -> tuple[str, ...]:
    if not tags:
        return ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in tags:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return tuple(cleaned)


def _same_annotations(
    adoption: Adoption,
    reason: str | None,
    tags: tuple[str, ...],
) -> bool:
    existing_reason = adoption.reason
    existing_tags = tuple(adoption.tags)
    if reason is None and not tags:
        return True
    return existing_reason == reason and existing_tags == tags


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_round_project(project_dir: Path, round_: Round) -> Path:
    """Locate the on-disk project directory for a recorded round."""

    recorded = Path(round_.project_dir)
    if recorded.is_dir():
        return recorded.resolve()
    by_name = project_dir.parent / recorded.name
    if by_name.is_dir():
        return by_name.resolve()
    if round_.index == 0 and project_dir.is_dir():
        return project_dir.resolve()
    expected = (
        project_dir
        if round_.index == 0
        else project_dir.parent / f"{project_dir.name}-rev{round_.index:02d}"
    )
    if expected.is_dir():
        return expected.resolve()
    raise FileNotFoundError(f"revision project not found: {recorded}")


def _verify_candidate_for_adoption(
    project_dir: Path,
    round_: Round,
) -> dict[str, str]:
    """Refuse candidates that are missing, unmeasured, or outside the lineage."""

    project_dir = project_dir.resolve()
    candidate = _resolve_round_project(project_dir, round_)
    expected_name = (
        project_dir.name
        if round_.index == 0
        else f"{project_dir.name}-rev{round_.index:02d}"
    )
    if candidate.name != expected_name:
        raise ValueError(
            f"revision round {round_.index} points at foreign project "
            f"{candidate.name!r}; expected {expected_name!r}"
        )
    if round_.index == 0:
        if candidate != project_dir:
            raise ValueError("round 0 must be the source project itself")
    else:
        stage_path = candidate / "repaint_stage.json"
        if not stage_path.is_file():
            raise ValueError(
                f"revision round {round_.index} is missing repaint provenance: "
                f"{stage_path}"
            )
        try:
            stage = json.loads(stage_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError(
                f"revision round {round_.index} has invalid provenance: {stage_path}"
            ) from error
        source_name = stage.get("source_project")
        if not isinstance(source_name, str) or not source_name:
            raise ValueError(
                f"revision round {round_.index} provenance lacks source_project"
            )
        # Lineage must chain back toward the source project name.
        allowed = {project_dir.name}
        for index in range(1, round_.index):
            allowed.add(f"{project_dir.name}-rev{index:02d}")
        if source_name not in allowed:
            raise ValueError(
                f"revision round {round_.index} provenance source "
                f"{source_name!r} is outside the revision lineage"
            )
        source_sha = stage.get("source_song_spec_sha256")
        spec_path = candidate / "song_spec.json"
        if isinstance(source_sha, str) and spec_path.is_file():
            from .models import SongSpec
            from .repaint_planner import song_spec_sha256

            actual_spec_sha = song_spec_sha256(
                SongSpec.from_json(spec_path.read_text(encoding="utf-8"))
            )
            if actual_spec_sha != source_sha:
                raise ValueError(
                    f"revision round {round_.index} SongSpec SHA does not match "
                    "repaint provenance"
                )

    if not _has_audio(candidate):
        raise FileNotFoundError(
            f"revision round {round_.index} has no audio "
            f"(missing or half-staged): {candidate / 'audio'}"
        )

    audio = Path(round_.audio_file)
    if not audio.is_file():
        analysis_path = candidate / "audio_analysis.json"
        if analysis_path.is_file():
            try:
                analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
                audio = candidate / analysis["audio_file"]
            except (OSError, ValueError, KeyError, TypeError):
                audio = Path()
        if not audio.is_file():
            # Fall back to the first WAV under audio/.
            audio_dir = candidate / "audio"
            wavs = sorted(audio_dir.glob("*.wav")) if audio_dir.is_dir() else []
            if not wavs:
                raise FileNotFoundError(
                    f"revision round {round_.index} audio not found: "
                    f"{round_.audio_file}"
                )
            audio = wavs[0]

    if not _analysis_is_current(candidate):
        raise ValueError(
            f"revision round {round_.index} is unmeasured or its analysis SHA "
            "does not match the audio on disk"
        )

    return {"audio_sha256": _file_sha256(audio), "audio_file": str(audio)}


def _preference_comparison(log: RevisionLog, selected: Round) -> dict[str, Any]:
    baseline = next((item for item in log.rounds if item.index == 0), None)
    if baseline is None or baseline.index == selected.index:
        alternatives = [item for item in log.ranked() if item.index != selected.index]
        baseline = alternatives[0] if alternatives else None
    if baseline is None:
        return {
            "baseline_round": None,
            "alignment_delta": 0.0,
            "blocking_delta": 0,
            "warnings_delta": 0,
        }
    delta = compare_rounds(baseline, selected)
    return {
        "baseline_round": baseline.index,
        "alignment_delta": delta["alignment"],
        "blocking_delta": delta["blocking"],
        "warnings_delta": delta["warnings"],
    }


def _validate_json_destination(path: Path, *, resume: bool) -> None:
    """Keep a fresh run from erasing the account of an earlier one."""

    if not path.exists():
        return
    if not resume:
        raise FileExistsError(
            f"refusing to overwrite revision log: {path} (use --resume to continue it)"
        )
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise FileExistsError(
            f"refusing to overwrite non-revision file: {path}"
        ) from error
    if (
        not isinstance(existing, dict)
        or existing.get("revision_log_version") != REVISION_LOG_VERSION
        or not isinstance(existing.get("rounds"), list)
    ):
        raise FileExistsError(f"refusing to overwrite non-revision file: {path}")


def _validate_markdown_destination(
    path: Path,
    *,
    json_log: Path,
    resume: bool,
) -> None:
    """Refuse to turn an existing project input into a revision summary."""

    if path.resolve() == json_log.resolve():
        raise ValueError("the Markdown log must not replace revision_log.json")
    if not path.exists():
        return
    if not resume:
        raise FileExistsError(
            f"refusing to overwrite Markdown log: {path} "
            "(use --resume only for an existing KIHACHI revision log)"
        )
    try:
        existing = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise FileExistsError(
            f"refusing to overwrite non-revision file: {path}"
        ) from error
    if not existing.startswith("# Revision Log\n"):
        raise FileExistsError(f"refusing to overwrite non-revision file: {path}")


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace the log in one step, so an interrupted write cannot truncate it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise
