"""VS8 — Human-gated Ableton repair planning.

Consumes a VS7 ``ableton_verification.json`` and writes a provenance-checked
repair plan that a human can review.  Never talks to Ableton Live, never
invokes AbletonGPT, never re-runs apply/verify, and never adopts a take.

Architectural contract preserved:

    KIHACHI Music AI = decides what should exist
    AbletonGPT       = reads/writes Ableton Live
    VS8              = turns failed checks into a reviewable plan, not a repair

``candidate_reapply`` means the failed check maps uniquely onto an adopted
arrangement operation.  It does not mean that operation is safe to execute.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .ableton_execution import AbletonExecutionError, load_validated_handoff
from .ableton_verification import (
    ABLETON_VERIFICATION_NAME,
    ABLETON_VERIFICATION_VERSION,
    CHECK_FAIL,
    CHECK_NOT_OBSERVABLE,
    CHECK_PASS,
    MAX_NOTES_IN_MANIFEST,
    STATE_FAILED,
    STATE_NOT_RUN,
    STATE_PARTIAL,
    STATE_VERIFIED,
    AbletonVerificationError,
    ableton_verification_path,
    build_expected_live_state,
    load_verified_execution,
)

ABLETON_REPAIR_PLAN_VERSION = "0.1"
ABLETON_REPAIR_PLAN_NAME = "ableton_repair_plan.json"
SUPPORTED_VERIFICATION_VERSIONS = frozenset({ABLETON_VERIFICATION_VERSION})
SUPPORTED_REPAIR_PLAN_VERSIONS = frozenset({ABLETON_REPAIR_PLAN_VERSION})

STATE_CANDIDATES_READY = "repair_candidates_ready"
STATE_MANUAL_REQUIRED = "manual_action_required"
REPAIRABLE_STATES = frozenset({STATE_FAILED, STATE_PARTIAL})

DISPOSITION_CANDIDATE = "candidate_reapply"
DISPOSITION_MANUAL = "manual_inspection"

KNOWN_CHECK_STATUSES = frozenset({CHECK_PASS, CHECK_FAIL, CHECK_NOT_OBSERVABLE})
KNOWN_CHECK_CATEGORIES = frozenset(
    {"tempo", "tracks", "devices", "clips", "arrangement"}
)
DEVICE_OPS = frozenset(
    {"apply_live_instrument_selection", "apply_live_drum_kit"}
)

DEVICE_ID_RE = re.compile(r"^device:(\d+)$")
SESSION_CLIP_ID_RE = re.compile(r"^session_clip:(\d+):(\d+)$")
ARRANGEMENT_ID_RE = re.compile(r"^arrangement:(\d+)$")
TRACK_ID_RE = re.compile(r"^track:(\d+)$")

_SEMANTIC_EXCLUDED_KEYS = frozenset({"created_at"})


class AbletonRepairPlanError(ValueError):
    """Actionable refusal before a repair plan is written."""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class AbletonRepairPlanManifest:
    project_dir: Path
    repair_plan_file: Path
    document: dict[str, Any]
    unchanged: bool = False

    @property
    def repair_state(self) -> str:
        return str(self.document.get("repair_state", ""))


@dataclass(frozen=True)
class ValidatedRepairPlan:
    """Existing ``ableton_repair_plan.json`` re-checked against current sources."""

    project_dir: Path
    repair_plan_file: Path
    repair_plan: dict[str, Any]
    repair_plan_sha256: str
    verification_file: Path
    verification: dict[str, Any]
    arrangement_plan_file: Path
    arrangement_plan: dict[str, Any]


def ableton_repair_plan_path(project_dir: Path) -> Path:
    return Path(project_dir) / ABLETON_REPAIR_PLAN_NAME


def source_operation_view(operation: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical identifying view of an arrangement operation (VS8 contract)."""

    return _source_operation_view(operation)


def load_validated_repair_plan(project_dir: Path) -> ValidatedRepairPlan:
    """Load an existing repair plan and re-verify it.  Writes nothing.

    Rebuilds the semantic plan from the current verification using the same
    classification as ``build_ableton_repair_plan``.  Does not talk to Live
    or AbletonGPT.
    """

    root = _require_project_dir(project_dir)
    destination = ableton_repair_plan_path(root)
    if not destination.is_file():
        raise AbletonRepairPlanError(
            f"No Ableton repair plan found: {destination}. "
            "Run `kihachi ableton-repair-plan PROJECT` first. VS9 does not "
            "invent a repair plan or execute from a missing one."
        )
    existing = _read_repair_plan_file(destination)
    composed = _compose_repair_plan(root)
    if not _repair_plan_equivalent(
        existing,
        composed.document,
        current_verification_sha256=composed.document["source"]["verification"]["sha256"],
    ):
        raise AbletonRepairPlanError(
            "Ableton repair plan is stale or does not match the plan rebuilt "
            "from the current verification, source SHA digests, adopted round, "
            "expected Live state, and candidate/manual classification. "
            "Re-run `kihachi ableton-repair-plan PROJECT` (with --overwrite "
            "if the plan must be replaced). Refusing execution."
        )
    return ValidatedRepairPlan(
        project_dir=root,
        repair_plan_file=destination.resolve(),
        repair_plan=existing,
        repair_plan_sha256=_file_sha256(destination),
        verification_file=composed.verification_file,
        verification=composed.verification,
        arrangement_plan_file=composed.loaded.arrangement_plan_file,
        arrangement_plan=composed.loaded.arrangement_plan,
    )


