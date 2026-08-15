"""Rank takes by what actually separates them, and say what it could not judge.

`report` already ranks candidates, on `(blocking defect, alignment score)`. That
answers "which take scores highest" and nothing else: it cannot say *why* one
take won, and it cannot tell a 0.3-point lead from a 20-point one. Both matter
once there are six takes of the same design and the generator's own instructions
are known not to steer the result -- picking well is the remaining lever.

Two rules keep this honest.

**A dimension that does not vary cannot decide anything.** Review v0.1 spent a
weight of 0.45 on `key` and `chords`; measured across the 25 reviewed takes in
`example_output`, `key` was the constant 0.350 in every one and `chords` never
left 0.000-0.098. Review v0.4 dropped both. That was found by looking at the
spread afterwards, so the check belongs in the code: every dimension is measured
across *this* set of candidates first, and one that stays flat is reported as
flat and excluded from the reasons, whatever weight it nominally carries.

**A lead smaller than the floor is not a lead.** When the top two are within
`MARGIN_FLOOR`, this says they are indistinguishable on what it can measure
rather than naming a winner by the third decimal.

Nothing here adopts, renders, copies or edits. It writes one JSON file and
prints the `decide` command to run; the reason for the choice still has to come
from someone who has listened, because timbre, vocal delivery and whether the
take is any good are not in these numbers at all.

Pure and stdlib-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .report import Candidate, load_candidate

SHORTLIST_VERSION = "0.1"
SHORTLIST_NAME = "take_shortlist.json"

SPREAD_FLOOR = 0.05
"""How far a dimension must range across the candidates to be allowed to decide.

A declared floor, not a measured noise figure -- 5% of each component's 0..1
scale. It is recorded in the output so a later measurement can replace it with
a real one.
"""

MARGIN_FLOOR = 3.0
"""Points (out of 100) the leader needs over the runner-up to be called ahead.

