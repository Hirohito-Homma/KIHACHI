"""VS9/VS10 — Human-authorized Ableton repair execution.

Consumes a VS8 ``ableton_repair_plan.json``, requires an explicit SHA-256
approval of that exact file, and applies one selected candidate through
AbletonGPT:

- VS9: ``set_tempo`` as a one-step JobPlan
- VS10: one guarded ``repair_live_device`` request (device power only,
  when VS7 evidence can prove identity and an inactive device)

Never talks to the Live socket itself, never auto-verifies, never replays
instrument/drum-kit loading, and never treats ``candidate_reapply`` as a
safety claim.

Architectural contract preserved:

    KIHACHI Music AI = decides what Live should contain
    AbletonGPT       = reads/writes Ableton Live
    VS9/VS10         = authorized repair + unverified receipt
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
    DEVICE_OPS,
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

ABLETON_REPAIR_EXECUTION_VERSION = "0.2"
ABLETON_REPAIR_EXECUTION_NAME = "ableton_repair_execution.json"
ABLETON_REPAIR_JOB_PLAN_NAME = "ableton_repair_job_plan.json"
SUPPORTED_TEMPO_OPERATIONS = frozenset({"set_tempo"})
SUPPORTED_DEVICE_OPERATIONS = frozenset(DEVICE_OPS)
SUPPORTED_REPAIR_OPERATIONS = SUPPORTED_TEMPO_OPERATIONS | SUPPORTED_DEVICE_OPERATIONS
REPAIR_KIND_TEMPO = "tempo"
REPAIR_KIND_DEVICE = "device"
DEVICE_POWER_OPERATION = "set_device_power"
AUTHORIZATION_METHOD = "explicit_cli_plan_sha256"
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
ABLETON_DEVICE_TYPE_INSTRUMENT = 1
ABLETON_DEVICE_TYPE_AUDIO_EFFECT = 2
ABLETON_DEVICE_TYPE_MIDI_EFFECT = 3
ABLETONGPT_DEVICE_REPAIR_STATUSES = frozenset(
    {"repaired", "noop", "refused", "failed"}
)

MODE_PREPARE = "prepare_only"
MODE_EXECUTE = "execute"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATE_PREPARED = "repair_prepared_not_applied"
STATE_APPLIED_UNVERIFIED = "repair_applied_unverified"
STATE_SATISFIED_UNVERIFIED = "repair_satisfied_unverified"
STATE_ATTEMPTED_UNVERIFIED = "repair_attempted_unverified"
SUCCESS_EXECUTION_STATES = frozenset(
    {STATE_APPLIED_UNVERIFIED, STATE_SATISFIED_UNVERIFIED}
)

NEXT_VERIFY = "run kihachi ableton-verify PROJECT explicitly"
NEXT_VERIFY_BEFORE_RETRY = (
    "run kihachi ableton-verify PROJECT before retrying; "
    "Live state may have changed"
)

# Runs inside the AbletonGPT interpreter.  Import-only: does not construct
# AbletonBridge or open the Live socket.  KIHACHI never imports this module.
ABLETONGPT_DEVICE_REPAIR_PROBE = r"""
import json
import sys

try:
    from abletongpt.device_repair import repair_live_device
    from abletongpt.bridge import AbletonBridge
except ImportError as exc:
    text = str(exc)
    name = getattr(exc, "name", "") or ""
    if (
        name == "abletongpt"
        or "No module named 'abletongpt'" in text
        or "No module named abletongpt" in text
    ):
        print("No module named abletongpt", file=sys.stderr)
        sys.exit(1)
    print("AbletonGPT device_repair is unavailable: %s" % exc, file=sys.stderr)
    sys.exit(2)
if not callable(repair_live_device):
    print(
        "AbletonGPT device_repair is unavailable: repair_live_device is not callable",
        file=sys.stderr,
    )
    sys.exit(2)
