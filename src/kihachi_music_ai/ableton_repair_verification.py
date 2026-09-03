"""VS11 — Explicit post-repair verification closure.

Consumes a current VS9/VS10 ``ableton_repair_execution.json`` and runs a
fresh read-only VS7 Live audit.  Closes only the selected repair check.
Never mutates Live, never retries a repair, never rebuilds a repair plan,
and never claims the repair caused the observed state.

Architectural contract preserved:

    KIHACHI Music AI = decides expected musical/Live state
    AbletonGPT       = reads/writes Ableton Live
    VS11             = explicitly observes and closes one repair check
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .ableton_execution import CommandRunner
from .ableton_handoff import ableton_handoff_path
from .ableton_repair import ABLETON_REPAIR_PLAN_NAME, ableton_repair_plan_path
from .ableton_repair_execution import (
    ABLETON_REPAIR_EXECUTION_NAME,
    MODE_EXECUTE,
    MODE_PREPARE,
    STATE_APPLIED_UNVERIFIED,
    STATE_ATTEMPTED_UNVERIFIED,
    STATE_PREPARED,
    STATE_SATISFIED_UNVERIFIED,
    ableton_repair_execution_path,
)
from .ableton_verification import (
    ABLETON_VERIFICATION_NAME,
    CHECK_FAIL,
    CHECK_NOT_OBSERVABLE,
    CHECK_PASS,
    STATE_FAILED,
    STATE_NOT_RUN,
    STATE_PARTIAL,
    STATE_VERIFIED,
    AbletonVerificationError,
    AbletonVerificationManifest,
    LiveEvidenceProvider,
    ableton_verification_path,
    verify_ableton_execution,
)

ABLETON_REPAIR_VERIFICATION_VERSION = "0.1"
ABLETON_REPAIR_VERIFICATION_NAME = "ableton_repair_verification.json"
SUPPORTED_REPAIR_EXECUTION_VERSIONS = frozenset({"0.1", "0.2"})
SUPPORTED_REPAIR_PLAN_VERSIONS = frozenset({"0.1"})
ELIGIBLE_EXECUTION_STATES = frozenset(
    {
        STATE_APPLIED_UNVERIFIED,
        STATE_SATISFIED_UNVERIFIED,
        STATE_ATTEMPTED_UNVERIFIED,
    }
)
STATE_REPAIR_CHECK_VERIFIED = "repair_check_verified"
STATE_REPAIR_CHECK_FAILED = "repair_check_failed"
STATE_REPAIR_CHECK_NOT_OBSERVABLE = "repair_check_not_observable"
STATE_REPAIR_VERIFICATION_NOT_RUN = "repair_verification_not_run"
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
NEXT_NO_FURTHER_REPAIR = (
    "Repair check closed. No further repair is required for this selected "
    "check. Causality is not claimed."
)
NEXT_OTHER_FAILURES = (
    "Other verification failures remain. Review ableton_verification.json, "
    "then explicitly run `kihachi ableton-repair-plan PROJECT --overwrite` "
    "if a new repair plan is desired."
)
NEXT_FAILED = (
    "Inspect ableton_verification.json and explicitly decide whether to "
    "generate a new repair plan. No automatic retry."
)
NEXT_NOT_OBSERVABLE = (
    "Selected check is not observable. Inspect ableton_verification.json. "
    "Do not treat this as repaired or failed."
)
NEXT_NOT_RUN = (
    "Ableton repair verification was not run. Inspect the verification "
    "error; no check success was fabricated. No automatic retry."
)


class AbletonRepairVerificationError(ValueError):
    """Actionable refusal before or during read-only repair-check closure."""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ValidatedRepairExecutionReceipt:
    project_dir: Path
    receipt_file: Path
    receipt: dict[str, Any]
    receipt_sha256: str
    repair_plan_file: Path
    repair_plan: dict[str, Any]
    repair_plan_sha256: str
    check_id: str
    repair_kind: str
    operation: str
    source_operation_index: int
    source_operation_sha256: str
    repair_request_sha256: str
    execution_state: str
    status: str | None
    live_mutation_attempted: bool


@dataclass(frozen=True)
class AbletonRepairVerificationManifest:
    project_dir: Path
    closure_file: Path
    document: dict[str, Any]
    verification: AbletonVerificationManifest | None
    selected_check: dict[str, Any] | None

    @property
    def repair_verification_state(self) -> str:
        return str(
            self.document.get(
                "repair_verification_state", STATE_REPAIR_VERIFICATION_NOT_RUN
            )
        )

    @property
    def exit_code(self) -> int:
        state = self.repair_verification_state
        if state == STATE_REPAIR_CHECK_VERIFIED:
            return 0
        if state == STATE_REPAIR_CHECK_FAILED:
            return 1
        return 2


def ableton_repair_verification_path(project_dir: Path) -> Path:
    return Path(project_dir) / ABLETON_REPAIR_VERIFICATION_NAME


def load_validated_repair_execution_receipt(
    project_dir: Path,
) -> ValidatedRepairExecutionReceipt:
    """Load the current repair receipt and bind it to the current plan file.

    Refuses before any AbletonGPT invocation or Live read.  Does not rebuild
    a repair plan from the latest verification snapshot.
    """

    root = Path(project_dir).resolve()
    if not root.is_dir():
        raise AbletonRepairVerificationError(f"project not found: {root}")

    receipt_file = ableton_repair_execution_path(root)
    if not receipt_file.is_file():
        raise AbletonRepairVerificationError(
            f"No Ableton repair execution receipt found: {receipt_file}. "
            "Run `kihachi ableton-repair-apply PROJECT` first. VS11 does not "
            "invent a repair or read Live without a current receipt."
        )

    receipt = _read_json_object(
        receipt_file,
        missing="Unable to read Ableton repair execution receipt",
        invalid="Ableton repair execution receipt is not valid JSON",
        not_object="Ableton repair execution receipt must be a JSON object",
        expected="Expected a VS9/VS10 ableton_repair_execution.json object.",
    )
    version = receipt.get("ableton_repair_execution_version")
    if (
        not isinstance(version, str)
        or version not in SUPPORTED_REPAIR_EXECUTION_VERSIONS
    ):
        raise AbletonRepairVerificationError(
            f"Unsupported ableton_repair_execution_version {version!r} in "
            f"{receipt_file} (supported: "
            f"{', '.join(sorted(SUPPORTED_REPAIR_EXECUTION_VERSIONS))}). "
            "Refusing Live read."
        )

    mode = receipt.get("mode")
    if mode == MODE_PREPARE:
        raise AbletonRepairVerificationError(
            "Ableton repair execution receipt is prepare-only. VS11 does not "
            "close a repair that was never applied. Run "
            "`kihachi ableton-repair-apply PROJECT` without --prepare-only, "
            "then `kihachi ableton-repair-verify PROJECT`. Refusing Live read."
        )
    if mode != MODE_EXECUTE:
        raise AbletonRepairVerificationError(
            f"Ableton repair execution mode is {mode!r}; expected "
            f"{MODE_EXECUTE!r}. Prepare-only and unknown modes are not "
            "eligible for post-repair verification. Refusing Live read."
        )

    execution_state = receipt.get("execution_state")
    if execution_state == STATE_PREPARED:
        raise AbletonRepairVerificationError(
            f"Repair execution_state is {STATE_PREPARED!r}. That receipt did "
            "not apply a Live mutation. VS11 closes an applied, satisfied, or "
            "attempted repair; it does not verify a prepare-only receipt. "
            "Refusing Live read."
        )
    if execution_state not in ELIGIBLE_EXECUTION_STATES:
        raise AbletonRepairVerificationError(
            f"Repair execution_state is {execution_state!r}; expected one of "
            f"{', '.join(sorted(ELIGIBLE_EXECUTION_STATES))}. Malformed, "
            "unknown, and unsupported states are refused before Live read."
        )

    selection = receipt.get("selection")
    if not isinstance(selection, Mapping):
        raise AbletonRepairVerificationError(
            "Ableton repair execution receipt is missing selection. VS11 "
            "closes the check identity committed to the receipt; it does not "
            "ask for --check-id and will not invent one. Refusing Live read."
        )
    check_id = selection.get("check_id")
    if not isinstance(check_id, str) or not check_id.strip():
        raise AbletonRepairVerificationError(
            "Ableton repair execution receipt selection.check_id is missing. "
            "VS11 will not invent a check identity. Refusing Live read."
        )
    check_id = check_id.strip()

    index = selection.get("source_operation_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise AbletonRepairVerificationError(
            f"source_operation_index for check {check_id!r} must be a "
            "non-negative integer (bool is not an index). Refusing Live read."
        )

    source_operation_sha256 = selection.get("source_operation_sha256")
    if not _sha256_hex(source_operation_sha256):
        raise AbletonRepairVerificationError(
            f"Repair receipt for check {check_id!r} is missing a valid "
            "source_operation_sha256. Ambiguous operation identity is "
            "refused before Live read."
        )
    repair_request_sha256 = selection.get("repair_request_sha256")
    if not _sha256_hex(repair_request_sha256):
        raise AbletonRepairVerificationError(
            f"Repair receipt for check {check_id!r} is missing a valid "
            "repair_request_sha256. Refusing Live read."
        )

    repair_kind = selection.get("repair_kind") or receipt.get("repair_kind")
    if not isinstance(repair_kind, str) or not repair_kind.strip():
        raise AbletonRepairVerificationError(
            f"Repair receipt for check {check_id!r} is missing repair_kind. "
            "Refusing Live read."
        )
    repair_kind = repair_kind.strip()
    operation = selection.get("operation")
    if not isinstance(operation, str) or not operation.strip():
        raise AbletonRepairVerificationError(
            f"Repair receipt for check {check_id!r} is missing operation. "
            "Refusing Live read."
        )
    operation = operation.strip()

    source = receipt.get("source")
    if not isinstance(source, Mapping):
        raise AbletonRepairVerificationError(
            "Ableton repair execution receipt is missing source provenance. "
            "Refusing Live read."
        )
    plan_row = source.get("repair_plan")
    if not isinstance(plan_row, Mapping) or not _sha256_hex(plan_row.get("sha256")):
        raise AbletonRepairVerificationError(
            "Repair execution receipt is missing the repair-plan SHA-256 it "
            "executed. Refusing Live read."
        )
    recorded_plan_sha = str(plan_row["sha256"])

    repair_plan_file = ableton_repair_plan_path(root)
    if not repair_plan_file.is_file():
        raise AbletonRepairVerificationError(
            f"No Ableton repair plan found: {repair_plan_file}. VS11 proves "
            "this receipt executed this repair plan; a missing plan cannot "
            "be closed. Refusing Live read."
        )
    current_plan_sha = _file_sha256(repair_plan_file)
    if not hmac.compare_digest(current_plan_sha, recorded_plan_sha):
        raise AbletonRepairVerificationError(
            "Current ableton_repair_plan.json SHA-256 does not match the "
            "SHA recorded in ableton_repair_execution.json. VS11 will not "
            "close a receipt against a different plan. Refusing Live read."
        )

    repair_plan = _read_json_object(
        repair_plan_file,
        missing="Unable to read Ableton repair plan",
        invalid="Ableton repair plan is not valid JSON",
        not_object="Ableton repair plan must be a JSON object",
        expected="Expected a VS8 ableton_repair_plan.json object.",
    )
    plan_version = repair_plan.get("ableton_repair_plan_version")
    if (
        not isinstance(plan_version, str)
        or plan_version not in SUPPORTED_REPAIR_PLAN_VERSIONS
    ):
        raise AbletonRepairVerificationError(
            f"Unsupported ableton_repair_plan_version {plan_version!r} in "
            f"{repair_plan_file}. Refusing Live read."
        )

    plan_action = _unique_plan_action(repair_plan, check_id)
    plan_index = plan_action.get("source_operation_index")
    if plan_index != index:
        raise AbletonRepairVerificationError(
            f"Repair receipt check {check_id!r} source_operation_index "
            f"{index} does not match the current repair plan "
            f"({plan_index!r}). Source operation identity mismatch. "
            "Refusing Live read."
        )

    arrangement_path, arrangement_sha = _source_file_identity(
        source, "arrangement_plan", fallback=repair_plan
    )
    arrangement_file = (root / arrangement_path).resolve() if arrangement_path else None
    if arrangement_file is None or not arrangement_file.is_file():
        raise AbletonRepairVerificationError(
            "Repair receipt is missing a readable arrangement plan to bind "
            f"check {check_id!r}. Refusing Live read."
        )
    current_arrangement_sha = _file_sha256(arrangement_file)
    if arrangement_sha and not hmac.compare_digest(
        current_arrangement_sha, arrangement_sha
    ):
        raise AbletonRepairVerificationError(
            "Current arrangement plan SHA-256 does not match the SHA recorded "
            "in the repair execution receipt. Source identity mismatch. "
            "Refusing Live read."
        )
    arrangement = _read_json_object(
        arrangement_file,
        missing="Unable to read arrangement plan",
        invalid="Arrangement plan is not valid JSON",
        not_object="Arrangement plan must be a JSON object",
        expected="Expected the adopted arrangement_plan.json.",
    )
    operations = arrangement.get("operations")
    if not isinstance(operations, list):
        raise AbletonRepairVerificationError(
            "Arrangement plan is missing operations. Refusing Live read."
        )
    if index >= len(operations):
        raise AbletonRepairVerificationError(
            f"source_operation_index {index} is outside the current "
            "arrangement plan operations. Refusing Live read."
        )
    full_operation = operations[index]
    if not isinstance(full_operation, dict):
        raise AbletonRepairVerificationError(
            f"Arrangement operation {index} is not an object. Refusing Live read."
        )
    actual_operation_sha = _canonical_sha256(full_operation)
    if not hmac.compare_digest(actual_operation_sha, source_operation_sha256):
        raise AbletonRepairVerificationError(
            f"source_operation_sha256 for check {check_id!r} does not match "
            "the arrangement operation at the recorded index. Source "
            "operation identity mismatch. Refusing Live read."
        )

    boundary = receipt.get("boundary") if isinstance(receipt.get("boundary"), Mapping) else {}
    live_mutation_attempted = bool(boundary.get("live_mutation_attempted"))
    if "mutation_performed" in receipt:
        live_mutation_attempted = live_mutation_attempted or bool(
            receipt.get("mutation_performed")
        )
    status = receipt.get("status")
    status_text = str(status) if isinstance(status, str) else None

    return ValidatedRepairExecutionReceipt(
        project_dir=root,
        receipt_file=receipt_file.resolve(),
        receipt=receipt,
        receipt_sha256=_file_sha256(receipt_file),
        repair_plan_file=repair_plan_file.resolve(),
        repair_plan=repair_plan,
        repair_plan_sha256=current_plan_sha,
        check_id=check_id,
        repair_kind=repair_kind,
        operation=operation,
        source_operation_index=index,
        source_operation_sha256=source_operation_sha256,
        repair_request_sha256=str(repair_request_sha256),
        execution_state=str(execution_state),
        status=status_text,
        live_mutation_attempted=live_mutation_attempted,
    )


def verify_ableton_repair(
    project_dir: Path,
    *,
    abletongpt_python: Path | str | None = None,
    runner: CommandRunner | None = None,
    provider: LiveEvidenceProvider | None = None,
) -> AbletonRepairVerificationManifest:
    """Close one selected repair check with a fresh read-only VS7 audit.

    Does not mutate Live, retry a repair, rebuild a plan, or adopt a take.
    """

    loaded = load_validated_repair_execution_receipt(project_dir)
    fingerprints = _source_fingerprints(loaded)
    closure_file = ableton_repair_verification_path(loaded.project_dir)
    try:
        verification = verify_ableton_execution(
            loaded.project_dir,
            abletongpt_python=abletongpt_python,
            runner=runner,
            provider=provider,
        )
    except AbletonVerificationError as error:
        document = _build_not_run_document(loaded, error=str(error))
        _atomic_write_json(closure_file, document)
        _assert_unchanged(fingerprints)
        raise AbletonRepairVerificationError(
            f"Ableton repair verification was not run. {error}",
            exit_code=2,
        ) from error

    try:
        selected = _resolve_selected_check(verification.document, loaded.check_id)
        status = selected.get("status")
        if status == CHECK_PASS:
            closure_state = STATE_REPAIR_CHECK_VERIFIED
        elif status == CHECK_FAIL:
            closure_state = STATE_REPAIR_CHECK_FAILED
        elif status == CHECK_NOT_OBSERVABLE:
            closure_state = STATE_REPAIR_CHECK_NOT_OBSERVABLE
        else:
            raise AbletonRepairVerificationError(
                f"Selected check {loaded.check_id!r} has unsupported status "
                f"{status!r} in the fresh verification. VS11 will not invent "
                "a pass, fail, or not-observable result."
            )
        document = _build_closure_document(
            loaded,
            verification=verification,
            selected_check=selected,
            closure_state=closure_state,
        )
        _atomic_write_json(closure_file, document)
        _assert_unchanged(fingerprints)
        return AbletonRepairVerificationManifest(
            project_dir=loaded.project_dir,
            closure_file=closure_file,
            document=document,
            verification=verification,
            selected_check=selected,
        )
    except AbletonRepairVerificationError:
        _assert_unchanged(fingerprints)
        raise


def describe_ableton_repair_verification(
    manifest: AbletonRepairVerificationManifest,
) -> list[str]:
    """Concise summary.  Never claims the repair caused Live state."""

    document = manifest.document
    state = manifest.repair_verification_state
    heading = {
        STATE_REPAIR_CHECK_VERIFIED: "REPAIR CHECK VERIFIED",
        STATE_REPAIR_CHECK_FAILED: "REPAIR CHECK FAILED",
        STATE_REPAIR_CHECK_NOT_OBSERVABLE: "REPAIR CHECK NOT OBSERVABLE",
        STATE_REPAIR_VERIFICATION_NOT_RUN: "REPAIR VERIFICATION NOT RUN",
    }.get(state, state.replace("_", " ").upper())
    selection = document.get("selection") if isinstance(document.get("selection"), Mapping) else {}
    result = document.get("result") if isinstance(document.get("result"), Mapping) else {}
    receipt = document.get("receipt") if isinstance(document.get("receipt"), Mapping) else {}
    full = (
        document.get("full_verification")
        if isinstance(document.get("full_verification"), Mapping)
        else {}
    )
    claims = document.get("claims") if isinstance(document.get("claims"), Mapping) else {}
    full_state = str(full.get("verification_state") or "")
    full_heading = {
        STATE_VERIFIED: "VERIFIED",
        STATE_PARTIAL: "PARTIALLY VERIFIED",
        STATE_FAILED: "FAILED",
        STATE_NOT_RUN: "NOT RUN",
    }.get(full_state, full_state.replace("_", " ").upper() if full_state else "UNKNOWN")
    remaining = int(full.get("failed") or 0) + int(full.get("not_observable") or 0)
    if result.get("check_status") == CHECK_FAIL:
        remaining = max(remaining, 1)
    elif result.get("check_status") == CHECK_PASS:
        remaining = int(full.get("failed") or 0) + int(full.get("not_observable") or 0)

    lines = [
        f"Ableton repair verification: {heading}",
        f"Selected check: {selection.get('check_id')}",
        f"Repair kind: {selection.get('repair_kind')}",
        f"Repair execution: {receipt.get('execution_state')}",
        f"Selected check after fresh Live read: {result.get('check_status')}",
        f"Full Ableton verification: {full_heading}",
    ]
    if state == STATE_REPAIR_CHECK_VERIFIED and full_state != STATE_VERIFIED:
        lines.append("Other failed/not-observable checks remain")
        lines.append("- this closes only the selected repair check")
        lines.append("- review ableton_verification.json before any next repair")
        if remaining:
            lines.append(f"Other failed checks remain: {remaining}")
    if state == STATE_REPAIR_CHECK_FAILED:
        lines.append("- no automatic retry")
        lines.append("- inspect ableton_verification.json")
    if state == STATE_REPAIR_CHECK_NOT_OBSERVABLE:
        lines.append("- not repaired")
        lines.append("- not failed")
        lines.append("- inspect ableton_verification.json")
    if remaining and full_state != STATE_VERIFIED and state == STATE_REPAIR_CHECK_VERIFIED:
        lines.append(
            "Other verification failures remain. Review "
            "ableton_verification.json, then explicitly run "
            "`kihachi ableton-repair-plan PROJECT --overwrite` "
            "if a new repair plan is desired."
        )
    lines.append(f"Closure: {ABLETON_REPAIR_VERIFICATION_NAME}")
    lines.append("- Live access: AbletonGPT read-only")
    lines.append("- Live mutation: no")
    lines.append("- automatic retry: no")
    lines.append("- automatic repair plan: no")
    lines.append("- automatic adoption: no")
    causality = "no" if claims.get("causality_claimed") is False else "unknown"
    lines.append(f"- causality claimed: {causality}")
    return lines


def _unique_plan_action(repair_plan: Mapping[str, Any], check_id: str) -> dict[str, Any]:
    candidates = _object_list(repair_plan.get("candidate_actions"), "candidate_actions")
    manuals = _object_list(repair_plan.get("manual_actions"), "manual_actions")
    matched = [
        item
        for item in (*candidates, *manuals)
        if item.get("check_id") == check_id
    ]
    if len(matched) > 1:
        raise AbletonRepairVerificationError(
            f"Check {check_id!r} is listed more than once in "
            "ableton_repair_plan.json. Ambiguous selection is refused "
            "before Live read."
        )
    if not matched:
        raise AbletonRepairVerificationError(
            f"Check {check_id!r} from the repair execution receipt is not "
            "present in the current ableton_repair_plan.json. Missing "
            "selection is refused before Live read."
        )
    return matched[0]


def _resolve_selected_check(
    verification: Mapping[str, Any], check_id: str
) -> dict[str, Any]:
    checks = verification.get("checks")
    if not isinstance(checks, list):
        raise AbletonRepairVerificationError(
            "Fresh ableton_verification.json is missing checks. VS11 will "
            "not invent a selected-check result."
        )
    matched = [
        item
        for item in checks
        if isinstance(item, Mapping) and item.get("id") == check_id
    ]
    if not matched:
        raise AbletonRepairVerificationError(
            f"Selected check {check_id!r} is missing from the fresh "
            "ableton_verification.json. Provenance/evaluator mismatch; "
            "VS11 will not substitute a different check."
        )
    if len(matched) > 1:
        raise AbletonRepairVerificationError(
            f"Selected check {check_id!r} appears more than once in the "
            "fresh verification. Ambiguous verification evidence is refused."
        )
    return dict(matched[0])


def _build_closure_document(
    loaded: ValidatedRepairExecutionReceipt,
    *,
    verification: AbletonVerificationManifest,
    selected_check: Mapping[str, Any],
    closure_state: str,
) -> dict[str, Any]:
    root = loaded.project_dir
    verification_file = verification.verification_file
    summary = verification.document.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    full_state = verification.verification_state
    check_status = str(selected_check.get("status"))
    remaining_failed = int(summary.get("failed") or 0)
    remaining_not_observable = int(summary.get("not_observable") or 0)
    next_action = NEXT_NO_FURTHER_REPAIR
    if closure_state == STATE_REPAIR_CHECK_FAILED:
        next_action = NEXT_FAILED
    elif closure_state == STATE_REPAIR_CHECK_NOT_OBSERVABLE:
        next_action = NEXT_NOT_OBSERVABLE
    elif full_state != STATE_VERIFIED:
        next_action = NEXT_OTHER_FAILURES
    return {
        "ableton_repair_verification_version": ABLETON_REPAIR_VERIFICATION_VERSION,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repair_verification_state": closure_state,
        "source": {
            "repair_execution": {
                "path": _relpath(loaded.receipt_file, root),
                "sha256": loaded.receipt_sha256,
            },
            "repair_plan": {
                "path": _relpath(loaded.repair_plan_file, root),
                "sha256": loaded.repair_plan_sha256,
            },
            "post_repair_verification": {
                "path": _relpath(verification_file, root),
                "sha256": _file_sha256(verification_file),
            },
        },
        "selection": {
            "check_id": loaded.check_id,
            "repair_kind": loaded.repair_kind,
            "operation": loaded.operation,
            "source_operation_index": loaded.source_operation_index,
            "source_operation_sha256": loaded.source_operation_sha256,
            "repair_request_sha256": loaded.repair_request_sha256,
        },
        "receipt": {
            "execution_state": loaded.execution_state,
            "status": loaded.status,
            "live_mutation_attempted": loaded.live_mutation_attempted,
        },
        "result": {
            "check_id": selected_check.get("id"),
            "check_status": check_status,
            "category": selected_check.get("category"),
            "expected": selected_check.get("expected"),
            "observed": selected_check.get("observed"),
            "message": selected_check.get("message"),
        },
        "full_verification": {
            "verification_state": full_state,
            "passed": int(summary.get("passed") or 0),
            "failed": remaining_failed,
            "not_observable": remaining_not_observable,
        },
        "claims": {
            "fresh_read_after_receipt_validation": True,
            "selected_postcondition_observed": check_status == CHECK_PASS,
            "causality_claimed": False,
            "full_live_set_verified": full_state == STATE_VERIFIED,
        },
        "boundary": {
            "live_read": True,
            "live_mutation": False,
            "abletongpt_used_for_read_only_evidence": True,
            "repair_attempted": False,
            "automatic_retry": False,
            "automatic_repair_plan": False,
            "automatic_adoption": False,
        },
        "next_action": next_action,
    }


def _build_not_run_document(
    loaded: ValidatedRepairExecutionReceipt,
    *,
    error: str,
) -> dict[str, Any]:
    root = loaded.project_dir
    verification_file = ableton_verification_path(root)
    verification_row: dict[str, Any] | None = None
    full_state = STATE_NOT_RUN
    passed = 0
    failed = 0
    not_observable = 0
    if verification_file.is_file():
        verification_row = {
            "path": _relpath(verification_file, root),
            "sha256": _file_sha256(verification_file),
        }
        try:
            payload = json.loads(verification_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            full_state = str(payload.get("verification_state") or STATE_NOT_RUN)
            summary = payload.get("summary")
            if isinstance(summary, Mapping):
                passed = int(summary.get("passed") or 0)
                failed = int(summary.get("failed") or 0)
                not_observable = int(summary.get("not_observable") or 0)
    return {
        "ableton_repair_verification_version": ABLETON_REPAIR_VERIFICATION_VERSION,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repair_verification_state": STATE_REPAIR_VERIFICATION_NOT_RUN,
        "source": {
            "repair_execution": {
                "path": _relpath(loaded.receipt_file, root),
                "sha256": loaded.receipt_sha256,
            },
            "repair_plan": {
                "path": _relpath(loaded.repair_plan_file, root),
                "sha256": loaded.repair_plan_sha256,
            },
            "post_repair_verification": verification_row,
        },
        "selection": {
            "check_id": loaded.check_id,
            "repair_kind": loaded.repair_kind,
            "operation": loaded.operation,
            "source_operation_index": loaded.source_operation_index,
            "source_operation_sha256": loaded.source_operation_sha256,
            "repair_request_sha256": loaded.repair_request_sha256,
        },
        "receipt": {
            "execution_state": loaded.execution_state,
            "status": loaded.status,
            "live_mutation_attempted": loaded.live_mutation_attempted,
        },
        "result": None,
        "full_verification": {
            "verification_state": full_state,
            "passed": passed,
            "failed": failed,
            "not_observable": not_observable,
        },
        "claims": {
            "fresh_read_after_receipt_validation": True,
            "selected_postcondition_observed": False,
            "causality_claimed": False,
            "full_live_set_verified": False,
        },
        "boundary": {
            "live_read": True,
            "live_mutation": False,
            "abletongpt_used_for_read_only_evidence": True,
            "repair_attempted": False,
            "automatic_retry": False,
            "automatic_repair_plan": False,
            "automatic_adoption": False,
        },
        "next_action": NEXT_NOT_RUN,
        "error": error,
    }


def _source_fingerprints(loaded: ValidatedRepairExecutionReceipt) -> dict[str, str]:
    """Immutable repair-source artifacts.  Verification snapshot may change."""

    root = loaded.project_dir
    fingerprints: dict[str, str] = {}
    names = (
        ABLETON_REPAIR_EXECUTION_NAME,
        ABLETON_REPAIR_PLAN_NAME,
        "ableton_handoff.json",
        "ableton_execution.json",
        "ableton_job_plan.json",
        "revision_log.json",
        "preference_memory.json",
        "song_spec.json",
    )
    for name in names:
        path = root / name
        if path.is_file():
            fingerprints[str(path.resolve())] = _file_sha256(path)
    handoff_file = ableton_handoff_path(root)
    if handoff_file.is_file():
        try:
            payload = json.loads(handoff_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            for key in ("arrangement_plan", "song_spec", "audio"):
                row = payload.get(key)
                if isinstance(row, Mapping) and isinstance(row.get("path"), str):
                    path = (root / row["path"]).resolve()
                    if path.is_file():
                        fingerprints[str(path)] = _file_sha256(path)
            for row in payload.get("midi") or []:
                if isinstance(row, Mapping) and isinstance(row.get("path"), str):
                    path = (root / row["path"]).resolve()
                    if path.is_file():
                        fingerprints[str(path)] = _file_sha256(path)
    return fingerprints


def _assert_unchanged(fingerprints: Mapping[str, str]) -> None:
    for path_text, digest in fingerprints.items():
        path = Path(path_text)
        if not path.is_file():
            raise AbletonRepairVerificationError(
                f"VS11 must not remove {path.name}; it disappeared during "
                "repair verification."
            )
        actual = _file_sha256(path)
        if actual != digest:
            raise AbletonRepairVerificationError(
                f"Source artifact changed during repair verification: "
                f"{path.name}. VS11 is read-only and refuses to continue."
            )


def _source_file_identity(
    source: Mapping[str, Any],
    key: str,
    *,
    fallback: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    row = source.get(key)
    if not isinstance(row, Mapping) and fallback is not None:
        nested = fallback.get("source")
        if isinstance(nested, Mapping):
            row = nested.get(key)
    if not isinstance(row, Mapping):
        return None, None
    path = row.get("path")
    sha = row.get("sha256")
    path_text = str(path) if isinstance(path, str) and path else None
    sha_text = str(sha) if _sha256_hex(sha) else None
    return path_text, sha_text


def _read_json_object(
    path: Path,
    *,
    missing: str,
    invalid: str,
    not_object: str,
    expected: str,
) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError) as error:
        raise AbletonRepairVerificationError(f"{missing}: {path}") from error
    except json.JSONDecodeError as error:
        raise AbletonRepairVerificationError(
            f"{invalid}: {path} ({error.msg}). {expected}"
        ) from error
    if not isinstance(payload, dict):
        raise AbletonRepairVerificationError(f"{not_object}: {path}")
    return payload


def _object_list(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AbletonRepairVerificationError(
            f"Repair plan {label} must be a list of objects. Refusing Live read."
        )
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise AbletonRepairVerificationError(
                f"Repair plan {label} contains a non-object entry. "
                "Ambiguous identity is refused before Live read."
            )
        rows.append(item)
    return rows


def _sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and SHA256_HEX_RE.fullmatch(value) is not None


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relpath(path: Path, base_dir: Path) -> str:
    resolved = Path(path).resolve()
    root = Path(base_dir).resolve()
    try:
        return Path(os.path.relpath(resolved, start=root)).as_posix()
    except ValueError:
        return str(resolved)


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