def build_ableton_repair_plan(
    project_dir: Path,
    *,
    overwrite: bool = False,
) -> AbletonRepairPlanManifest:
    """Convert a VS7 verification artifact into a human-gated repair plan.

    Does not talk to Live, does not invoke AbletonGPT, and does not modify
    source artifacts.  ``candidate_reapply`` is a unique mapping, not a
    permission to execute.
    """

    root = _require_project_dir(project_dir)
    verification_file = ableton_verification_path(root)
    if not verification_file.is_file():
        raise AbletonRepairPlanError(
            f"No Ableton verification found: {verification_file}. "
            "Run `kihachi ableton-verify PROJECT` first. VS8 does not invent "
            "Live postconditions or repair a Set."
        )

    fingerprints = _source_fingerprints(root, verification_file)
    composed = _compose_repair_plan(root)
    document = composed.document
    destination = ableton_repair_plan_path(root)
    unchanged = False
    _assert_unchanged(fingerprints)
    if destination.exists() and not overwrite:
        existing = _load_existing_repair_plan(destination)
        if _repair_plan_equivalent(
            existing,
            document,
            current_verification_sha256=document["source"]["verification"]["sha256"],
        ):
            unchanged = True
            document = existing
        else:
            raise AbletonRepairPlanError(
                f"refusing to overwrite Ableton repair plan with different "
                f"content: {destination} (use --overwrite to replace the plan "
                "only; source artifacts stay unchanged)"
            )
    else:
        _atomic_write_json(destination, document)
        _assert_unchanged(fingerprints)

    return AbletonRepairPlanManifest(
        project_dir=root,
        repair_plan_file=destination,
        document=document,
        unchanged=unchanged,
    )


def describe_ableton_repair_plan(manifest: AbletonRepairPlanManifest) -> list[str]:
    """Concise summary lines for the CLI.  Never claims Live was repaired."""

    state = manifest.repair_state
    heading = {
        STATE_CANDIDATES_READY: "REPAIR CANDIDATES READY",
        STATE_MANUAL_REQUIRED: "MANUAL ACTION REQUIRED",
    }.get(state, state.upper())
    root = manifest.project_dir
    source = manifest.document.get("source") or {}
    summary = manifest.document.get("summary") or {}
    verification = source.get("verification") or {}
    lines = [
        f"Ableton repair plan: {heading}",
        f"Adopted round: {source.get('adopted_round')}",
        f"Verification: {verification.get('path', ABLETON_VERIFICATION_NAME)}",
        f"Candidate reapply actions: {summary.get('candidate_actions', 0)}",
        f"Manual actions: {summary.get('manual_actions', 0)}",
        f"Repair plan: {_relpath(manifest.repair_plan_file, root)}",
    ]
    if manifest.unchanged:
        lines.append(
            "Repair plan unchanged (identical verification SHA and plan already on disk)"
        )
    for action in manifest.document.get("candidate_actions") or []:
        lines.append(_describe_candidate(action))
    for action in manifest.document.get("manual_actions") or []:
        lines.append(_describe_manual(action))
    lines.append("- Live access: none")
    lines.append("- Live mutation: no")
    lines.append("- AbletonGPT invoked: no")
    lines.append("- auto-execute: no")
    lines.append("- auto-verify: no")
    lines.append("- adoption unchanged: yes")
    lines.append("- preference memory appended: no")
    return lines


@dataclass(frozen=True)
class _ComposedRepairPlan:
    document: dict[str, Any]
    verification_file: Path
    verification: dict[str, Any]
    loaded: Any


def _require_project_dir(project_dir: Path) -> Path:
    root = Path(project_dir).resolve()
    if not root.is_dir():
        raise AbletonRepairPlanError(f"project not found: {root}")
    return root