print(json.dumps({"capability": "repair_live_device", "status": "available"}))
"""

# Runs inside the AbletonGPT interpreter.  One repair_live_device call, then
# exit.  KIHACHI never opens the Live socket / LOM protocol itself.
ABLETONGPT_DEVICE_REPAIR_RUNNER = r"""
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
try:
    from abletongpt.device_repair import repair_live_device
    from abletongpt.bridge import AbletonBridge, AbletonConnectionError
except ImportError as exc:
    text = str(exc)
    name = getattr(exc, "name", "") or ""
    if (
        name == "abletongpt"
        or "No module named 'abletongpt'" in text
        or "No module named abletongpt" in text
    ):
        print("No module named abletongpt", file=sys.stderr)
        sys.exit(1)
    print("AbletonGPT device_repair is unavailable: %s" % exc, file=sys.stderr)
    sys.exit(2)

try:
    bridge = AbletonBridge()
except AbletonConnectionError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)

result = repair_live_device(bridge, request)
if not isinstance(result, dict):
    print("device repair result is not a JSON object", file=sys.stderr)
    sys.exit(1)
print(json.dumps(result, ensure_ascii=False))
"""
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
    repair_kind: str
    guarded_request: dict[str, Any] | None


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
    """Resolve one tempo or guarded-device candidate against the arrangement plan.

    Refuses before any external process or Live read.  ``candidate_reapply``
    is not treated as permission to execute.
    """

    if not isinstance(check_id, str) or not check_id.strip():
        raise AbletonRepairExecutionError(
            "A --check-id is required. VS9/VS10 execute one selected repair "
            "candidate; they do not apply the whole repair plan."
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
                "candidate_reapply. VS9/VS10 do not execute manual items, "
                "track repairs, or unmapped checks."
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
            f"Repair execution supports set_tempo and one guarded device "
            f"repair (selected {op_name!r} for check {selected_id!r}). "
            "This candidate is unsupported_for_execution: Session clip, "
            "Arrangement, and track handlers can overwrite or duplicate Live "
            "content. candidate_reapply is not a safety claim."
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

    repair_kind = REPAIR_KIND_TEMPO
    guarded_request: dict[str, Any] | None = None
    if op_name in SUPPORTED_TEMPO_OPERATIONS:
        tempo_check = _require_failed_tempo_check(loaded.verification, selected_id)
        expected_bpm = _finite_bpm(
            tempo_check.get("expected"), "verification expected tempo"
        )
        operation_bpm = _validate_set_tempo_params(full_operation.get("params"))
        if abs(expected_bpm - operation_bpm) > TEMPO_TOLERANCE_BPM:
            raise AbletonRepairExecutionError(
                f"Expected BPM {expected_bpm:g} does not match set_tempo "
                f"operation BPM {operation_bpm:g}. Refusing execution."
            )
    else:
        repair_kind = REPAIR_KIND_DEVICE
        guarded_request = _derive_guarded_device_request(
            verification=loaded.verification,
            check_id=selected_id,
            source_operation=full_operation,
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
        repair_kind=repair_kind,
        guarded_request=guarded_request,
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
    """Authorize and apply one tempo or guarded-device repair through AbletonGPT."""

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

    if selection.repair_kind == REPAIR_KIND_DEVICE:
        return _execute_device_repair(
            selection,
            prepare_only=prepare_only,
            authorization=authorization,
            already_applied=already_applied,
            previous=previous,
            abletongpt_python=abletongpt_python,
            runner=runner,
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
    repair_kind = selection.get("repair_kind") or (
        REPAIR_KIND_DEVICE if operation == DEVICE_POWER_OPERATION else REPAIR_KIND_TEMPO
    )
    if manifest.prepare_only or receipt.get("mode") == MODE_PREPARE:
        lines = [
            "Prepared Ableton repair execution (no Live job)",
            f"Selected check: {check_id}",
            f"Operation: {operation}",
            f"Repair kind: {repair_kind}",
            f"Repair plan SHA-256: {sha}",
        ]
        if repair_kind == REPAIR_KIND_TEMPO:
            lines.append(f"Repair job plan: {ABLETON_REPAIR_JOB_PLAN_NAME}")
        else:
            lines.append("Guarded request: repair_live_device (not a JobPlan)")
        lines.extend(
            [
                f"Receipt: {ABLETON_REPAIR_EXECUTION_NAME}",
                "- Live read: no",
                "- Live mutation: no",
                "- candidate_reapply safety claim: no",
                f"- run again with --approve-plan-sha {sha} to authorize this exact plan",
            ]
        )
        return lines
    mutation = receipt.get("boundary") if isinstance(receipt.get("boundary"), Mapping) else {}
    mutation_attempted = mutation.get("live_mutation_attempted")
    ableton_status = receipt.get("abletongpt_result")
    result_status = None
    if isinstance(ableton_status, Mapping):
        result_status = ableton_status.get("status")
    run_label = (
        "success" if receipt.get("status") == STATUS_SUCCESS else "failed"
    )
    if repair_kind == REPAIR_KIND_DEVICE and result_status:
        run_label = str(result_status)
    return [
        "Applied authorized Ableton repair candidate through AbletonGPT",
        f"Selected check: {check_id}",
        f"Operation: {operation}",
        f"Repair kind: {repair_kind}",
        f"AbletonGPT run: {run_label}",
        f"Receipt: {ABLETON_REPAIR_EXECUTION_NAME}",
        f"- Live mutation attempted: {'yes' if mutation_attempted else 'no'}",
        "- Live repair verified: no",
        "- auto-verify: no",
        "- adoption unchanged: yes",
        "- preference memory appended: no",
        "Next: run kihachi ableton-verify PROJECT explicitly",
    ]


def _selection_receipt_fields(
    selection: ValidatedRepairSelection,
    *,
    request_sha256: str,
    source_operation_sha256: str,
    guarded_request: Mapping[str, Any] | None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "check_id": selection.selected_check_id,
        "source_operation_index": selection.source_operation_index,
        "source_operation_sha256": source_operation_sha256,
        "repair_request_sha256": request_sha256,
        "repair_kind": selection.repair_kind,
        "candidate_reapply_is_safety_claim": False,
    }
    if selection.repair_kind == REPAIR_KIND_DEVICE:
        request = guarded_request if guarded_request is not None else selection.guarded_request
        operation = None
        if isinstance(request, Mapping):
            operation = request.get("operation")
        fields["operation"] = operation or DEVICE_POWER_OPERATION
        fields["guarded_request"] = dict(request) if isinstance(request, Mapping) else None
        fields["guarded_request_sha256"] = request_sha256
    else:
        fields["operation"] = "set_tempo"
        fields["expected_bpm"] = _expected_bpm(selection)
    return fields


def _execute_device_repair(
    selection: ValidatedRepairSelection,
    *,
    prepare_only: bool,
    authorization: Mapping[str, Any] | None,
    already_applied: bool,
    previous: Mapping[str, Any] | None,
    abletongpt_python: Path | str | None,
    runner: CommandRunner | None,
) -> AbletonRepairExecutionManifest:
    """Authorize one guarded repair_live_device call.  Not JobPlan replay."""

    receipt_file = ableton_repair_execution_path(selection.project_dir)
    fingerprints = _source_fingerprints(selection)
    run = runner if runner is not None else run_command
    python = _python_executable(abletongpt_python)
    request = dict(selection.guarded_request or {})
    request_sha256 = _canonical_sha256(request)
    source_operation_sha256 = _canonical_sha256(selection.source_operation)
    probe_result: CommandResult | None = None

    try:
        probe_result = run([python, "-c", ABLETONGPT_DEVICE_REPAIR_PROBE])
        if _module_unavailable(probe_result):
            raise AbletonRepairExecutionError(
                "AbletonGPT is not available in "
                f"{python}. Install AbletonGPT in that interpreter or pass "
                "--abletongpt-python PATH. The Live mutation was not attempted. "
                f"Captured stderr: {_bound_text(probe_result.stderr, 400)}"
            )
        if _device_repair_unavailable(probe_result):
            raise AbletonRepairExecutionError(
                "AbletonGPT in "
                f"{python} does not provide abletongpt.device_repair."
                "repair_live_device. Install the guarded selective device-repair "
                "AbletonGPT (PR #138). The Live mutation was not attempted. "
                "VS10 does not fall back to apply_live_instrument_selection / "
                "apply_live_drum_kit replay."
            )
        if probe_result.returncode != 0:
            raise AbletonRepairExecutionError(
                "AbletonGPT device-repair capability probe failed "
                f"(exit {probe_result.returncode}). The Live mutation was not "
                f"attempted. stderr: {_bound_text(probe_result.stderr, 800)}"
            )
    except AbletonRepairExecutionError:
        if not (prepare_only and already_applied):
            _write_failed_receipt(
                receipt_file,
                selection,
                mode=MODE_PREPARE if prepare_only else MODE_EXECUTE,
                authorization=authorization,
                job_plan_file=None,
                job_plan_sha256=None,
                import_result=probe_result,
                run_result=None,
                preflight=None,
                request_sha256=request_sha256,
                source_operation_sha256=source_operation_sha256,
                live_mutation_attempted=False,
                error="abletongpt_unavailable",
                guarded_request=request,
            )
        raise

    if prepare_only:
        if already_applied:
            return AbletonRepairExecutionManifest(
                project_dir=selection.project_dir,
                repair_plan_file=selection.repair_plan_file,
                job_plan_file=None,
                receipt_file=receipt_file,
                receipt=dict(previous) if previous is not None else {},
                prepare_only=True,
            )
        receipt = _build_receipt(
            selection,
            mode=MODE_PREPARE,
            status=STATUS_SUCCESS,
            execution_state=STATE_PREPARED,
            authorization=None,
            preflight=None,
            job_plan_file=None,
            job_plan_sha256=None,
            request_sha256=request_sha256,
            source_operation_sha256=source_operation_sha256,
            import_result=probe_result,
            run_result=None,
            completed=None,
            failed=None,
            pending=None,
            live_mutation_attempted=False,
            next_action=(
                "run again with --approve-plan-sha of the printed repair plan "
                "SHA-256 to authorize this exact plan"
            ),
            guarded_request=request,
        )
        _atomic_write_json(receipt_file, receipt)
        return AbletonRepairExecutionManifest(
            project_dir=selection.project_dir,
            repair_plan_file=selection.repair_plan_file,
            job_plan_file=None,
            receipt_file=receipt_file,
            receipt=receipt,
            prepare_only=True,
        )

    try:
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
    except AbletonRepairExecutionError:
        _write_failed_receipt(
            receipt_file,
            selection,
            mode=MODE_EXECUTE,
            authorization=authorization,
            job_plan_file=None,
            job_plan_sha256=None,
            import_result=probe_result,
            run_result=None,
            preflight=None,
            request_sha256=request_sha256,
            source_operation_sha256=source_operation_sha256,
            live_mutation_attempted=False,
            error="plan_changed",
            guarded_request=request,
        )
        raise

    with tempfile.TemporaryDirectory(prefix="kihachi-vs10-") as temp:
        request_path = Path(temp) / "repair_device_request.json"
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        run_result = run(
            [python, "-c", ABLETONGPT_DEVICE_REPAIR_RUNNER, str(request_path)]
        )

    parsed = _parse_device_repair_result(run_result)
    if parsed is None:
        _write_failed_receipt(
            receipt_file,
            selection,
            mode=MODE_EXECUTE,
            authorization=authorization,
            job_plan_file=None,
            job_plan_sha256=None,
            import_result=probe_result,
            run_result=run_result,
            preflight=None,
            request_sha256=request_sha256,
            source_operation_sha256=source_operation_sha256,
            live_mutation_attempted=False,
            error="device_repair_unreadable",
            guarded_request=request,
        )
        raise AbletonRepairExecutionError(
            "AbletonGPT device repair did not return a structured result. "
            "The Live mutation was not confirmed. Do not retry until "
            "`kihachi ableton-verify PROJECT` has been run explicitly. "
            f"stderr: {_bound_text(run_result.stderr, 800)}",
            exit_code=1,
        )

    status = str(parsed.get("status") or "")
    mutation_performed = bool(parsed.get("mutation_performed"))
    if status not in ABLETONGPT_DEVICE_REPAIR_STATUSES:
        _write_failed_receipt(
            receipt_file,
            selection,
            mode=MODE_EXECUTE,
            authorization=authorization,
            job_plan_file=None,
            job_plan_sha256=None,
            import_result=probe_result,
            run_result=run_result,
            preflight=None,
            request_sha256=request_sha256,
            source_operation_sha256=source_operation_sha256,
            live_mutation_attempted=mutation_performed,
            error="device_repair_unknown_status",
            guarded_request=request,
            abletongpt_result=parsed,
        )
        raise AbletonRepairExecutionError(
            f"AbletonGPT device repair returned unknown status {status!r}. "
            "No retry. Run `kihachi ableton-verify PROJECT` explicitly.",
            exit_code=1,
        )

    if status in {"refused", "failed"}:
        live_attempted = mutation_performed or status == "failed"
        _write_failed_receipt(
            receipt_file,
            selection,
            mode=MODE_EXECUTE,
            authorization=authorization,
            job_plan_file=None,
            job_plan_sha256=None,
            import_result=probe_result,
            run_result=run_result,
            preflight=None,
            request_sha256=request_sha256,
            source_operation_sha256=source_operation_sha256,
            live_mutation_attempted=live_attempted,
            error=str(parsed.get("reason") or status),
            execution_state=(
                STATE_ATTEMPTED_UNVERIFIED if live_attempted else STATE_PREPARED
            ),
            next_action=(
                NEXT_VERIFY_BEFORE_RETRY
                if live_attempted
                else "inspect the repair receipt; no retry and no JobPlan fallback"
            ),
            guarded_request=request,
            abletongpt_result=parsed,
        )
        reason = parsed.get("reason")
        if status == "refused":
            raise AbletonRepairExecutionError(
                "AbletonGPT refused the guarded device repair "
                f"({reason}). No mutation was retried and the original "
                "instrument/drum-kit JobPlan was not replayed. "
                f"stderr: {_bound_text(run_result.stderr, 400)}"
            )
        raise AbletonRepairExecutionError(
            "AbletonGPT device repair failed "
            f"({reason}). Live mutation may have occurred; this is not "
            "evidence that it did not. Do not retry until "
            "`kihachi ableton-verify PROJECT` has been run explicitly. "
            f"stderr: {_bound_text(run_result.stderr, 800)}",
            exit_code=1,
        )

    execution_state = (
        STATE_APPLIED_UNVERIFIED if status == "repaired" else STATE_SATISFIED_UNVERIFIED
    )
    receipt = _build_receipt(
        selection,
        mode=MODE_EXECUTE,
        status=STATUS_SUCCESS,
        execution_state=execution_state,
        authorization=authorization,
        preflight=None,
        job_plan_file=None,
        job_plan_sha256=None,
        request_sha256=request_sha256,
        source_operation_sha256=source_operation_sha256,
        import_result=probe_result,
        run_result=run_result,
        completed=1 if status == "repaired" else 0,
        failed=0,
        pending=0,
        live_mutation_attempted=mutation_performed,
        next_action=NEXT_VERIFY,
        guarded_request=request,
        abletongpt_result=parsed,
    )
    _atomic_write_json(receipt_file, receipt)
    _assert_unchanged(fingerprints)
    return AbletonRepairExecutionManifest(
        project_dir=selection.project_dir,
        repair_plan_file=selection.repair_plan_file,
        job_plan_file=None,
        receipt_file=receipt_file,
        receipt=receipt,
        prepare_only=False,
    )


def _derive_guarded_device_request(
    *,
    verification: Mapping[str, Any],
    check_id: str,
    source_operation: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one repair_live_device request from VS7 evidence.  Never guesses."""

    check = _require_failed_device_check(verification, check_id)
    expected = check.get("expected") if isinstance(check.get("expected"), Mapping) else {}
    track_index = expected.get("track_index")
    if isinstance(track_index, bool) or not isinstance(track_index, int) or track_index < 0:
        raise AbletonRepairExecutionError(
            f"Device check {check_id!r} is missing a non-negative track_index. "
            "Refusing execution."
        )
    kind = expected.get("kind")
    params = (
        source_operation.get("params")
        if isinstance(source_operation.get("params"), Mapping)
        else {}
    )
    if params.get("track_index") != track_index:
        raise AbletonRepairExecutionError(
            "Source operation track_index does not match the selected device "
            "check. Refusing to execute from the repair-plan display payload."
        )

    payload = _observed_device_payload(verification, track_index)
    if isinstance(payload, Mapping) and payload.get("error"):
        raise AbletonRepairExecutionError(
            f"get_track_devices failed for track {track_index}: "
            f"{payload.get('error')}. Missing/unreadable devices are never "
            "turned into insertion. This check remains manual."
        )
    if not isinstance(payload, list):
        raise AbletonRepairExecutionError(
            f"Device evidence for track {track_index} is missing or malformed. "
            "Insufficient expected/observed state; refusing execution."
        )
    if not payload:
        raise AbletonRepairExecutionError(
            f"Device on track {track_index} is missing. VS10 will not insert, "
            "replace, or reload an instrument/Drum Rack. This remains manual."
        )

    rows = [item for item in payload if isinstance(item, Mapping)]
    if len(payload) != 1 or len(rows) != 1:
        raise AbletonRepairExecutionError(
            f"Device identity on track {track_index} is ambiguous "
            f"({len(payload)} observed device(s)). VS10 repairs exactly one "
            "unambiguous device; this remains manual."
        )
    device = rows[0]
    _assert_supported_device_type(kind, device, track_index)
    device_index = device.get("index")
    if isinstance(device_index, bool) or not isinstance(device_index, int) or device_index < 0:
        raise AbletonRepairExecutionError(
            f"Observed device on track {track_index} has an unknown device index. "
            "Refusing execution rather than guessing."
        )
    device_name = _observed_device_name(device)
    if device_name is None:
        raise AbletonRepairExecutionError(
            f"Observed device on track {track_index} has no usable name. "
            "Wrong or unknown device identity is never turned into insertion."
        )
    if device.get("is_active") is not False:
        raise AbletonRepairExecutionError(
            f"Insufficient device evidence on track {track_index} to prove a "
            "safe guarded repair. VS10 does not invent set_device_parameter "
            "or reset_device_parameter from role/kind alone, and will not "
            "replay the original JobPlan."
        )
    track_name = _expected_track_name(verification, track_index)
    return {
        "track_index": track_index,
        "device_index": device_index,
        "operation": DEVICE_POWER_OPERATION,
        "enabled": True,
        "expected_track_name": track_name,
        "expected_device_name": device_name,
        "expected_power_state": False,
    }


