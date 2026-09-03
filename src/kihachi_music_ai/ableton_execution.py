"""VS6 — Ableton handoff execution integration.

Consumes an existing VS5 ``ableton_handoff.json`` and invokes AbletonGPT as an
external executor.  Never adopts a take, never talks to Ableton Live itself,
and never lets ranking or preference memory choose the Live target.

Architectural contract preserved:

    KIHACHI Music AI = decides what to make
    AbletonGPT       = operates Ableton Live
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .ableton_handoff import (
    ABLETON_HANDOFF_VERSION,
    ableton_handoff_path,
)
from .models import SongSpec
from .repaint_planner import song_spec_sha256

ABLETON_EXECUTION_VERSION = "0.1"
ABLETON_EXECUTION_NAME = "ableton_execution.json"
ABLETON_JOB_PLAN_NAME = "ableton_job_plan.json"
HANDOFF_READY_STATES = frozenset({"handoff_ready_not_applied"})
SUPPORTED_HANDOFF_VERSIONS = frozenset({ABLETON_HANDOFF_VERSION})
ABLETONGPT_JOBS_MODULE = "abletongpt.cli.jobs"
MAX_CAPTURED_CHARS = 4000
_COUNT_RE = re.compile(
    r"completed\s*=\s*(\d+)\s+failed\s*=\s*(\d+)(?:\s+pending\s*=\s*(\d+))?",
    re.IGNORECASE,
)


class AbletonExecutionError(ValueError):
    """Actionable refusal before or during AbletonGPT invocation."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True)
class ValidatedHandoff:
    """Provenance-checked VS5 handoff, ready to hand to AbletonGPT."""

    project_dir: Path
    handoff_file: Path
    handoff: dict[str, Any]
    handoff_sha256: str
    arrangement_plan_file: Path
    arrangement_plan_sha256: str
    adopted_round: int


@dataclass(frozen=True)
class AbletonExecutionManifest:
    project_dir: Path
    handoff_file: Path
    arrangement_plan_file: Path
    job_plan_file: Path | None
    receipt_file: Path
    receipt: dict[str, Any]
    prepare_only: bool


def ableton_execution_path(project_dir: Path) -> Path:
    return Path(project_dir) / ABLETON_EXECUTION_NAME


def ableton_job_plan_path(project_dir: Path) -> Path:
    return Path(project_dir) / ABLETON_JOB_PLAN_NAME