def _compose_repair_plan(project_dir: Path) -> _ComposedRepairPlan:
    """Rebuild the semantic repair plan from current verification.  Writes nothing."""

    root = Path(project_dir).resolve()
    verification_file = ableton_verification_path(root)
    if not verification_file.is_file():
        raise AbletonRepairPlanError(
            f"No Ableton verification found: {verification_file}. "
            "Run `kihachi ableton-verify PROJECT` first. VS8 does not invent "
            "Live postconditions or repair a Set."
        )
    verification = _load_verification_document(verification_file)
    _require_repairable_verification(verification, verification_file)

    try:
        validated = load_validated_handoff(root)
    except AbletonExecutionError as error:
        raise AbletonRepairPlanError(
            f"{error} Refusing repair planning until the VS5 handoff is valid."
        ) from error

    try:
        loaded = load_verified_execution(root)
    except AbletonVerificationError as error:
        raise AbletonRepairPlanError(
            f"{error} Refusing repair planning until the VS6 execution "
            "receipt is valid."
        ) from error
    except FileNotFoundError as error:
        raise AbletonRepairPlanError(str(error)) from error

    source_files = _validate_verification_sources(
        verification,
        project_dir=root,
        validated=validated,
        loaded=loaded,
    )
    expected = build_expected_live_state(
        loaded.arrangement_plan, job_plan=loaded.job_plan
    )
    _assert_expected_current(verification, expected)
    checks = _require_checks(verification)
    operations = _arrangement_operations(loaded.arrangement_plan)
    passed, candidates, manuals = _classify_checks(checks, operations)
    document = _build_repair_document(
        root,
        verification=verification,
        verification_file=verification_file,
        verification_sha256=_file_sha256(verification_file),
        source_files=source_files,
        loaded=loaded,
        passed=passed,
        candidates=candidates,
        manuals=manuals,
    )
    return _ComposedRepairPlan(
        document=document,
        verification_file=verification_file.resolve(),
        verification=verification,
        loaded=loaded,
    )


def _read_repair_plan_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError) as error:
        raise AbletonRepairPlanError(
            f"Unable to read Ableton repair plan: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise AbletonRepairPlanError(
            f"Ableton repair plan is not valid JSON: {path} ({error.msg}). "
            "Expected a VS8 ableton_repair_plan.json object."
        ) from error
    if not isinstance(payload, dict):
        raise AbletonRepairPlanError(
            f"Ableton repair plan must be a JSON object: {path}"
        )
    version = payload.get("ableton_repair_plan_version")
    if not isinstance(version, str) or version not in SUPPORTED_REPAIR_PLAN_VERSIONS:
        raise AbletonRepairPlanError(
            f"Unsupported ableton_repair_plan_version {version!r} in {path} "
            f"(supported: {', '.join(sorted(SUPPORTED_REPAIR_PLAN_VERSIONS))}). "
            "Refusing to load a repair plan."
        )
    state = payload.get("repair_state")
    if state not in {STATE_CANDIDATES_READY, STATE_MANUAL_REQUIRED}:
        raise AbletonRepairPlanError(
            f"Ableton repair_state is {state!r}; expected "
            f"{STATE_CANDIDATES_READY} or {STATE_MANUAL_REQUIRED}. "
            "Refusing to load a repair plan."
        )
    return payload


def _load_verification_document(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError) as error:
        raise AbletonRepairPlanError(
            f"Unable to read Ableton verification: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise AbletonRepairPlanError(
            f"Ableton verification is not valid JSON: {path} ({error.msg}). "
            "Expected a VS7 ableton_verification.json object."
        ) from error
    if not isinstance(payload, dict):
        raise AbletonRepairPlanError(
            f"Ableton verification must be a JSON object: {path}"
        )
    return payload


def _require_repairable_verification(
    verification: Mapping[str, Any], path: Path
) -> None:
    version = verification.get("ableton_verification_version")
    if not isinstance(version, str) or version not in SUPPORTED_VERIFICATION_VERSIONS:
        raise AbletonRepairPlanError(
            f"Unsupported ableton_verification_version {version!r} in {path} "
            f"(supported: {', '.join(sorted(SUPPORTED_VERIFICATION_VERSIONS))}). "
            "Refusing repair planning."
        )
    state = verification.get("verification_state")
    if state == STATE_NOT_RUN:
        raise AbletonRepairPlanError(
            "Ableton verification state is not_run: there is no Live evidence "
            "to plan from. Run `kihachi ableton-verify PROJECT` until it "
            "produces failed or partially_verified checks. VS8 does not "
            "fabricate a repair plan without verification evidence."
        )
    if state == STATE_VERIFIED:
        raise AbletonRepairPlanError(
            "Ableton verification state is verified: no repair plan is needed. "
            "Keep the existing ableton-verify result; VS8 does not rewrite a "
            "matching Live audit into a repair."
        )
    if state not in REPAIRABLE_STATES:
        raise AbletonRepairPlanError(
            f"Ableton verification_state is {state!r}; expected "
            f"{STATE_FAILED} or {STATE_PARTIAL}. Refusing repair planning."
        )