Below this the recommendation is "listen to both", which is the truthful answer
when the deciding dimensions are ratios estimated off one render each.
"""

UNJUDGED = (
    "timbre and sound quality",
    "vocal delivery and intelligibility",
    "whether the take is musically interesting",
    "whether the harmony sounds right (the mix cannot be asked; see midi-review)",
)
"""Named here so the output cannot be mistaken for a verdict on the music."""


@dataclass(frozen=True)
class Dimension:
    """One comparable number, across every eligible candidate."""

    name: str
    weight: float
    values: dict[str, float]
    quantum: float | None = None
    """Smallest step this dimension can take, when it is a count over a plan.

    `section_boundaries` is a recall over the boundaries the SongSpec planned:
    with three of them it can only be 0, 1/3, 2/3 or 1. Its smallest non-zero
    spread is therefore 0.333 -- six times `SPREAD_FLOOR`, so the flatness check
    can never catch it, and once weights are renormalised a single boundary call
    that landed a bar late can swing the ranking by a third of its range.
    Recording the step is what lets that be said out loud.
    """

    @property
    def spread(self) -> float:
        return max(self.values.values()) - min(self.values.values()) if self.values else 0.0

    @property
    def evidence(self) -> str | None:
        """`single_step` when one detector call, flipped, would erase the gap."""

        if self.quantum is None or not self.decides:
            return None
        return "single_step" if self.spread <= self.quantum * 1.001 else "multi_step"

    @property
    def standing(self) -> str:
        if not self.values:
            return "missing"
        if self.spread == 0.0:
            return "constant"
        return "deciding" if self.spread >= SPREAD_FLOOR else "flat"

    @property
    def decides(self) -> bool:
        return self.standing == "deciding"


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float
    parts: dict[str, float]


def _tie_break(scored: Sequence[Scored], tied: Sequence[str]) -> dict[str, Any] | None:
    """The cleanest of the takes the score could not separate, when there is one.

    Deliberately not part of the score. A warning-level defect is a measurement
    near a threshold -- a repaint has been seen to move a click rather than
    remove it -- so it is not worth points. It is worth mentioning when the
    numbers have already run out.
    """

    if len(tied) < 2:
        return None
    band = [item for item in scored if item.candidate.name in set(tied)]
    counts = {item.candidate.name: len(item.candidate.defects) for item in band}
    fewest = min(counts.values())
    cleanest = sorted(name for name, count in counts.items() if count == fewest)
    if len(cleanest) != 1 or fewest == max(counts.values()):
        return None
    return {
        "basis": "fewest_warning_defects",
        "name": cleanest[0],
        "warning_counts": dict(sorted(counts.items())),
        "note": "a tiebreak among takes the score could not separate, not part of the score",
    }


@dataclass(frozen=True)
class ShortlistManifest:
    project_dir: Path
    shortlist_file: Path | None
    shortlist: dict[str, Any]


def _spec_identity(project_dir: Path) -> str | None:
    """Canonical hash of the design, so only takes of one design are compared.

    ADR-0005 allows comparison against a baseline "only with an identical
    SongSpec". The same rule has to hold for six candidates as for two.
    """

    spec_path = Path(project_dir) / "song_spec.json"
    if not spec_path.is_file():
        return None
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _components(project_dir: Path) -> dict[str, tuple[float, float]]:
    """Every scored component of one take, as `name -> (score, weight)`.

    Audio components keep their own names; the MIDI ones are prefixed. The keys
    are read from the file rather than hard-coded: review v0.1 wrote six audio
    components and v0.4 writes four, and a shortlist that assumed either would
    quietly compare nothing.
    """

    review = json.loads((Path(project_dir) / "generation_review.json").read_text(encoding="utf-8"))
    found: dict[str, tuple[float, float]] = {}
    for prefix, payload in (
        ("", review.get("alignment")),
        ("midi:", (review.get("midi_alignment") or {}).get("alignment")),
    ):
        for name, item in ((payload or {}).get("components") or {}).items():
            try:
                found[f"{prefix}{name}"] = (float(item["score"]), float(item["weight"]))
            except (KeyError, TypeError, ValueError):
                continue
    return found


def _quanta(project_dir: Path) -> dict[str, float]:
    """Step sizes for the dimensions that are counts over a plan.

    Read from the analysis rather than assumed: the number of planned boundaries
    is a property of this song's arrangement, so the step is 1/3 for a four-part
    arrangement and 1/8 for the five-minute one.
    """

    analysis_path = Path(project_dir) / "audio_analysis.json"
    if not analysis_path.is_file():
        return {}
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    planned = (analysis.get("sections") or {}).get("planned_boundaries_after_bar") or []
    return {"section_boundaries": 1.0 / len(planned)} if planned else {}


TRIMMED_MARKER = ".tail-trimmed."


def _is_trimmed(candidate: Candidate) -> bool:
    """Whether the take that was analysed is a tail-trimmed copy.

    This matters because `duration` scores distance from the design length and
    saturates at zero two seconds out. Trimming a silent tail makes the take
    genuinely shorter, so a trimmed take loses duration points to an untrimmed
    one -- measured here at a full 1.000 of spread across five re-rolls where
    four had been trimmed and one had not. Comparing a mixed set ranks the
    trimming, not the music.
    """

    return candidate.audio_file is not None and TRIMMED_MARKER in candidate.audio_file.name


def _exclusion(candidate: Candidate, identity: str | None, base_identity: str | None) -> str | None:
    if identity is None or identity != base_identity:
        return "different_song_spec"
    if not candidate.scanned:
        return "not_scanned"
    if candidate.blocking:
        return "blocking_defect"
    return None


def build_shortlist(
    project_dir: Path,
    candidate_projects: Sequence[Path] = (),
) -> dict[str, Any]:
    """Rank the candidates and record what decided it. Reads only."""

    project_dir = Path(project_dir)
    requested = [project_dir, *(Path(item) for item in candidate_projects)]
    unique: list[Path] = []
    seen: set[Path] = set()
    for item in requested:
        identity = item.resolve()
        if identity not in seen:
            unique.append(item)
            seen.add(identity)

    base_identity = _spec_identity(project_dir)
    loaded = [(path, load_candidate(path)) for path in unique]
    identities = {path: _spec_identity(path) for path, _ in loaded}

    eligible: list[Candidate] = []
    excluded: list[dict[str, Any]] = []
    for path, candidate in loaded:
        reason = _exclusion(candidate, identities[path], base_identity)
        if reason is None:
            eligible.append(candidate)
        else:
            excluded.append(
                {
                    "project": str(path),
                    "name": candidate.name,
                    "reason": reason,
                    "defects": [item["code"] for item in candidate.defects],
                }
            )

    per_take = {item.name: _components(item.project_dir) for item in eligible}
    quanta = _quanta(project_dir)
    names = sorted({name for found in per_take.values() for name in found})
    dimensions: list[Dimension] = []
    for name in names:
        values = {
            take: found[name][0] for take, found in per_take.items() if name in found
        }
        if len(values) != len(eligible):
            # Present on some takes only: comparing it would rank on whether a
            # file happened to exist, not on the music.
            dimensions.append(Dimension(name=name, weight=0.0, values={}))
            continue
        weight = max(found[name][1] for found in per_take.values() if name in found)
        dimensions.append(
            Dimension(
                name=name,
                weight=weight,
                values=values,
                quantum=quanta.get(name),
            )
        )

    deciding = [item for item in dimensions if item.decides]
    total_weight = sum(item.weight for item in deciding)
    scored: list[Scored] = []
    for candidate in eligible:
        parts = {
            item.name: item.values[candidate.name] * item.weight / total_weight * 100.0
            for item in deciding
        } if total_weight else {}
        scored.append(
            Scored(candidate=candidate, score=round(sum(parts.values()), 2), parts=parts)
        )
    scored.sort(key=lambda item: (-item.score, item.candidate.name))

    margin = round(scored[0].score - scored[1].score, 2) if len(scored) > 1 else None
    if not scored:
        verdict, reason = "nothing_to_rank", "no_eligible_take"
    elif len(scored) == 1:
        verdict, reason = "single_candidate", "only_one_comparable_take"
    elif not deciding:
        # Every take measures the same. That is not "cannot rank" -- it is the
        # strongest form of a tie, and the takes still have to be told apart by
        # ear, so it lands in the same verdict rather than in a dead end.
        verdict, reason = "too_close_to_call", "no_deciding_dimension"
    elif margin is not None and margin < MARGIN_FLOOR:
        verdict, reason = "too_close_to_call", "margin_under_floor"
    else:
        verdict, reason = "recommended", "clear_margin"

    recommended = scored[0].candidate.name if verdict == "recommended" else None
    tied = (
        [item.candidate.name for item in scored if scored[0].score - item.score < MARGIN_FLOOR]
        if verdict == "too_close_to_call"
        else []
    )
    tie_break = _tie_break(scored, tied)

    trimmed = sorted(item.name for item in eligible if _is_trimmed(item))
    mixed_trim = (
        {
            "trimmed": trimmed,
            "untrimmed": sorted(
                item.name for item in eligible if not _is_trimmed(item)
            ),
            "note": (
                "some takes were tail-trimmed and some were not; `duration` measures "
                "distance from the design length, so the trimmed ones lose points "
                "there for a cut that removed silence. Trim all of them or none"
            ),
        }
        if 0 < len(trimmed) < len(eligible)
        else None
    )

    return {
        "shortlist_version": SHORTLIST_VERSION,
        "scope": "ranks_takes_on_measured_alignment_only_adopts_nothing",
        "project": project_dir.name,
        "song_spec_sha256": base_identity,
        "spread_floor": SPREAD_FLOOR,
        "spread_floor_meaning": (
            "declared floor on each 0..1 component, not a measured noise figure; "
            "a dimension flatter than this is reported and not used"
        ),
        "margin_floor": MARGIN_FLOOR,
        "verdict": verdict,
        "verdict_reason": reason,
        "recommended": recommended,
        "margin": margin,
        "tied_with": tied,
        "tie_break": tie_break,
        "mixed_tail_trim": mixed_trim,
        "deciding_dimension_count": len(deciding),
        "ranking": [
            {
                "position": position,
                "name": item.candidate.name,
                "project": str(item.candidate.project_dir),
                "score": item.score,
                "audio_alignment": item.candidate.alignment,
                "grade": item.candidate.grade,
                "warnings": [entry["code"] for entry in item.candidate.defects],
                "contributions": {
                    name: round(value, 2) for name, value in sorted(item.parts.items())
                },
            }
            for position, item in enumerate(scored, start=1)
        ],
        "dimensions": [
            {
                "name": item.name,
                "standing": item.standing,
                "weight": item.weight,
                "spread": round(item.spread, 4),
                "quantum": None if item.quantum is None else round(item.quantum, 4),
                "evidence": item.evidence,
                "values": {take: round(value, 4) for take, value in sorted(item.values.items())},
            }
            for item in dimensions
        ],
        "excluded": excluded,
        "not_judged": list(UNJUDGED),
        "next_step": (
            "listen to the ranked takes, then record the choice with `kihachi decide`; "
            "this file is evidence for that decision, not the decision"
        ),
    }


def write_shortlist(
    project_dir: Path,
    candidate_projects: Sequence[Path] = (),
    *,
    overwrite: bool = False,
) -> ShortlistManifest:
    project_dir = Path(project_dir)
    shortlist = build_shortlist(project_dir, candidate_projects)
    destination = project_dir / SHORTLIST_NAME
    if destination.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite shortlist: {destination}")
    _atomic_write_text(
        destination, json.dumps(shortlist, indent=2, ensure_ascii=False) + "\n"
    )
    return ShortlistManifest(
        project_dir=project_dir, shortlist_file=destination, shortlist=shortlist
    )


def describe(shortlist: dict[str, Any]) -> list[str]:
    """The shortlist as lines to print, with nothing adopted."""

    lines = [f"KIHACHI take shortlist for {shortlist['project']}:"]
    for row in shortlist["ranking"]:
        warnings = ", ".join(row["warnings"]) or "clean"
        lines.append(
            f"  #{row['position']} {row['score']:6.2f}  {row['grade']:<14} "
            f"{warnings:<26} {row['name']}"
        )

    deciding = [item for item in shortlist["dimensions"] if item["standing"] == "deciding"]
    ignored = [item for item in shortlist["dimensions"] if item["standing"] in {"constant", "flat"}]
    if deciding:
        lines.append(
            "- decided on: "
            + ", ".join(f"{item['name']} (spread {item['spread']:.3f})" for item in deciding)
        )
    if ignored:
        lines.append(
            "- identical across these takes, so not used: "
            + ", ".join(item["name"] for item in ignored)
        )

    verdict = shortlist["verdict"]
    if verdict == "recommended":
        lines.append(
            f"- ahead: {shortlist['recommended']} by {shortlist['margin']:.2f} points"
        )
    elif verdict == "too_close_to_call" and shortlist["verdict_reason"] == "no_deciding_dimension":
        lines.append(
            "- nothing measurable separates these takes: every dimension is identical "
            "across all of them. The numbers cannot choose here; ears can"
        )
    elif verdict == "too_close_to_call":
        lines.append(
            "- too close to call: "
            + ", ".join(shortlist["tied_with"])
            + f" are within {shortlist['margin_floor']:.1f} points; listen to all of them"
        )
    elif verdict == "single_candidate":
        lines.append("- only one comparable take; there is nothing to rank it against")
    else:
        lines.append("- no eligible take to rank; every candidate was excluded above")

    if shortlist["mixed_tail_trim"] is not None:
        mixed = shortlist["mixed_tail_trim"]
        lines.append(
            f"- confounded: {len(mixed['trimmed'])} of "
            f"{len(mixed['trimmed']) + len(mixed['untrimmed'])} takes are tail-trimmed "
            f"(untrimmed: {', '.join(mixed['untrimmed'])}). {mixed['note']}"
        )
    for item in deciding:
        if item["evidence"] == "single_step":
            steps = round(1.0 / item["quantum"])
            lines.append(
                f"- weak evidence: {item['name']} separates these takes by one step "
                f"of {steps} ({item['spread']:.3f}); one detector call landing a bar "
                "late would erase it"
            )
    if shortlist["deciding_dimension_count"] == 1 and deciding:
        # A one-dimensional ranking prints two decimals it has not earned.
        lines.append(
            f"- caution: this is a ranking on {deciding[0]['name']} alone; "
            "every other dimension is identical across these takes"
        )
    if shortlist["tie_break"] is not None:
        lines.append(
            f"- cleanest of the tied takes: {shortlist['tie_break']['name']} "
            f"({shortlist['tie_break']['note']})"
        )

    for item in shortlist["excluded"]:
        lines.append(f"- excluded {item['name']}: {item['reason']}")
    lines.append("- not judged here: " + "; ".join(shortlist["not_judged"]))
    lines.append("- adopts nothing. Listen, then run:")
    lines.append(f"    {decide_command(shortlist)}")
    return lines


def decide_command(shortlist: dict[str, Any]) -> str:
    """The `decide` call to run after listening, with the reason left blank.

    Printed rather than run. The reason is the whole point of the decision log,
    and this module has not heard anything.
    """

    ranking = shortlist["ranking"]
    if not ranking:
        return "kihachi decide <project> --selected <take> --reason '<why, after listening>'"
    base = ranking[0]["project"]
    others = " ".join(f"--also {row['project']}" for row in ranking[1:])
    # Only fill in a take when one is actually ahead. Pre-filling the leader of
    # a tie would hand back, as a ready-to-run command, the choice this refused
    # to make one line earlier.
    chosen = shortlist["recommended"] if shortlist["verdict"] == "recommended" else None
    selected = next(
        (row["project"] for row in ranking if row["name"] == chosen), "<take>"
    )
    parts = ["kihachi decide", base]
    if others:
        parts.append(others)
    parts.append(f"--selected {selected}")
    parts.append("--reason '<why, after listening>'")
    return " ".join(parts)


def _atomic_write_text(path: Path, content: str) -> None:
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as sink:
            sink.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