def _require_failed_device_check(
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
    if check.get("category") != "devices" or check.get("status") != CHECK_FAIL:
        raise AbletonRepairExecutionError(
            f"Verification check {check_id!r} is not a failed device check "
            f"(category={check.get('category')!r}, status={check.get('status')!r}). "
            "Refusing execution."
        )
    return check


def _observed_device_payload(
    verification: Mapping[str, Any], track_index: int
) -> Any:
    observed = verification.get("observed")
    if not isinstance(observed, Mapping):
        return None
    devices = observed.get("devices")
    if not isinstance(devices, Mapping):
        return None
    return devices.get(str(track_index))


def _observed_device_name(device: Mapping[str, Any]) -> str | None:
    for key in ("name", "class_display_name", "class_name"):
        value = device.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _assert_supported_device_type(
    kind: Any, device: Mapping[str, Any], track_index: int
) -> None:
    device_type = device.get("type")
    if device_type is None:
        return
    if isinstance(device_type, bool) or not isinstance(device_type, int):
        raise AbletonRepairExecutionError(
            f"Observed device type on track {track_index} is unusable. "
            "Wrong device type is refused."
        )
    if kind in {"instrument", "drum_kit"} and device_type in {
        ABLETON_DEVICE_TYPE_AUDIO_EFFECT,
        ABLETON_DEVICE_TYPE_MIDI_EFFECT,
    }:
        raise AbletonRepairExecutionError(
            f"Observed device type {device_type} on track {track_index} does "
            f"not match expected {kind}. Wrong device type is refused; VS10 "
            "will not replace or insert a device."
        )
    if kind in {"instrument", "drum_kit"} and device_type != ABLETON_DEVICE_TYPE_INSTRUMENT:
        raise AbletonRepairExecutionError(
            f"Observed device type {device_type} on track {track_index} is not "
            "an instrument. Wrong device type is refused."
        )


def _expected_track_name(verification: Mapping[str, Any], track_index: int) -> str:
    expected = verification.get("expected")
    if not isinstance(expected, Mapping):
        raise AbletonRepairExecutionError(
            "Verification is missing expected Live state. Refusing execution."
        )
    tracks = expected.get("tracks") or []
    if not isinstance(tracks, list):
        raise AbletonRepairExecutionError(
            "Verification expected.tracks must be a list. Refusing execution."
        )
    matched = [
        item
        for item in tracks
        if isinstance(item, Mapping) and item.get("index") == track_index
    ]
    if len(matched) != 1:
        raise AbletonRepairExecutionError(
            f"Expected track identity for index {track_index} is missing or "
            "ambiguous. Refusing execution."
        )
    name = matched[0].get("name")
    if not isinstance(name, str) or not name:
        raise AbletonRepairExecutionError(
            f"Expected track {track_index} is missing a name. Refusing execution."
        )
    observed_tracks = _verification_observed_tracks(verification)
    if observed_tracks is not None:
        found = _tracks_by_index(observed_tracks).get(track_index)
        if found is not None and found.get("name") not in {None, name}:
            raise AbletonRepairExecutionError(
                f"Track identity mismatch at index {track_index}: expected "
                f"{name!r}, observed {found.get('name')!r}. Refusing execution."
            )
    return name


def _parse_device_repair_result(result: CommandResult) -> dict[str, Any] | None:
    text = result.stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def _bounded_device_result(result: Mapping[str, Any]) -> dict[str, Any]:
    bounded: dict[str, Any] = {}
    for key in (
        "status",
        "operation",
        "target",
        "before",
        "after",
        "mutation_performed",
        "reason",
        "detail",
    ):
        if key in result:
            bounded[key] = result[key]
    return bounded


def _device_repair_unavailable(result: CommandResult) -> bool:
    blob = f"{result.stderr}\n{result.stdout}"
    return (
        "device_repair is unavailable" in blob
        or "No module named 'abletongpt.device_repair'" in blob
        or "No module named abletongpt.device_repair" in blob
        or "cannot import name 'repair_live_device'" in blob
    )


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
    if receipt.get("execution_state") not in SUCCESS_EXECUTION_STATES:
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
    guarded_request: Mapping[str, Any] | None = None,
    abletongpt_result: Mapping[str, Any] | None = None,
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
        "selection": _selection_receipt_fields(
            selection,
            request_sha256=request_sha256,
            source_operation_sha256=source_operation_sha256,
            guarded_request=guarded_request,
        ),
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
    if selection.repair_kind == REPAIR_KIND_DEVICE:
        request = guarded_request if guarded_request is not None else selection.guarded_request
        receipt["repair_kind"] = REPAIR_KIND_DEVICE
        receipt["guarded_request"] = dict(request) if isinstance(request, Mapping) else None
        receipt["abletongpt_result"] = (
            _bounded_device_result(abletongpt_result)
            if abletongpt_result is not None
            else None
        )
        if isinstance(abletongpt_result, Mapping):
            receipt["mutation_performed"] = bool(
                abletongpt_result.get("mutation_performed")
            )
        else:
            receipt["mutation_performed"] = False
    else:
        receipt["repair_kind"] = REPAIR_KIND_TEMPO
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
    guarded_request: Mapping[str, Any] | None = None,
    abletongpt_result: Mapping[str, Any] | None = None,
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
        guarded_request=guarded_request,
        abletongpt_result=abletongpt_result,
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