def run_command(argv: Sequence[str]) -> CommandResult:
    """Safe subprocess boundary: argument list, no shell, captured output."""

    argv_list = [str(part) for part in argv]
    try:
        completed = subprocess.run(
            argv_list,
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as error:
        missing = argv_list[0] if argv_list else "<empty argv>"
        raise AbletonExecutionError(
            f"AbletonGPT Python interpreter not found: {missing}. "
            "Pass --abletongpt-python PATH to a Python that has AbletonGPT installed."
        ) from error
    except OSError as error:
        raise AbletonExecutionError(
            f"Failed to invoke AbletonGPT ({argv_list[:4]}): {error}"
        ) from error
    return CommandResult(
        argv=tuple(argv_list),
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def load_validated_handoff(project_dir: Path) -> ValidatedHandoff:
    """Load ``ableton_handoff.json`` and refuse before any external process."""

    root = Path(project_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"project not found: {root}")

    handoff_file = ableton_handoff_path(root)
    if not handoff_file.is_file():
        raise AbletonExecutionError(
            f"No Ableton handoff found: {handoff_file}. "
            "Run `kihachi ableton-handoff PROJECT` after an explicit "
            "`kihachi adopt PROJECT --round N` first."
        )

    try:
        raw = handoff_file.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError) as error:
        raise AbletonExecutionError(
            f"Unable to read Ableton handoff: {handoff_file}"
        ) from error
    except json.JSONDecodeError as error:
        raise AbletonExecutionError(
            f"Ableton handoff is not valid JSON: {handoff_file} ({error.msg})"
        ) from error
    if not isinstance(payload, dict):
        raise AbletonExecutionError(
            f"Ableton handoff must be a JSON object: {handoff_file}"
        )

    version = payload.get("ableton_handoff_version")
    if not isinstance(version, str) or version not in SUPPORTED_HANDOFF_VERSIONS:
        raise AbletonExecutionError(
            f"Unsupported ableton_handoff_version {version!r} in {handoff_file} "
            f"(supported: {', '.join(sorted(SUPPORTED_HANDOFF_VERSIONS))}). "
            "Refusing to invoke AbletonGPT."
        )

    state = payload.get("execution_state")
    if state not in HANDOFF_READY_STATES:
        raise AbletonExecutionError(
            f"Ableton handoff is not ready to apply (execution_state={state!r}). "
            f"Expected one of: {', '.join(sorted(HANDOFF_READY_STATES))}."
        )

    adopted_round = payload.get("adopted_round")
    if not isinstance(adopted_round, int):
        raise AbletonExecutionError(
            f"Ableton handoff is missing a valid adopted_round: {handoff_file}"
        )

    base_dir = _handoff_base_dir(handoff_file, payload)
    allowed_root = handoff_file.parent.parent.resolve()

    arrangement = payload.get("arrangement_plan")
    if not isinstance(arrangement, Mapping):
        raise AbletonExecutionError(
            f"Ableton handoff is missing arrangement_plan provenance: {handoff_file}"
        )
    arrangement_path = _resolve_declared_path(
        arrangement.get("path"),
        base_dir=base_dir,
        allowed_root=allowed_root,
        label="arrangement plan",
    )
    arrangement_sha = _require_digest(arrangement.get("sha256"), "arrangement plan")
    if not arrangement_path.is_file():
        raise AbletonExecutionError(
            f"Arrangement plan is missing: {arrangement_path} "
            f"(declared path {arrangement.get('path')!r}). "
            "Refusing to invoke AbletonGPT."
        )
    actual_arrangement_sha = _file_sha256(arrangement_path)
    if actual_arrangement_sha != arrangement_sha:
        raise AbletonExecutionError(
            "Arrangement plan SHA-256 mismatch: the file changed after the "
            "VS5 handoff was written (or does not match the recorded digest). "
            f"Declared {arrangement_sha}, on disk {actual_arrangement_sha}. "
            "Refusing to invoke AbletonGPT."
        )

    _verify_declared_audio(payload.get("audio"), base_dir, allowed_root)
    _verify_declared_song_spec(payload.get("song_spec"), base_dir, allowed_root)
    _verify_declared_midi(payload.get("midi"), base_dir, allowed_root)

    return ValidatedHandoff(
        project_dir=root,
        handoff_file=handoff_file.resolve(),
        handoff=payload,
        handoff_sha256=_file_sha256(handoff_file),
        arrangement_plan_file=arrangement_path,
        arrangement_plan_sha256=actual_arrangement_sha,
        adopted_round=adopted_round,
    )


def prepare_ableton_execution(
    project_dir: Path,
    *,
    rerun: bool = False,
    abletongpt_python: Path | str | None = None,
    runner: CommandRunner | None = None,
) -> AbletonExecutionManifest:
    """Validate the VS5 handoff and run AbletonGPT ``import-kihachi`` only."""

    return execute_ableton_handoff(
        project_dir,
        prepare_only=True,
        rerun=rerun,
        abletongpt_python=abletongpt_python,
        runner=runner,
    )


def execute_ableton_handoff(
    project_dir: Path,
    *,
    prepare_only: bool = False,
    rerun: bool = False,
    abletongpt_python: Path | str | None = None,
    runner: CommandRunner | None = None,
) -> AbletonExecutionManifest:
    """Apply a VS5 handoff through AbletonGPT.  Does not talk to Live itself."""

    validated = load_validated_handoff(project_dir)
    receipt_file = ableton_execution_path(validated.project_dir)
    job_plan_file = ableton_job_plan_path(validated.project_dir)
    previous = _load_receipt(receipt_file)
    live_applied = _live_applied_for_identity(
        previous,
        handoff_sha256=validated.handoff_sha256,
        arrangement_sha256=validated.arrangement_plan_sha256,
    )

    if not prepare_only and live_applied and not rerun:
        raise AbletonExecutionError(
            "This exact Ableton handoff was already applied successfully "
            f"(handoff sha256={validated.handoff_sha256}, "
            f"arrangement plan sha256={validated.arrangement_plan_sha256}). "
            "Re-running the Live job can duplicate tracks and clips. "
            "Pass --rerun to apply it again explicitly."
        )

    run = runner if runner is not None else run_command
    python = _python_executable(abletongpt_python)
    import_argv = _import_kihachi_argv(
        python,
        arrangement_plan=validated.arrangement_plan_file,
        job_plan=job_plan_file,
    )
    import_result = run(import_argv)
    if _module_unavailable(import_result):
        _write_failed_receipt(
            receipt_file,
            validated,
            mode="prepare_only" if prepare_only else "execute",
            job_plan_file=None,
            import_result=import_result,
            run_result=None,
            live_applied=live_applied,
            error="abletongpt_unavailable",
        )
        raise AbletonExecutionError(
            "AbletonGPT is not available in "
            f"{python}. Install AbletonGPT in that interpreter or pass "
            "--abletongpt-python PATH. "
            f"Captured stderr: {_bound_text(import_result.stderr, 400)}"
        )
    if import_result.returncode != 0:
        _write_failed_receipt(
            receipt_file,
            validated,
            mode="prepare_only" if prepare_only else "execute",
            job_plan_file=job_plan_file if job_plan_file.is_file() else None,
            import_result=import_result,
            run_result=None,
            live_applied=live_applied,
            error="import_kihachi_failed",
        )
        raise AbletonExecutionError(
            "AbletonGPT import-kihachi failed "
            f"(exit {import_result.returncode}). The Live job was not started. "
            f"stderr: {_bound_text(import_result.stderr, 800)}"
        )

    try:
        job_plan = _load_job_plan(job_plan_file)
    except AbletonExecutionError as error:
        _write_failed_receipt(
            receipt_file,
            validated,
            mode="prepare_only" if prepare_only else "execute",
            job_plan_file=job_plan_file if job_plan_file.is_file() else None,
            import_result=import_result,
            run_result=None,
            live_applied=live_applied,
            error="invalid_job_plan",
        )
        raise AbletonExecutionError(
            f"{error} The Live job was not started."
        ) from error

    job_plan_sha = _file_sha256(job_plan_file)
    if prepare_only:
        receipt = _build_receipt(
            validated,
            mode="prepare_only",
            status="success",
            execution_state="prepared_not_applied",
            job_plan_file=job_plan_file,
            job_plan_sha256=job_plan_sha,
            import_result=import_result,
            run_result=None,
            completed=len(job_plan["steps"]),
            failed=0,
            live_applied=live_applied,
        )
        _atomic_write_json(receipt_file, receipt)
        return AbletonExecutionManifest(
            project_dir=validated.project_dir,
            handoff_file=validated.handoff_file,
            arrangement_plan_file=validated.arrangement_plan_file,
            job_plan_file=job_plan_file,
            receipt_file=receipt_file,
            receipt=receipt,
            prepare_only=True,
        )

    run_argv = _run_argv(python, job_plan_file)
    run_result = run(run_argv)
    completed, failed, pending = _execution_counts(run_result, job_plan_file)
    success = (
        run_result.returncode == 0
        and (failed is None or failed == 0)
    )
    if not success:
        _write_failed_receipt(
            receipt_file,
            validated,
            mode="execute",
            job_plan_file=job_plan_file,
            import_result=import_result,
            run_result=run_result,
            live_applied=live_applied,
            error="run_failed",
            completed=completed,
            failed=failed,
            pending=pending,
        )
        counts = _format_counts(completed, failed, pending)
        raise AbletonExecutionError(
            "AbletonGPT run failed "
            f"(exit {run_result.returncode}{f', {counts}' if counts else ''}). "
            "The execution receipt records a failure, not success. "
            "The Live set may contain a partial arrangement; inspect "
            f"{job_plan_file.name} before retrying. "
            f"stderr: {_bound_text(run_result.stderr, 800)}"
        )

    receipt = _build_receipt(
        validated,
        mode="execute",
        status="success",
        execution_state="applied",
        job_plan_file=job_plan_file,
        job_plan_sha256=_file_sha256(job_plan_file),
        import_result=import_result,
        run_result=run_result,
        completed=completed if completed is not None else len(job_plan["steps"]),
        failed=failed if failed is not None else 0,
        pending=pending,
        live_applied=True,
    )
    _atomic_write_json(receipt_file, receipt)
    return AbletonExecutionManifest(
        project_dir=validated.project_dir,
        handoff_file=validated.handoff_file,
        arrangement_plan_file=validated.arrangement_plan_file,
        job_plan_file=job_plan_file,
        receipt_file=receipt_file,
        receipt=receipt,
        prepare_only=False,
    )


def describe_ableton_execution(manifest: AbletonExecutionManifest) -> list[str]:
    """Concise summary lines for the CLI."""

    receipt = manifest.receipt
    root = manifest.project_dir
    mode = receipt.get("mode")
    heading = (
        "Prepared Ableton execution (no Live job)"
        if manifest.prepare_only or mode == "prepare_only"
        else "Applied Ableton handoff through AbletonGPT"
    )
    imported = receipt.get("import_kihachi") or {}
    ran = receipt.get("run")
    lines = [
        heading,
        f"Adopted round: {receipt.get('adopted_round')} (from handoff; ranking unused)",
        f"Handoff: {_relpath(manifest.handoff_file, root)}",
        f"Arrangement plan: {_relpath(manifest.arrangement_plan_file, root)}",
    ]
    if manifest.job_plan_file is not None:
        lines.append(f"Job plan: {_relpath(manifest.job_plan_file, root)}")
    lines.append(f"Receipt: {_relpath(manifest.receipt_file, root)}")
    lines.append(f"- import-kihachi: exit {imported.get('returncode')}")
    if ran is None:
        lines.append("- Live job: not invoked (--prepare-only)")
    else:
        lines.append(f"- run: exit {ran.get('returncode')}")
        counts = _format_counts(
            receipt.get("completed"),
            receipt.get("failed"),
            receipt.get("pending"),
        )
        if counts:
            lines.append(f"- {counts}")
    lines.append("- handoff unchanged: yes")
    lines.append("- adoption unchanged: yes")
    lines.append("- preference memory appended: no")
    lines.append("- AbletonGPT operates Live; KIHACHI does not")
    return lines


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


def _handoff_base_dir(handoff_file: Path, document: Mapping[str, Any]) -> Path:
    """Resolve artifact paths using the VS5 manifest's ``path_base`` semantics."""

    path_base = document.get("path_base")
    if not isinstance(path_base, str) or not path_base.strip():
        raise AbletonExecutionError(
            f"Ableton handoff is missing path_base: {handoff_file}. "
            "VS5 manifests declare path_base='.' (paths relative to the handoff)."
        )
    if path_base == ".":
        return handoff_file.parent.resolve()
    base = Path(path_base)
    if base.is_absolute():
        return base.resolve()
    return (handoff_file.parent / base).resolve()


def _resolve_declared_path(
    stored: Any,
    *,
    base_dir: Path,
    allowed_root: Path,
    label: str,
) -> Path:
    if not isinstance(stored, str) or not stored.strip():
        raise AbletonExecutionError(f"Handoff {label} path is missing or empty")
    path = Path(stored)
    resolved = path.resolve() if path.is_absolute() else (base_dir / path).resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as error:
        raise AbletonExecutionError(
            f"Handoff {label} path escapes the project parent: {stored}"
        ) from error
    return resolved


def _verify_declared_audio(
    audio: Any,
    base_dir: Path,
    allowed_root: Path,
) -> None:
    if not isinstance(audio, Mapping):
        raise AbletonExecutionError("Ableton handoff is missing audio provenance")
    path = _resolve_declared_path(
        audio.get("path"),
        base_dir=base_dir,
        allowed_root=allowed_root,
        label="adopted audio",
    )
    digest = audio.get("sha256")
    if not isinstance(digest, str) or not digest.strip():
        return
    if not path.is_file():
        raise AbletonExecutionError(
            f"Adopted audio is missing: {path}. Refusing to invoke AbletonGPT."
        )
    actual = _file_sha256(path)
    if actual != digest:
        raise AbletonExecutionError(
            "Adopted audio SHA-256 mismatch: the WAV changed after the VS5 "
            f"handoff was written. Declared {digest}, on disk {actual}. "
            "Refusing to invoke AbletonGPT."
        )


def _verify_declared_song_spec(
    song_spec: Any,
    base_dir: Path,
    allowed_root: Path,
) -> None:
    if not isinstance(song_spec, Mapping):
        raise AbletonExecutionError("Ableton handoff is missing SongSpec provenance")
    path = _resolve_declared_path(
        song_spec.get("path"),
        base_dir=base_dir,
        allowed_root=allowed_root,
        label="SongSpec",
    )
    digest = song_spec.get("sha256")
    if not isinstance(digest, str) or not digest.strip():
        return
    if not path.is_file():
        raise AbletonExecutionError(
            f"Adopted SongSpec is missing: {path}. Refusing to invoke AbletonGPT."
        )
    try:
        spec = SongSpec.from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise AbletonExecutionError(
            f"Adopted SongSpec is invalid: {path}"
        ) from error
    actual = song_spec_sha256(spec)
    if actual != digest:
        raise AbletonExecutionError(
            "Adopted SongSpec SHA-256 mismatch: the spec changed after the VS5 "
            f"handoff was written. Declared {digest}, on disk {actual}. "
            "Refusing to invoke AbletonGPT."
        )


def _verify_declared_midi(
    midi: Any,
    base_dir: Path,
    allowed_root: Path,
) -> None:
    if midi is None:
        return
    if not isinstance(midi, list):
        raise AbletonExecutionError("Ableton handoff midi provenance must be a list")
    for index, row in enumerate(midi):
        if not isinstance(row, Mapping):
            raise AbletonExecutionError(
                f"Ableton handoff midi[{index}] must be an object"
            )
        part = row.get("part", index)
        path = _resolve_declared_path(
            row.get("path"),
            base_dir=base_dir,
            allowed_root=allowed_root,
            label=f"managed MIDI ({part})",
        )
        digest = row.get("sha256")
        if not isinstance(digest, str) or not digest.strip():
            continue
        if not path.is_file():
            raise AbletonExecutionError(
                f"Managed MIDI is missing for {part}: {path}. "
                "Refusing to invoke AbletonGPT."
            )
        actual = _file_sha256(path)
        if actual != digest:
            raise AbletonExecutionError(
                f"Managed MIDI SHA-256 mismatch for {part}: the file changed "
                f"after the VS5 handoff was written. Declared {digest}, "
                f"on disk {actual}. Refusing to invoke AbletonGPT."
            )


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AbletonExecutionError(
            f"Handoff {label} is missing sha256. Refusing to invoke AbletonGPT."
        )
    return value


def _load_job_plan(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AbletonExecutionError(
            f"AbletonGPT import-kihachi did not produce a job plan: {path}."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise AbletonExecutionError(
            f"Unable to read AbletonGPT job plan: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise AbletonExecutionError(
            f"AbletonGPT job plan is not valid JSON: {path} ({error.msg})"
        ) from error
    if not isinstance(payload, dict) or not payload:
        raise AbletonExecutionError(
            f"AbletonGPT job plan is empty or not a JSON object: {path}"
        )
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise AbletonExecutionError(
            f"AbletonGPT job plan has no steps: {path}"
        )
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping) or not str(step.get("command") or "").strip():
            raise AbletonExecutionError(
                f"AbletonGPT job plan step {index} is invalid: {path}"
            )
    return payload


def _load_receipt(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _live_applied_for_identity(
    receipt: Mapping[str, Any] | None,
    *,
    handoff_sha256: str,
    arrangement_sha256: str,
) -> bool:
    if not receipt:
        return False
    source = receipt.get("source_handoff")
    plan = receipt.get("arrangement_plan")
    if not isinstance(source, Mapping) or not isinstance(plan, Mapping):
        return False
    if source.get("sha256") != handoff_sha256:
        return False
    if plan.get("sha256") != arrangement_sha256:
        return False
    if receipt.get("live_applied") is True:
        return True
    return receipt.get("mode") == "execute" and receipt.get("status") == "success"


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
        "argv": list(result.argv),
        "returncode": result.returncode,
        "stdout": _bound_text(result.stdout),
        "stderr": _bound_text(result.stderr),
    }


def _build_receipt(
    validated: ValidatedHandoff,
    *,
    mode: str,
    status: str,
    execution_state: str,
    job_plan_file: Path | None,
    job_plan_sha256: str | None,
    import_result: CommandResult | None,
    run_result: CommandResult | None,
    completed: int | None,
    failed: int | None,
    live_applied: bool,
    pending: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    root = validated.project_dir
    job_plan: dict[str, Any] | None = None
    if job_plan_file is not None:
        job_plan = {
            "path": _relpath(job_plan_file, root),
            "sha256": job_plan_sha256,
        }
    receipt: dict[str, Any] = {
        "ableton_execution_version": ABLETON_EXECUTION_VERSION,
        "execution_state": execution_state,
        "status": status,
        "mode": mode,
        "live_applied": live_applied,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "adopted_round": validated.adopted_round,
        "source_handoff": {
            "path": _relpath(validated.handoff_file, root),
            "sha256": validated.handoff_sha256,
        },
        "arrangement_plan": {
            "path": _relpath(validated.arrangement_plan_file, root),
            "sha256": validated.arrangement_plan_sha256,
        },
        "job_plan": job_plan,
        "import_kihachi": _command_record(import_result),
        "run": _command_record(run_result),
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "boundary": {
            "kihachi": "decides what to make",
            "abletongpt": "operates Ableton Live",
            "live_execution": mode == "execute" and status == "success",
            "auto_adoption": False,
        },
    }
    if error is not None:
        receipt["error"] = error
    return receipt


def _write_failed_receipt(
    receipt_file: Path,
    validated: ValidatedHandoff,
    *,
    mode: str,
    job_plan_file: Path | None,
    import_result: CommandResult | None,
    run_result: CommandResult | None,
    live_applied: bool,
    error: str,
    completed: int | None = None,
    failed: int | None = None,
    pending: int | None = None,
) -> None:
    job_sha = None
    if job_plan_file is not None and job_plan_file.is_file():
        job_sha = _file_sha256(job_plan_file)
    receipt = _build_receipt(
        validated,
        mode=mode,
        status="failed",
        execution_state="failed",
        job_plan_file=job_plan_file,
        job_plan_sha256=job_sha,
        import_result=import_result,
        run_result=run_result,
        completed=completed,
        failed=failed,
        pending=pending,
        live_applied=live_applied,
        error=error,
    )
    _atomic_write_json(receipt_file, receipt)


def _execution_counts(
    result: CommandResult,
    job_plan_file: Path,
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


def _format_counts(
    completed: Any,
    failed: Any,
    pending: Any,
) -> str:
    parts: list[str] = []
    if isinstance(completed, int):
        parts.append(f"completed={completed}")
    if isinstance(failed, int):
        parts.append(f"failed={failed}")
    if isinstance(pending, int):
        parts.append(f"pending={pending}")
    return " ".join(parts)


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