def _validate_verification_sources(
    verification: Mapping[str, Any],
    *,
    project_dir: Path,
    validated: Any,
    loaded: Any,
) -> dict[str, dict[str, str]]:
    source = verification.get("source")
    if not isinstance(source, Mapping):
        raise AbletonRepairPlanError(
            "Ableton verification is missing source provenance. "
            "Refusing repair planning."
        )

    handoff_row = _require_source_row(source.get("handoff"), "handoff")
    receipt_row = _require_source_row(
        source.get("execution_receipt"), "execution receipt"
    )
    plan_row = _require_source_row(
        source.get("arrangement_plan"), "arrangement plan"
    )
    job_row = _require_source_row(source.get("job_plan"), "job plan")

    adopted_round = source.get("adopted_round")
    if not isinstance(adopted_round, int):
        raise AbletonRepairPlanError(
            "Ableton verification is missing a valid source.adopted_round. "
            "Refusing repair planning."
        )
    if adopted_round != loaded.adopted_round or adopted_round != validated.adopted_round:
        raise AbletonRepairPlanError(
            "Adopted round identity mismatch: verification "
            f"adopted_round={adopted_round!r}, handoff "
            f"adopted_round={validated.adopted_round}, execution receipt "
            f"adopted_round={loaded.adopted_round}. Ranking cannot change "
            "the repair-plan target."
        )

    handoff_file = _resolve_project_path(
        handoff_row["path"], project_dir=project_dir, label="handoff"
    )
    receipt_file = _resolve_project_path(
        receipt_row["path"], project_dir=project_dir, label="execution receipt"
    )
    plan_file = _resolve_project_path(
        plan_row["path"], project_dir=project_dir, label="arrangement plan"
    )
    job_file = _resolve_project_path(
        job_row["path"], project_dir=project_dir, label="job plan"
    )

    _require_file(handoff_file, "handoff")
    _require_file(receipt_file, "execution receipt")
    _require_file(plan_file, "arrangement plan")
    _require_file(job_file, "job plan")

    _assert_source_sha(handoff_file, handoff_row["sha256"], "handoff")
    _assert_source_sha(receipt_file, receipt_row["sha256"], "execution receipt")
    _assert_source_sha(plan_file, plan_row["sha256"], "arrangement plan")
    _assert_source_sha(job_file, job_row["sha256"], "job plan")

    if _file_sha256(handoff_file) != loaded.handoff_sha256:
        raise AbletonRepairPlanError(
            "Handoff SHA-256 mismatch: verification source does not match the "
            "current VS5 handoff on disk. Refusing repair planning."
        )
    if _file_sha256(receipt_file) != loaded.receipt_sha256:
        raise AbletonRepairPlanError(
            "Execution receipt SHA-256 mismatch: verification source does not "
            "match the current VS6 receipt on disk. Refusing repair planning."
        )
    if _file_sha256(plan_file) != loaded.arrangement_plan_sha256:
        raise AbletonRepairPlanError(
            "Arrangement plan SHA-256 mismatch: verification source does not "
            "match the current arrangement plan on disk. Refusing repair planning."
        )
    if loaded.job_plan_sha256 is None or _file_sha256(job_file) != loaded.job_plan_sha256:
        raise AbletonRepairPlanError(
            "Job plan SHA-256 mismatch: verification source does not match "
            "the current AbletonGPT job plan on disk. Refusing repair planning."
        )

    return {
        "handoff": {
            "path": _relpath(handoff_file, project_dir),
            "sha256": loaded.handoff_sha256,
        },
        "execution_receipt": {
            "path": _relpath(receipt_file, project_dir),
            "sha256": loaded.receipt_sha256,
        },
        "arrangement_plan": {
            "path": _relpath(plan_file, project_dir),
            "sha256": loaded.arrangement_plan_sha256,
        },
        "job_plan": {
            "path": _relpath(job_file, project_dir),
            "sha256": loaded.job_plan_sha256,
        },
    }


def _require_source_row(row: Any, label: str) -> dict[str, str]:
    if not isinstance(row, Mapping):
        raise AbletonRepairPlanError(
            f"Ableton verification is missing a valid source.{label.replace(' ', '_')} "
            "object. Refusing repair planning."
        )
    path = row.get("path")
    digest = row.get("sha256")
    if not isinstance(path, str) or not path.strip():
        raise AbletonRepairPlanError(
            f"Ableton verification source {label} path is missing. "
            "Refusing repair planning."
        )
    if not isinstance(digest, str) or not digest.strip():
        raise AbletonRepairPlanError(
            f"Ableton verification source {label} sha256 is missing. "
            "Refusing repair planning."
        )
    return {"path": path, "sha256": digest}


def _resolve_project_path(stored: str, *, project_dir: Path, label: str) -> Path:
    path = Path(stored)
    resolved = path.resolve() if path.is_absolute() else (project_dir / path).resolve()
    try:
        resolved.relative_to(project_dir.parent.resolve())
    except ValueError as error:
        raise AbletonRepairPlanError(
            f"Verification source {label} path escapes the project parent: {stored}"
        ) from error
    return resolved


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise AbletonRepairPlanError(
            f"Verification source {label} is missing: {path}. "
            "Refusing repair planning."
        )


