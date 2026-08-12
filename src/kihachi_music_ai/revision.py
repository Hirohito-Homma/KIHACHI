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
the caller explicitly names one.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .analyzer import analyze_project
from .repaint_planner import stage_repaint_project
from .reviewer import review_project

REVISION_LOG_VERSION = "0.1"
DEFAULT_ROUNDS = 3
MIN_GAIN = 1.0
"""Points of alignment a round has to win to count as progress.

Not zero: re-rendering the same settings with a different seed moves this score
by tens of points, so a fraction of a point is noise wearing a result's clothes.
"""


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
    adopted: None = field(default=None, init=False)

    def ranked(self) -> tuple[Round, ...]:
        """Best first: usable takes above unusable ones, then by alignment.

        A take with a hole in it does not win on points. That ordering is the
        whole reason defects are measured separately from conformance -- the
        baseline scores 88.69 "aligned" with 2.28 s of silence in it.
        """

        return tuple(sorted(self.rounds, key=lambda r: (not r.usable, -r.alignment)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_log_version": REVISION_LOG_VERSION,
            "execution_state": self.execution_state,
            "stopped_because": self.stopped_because,
            "rounds": [item.to_dict() for item in self.rounds],
            "ranking": [item.index for item in self.ranked()],
            "adopted": None,
            "adoption_note": (
                "Nothing was adopted. These are candidates: the alignment score "
                "measures whether a take followed the SongSpec, not whether it "
                "sounds good, and a seed change alone moves it by tens of points."
            ),
        }


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


def describe(log: RevisionLog) -> list[str]:
    """The log as lines to print, ranked, with nothing adopted."""

    lines = [f"{len(log.rounds)} take(s); stopped because {log.stopped_because}"]
    for item in log.ranked():
        defects = ", ".join(item.defect_codes) or "clean"
        lines.append(
            f"  [{item.index}] {item.alignment:6.2f} {item.grade:<14} "
            f"{defects:<28} {item.project_dir.name}"
        )
    lines.append("Nothing adopted -- these are candidates. Listen before choosing.")
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
