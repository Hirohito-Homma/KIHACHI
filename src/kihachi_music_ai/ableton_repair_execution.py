"""VS9 — Human-authorized Ableton repair execution (tempo only).

Consumes a VS8 ``ableton_repair_plan.json``, requires an explicit SHA-256
approval of that exact file, and applies one ``set_tempo`` candidate through
AbletonGPT.  Never talks to the Live socket itself, never auto-verifies, and
never treats ``candidate_reapply`` as a safety claim.

Architectural contract preserved:

    KIHACHI Music AI = decides what should exist
    AbletonGPT       = reads/writes Ableton Live
    VS9              = authorized tempo repair job + unverified receipt
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .ableton_execution import (
    ABLETONGPT_JOBS_MODULE,
    MAX_CAPTURED_CHARS,
    CommandResult,
    CommandRunner,
    run_command,
)
from .ableton_repair import (
    DISPOSITION_CANDIDATE,
    STATE_CANDIDATES_READY,
    AbletonRepairPlanError,
    load_validated_repair_plan,
    source_operation_view,
)
from .ableton_verification import (
    CHECK_FAIL,
    TEMPO_TOLERANCE_BPM,
    AbletonVerificationError,
    LiveEvidenceProvider,
    collect_live_evidence,
)

ABLETON_REPAIR_EXECUTION_VERSION = "0.1"
ABLETON_REPAIR_EXECUTION_NAME = "ableton_repair_execution.json"
ABLETON_REPAIR_JOB_PLAN_NAME = "ableton_repair_job_plan.json"
SUPPORTED_REPAIR_OPERATIONS = frozenset({"set_tempo"})
AUTHORIZATION_METHOD = "explicit_cli_plan_sha256"
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

MODE_PREPARE = "prepare_only"
MODE_EXECUTE = "execute"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATE_PREPARED = "repair_prepared_not_applied"
STATE_APPLIED_UNVERIFIED = "repair_applied_unverified"
STATE_ATTEMPTED_UNVERIFIED = "repair_attempted_unverified"

NEXT_VERIFY = "run kihachi ableton-verify PROJECT explicitly"
NEXT_VERIFY_BEFORE_RETRY = (
    "run kihachi ableton-verify PROJECT before retrying; "
    "Live state may have changed"
)
_COUNT_RE = re.compile(
    r"completed\s*=\s*(\d+)\s+failed\s*=\s*(\d+)(?:\s+pending\s*=\s*(\d+))?",
    re.IGNORECASE,
)


class AbletonRepairExecutionError(ValueError):
    """Actionable refusal before or during authorized repair execution."""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ValidatedRepairSelection:
    project_dir: Path
    repair_plan_file: Path
    repair_plan: dict[str, Any]
    repair_plan_sha256: str
    verification_file: Path
    verification: dict[str, Any]
    arrangement_plan_file: Path
    arrangement_plan: dict[str, Any]
    selected_check_id: str
    source_operation_index: int
    source_operation: dict[str, Any]


@dataclass(frozen=True)
class AbletonRepairExecutionManifest:
    project_dir: Path
    repair_plan_file: Path
    job_plan_file: Path | None
    receipt_file: Path
    receipt: dict[str, Any]
    prepare_only: bool


def ableton_repair_execution_path(project_dir: Path) -> Path:
    return Path(project_dir) / ABLETON_REPAIR_EXECUTION_NAME


def ableton_repair_job_plan_path(project_dir: Path) -> Path:
    return Path(project_dir) / ABLETON_REPAIR_JOB_PLAN_NAME


def load_validated_repair_selection(
    project_dir: Path,
    *,
    check_id: str,
) -> ValidatedRepairSelection:
    """Resolve one tempo candidate against the current arrangement plan.

    Refuses before any external process or Live read.  ``candidate_reapply``
    is not treated as permission to execute.
    """

    if not isinstance(check_id, str) or not check_id.strip():
        raise AbletonRepairExecutionError(
            "A --check-id is required. VS9 executes one selected repair "
            "candidate; it does not apply the whole repair plan."
        )
    selected_id = check_id.strip()

    try:
        loaded = load_validated_repair_plan(project_dir)
    except AbletonRepairPlanError as error:
        raise AbletonRepairExecutionError(
            str(error), exit_code=error.exit_code
        ) from error

    repair_plan = loaded.repair_plan
    if repair_plan.get("repair_state") != STATE_CANDIDATES_READY:
        raise AbletonRepairExecutionError(
            f"Repair state is {repair_plan.get('repair_state')!r}; expected "
            f"{STATE_CANDIDATES_READY}. Refusing execution. "
            "candidate_reapply is not a safety claim, and manual-only plans "
            "are not executed."
        )

    candidates = _object_list(repair_plan.get("candidate_actions"), "candidate_actions")
    manuals = _object_list(repair_plan.get("manual_actions"), "manual_actions")
    matched = [item for item in candidates if item.get("check_id") == selected_id]
    if len(matched) > 1:
        raise AbletonRepairExecutionError(
            f"Check {selected_id!r} is listed more than once as a candidate "
            "reapply action. Ambiguous selection is refused."
        )
    if not matched:
        if any(item.get("check_id") == selected_id for item in manuals):
            raise AbletonRepairExecutionError(
                f"Check {selected_id!r} is a manual_inspection item, not a "
                "candidate_reapply. VS9 does not execute manual items, track "
                "repairs, or unmapped checks."
            )
        raise AbletonRepairExecutionError(
            f"Check {selected_id!r} is not a unique candidate_reapply action "
            "in ableton_repair_plan.json. Missing and ambiguous selections "
            "are refused."
        )

    action = matched[0]
    if action.get("disposition") != DISPOSITION_CANDIDATE:
        raise AbletonRepairExecutionError(
            f"Check {selected_id!r} is not disposition={DISPOSITION_CANDIDATE!r}. "
            "Refusing execution."
        )

    index = action.get("source_operation_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise AbletonRepairExecutionError(
            f"source_operation_index for check {selected_id!r} must be a "
            "non-negative integer (bool is not an index). Refusing execution."
        )

    operations = loaded.arrangement_plan.get("operations")
    if not isinstance(operations, list):
        raise AbletonRepairExecutionError(
            "Arrangement plan is missing operations. Refusing execution."
        )
    if index >= len(operations):
        raise AbletonRepairExecutionError(
            f"source_operation_index {index} is outside the current arrangement "
            "plan operations. Refusing execution from the repair-plan view alone."
        )
    full_operation = operations[index]
    if not isinstance(full_operation, dict):
        raise AbletonRepairExecutionError(
            f"Arrangement operations[{index}] must be an object. "
            "Refusing to execute from the repair-plan display payload."
        )

    op_name = str(full_operation.get("op") or "")
    if op_name not in SUPPORTED_REPAIR_OPERATIONS:
        raise AbletonRepairExecutionError(
            f"Repair execution supports only set_tempo (selected {op_name!r} "
            f"for check {selected_id!r}). This candidate is "
            "unsupported_for_execution in VS9: device, session clip, and "
            "Arrangement handlers can overwrite or duplicate Live content "
            "under the current AbletonGPT contract. candidate_reapply is not "
            "a safety claim."
        )

    view = source_operation_view(full_operation)
    stored_view = action.get("source_operation")
    if stored_view != view:
        raise AbletonRepairExecutionError(
            "Repair plan source_operation view does not match the current "
            "arrangement plan operation. Refusing to execute a reduced payload "
            "or a stale operation identity. Re-run "
            "`kihachi ableton-repair-plan PROJECT`."
        )

    tempo_check = _require_failed_tempo_check(loaded.verification, selected_id)
    expected_bpm = _finite_bpm(tempo_check.get("expected"), "verification expected tempo")
    operation_bpm = _validate_set_tempo_params(full_operation.get("params"))
    if abs(expected_bpm - operation_bpm) > TEMPO_TOLERANCE_BPM:
        raise AbletonRepairExecutionError(
            f"Expected BPM {expected_bpm:g} does not match set_tempo "
            f"operation BPM {operation_bpm:g}. Refusing execution."
        )

    return ValidatedRepairSelection(
        project_dir=loaded.project_dir,
        repair_plan_file=loaded.repair_plan_file,
        repair_plan=loaded.repair_plan,
        repair_plan_sha256=loaded.repair_plan_sha256,
        verification_file=loaded.verification_file,
        verification=loaded.verification,
        arrangement_plan_file=loaded.arrangement_plan_file,
        arrangement_plan=loaded.arrangement_plan,
        selected_check_id=selected_id,
        source_operation_index=index,
        source_operation=dict(full_operation),
    )


def execute_ableton_repair(
    project_dir: Path,
    *,
    check_id: str,
    prepare_only: bool = False,
    approved_plan_sha256: str | None = None,
    rerun: bool = False,
    abletongpt_python: Path | str | None = None,
    runner: CommandRunner | None = None,
    preflight_provider: LiveEvidenceProvider | None = None,
) -> AbletonRepairExecutionManifest:
    """Authorize and apply one tempo repair candidate through AbletonGPT."""

    if prepare_only and rerun:
        raise AbletonRepairExecutionError(
            "--prepare-only and --rerun cannot be combined. Prepare does not "
            "run the Live job; rerun is for a previously successful execute."
        )
    if prepare_only and approved_plan_sha256 is not None:
        raise AbletonRepairExecutionError(
            "--prepare-only and --approve-plan-sha cannot be combined. Copy the "
            "repair plan SHA from prepare-only output, then pass it on a "
            "separate execute invocation."
        )

    selection = load_validated_repair_selection(project_dir, check_id=check_id)
    receipt_file = ableton_repair_execution_path(selection.project_dir)
    job_plan_file = ableton_repair_job_plan_path(selection.project_dir)
    previous = _load_receipt(receipt_file)
    already_applied = _success_receipt_for_identity(
        previous,
        repair_plan_sha256=selection.repair_plan_sha256,
        check_id=selection.selected_check_id,
    )

    authorization: dict[str, Any] | None = None
    if not prepare_only:
        authorization = _require_authorization(
            approved_plan_sha256, selection=selection
        )
        if already_applied and not rerun:
            raise AbletonRepairExecutionError(
                "This exact repair plan and check were already executed "
                "successfully (repair applied, Live unverified). Re-running "
                "can still race a changed Set. Pass --rerun with a fresh "
                "--approve-plan-sha of the current ableton_repair_plan.json."
            )

    fingerprints = _source_fingerprints(selection)
    run = runner if runner is not None else run_command
    python = _python_executable(abletongpt_python)
    request_document = _repair_request_document(selection)
    request_sha256 = _canonical_sha256(request_document)
    source_operation_sha256 = _canonical_sha256(selection.source_operation)

    import_result: CommandResult | None = None
    job_plan: dict[str, Any] | None = None
    job_plan_sha: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="kihachi-vs9-") as temp:
            request_path = Path(temp) / "repair_tempo_request.json"
            request_path.write_text(
                json.dumps(request_document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            import_argv = _import_kihachi_argv(
                python,
                arrangement_plan=request_path,
                job_plan=job_plan_file,
            )
            import_result = run(import_argv)
        _assert_unchanged(fingerprints)
        if _module_unavailable(import_result):
            raise AbletonRepairExecutionError(
                "AbletonGPT is not available in "
                f"{python}. Install AbletonGPT in that interpreter or pass "
                "--abletongpt-python PATH. The Live repair job was not started. "
                f"Captured stderr: {_bound_text(import_result.stderr, 400)}"
            )
        if import_result.returncode != 0:
            raise AbletonRepairExecutionError(
                "AbletonGPT import-kihachi failed "
                f"(exit {import_result.returncode}). The Live repair job was "
                "not started. "
                f"stderr: {_bound_text(import_result.stderr, 800)}"
            )
        job_plan = _require_repair_job_plan(
            job_plan_file, source_operation=selection.source_operation
        )
        job_plan_sha = _file_sha256(job_plan_file)
    except AbletonRepairExecutionError as error:
        if not (prepare_only and already_applied):
            _write_failed_receipt(
                receipt_file,
                selection,
                mode=MODE_PREPARE if prepare_only else MODE_EXECUTE,
                authorization=authorization,
                job_plan_file=job_plan_file if job_plan_file.is_file() else None,
                job_plan_sha256=(
                    _file_sha256(job_plan_file) if job_plan_file.is_file() else None
                ),
                import_result=import_result,
                run_result=None,
                preflight=None,
                request_sha256=request_sha256,
                source_operation_sha256=source_operation_sha256,
                live_mutation_attempted=False,
                error="import_kihachi_failed",
            )
        raise

    if prepare_only:
        if already_applied:
            return AbletonRepairExecutionManifest(
                project_dir=selection.project_dir,
                repair_plan_file=selection.repair_plan_file,
                job_plan_file=job_plan_file,
                receipt_file=receipt_file,
                receipt=previous if previous is not None else {},
                prepare_only=True,
            )
        receipt = _build_receipt(
            selection,
            mode=MODE_PREPARE,
            status=STATUS_SUCCESS,
            execution_state=STATE_PREPARED,
            authorization=None,
            preflight=None,
            job_plan_file=job_plan_file,
            job_plan_sha256=job_plan_sha,
            request_sha256=request_sha256,
            source_operation_sha256=source_operation_sha256,
            import_result=import_result,
            run_result=None,
            completed=1,
            failed=0,
            pending=0,
            live_mutation_attempted=False,
            next_action=(
                "run again with --approve-plan-sha of the printed repair plan "
                "SHA-256 to authorize this exact plan"
            ),
        )
        _atomic_write_json(receipt_file, receipt)
        return AbletonRepairExecutionManifest(
            project_dir=selection.project_dir,
            repair_plan_file=selection.repair_plan_file,
            job_plan_file=job_plan_file,
            receipt_file=receipt_file,
            receipt=receipt,
            prepare_only=True,
        )

    try:
        preflight = _run_preflight(
            selection,
            provider=preflight_provider,
            abletongpt_python=abletongpt_python,
            runner=runner,
        )
        _assert_unchanged(fingerprints)
        current_plan_sha = _file_sha256(selection.repair_plan_file)
        if not hmac.compare_digest(current_plan_sha, selection.repair_plan_sha256):
            raise AbletonRepairExecutionError(
                "ableton_repair_plan.json changed after authorization. "
                "Refusing Live mutation. Re-run prepare-only and approve the "
                "current plan SHA-256."
            )
        if authorization is not None and not hmac.compare_digest(
            current_plan_sha, str(authorization["approved_plan_sha256"])
        ):
            raise AbletonRepairExecutionError(
                "Approved plan SHA-256 no longer matches the current repair "
                "plan. Refusing Live mutation."
            )
    except AbletonRepairExecutionError as error:
        _write_failed_receipt(
            receipt_file,
            selection,
            mode=MODE_EXECUTE,
            authorization=authorization,
            job_plan_file=job_plan_file,
            job_plan_sha256=job_plan_sha,
            import_result=import_result,
            run_result=None,
            preflight=None,
            request_sha256=request_sha256,
            source_operation_sha256=source_operation_sha256,
            live_mutation_attempted=False,
            error="preflight_failed",
        )
        raise

    run_argv = _run_argv(python, job_plan_file)
    run_result = run(run_argv)
    completed, failed, pending = _execution_counts(run_result, job_plan_file)
    success = run_result.returncode == 0 and (failed is None or failed == 0)
    job_plan_sha = (
        _file_sha256(job_plan_file) if job_plan_file.is_file() else job_plan_sha
    )
    if not success:
        _write_failed_receipt(
            receipt_file,
            selection,
            mode=MODE_EXECUTE,
            authorization=authorization,
            job_plan_file=job_plan_file if job_plan_file.is_file() else None,
            job_plan_sha256=job_plan_sha,
            import_result=import_result,
            run_result=run_result,
            preflight=preflight,
            request_sha256=request_sha256,
            source_operation_sha256=source_operation_sha256,
            live_mutation_attempted=True,
            error="run_failed",
            completed=completed,
            failed=failed,
            pending=pending,
            execution_state=STATE_ATTEMPTED_UNVERIFIED,
            next_action=NEXT_VERIFY_BEFORE_RETRY,
        )
        counts = _format_counts(completed, failed, pending)
        raise AbletonRepairExecutionError(
            "AbletonGPT repair job failed "
            f"(exit {run_result.returncode}{f', {counts}' if counts else ''}). "
            "Live mutation may have occurred; this is not evidence that it did "
            "not. Do not retry until `kihachi ableton-verify PROJECT` has been "
            "run explicitly. "
            f"stderr: {_bound_text(run_result.stderr, 800)}",
            exit_code=1,
        )

    receipt = _build_receipt(
        selection,
        mode=MODE_EXECUTE,
        status=STATUS_SUCCESS,
        execution_state=STATE_APPLIED_UNVERIFIED,
        authorization=authorization,
        preflight=preflight,
        job_plan_file=job_plan_file,
        job_plan_sha256=job_plan_sha,
        request_sha256=request_sha256,
        source_operation_sha256=source_operation_sha256,
        import_result=import_result,
        run_result=run_result,
        completed=completed if completed is not None else 1,
        failed=failed if failed is not None else 0,
        pending=pending if pending is not None else 0,
        live_mutation_attempted=True,
        next_action=NEXT_VERIFY,
    )
    _atomic_write_json(receipt_file, receipt)
    _assert_unchanged(fingerprints)
    return AbletonRepairExecutionManifest(
        project_dir=selection.project_dir,
        repair_plan_file=selection.repair_plan_file,
        job_plan_file=job_plan_file,
        receipt_file=receipt_file,
        receipt=receipt,
        prepare_only=False,
    )


def describe_ableton_repair_execution(
    manifest: AbletonRepairExecutionManifest,
) -> list[str]:
    """Concise summary lines.  Never claims Live was repaired or verified."""

    receipt = manifest.receipt
    selection = receipt.get("selection") if isinstance(receipt.get("selection"), Mapping) else {}
    check_id = selection.get("check_id")
    operation = selection.get("operation")
    source = receipt.get("source") if isinstance(receipt.get("source"), Mapping) else {}
    repair = source.get("repair_plan") if isinstance(source.get("repair_plan"), Mapping) else {}
    sha = str(repair.get("sha256") or "")
    if manifest.prepare_only or receipt.get("mode") == MODE_PREPARE:
        return [
            "Prepared Ableton repair execution (no Live job)",
            f"Selected check: {check_id}",
            f"Operation: {operation}",
            f"Repair plan SHA-256: {sha}",
            f"Repair job plan: {ABLETON_REPAIR_JOB_PLAN_NAME}",
            f"Receipt: {ABLETON_REPAIR_EXECUTION_NAME}",
            "- Live read: no",
            "- Live mutation: no",
            "- candidate_reapply safety claim: no",
            f"- run again with --approve-plan-sha {sha} to authorize this exact plan",
        ]
    run_label = (
        "success" if receipt.get("status") == STATUS_SUCCESS else "failed"
    )
    return [
        "Applied authorized Ableton repair candidate through AbletonGPT",
        f"Selected check: {check_id}",
        f"Operation: {operation}",
        f"AbletonGPT run: {run_label}",
        f"Receipt: {ABLETON_REPAIR_EXECUTION_NAME}",
        "- Live mutation attempted: yes",
        "- Live repair verified: no",
        "- auto-verify: no",
        "- adoption unchanged: yes",
        "- preference memory appended: no",
        "Next: run kihachi ableton-verify PROJECT explicitly",
    ]


def _require_authorization(
    approved_plan_sha256: str | None,
    *,
    selection: ValidatedRepairSelection,
) -> dict[str, str]:
    if approved_plan_sha256 is None:
        raise AbletonRepairExecutionError(
            "Execute mode requires --approve-plan-sha matching the current "
            "ableton_repair_plan.json SHA-256. Run --prepare-only first and "
            "copy the printed digest."
        )
    if not isinstance(approved_plan_sha256, str) or SHA256_HEX_RE.fullmatch(
        approved_plan_sha256
    ) is None:
        raise AbletonRepairExecutionError(
            "--approve-plan-sha must be the full 64-character lowercase "
            "hexadecimal SHA-256 of ableton_repair_plan.json. Uppercase, "
            "whitespace, and partial digests are refused."
        )
    current = selection.repair_plan_sha256
    if not hmac.compare_digest(approved_plan_sha256, current):
        raise AbletonRepairExecutionError(
            "Approved plan SHA-256 does not match the current "
            "ableton_repair_plan.json. Stale or wrong approvals are refused. "
            "Re-run --prepare-only and authorize that exact digest."
        )
    return {
        "method": AUTHORIZATION_METHOD,
        "approved_plan_sha256": approved_plan_sha256,
        "selected_check_id": selection.selected_check_id,
    }


def _require_failed_tempo_check(
    verification: Mapping[str, Any], check_id: str
) -> dict[str, Any]:
    checks = verification.get("checks")
    if not isinstance(checks, list):
        raise AbletonRepairExecutionError(
            "Ableton verification checks must be a list. Refusing execution."
        )
    matched = [
        item
        for item in checks
        if isinstance(item, dict) and item.get("id") == check_id
    ]
    if len(matched) != 1:
        raise AbletonRepairExecutionError(
            f"Verification check {check_id!r} is missing or duplicated. "
            "Refusing execution."
        )
    check = matched[0]
    if check.get("category") != "tempo" or check.get("status") != CHECK_FAIL:
        raise AbletonRepairExecutionError(
            f"Verification check {check_id!r} is not a failed tempo check "
            f"(category={check.get('category')!r}, status={check.get('status')!r}). "
            "Refusing execution."
        )
    return check


def _validate_set_tempo_params(params: Any) -> float:
    if not isinstance(params, Mapping):
        raise AbletonRepairExecutionError(
            "set_tempo params must be an object. Refusing execution."
        )
    unexpected = sorted(set(params) - {"bpm"})
    if unexpected:
        raise AbletonRepairExecutionError(
            "set_tempo params have unexpected fields "
            f"{unexpected}; AbletonGPT import-kihachi would reject them. "
            "Refusing execution."
        )
    return _finite_bpm(params.get("bpm"), "set_tempo bpm")


def _finite_bpm(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AbletonRepairExecutionError(
            f"{label} must be a finite number. Refusing execution."
        )
    number = float(value)
    if not math.isfinite(number):
        raise AbletonRepairExecutionError(
            f"{label} must be a finite number. Refusing execution."
        )
    if not 20 <= number <= 999:
        raise AbletonRepairExecutionError(
            f"{label} must be between 20 and 999. Refusing execution."
        )
    return number


def _repair_request_document(selection: ValidatedRepairSelection) -> dict[str, Any]:
    song = selection.arrangement_plan.get("song")
    title = "KIHACHI"
    if isinstance(song, Mapping) and isinstance(song.get("title"), str):
        stripped = song["title"].strip()
        if stripped:
            title = stripped
    operation = dict(selection.source_operation)
    return {
        "arrangement_plan_version": "0.1",
        "execution_state": "planned_not_applied",
        "song": {"title": f"{title} — repair tempo"},
        "operations": [operation],
    }


def _require_repair_job_plan(
    path: Path, *, source_operation: Mapping[str, Any]
) -> dict[str, Any]:
    if not path.is_file():
        raise AbletonRepairExecutionError(
            f"AbletonGPT import-kihachi did not produce a repair job plan: {path}."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise AbletonRepairExecutionError(
            f"Unable to read AbletonGPT repair job plan: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise AbletonRepairExecutionError(
            f"AbletonGPT repair job plan is not valid JSON: {path} ({error.msg})"
        ) from error
    if not isinstance(payload, dict) or not payload:
        raise AbletonRepairExecutionError(
            f"AbletonGPT repair job plan is empty or not a JSON object: {path}"
        )
    steps = payload.get("steps")
    if not isinstance(steps, list) or len(steps) != 1:
        raise AbletonRepairExecutionError(
            f"AbletonGPT repair job plan must contain exactly one step: {path}."
        )
    step = steps[0]
    if not isinstance(step, Mapping) or str(step.get("command") or "") != "set_tempo":
        raise AbletonRepairExecutionError(
            f"AbletonGPT repair job plan step 0 must be command=set_tempo: {path}."
        )
    params = step.get("params") if isinstance(step.get("params"), Mapping) else {}
    source_params = (
        source_operation.get("params")
        if isinstance(source_operation.get("params"), Mapping)
        else {}
    )
    try:
        imported_bpm = _finite_bpm(params.get("bpm"), "imported set_tempo bpm")
        source_bpm = _finite_bpm(source_params.get("bpm"), "source set_tempo bpm")
    except AbletonRepairExecutionError as error:
        raise AbletonRepairExecutionError(
            f"{error} Repair job plan params do not match the source operation."
        ) from error
    if abs(imported_bpm - source_bpm) > TEMPO_TOLERANCE_BPM:
        raise AbletonRepairExecutionError(
            "Repair job plan set_tempo params do not match the current "
            "arrangement plan operation. Refusing to run."
        )
    return payload


def _run_preflight(
    selection: ValidatedRepairSelection,
    *,
    provider: LiveEvidenceProvider | None,
    abletongpt_python: Path | str | None,
    runner: CommandRunner | None,
) -> dict[str, Any]:
    tempo_check = _require_failed_tempo_check(
        selection.verification, selection.selected_check_id
    )
    expected_bpm = _finite_bpm(
        tempo_check.get("expected"), "verification expected tempo"
    )
    request = {
        "read_only": True,
        "device_indices": [],
        "session_clips": [],
        "arrangement_indices": [],
    }
    try:
        evidence = collect_live_evidence(
            request,
            provider=provider,
            abletongpt_python=abletongpt_python,
            runner=runner,
        )
    except AbletonVerificationError as error:
        raise AbletonRepairExecutionError(
            f"{error} Repair job was not started. Live mutation was not attempted.",
            exit_code=error.exit_code,
        ) from error

    if not isinstance(evidence, dict) or evidence.get("read_only") is not True:
        raise AbletonRepairExecutionError(
            "Preflight evidence is missing or not marked read_only. "
            "Refusing Live mutation."
        )
    live_state = evidence.get("live_state")
    if not isinstance(live_state, Mapping):
        raise AbletonRepairExecutionError(
            "Preflight evidence is missing live_state. Refusing Live mutation."
        )
    current_tempo = _require_observed_tempo(live_state.get("tempo"), "current Live tempo")
    verified_observed = _require_observed_tempo(
        tempo_check.get("observed"), "verification observed tempo"
    )
    if abs(current_tempo - expected_bpm) <= TEMPO_TOLERANCE_BPM:
        raise AbletonRepairExecutionError(
            "Live tempo already matches the expected BPM. The repair job was "
            "not started. Run `kihachi ableton-verify PROJECT` explicitly; "
            "VS9 does not treat an already-matching tempo as a successful repair."
        )
    if abs(current_tempo - verified_observed) > TEMPO_TOLERANCE_BPM:
        raise AbletonRepairExecutionError(
            "Live tempo has changed since verification "
            f"(then {verified_observed:g} BPM, now {current_tempo:g} BPM). "
            "Refusing Live mutation. Re-run `kihachi ableton-verify PROJECT` first."
        )

    tracks = live_state.get("tracks")
    if not isinstance(tracks, list):
        raise AbletonRepairExecutionError(
            "Preflight live_state.tracks is missing or not a list. "
            "Refusing Live mutation."
        )
    expected = selection.verification.get("expected")
    if not isinstance(expected, Mapping):
        raise AbletonRepairExecutionError(
            "Verification is missing expected Live state. Refusing Live mutation."
        )
    expected_count = expected.get("expected_track_count")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise AbletonRepairExecutionError(
            "Verification expected_track_count is missing. Refusing Live mutation."
        )
    required_count = expected_count
    observed_tracks = _verification_observed_tracks(selection.verification)
    if observed_tracks is not None:
        required_count = len(observed_tracks)
    if len(tracks) != required_count:
        raise AbletonRepairExecutionError(
            "Live track count does not match verification "
            f"(expected {required_count}, observed {len(tracks)}). "
            "Refusing Live mutation."
        )
    current_by_index = _tracks_by_index(tracks)
    if observed_tracks is not None:
        _assert_track_identity(observed_tracks, current_by_index, origin="verification")
    expected_tracks = expected.get("tracks") or []
    if not isinstance(expected_tracks, list):
        raise AbletonRepairExecutionError(
            "Verification expected.tracks must be a list. Refusing Live mutation."
        )
    _assert_track_identity(expected_tracks, current_by_index, origin="expected")
    return {
        "read_only": True,
        "observed_tempo": current_tempo,
        "expected_tempo": expected_bpm,
        "track_count": len(tracks),
        "track_identity_match": True,
    }


def _require_observed_tempo(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AbletonRepairExecutionError(
            f"{label} is missing or not a finite number. Refusing Live mutation."
        )
    number = float(value)
    if not math.isfinite(number):
        raise AbletonRepairExecutionError(
            f"{label} is missing or not a finite number. Refusing Live mutation."
        )
    return number


def _verification_observed_tracks(
    verification: Mapping[str, Any],
) -> list[Any] | None:
    observed = verification.get("observed")
    if not isinstance(observed, Mapping):
        return None
    live_state = observed.get("live_state")
    if not isinstance(live_state, Mapping):
        return None
    tracks = live_state.get("tracks")
    if not isinstance(tracks, list):
        return None
    return tracks


def _assert_track_identity(
    expected_rows: Sequence[Any],
    current_by_index: Mapping[int, Mapping[str, Any]],
    *,
    origin: str,
) -> None:
    for row in expected_rows:
        if not isinstance(row, Mapping):
            raise AbletonRepairExecutionError(
                f"Malformed {origin} track row. Refusing Live mutation."
            )
        index = row.get("index")
        name = row.get("name")
        if isinstance(index, bool) or not isinstance(index, int):
            raise AbletonRepairExecutionError(
                f"Malformed {origin} track index. Refusing Live mutation."
            )
        if not isinstance(name, str):
            raise AbletonRepairExecutionError(
                f"Malformed {origin} track name. Refusing Live mutation."
            )
        found = current_by_index.get(index)
        if found is None or found.get("name") != name:
            raise AbletonRepairExecutionError(
                f"Live track identity mismatch at index {index}: {origin} name "
                f"{name!r}, current {None if found is None else found.get('name')!r}. "
                "Refusing Live mutation."
            )


def _tracks_by_index(tracks: Sequence[Any]) -> dict[int, Mapping[str, Any]]:
    by_index: dict[int, Mapping[str, Any]] = {}
    for item in tracks:
        if not isinstance(item, Mapping):
            continue
        index = item.get("index")
        if isinstance(index, int) and not isinstance(index, bool):
            by_index[index] = item
    return by_index


def _success_receipt_for_identity(
    receipt: Mapping[str, Any] | None,
    *,
    repair_plan_sha256: str,
    check_id: str,
) -> bool:
    if not receipt:
        return False
    if receipt.get("mode") != MODE_EXECUTE or receipt.get("status") != STATUS_SUCCESS:
        return False
    if receipt.get("execution_state") != STATE_APPLIED_UNVERIFIED:
        return False
    source = receipt.get("source")
    selection = receipt.get("selection")
    if not isinstance(source, Mapping) or not isinstance(selection, Mapping):
        return False
    repair = source.get("repair_plan")
    if not isinstance(repair, Mapping):
        return False
    return (
        repair.get("sha256") == repair_plan_sha256
        and selection.get("check_id") == check_id
    )


def _load_receipt(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _source_row(repair_plan: Mapping[str, Any], key: str) -> dict[str, Any]:
    source = repair_plan.get("source")
    if not isinstance(source, Mapping):
        return {"path": key, "sha256": None}
    if key == "verification":
        row = source.get("verification")
    elif key == "handoff":
        row = source.get("handoff")
    elif key == "arrangement_plan":
        row = source.get("arrangement_plan")
    elif key == "original_execution_receipt":
        row = source.get("execution_receipt")
    elif key == "original_job_plan":
        row = source.get("job_plan")
    else:
        row = source.get(key)
    if not isinstance(row, Mapping):
        return {"path": key, "sha256": None}
    return {"path": row.get("path"), "sha256": row.get("sha256")}


def _unresolved_counts(
    repair_plan: Mapping[str, Any], selected_check_id: str
) -> dict[str, int]:
    candidates = _object_list(repair_plan.get("candidate_actions"), "candidate_actions")
    manuals = _object_list(repair_plan.get("manual_actions"), "manual_actions")
    remaining = sum(
        1 for item in candidates if item.get("check_id") != selected_check_id
    )
    return {
        "candidate_actions": remaining,
        "manual_actions": len(manuals),
    }


def _object_list(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AbletonRepairExecutionError(
            f"Repair plan {label} must be a list of objects. Refusing execution."
        )
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise AbletonRepairExecutionError(
                f"Repair plan {label} must contain objects. Refusing execution."
            )
        rows.append(item)
    return rows


def _expected_bpm(selection: ValidatedRepairSelection) -> float:
    tempo_check = _require_failed_tempo_check(
        selection.verification, selection.selected_check_id
    )
    return _finite_bpm(tempo_check.get("expected"), "verification expected tempo")


def _build_receipt(
    selection: ValidatedRepairSelection,
    *,
    mode: str,
    status: str,
    execution_state: str,
    authorization: Mapping[str, Any] | None,
    preflight: Mapping[str, Any] | None,
    job_plan_file: Path | None,
    job_plan_sha256: str | None,
    request_sha256: str,
    source_operation_sha256: str,
    import_result: CommandResult | None,
    run_result: CommandResult | None,
    completed: int | None,
    failed: int | None,
    pending: int | None,
    live_mutation_attempted: bool,
    next_action: str,
    error: str | None = None,
) -> dict[str, Any]:
    root = selection.project_dir
    source_plan = selection.repair_plan.get("source")
    source_plan = source_plan if isinstance(source_plan, Mapping) else {}
    job_plan_row: dict[str, Any] | None = None
    if job_plan_file is not None:
        job_plan_row = {
            "path": _relpath(job_plan_file, root),
            "sha256": job_plan_sha256,
        }
    receipt: dict[str, Any] = {
        "ableton_repair_execution_version": ABLETON_REPAIR_EXECUTION_VERSION,
        "mode": mode,
        "status": status,
        "execution_state": execution_state,
        "executed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "repair_plan": {
                "path": _relpath(selection.repair_plan_file, root),
                "sha256": selection.repair_plan_sha256,
            },
            "verification": {
                "path": _relpath(selection.verification_file, root),
                "sha256": _source_row(selection.repair_plan, "verification").get(
                    "sha256"
                ),
            },
            "arrangement_plan": dict(_source_row(selection.repair_plan, "arrangement_plan")),
            "handoff": dict(_source_row(selection.repair_plan, "handoff")),
            "original_execution_receipt": dict(
                _source_row(selection.repair_plan, "original_execution_receipt")
            ),
            "original_job_plan": dict(
                _source_row(selection.repair_plan, "original_job_plan")
            ),
            "adopted_round": source_plan.get("adopted_round"),
        },
        "selection": {
            "check_id": selection.selected_check_id,
            "source_operation_index": selection.source_operation_index,
            "source_operation_sha256": source_operation_sha256,
            "repair_request_sha256": request_sha256,
            "operation": "set_tempo",
            "expected_bpm": _expected_bpm(selection),
            "candidate_reapply_is_safety_claim": False,
        },
        "authorization": dict(authorization) if authorization is not None else None,
        "preflight": dict(preflight) if preflight is not None else None,
        "repair_job_plan": job_plan_row,
        "import_kihachi": _command_record(import_result),
        "run": _command_record(run_result),
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "unresolved": _unresolved_counts(
            selection.repair_plan, selection.selected_check_id
        ),
        "boundary": {
            "live_access": "AbletonGPT",
            "kihachi_direct_live_access": False,
            "live_mutation_attempted": live_mutation_attempted,
            "live_repair_verified": False,
            "auto_verify": False,
            "auto_adoption": False,
            "preference_memory_appended": False,
        },
        "next_action": next_action,
    }
    if error is not None:
        receipt["error"] = error
    return receipt


def _write_failed_receipt(
    receipt_file: Path,
    selection: ValidatedRepairSelection,
    *,
    mode: str,
    authorization: Mapping[str, Any] | None,
    job_plan_file: Path | None,
    job_plan_sha256: str | None,
    import_result: CommandResult | None,
    run_result: CommandResult | None,
    preflight: Mapping[str, Any] | None,
    request_sha256: str,
    source_operation_sha256: str,
    live_mutation_attempted: bool,
    error: str,
    completed: int | None = None,
    failed: int | None = None,
    pending: int | None = None,
    execution_state: str | None = None,
    next_action: str | None = None,
) -> None:
    if execution_state is None:
        execution_state = (
            STATE_ATTEMPTED_UNVERIFIED if live_mutation_attempted else STATE_PREPARED
        )
        if mode == MODE_EXECUTE and not live_mutation_attempted:
            execution_state = STATE_PREPARED
        if error == "run_failed":
            execution_state = STATE_ATTEMPTED_UNVERIFIED
        elif mode == MODE_EXECUTE:
            execution_state = (
                STATE_ATTEMPTED_UNVERIFIED
                if live_mutation_attempted
                else STATE_PREPARED
            )
    if next_action is None:
        next_action = (
            NEXT_VERIFY_BEFORE_RETRY
            if live_mutation_attempted
            else "inspect the repair receipt; the Live job was not started"
        )
    receipt = _build_receipt(
        selection,
        mode=mode,
        status=STATUS_FAILED,
        execution_state=execution_state,
        authorization=authorization,
        preflight=preflight,
        job_plan_file=job_plan_file,
        job_plan_sha256=job_plan_sha256,
        request_sha256=request_sha256,
        source_operation_sha256=source_operation_sha256,
        import_result=import_result,
        run_result=run_result,
        completed=completed,
        failed=failed,
        pending=pending,
        live_mutation_attempted=live_mutation_attempted,
        next_action=next_action,
        error=error,
    )
    _atomic_write_json(receipt_file, receipt)


def _source_fingerprints(selection: ValidatedRepairSelection) -> dict[str, str]:
    root = selection.project_dir
    fingerprints: dict[str, str] = {}
    for path in (
        selection.repair_plan_file,
        selection.verification_file,
        selection.arrangement_plan_file,
        root / "ableton_handoff.json",
        root / "ableton_execution.json",
        root / "ableton_job_plan.json",
        root / "revision_log.json",
        root / "preference_memory.json",
        root / "song_spec.json",
    ):
        if path.is_file():
            fingerprints[str(path.resolve())] = _file_sha256(path)
    return fingerprints


def _assert_unchanged(fingerprints: Mapping[str, str]) -> None:
    for path_text, digest in fingerprints.items():
        path = Path(path_text)
        if not path.is_file():
            raise AbletonRepairExecutionError(
                f"VS9 must not remove {path.name}; it disappeared during repair execution."
            )
        actual = _file_sha256(path)
        if actual != digest:
            raise AbletonRepairExecutionError(
                f"Source artifact changed during repair execution: {path.name}. "
                "Refusing Live mutation."
            )


def _python_executable(abletongpt_python: Path | str | None) -> str:
    if abletongpt_python is None:
        return sys.executable
    path = Path(abletongpt_python)
    if path.exists():
        return str(path.resolve())
    return str(path)


def _import_kihachi_argv(
    python: str,
    *,
    arrangement_plan: Path,
    job_plan: Path,
) -> list[str]:
    return [
        python,
        "-m",
        ABLETONGPT_JOBS_MODULE,
        "import-kihachi",
        "--arrangement-plan",
        str(arrangement_plan),
        "--out",
        str(job_plan),
    ]


def _run_argv(python: str, job_plan: Path) -> list[str]:
    return [
        python,
        "-m",
        ABLETONGPT_JOBS_MODULE,
        "run",
        "--plan",
        str(job_plan),
    ]


def _module_unavailable(result: CommandResult) -> bool:
    blob = f"{result.stderr}\n{result.stdout}"
    return (
        "No module named abletongpt" in blob
        or "ModuleNotFoundError: No module named 'abletongpt'" in blob
    )


def _command_record(result: CommandResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "returncode": result.returncode,
        "stdout": _bound_text(result.stdout),
        "stderr": _bound_text(result.stderr),
    }


def _execution_counts(
    result: CommandResult, job_plan_file: Path
) -> tuple[int | None, int | None, int | None]:
    match = _COUNT_RE.search(f"{result.stdout}\n{result.stderr}")
    if match:
        pending = int(match.group(3)) if match.group(3) is not None else None
        return int(match.group(1)), int(match.group(2)), pending
    try:
        payload = json.loads(job_plan_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, None, None
    steps = payload.get("steps") if isinstance(payload, dict) else None
    if not isinstance(steps, list) or not steps:
        return None, None, None
    statuses = [
        str(step.get("status", "")).lower()
        for step in steps
        if isinstance(step, Mapping)
    ]
    if not any(status in {"succeeded", "skipped", "failed", "pending"} for status in statuses):
        return None, None, None
    completed = sum(1 for status in statuses if status in {"succeeded", "skipped"})
    failed = sum(1 for status in statuses if status == "failed")
    pending = sum(1 for status in statuses if status == "pending")
    return completed, failed, pending


def _format_counts(completed: Any, failed: Any, pending: Any) -> str:
    parts: list[str] = []
    if isinstance(completed, int):
        parts.append(f"completed={completed}")
    if isinstance(failed, int):
        parts.append(f"failed={failed}")
    if isinstance(pending, int):
        parts.append(f"pending={pending}")
    return " ".join(parts)


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bound_text(text: str, limit: int = MAX_CAPTURED_CHARS) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n... [{omitted} characters omitted]"


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