def _assert_source_sha(path: Path, declared: str, label: str) -> None:
    actual = _file_sha256(path)
    if actual != declared:
        raise AbletonRepairPlanError(
            f"{label[0].upper() + label[1:]} SHA-256 mismatch: the file changed "
            f"after ableton_verification.json was written (or does not match "
            f"the recorded digest). Declared {declared}, on disk {actual}. "
            "Refusing repair planning."
        )


def _assert_expected_current(
    verification: Mapping[str, Any], rebuilt: Mapping[str, Any]
) -> None:
    stored = verification.get("expected")
    if not isinstance(stored, Mapping):
        raise AbletonRepairPlanError(
            "Ableton verification is missing expected Live state. "
            "Refusing repair planning."
        )
    if _expected_identity(stored) != _expected_identity(rebuilt):
        raise AbletonRepairPlanError(
            "Verification expected Live state is stale: it does not match the "
            "state rebuilt from the current arrangement plan. Refusing repair "
            "planning. Re-run `kihachi ableton-verify PROJECT` against the "
            "current adopted plan."
        )


def _expected_identity(expected: Mapping[str, Any]) -> dict[str, Any]:
    clips = []
    for clip in _expected_object_list(expected.get("clips"), "clips"):
        notes = _expected_notes(clip)
        clips.append(
            {
                "track_index": clip.get("track_index"),
                "clip_index": clip.get("clip_index"),
                "name": clip.get("name"),
                "length_beats": clip.get("length_beats"),
                "note_count": clip.get("note_count", len(notes)),
                "notes": notes[:MAX_NOTES_IN_MANIFEST],
            }
        )
    return {
        "tempo": expected.get("tempo"),
        "first_track_index": expected.get("first_track_index"),
        "created_track_count": expected.get("created_track_count"),
        "expected_track_count": expected.get("expected_track_count"),
        "tracks": _expected_object_list(expected.get("tracks"), "tracks"),
        "devices": _expected_object_list(expected.get("devices"), "devices"),
        "clips": clips,
        "arrangement": _expected_object_list(expected.get("arrangement"), "arrangement"),
    }


def _expected_object_list(value: Any, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AbletonRepairPlanError(
            f"Ableton verification expected.{label} must be a list of objects. "
            "Refusing repair planning."
        )
    return [item for item in value if isinstance(item, Mapping)]


def _expected_notes(clip: Mapping[str, Any]) -> list[Any]:
    notes = clip.get("notes")
    if notes is None:
        return []
    if not isinstance(notes, list):
        raise AbletonRepairPlanError(
            "Ableton verification expected clip notes must be a list. "
            "Refusing repair planning."
        )
    return notes


def _require_checks(verification: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks = verification.get("checks")
    if not isinstance(checks, list):
        raise AbletonRepairPlanError(
            "Ableton verification checks must be a list of objects. "
            "Refusing repair planning; message text is not a substitute."
        )
    validated: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise AbletonRepairPlanError(
                f"Ableton verification checks[{index}] must be an object. "
                "Refusing repair planning."
            )
        for key in ("id", "category", "status"):
            value = check.get(key)
            if not isinstance(value, str) or not value.strip():
                raise AbletonRepairPlanError(
                    f"Ableton verification checks[{index}] is missing a valid "
                    f"{key}. Classification uses structured fields, not message "
                    "strings. Refusing repair planning."
                )
        validated.append(check)
    return validated


def _arrangement_operations(plan: Mapping[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    operations = plan.get("operations")
    if not isinstance(operations, list):
        raise AbletonRepairPlanError(
            "Arrangement plan is missing operations. Refusing repair planning."
        )
    indexed: list[tuple[int, dict[str, Any]]] = []
    for index, operation in enumerate(operations):
        if isinstance(operation, dict):
            indexed.append((index, operation))
    return indexed


def _classify_checks(
    checks: Sequence[Mapping[str, Any]],
    operations: Sequence[tuple[int, Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split checks by structured id/category/status.  Never parse messages."""

    passed: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    manuals: list[dict[str, Any]] = []
    for check in checks:
        status = str(check["status"])
        category = str(check["category"])
        if status not in KNOWN_CHECK_STATUSES or category not in KNOWN_CHECK_CATEGORIES:
            manuals.append(_manual_action(check, operations))
            continue
        if status == CHECK_PASS:
            passed.append(dict(check))
            continue
        mapped = _map_failed_check(check, operations)
        if mapped is None:
            manuals.append(_manual_action(check, operations))
        else:
            candidates.append(mapped)
    candidates.sort(key=lambda item: (item["source_operation_index"], item["check_id"]))
    return passed, candidates, manuals


def _map_failed_check(
    check: Mapping[str, Any],
    operations: Sequence[tuple[int, Mapping[str, Any]]],
) -> dict[str, Any] | None:
    status = str(check["status"])
    category = str(check["category"])
    check_id = str(check["id"])
    if status != CHECK_FAIL:
        return None
    if category not in KNOWN_CHECK_CATEGORIES or status not in KNOWN_CHECK_STATUSES:
        return None
    if category == "tempo" and check_id == "tempo":
        matches = _matching_operations(operations, op="set_tempo")
        return _candidate_if_unique(check, matches, reason=_tempo_reason())
    if category == "devices":
        matched = DEVICE_ID_RE.fullmatch(check_id)
        if matched is None:
            return None
        track_index = int(matched.group(1))
        matches = _matching_operations(
            operations,
            ops=DEVICE_OPS,
            track_index=track_index,
        )
        return _candidate_if_unique(
            check,
            matches,
            reason=_device_reason(track_index),
        )
    if category == "clips":
        matched = SESSION_CLIP_ID_RE.fullmatch(check_id)
        if matched is None:
            return None
        track_index = int(matched.group(1))
        clip_index = int(matched.group(2))
        matches = _matching_operations(
            operations,
            op="create_midi_clip",
            track_index=track_index,
            clip_index=clip_index,
        )
        return _candidate_if_unique(
            check,
            matches,
            reason=_clip_reason(track_index, clip_index),
        )
    if category == "arrangement":
        matched = ARRANGEMENT_ID_RE.fullmatch(check_id)
        if matched is None:
            return None
        track_index = int(matched.group(1))
        destination = _expected_destination(check)
        matches = _matching_operations(
            operations,
            op="copy_session_clip_to_arrangement",
            track_index=track_index,
            destination_time_beats=destination,
        )
        return _candidate_if_unique(
            check,
            matches,
            reason=_arrangement_reason(track_index, destination),
        )
    return None


def _matching_operations(
    operations: Sequence[tuple[int, Mapping[str, Any]]],
    *,
    op: str | None = None,
    ops: frozenset[str] | None = None,
    track_index: int | None = None,
    clip_index: int | None = None,
    destination_time_beats: float | None = None,
) -> list[tuple[int, Mapping[str, Any]]]:
    names = ops if ops is not None else (frozenset({op}) if op is not None else frozenset())
    matches: list[tuple[int, Mapping[str, Any]]] = []
    for index, operation in operations:
        if str(operation.get("op") or "") not in names:
            continue
        params = operation.get("params") if isinstance(operation.get("params"), Mapping) else {}
        if track_index is not None and params.get("track_index") != track_index:
            continue
        if clip_index is not None and params.get("clip_index") != clip_index:
            continue
        if destination_time_beats is not None:
            stored = params.get("destination_time_beats")
            if not isinstance(stored, (int, float)):
                continue
            if float(stored) != float(destination_time_beats):
                continue
        matches.append((index, operation))
    return matches


def _candidate_if_unique(
    check: Mapping[str, Any],
    matches: Sequence[tuple[int, Mapping[str, Any]]],
    *,
    reason: str,
) -> dict[str, Any] | None:
    if len(matches) != 1:
        return None
    index, operation = matches[0]
    return {
        "check_id": str(check["id"]),
        "category": str(check["category"]),
        "disposition": DISPOSITION_CANDIDATE,
        "source_operation_index": index,
        "source_operation": _source_operation_view(operation),
        "reason": reason,
    }


def _manual_action(
    check: Mapping[str, Any],
    operations: Sequence[tuple[int, Mapping[str, Any]]],
) -> dict[str, Any]:
    return {
        "check_id": str(check["id"]),
        "category": str(check["category"]),
        "disposition": DISPOSITION_MANUAL,
        "reason": _manual_reason(check, operations),
    }


def _manual_reason(
    check: Mapping[str, Any],
    operations: Sequence[tuple[int, Mapping[str, Any]]],
) -> str:
    status = str(check["status"])
    category = str(check["category"])
    check_id = str(check["id"])
    if status == CHECK_NOT_OBSERVABLE:
        return (
            "this check was not observable through current evidence; inspect "
            "Live rather than guessing a reapply"
        )
    if status not in KNOWN_CHECK_STATUSES or category not in KNOWN_CHECK_CATEGORIES:
        return (
            "unknown check category/status is not converted into a machine "
            "reapply candidate"
        )
    if category == "tracks":
        if check_id == "track_count" or check_id.startswith("track_count"):
            return (
                "track-count repair would create additional tracks and can "
                "shift existing Live indexes"
            )
        if TRACK_ID_RE.fullmatch(check_id):
            return (
                "track index/name repair could overwrite or shift existing "
                "Live content"
            )
        return (
            "track mismatch/count mismatch is not converted into a machine "
            "reapply candidate"
        )
    if status == CHECK_FAIL:
        count = _source_match_count(check, operations)
        if count == 0:
            return (
                "no adopted arrangement operation uniquely corresponds to this "
                "check; inspect Live rather than guessing a reapply"
            )
        if count > 1:
            return (
                "multiple adopted arrangement operations correspond to this "
                "check; ambiguous reapply is sent to manual inspection"
            )
        return (
            "source operation is missing or not unique for this check; "
            "inspect the adopted arrangement plan rather than guessing"
        )
    return (
        "this check cannot be mapped onto a unique adopted operation; "
        "inspect Live before any reapply"
    )


def _source_match_count(
    check: Mapping[str, Any],
    operations: Sequence[tuple[int, Mapping[str, Any]]],
) -> int:
    status = str(check["status"])
    category = str(check["category"])
    check_id = str(check["id"])
    if status != CHECK_FAIL:
        return 0
    if category == "tempo" and check_id == "tempo":
        return len(_matching_operations(operations, op="set_tempo"))
    if category == "devices":
        matched = DEVICE_ID_RE.fullmatch(check_id)
        if matched is None:
            return 0
        return len(
            _matching_operations(
                operations,
                ops=DEVICE_OPS,
                track_index=int(matched.group(1)),
            )
        )
    if category == "clips":
        matched = SESSION_CLIP_ID_RE.fullmatch(check_id)
        if matched is None:
            return 0
        return len(
            _matching_operations(
                operations,
                op="create_midi_clip",
                track_index=int(matched.group(1)),
                clip_index=int(matched.group(2)),
            )
        )
    if category == "arrangement":
        matched = ARRANGEMENT_ID_RE.fullmatch(check_id)
        if matched is None:
            return 0
        return len(
            _matching_operations(
                operations,
                op="copy_session_clip_to_arrangement",
                track_index=int(matched.group(1)),
                destination_time_beats=_expected_destination(check),
            )
        )
    return 0


def _tempo_reason() -> str:
    return "observed tempo differs from the adopted arrangement plan"


def _device_reason(track_index: int) -> str:
    return (
        f"observed device chain on track {track_index} differs from the "
        "adopted arrangement plan"
    )


def _clip_reason(track_index: int, clip_index: int) -> str:
    return (
        f"observed session clip on track {track_index} slot {clip_index} "
        "differs from the adopted arrangement plan"
    )


def _arrangement_reason(track_index: int, destination: float | None) -> str:
    if destination is None:
        return (
            f"observed arrangement placement on track {track_index} differs "
            "from the adopted arrangement plan"
        )
    return (
        f"observed arrangement placement on track {track_index} at "
        f"{destination:g} beats differs from the adopted arrangement plan"
    )


def _expected_destination(check: Mapping[str, Any]) -> float | None:
    expected = check.get("expected")
    if not isinstance(expected, Mapping):
        return None
    value = expected.get("destination_time_beats")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _source_operation_view(operation: Mapping[str, Any]) -> dict[str, Any]:
    """Copy identifying fields only: no notes dumps, no secrets, no snapshots."""

    params = operation.get("params") if isinstance(operation.get("params"), Mapping) else {}
    view_params: dict[str, Any] = {}
    for key, value in params.items():
        if key == "notes":
            if isinstance(value, list):
                view_params["note_count"] = len(value)
            continue
        if key == "file_path":
            view_params[key] = str(value)
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            view_params[key] = value
            continue
        if isinstance(value, list) and all(
            isinstance(item, (str, int, float, bool)) or item is None for item in value
        ):
            view_params[key] = list(value)
    return {"op": operation.get("op"), "params": view_params}


def _build_repair_document(
    project_dir: Path,
    *,
    verification: Mapping[str, Any],
    verification_file: Path,
    verification_sha256: str,
    source_files: Mapping[str, Mapping[str, str]],
    loaded: Any,
    passed: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    manuals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if candidates:
        state = STATE_CANDIDATES_READY
    elif manuals:
        state = STATE_MANUAL_REQUIRED
    else:
        raise AbletonRepairPlanError(
            "Ableton verification is failed or partially_verified but has no "
            "failed or not-observable checks to plan from. Refusing to write "
            "an empty repair plan."
        )
    verification_state = verification.get("verification_state")
    return {
        "ableton_repair_plan_version": ABLETON_REPAIR_PLAN_VERSION,
        "repair_state": state,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "verification": {
                "path": _relpath(verification_file, project_dir),
                "sha256": verification_sha256,
                "verification_state": verification_state,
            },
            "handoff": dict(source_files["handoff"]),
            "execution_receipt": dict(source_files["execution_receipt"]),
            "arrangement_plan": dict(source_files["arrangement_plan"]),
            "job_plan": dict(source_files["job_plan"]),
            "adopted_round": loaded.adopted_round,
        },
        "summary": {
            "passed": len(passed),
            "candidate_actions": len(candidates),
            "manual_actions": len(manuals),
        },
        "candidate_actions": [dict(item) for item in candidates],
        "manual_actions": [dict(item) for item in manuals],
        "boundary": {
            "live_access": "none",
            "live_mutation": False,
            "abletongpt_invoked": False,
            "auto_execute": False,
            "auto_verify": False,
            "auto_adoption": False,
            "preference_memory_appended": False,
        },
    }


def _describe_candidate(action: Mapping[str, Any]) -> str:
    operation = action.get("source_operation") or {}
    params = operation.get("params") if isinstance(operation.get("params"), Mapping) else {}
    op_name = operation.get("op")
    check_id = action.get("check_id")
    if op_name == "set_tempo":
        return f"- candidate reapply: {check_id} / set_tempo bpm={params.get('bpm')}"
    track = params.get("track_index")
    if op_name in DEVICE_OPS:
        return (
            f"- candidate reapply: {check_id} / {op_name} track {track}"
        )
    if op_name == "create_midi_clip":
        return (
            f"- candidate reapply: {check_id} / create_midi_clip track {track} "
            f"slot {params.get('clip_index')}"
        )
    if op_name == "copy_session_clip_to_arrangement":
        destination = params.get("destination_time_beats")
        return (
            f"- candidate reapply: {check_id} / copy_session_clip_to_arrangement "
            f"track {track} at {destination:g} beats"
            if isinstance(destination, (int, float))
            else (
                f"- candidate reapply: {check_id} / "
                f"copy_session_clip_to_arrangement track {track}"
            )
        )
    return f"- candidate reapply: {check_id} / {op_name}"


def _describe_manual(action: Mapping[str, Any]) -> str:
    return (
        f"- manual inspection: {action.get('check_id')} / {action.get('category')} "
        f"— {action.get('reason')}"
    )


def _load_existing_repair_plan(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AbletonRepairPlanError(
            f"refusing to overwrite non-repair-plan file: {path}"
        ) from error
    if not isinstance(payload, dict):
        raise AbletonRepairPlanError(f"refusing to overwrite non-repair-plan file: {path}")
    version = payload.get("ableton_repair_plan_version")
    if version not in SUPPORTED_REPAIR_PLAN_VERSIONS:
        raise AbletonRepairPlanError(f"refusing to overwrite non-repair-plan file: {path}")
    return payload


def _repair_plan_equivalent(
    existing: Mapping[str, Any],
    proposed: Mapping[str, Any],
    *,
    current_verification_sha256: str,
) -> bool:
    existing_source = existing.get("source") if isinstance(existing.get("source"), Mapping) else {}
    existing_verification = (
        existing_source.get("verification")
        if isinstance(existing_source.get("verification"), Mapping)
        else {}
    )
    if existing_verification.get("sha256") != current_verification_sha256:
        return False
    return _semantic_body(existing) == _semantic_body(proposed)


def _semantic_body(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in _SEMANTIC_EXCLUDED_KEYS}


def _source_fingerprints(project_dir: Path, verification_file: Path) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for name in (
        ABLETON_VERIFICATION_NAME,
        "ableton_handoff.json",
        "ableton_execution.json",
        "ableton_job_plan.json",
        "revision_log.json",
        "preference_memory.json",
        "song_spec.json",
    ):
        path = project_dir / name
        if path.is_file():
            fingerprints[str(path)] = _file_sha256(path)
    if verification_file.is_file():
        fingerprints[str(verification_file.resolve())] = _file_sha256(verification_file)
    try:
        payload = json.loads(verification_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fingerprints
    if not isinstance(payload, dict):
        return fingerprints
    source = payload.get("source")
    if isinstance(source, Mapping):
        for key, label in (
            ("handoff", "handoff"),
            ("execution_receipt", "execution receipt"),
            ("arrangement_plan", "arrangement plan"),
            ("job_plan", "job plan"),
        ):
            row = source.get(key)
            if isinstance(row, Mapping) and isinstance(row.get("path"), str):
                try:
                    path = _resolve_project_path(
                        row["path"], project_dir=project_dir, label=label
                    )
                except AbletonRepairPlanError:
                    continue
                if path.is_file():
                    fingerprints[str(path)] = _file_sha256(path)
    return fingerprints


def _assert_unchanged(fingerprints: Mapping[str, str]) -> None:
    for path_text, digest in fingerprints.items():
        path = Path(path_text)
        if not path.is_file():
            raise AbletonRepairPlanError(
                f"VS8 must not remove {path.name}; it disappeared during repair planning."
            )
        actual = _file_sha256(path)
        if actual != digest:
            raise AbletonRepairPlanError(
                f"Source artifact changed during repair planning: {path.name}. "
                "Refusing to write a partial repair plan."
            )


def _relpath(path: Path, base_dir: Path) -> str:
    resolved = Path(path).resolve()
    root = Path(base_dir).resolve()
    try:
        return Path(os.path.relpath(resolved, start=root)).as_posix()
    except ValueError:
        return str(resolved)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise
