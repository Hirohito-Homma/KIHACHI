"""VS7 — Ableton Live postcondition audit.

Consumes a successful VS6 execution receipt and compares the intended
arrangement with read-only Live evidence collected through AbletonGPT.
Never talks to the Live socket or Live Object Model itself, never repairs a
Set, and never lets ranking or preference memory choose the target.

Architectural contract preserved:

    KIHACHI Music AI = decides what should exist
    AbletonGPT       = reads/writes Ableton Live
    KIHACHI          = compares intended state with observed evidence
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .ableton_execution import (
    ABLETON_EXECUTION_NAME,
    AbletonExecutionError,
    CommandResult,
    CommandRunner,
    ableton_execution_path,
    load_validated_handoff,
    run_command,
)
from .ableton_handoff import ableton_handoff_path

ABLETON_VERIFICATION_VERSION = "0.1"
ABLETON_VERIFICATION_NAME = "ableton_verification.json"
ABLETON_EXECUTION_VERSION = "0.1"
SUPPORTED_EXECUTION_VERSIONS = frozenset({ABLETON_EXECUTION_VERSION})
EVIDENCE_SCHEMA_VERSION = "0.1"

STATE_VERIFIED = "verified"
STATE_PARTIAL = "partially_verified"
STATE_FAILED = "failed"
STATE_NOT_RUN = "not_run"

CHECK_PASS = "pass"
CHECK_FAIL = "fail"
CHECK_NOT_OBSERVABLE = "not_observable"

TEMPO_TOLERANCE_BPM = 1e-3
NOTE_TIME_TOLERANCE_BEATS = 1e-4
CLIP_LENGTH_TOLERANCE_BEATS = 1e-4
ARRANGEMENT_TIME_TOLERANCE_BEATS = 1e-4
MAX_CAPTURED_CHARS = 4000
MAX_DEVICE_EVIDENCE = 16
MAX_NOTES_IN_MANIFEST = 4096

# AbletonGPT 0.2 (SHA 5fcf063) has MCP/bridge read tools but no dedicated
# machine-readable bulk-read CLI.  This collector is the external invocation
# boundary: it runs inside the AbletonGPT interpreter and only calls the
# existing read commands (ping, get_state, get_track_devices,
# get_midi_clip_notes, get_arrangement_clips).  KIHACHI never opens the
# Live socket itself.
ABLETONGPT_EVIDENCE_COLLECTOR = r"""
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if request.get("read_only") is not True:
    print("evidence request is not marked read_only", file=sys.stderr)
    sys.exit(1)

try:
    from abletongpt.bridge import AbletonBridge, AbletonConnectionError
except ImportError:
    print("No module named abletongpt", file=sys.stderr)
    sys.exit(1)

def _strip_device(device):
    if not isinstance(device, dict):
        return {"name": str(device)}
    return {
        "index": device.get("index"),
        "name": device.get("name"),
        "class_name": device.get("class_name"),
        "class_display_name": device.get("class_display_name"),
        "type": device.get("type"),
        "is_active": device.get("is_active"),
    }

def _strip_note(note):
    if not isinstance(note, dict):
        return note
    return {
        "pitch": note.get("pitch"),
        "start_time": note.get("start_time"),
        "duration": note.get("duration"),
        "velocity": note.get("velocity"),
    }

def _bounded_clip(payload):
    if not isinstance(payload, dict):
        return payload
    notes = payload.get("notes") or []
    if not isinstance(notes, list):
        notes = []
    return {
        "track_index": payload.get("track_index"),
        "track": payload.get("track"),
        "clip_index": payload.get("clip_index"),
        "clip": payload.get("clip"),
        "length_beats": payload.get("length_beats"),
        "note_count": payload.get("note_count", len(notes)),
        "truncated": bool(payload.get("truncated")),
        "notes": [_strip_note(note) for note in notes[:4096]],
    }

try:
    bridge = AbletonBridge()
    ping = bridge.call("ping")
    state = bridge.call("get_state")
except AbletonConnectionError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)

tracks = []
for track in state.get("tracks") or []:
    if not isinstance(track, dict):
        continue
    tracks.append({
        "index": track.get("index"),
        "name": track.get("name"),
        "clip_slots": track.get("clip_slots"),
    })

devices = {}
for index in request.get("device_indices") or []:
    try:
        payload = bridge.call("get_track_devices", track_index=int(index))
        raw = payload.get("devices") if isinstance(payload, dict) else []
        devices[str(int(index))] = [_strip_device(item) for item in (raw or [])[:16]]
    except Exception as exc:
        devices[str(int(index))] = {"error": str(exc)}

session_clips = {}
for item in request.get("session_clips") or []:
    track_index = int(item["track_index"])
    clip_index = int(item["clip_index"])
    key = "%d:%d" % (track_index, clip_index)
    try:
        payload = bridge.call(
            "get_midi_clip_notes",
            track_index=track_index,
            clip_index=clip_index,
        )
        session_clips[key] = _bounded_clip(payload)
    except Exception as exc:
        session_clips[key] = {"error": str(exc)}

arrangement_clips = {}
arrangement_observable = True
for index in request.get("arrangement_indices") or []:
    try:
        payload = bridge.call("get_arrangement_clips", track_index=int(index))
        if not isinstance(payload, dict):
            arrangement_clips[str(int(index))] = {"error": "invalid arrangement payload"}
            continue
        clips = []
        for clip in (payload.get("clips") or [])[:64]:
            if not isinstance(clip, dict):
                continue
            clips.append({
                "index": clip.get("index"),
                "name": clip.get("name"),
                "start_time": clip.get("start_time"),
                "end_time": clip.get("end_time"),
                "length_beats": clip.get("length_beats"),
                "is_midi_clip": clip.get("is_midi_clip"),
                "is_audio_clip": clip.get("is_audio_clip"),
                "muted": clip.get("muted"),
            })
        arrangement_clips[str(int(index))] = {
            "track_index": payload.get("track_index", index),
            "track": payload.get("track"),
            "clips": clips,
            "clip_count": payload.get("clip_count", len(clips)),
            "truncated": bool(payload.get("truncated")),
            "read_only": True,
        }
    except Exception as exc:
        message = str(exc)
        unknown = "unknown command" in message.lower() or "get_arrangement_clips" in message
        arrangement_clips[str(int(index))] = {
            "not_observable": True,
            "error": message,
        }
        if unknown:
            arrangement_observable = False

evidence = {
    "abletongpt_evidence_version": "0.1",
    "read_only": True,
    "ping": ping if isinstance(ping, dict) else {"raw": ping},
    "live_state": {
        "tempo": state.get("tempo") if isinstance(state, dict) else None,
        "signature": state.get("signature") if isinstance(state, dict) else None,
        "tracks": tracks,
    },
    "devices": devices,
    "session_clips": session_clips,
    "arrangement_clips": arrangement_clips,
    "arrangement_observable": arrangement_observable,
}
print(json.dumps(evidence, ensure_ascii=False))
"""


class AbletonVerificationError(ValueError):
    """Actionable refusal before Live read, or when evidence cannot be used."""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class VerifiedExecution:
    """Provenance-checked VS5 handoff + successful VS6 execution receipt."""

    project_dir: Path
    handoff_file: Path
    handoff: dict[str, Any]
    handoff_sha256: str
    arrangement_plan_file: Path
    arrangement_plan: dict[str, Any]
    arrangement_plan_sha256: str
    job_plan_file: Path | None
    job_plan: dict[str, Any] | None
    job_plan_sha256: str | None
    receipt_file: Path
    receipt: dict[str, Any]
    receipt_sha256: str
    adopted_round: int


@dataclass(frozen=True)
class AbletonVerificationManifest:
    project_dir: Path
    verification_file: Path
    document: dict[str, Any]
    expected: dict[str, Any]
    observed: dict[str, Any] | None
    checks: tuple[dict[str, Any], ...]

    @property
    def verification_state(self) -> str:
        return str(self.document.get("verification_state", STATE_NOT_RUN))

    @property
    def exit_code(self) -> int:
        state = self.verification_state
        if state == STATE_VERIFIED:
            return 0
        if state == STATE_FAILED:
            return 1
        return 2


LiveEvidenceProvider = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def ableton_verification_path(project_dir: Path) -> Path:
    return Path(project_dir) / ABLETON_VERIFICATION_NAME


def load_verified_execution(project_dir: Path) -> VerifiedExecution:
    """Load handoff + execution receipt and refuse before any Live read."""

    root = Path(project_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"project not found: {root}")

    handoff_file = ableton_handoff_path(root)
    if not handoff_file.is_file():
        raise AbletonVerificationError(
            f"No Ableton handoff found: {handoff_file}. "
            "Run `kihachi ableton-handoff PROJECT` after an explicit "
            "`kihachi adopt PROJECT --round N` first."
        )

    receipt_file = ableton_execution_path(root)
    if not receipt_file.is_file():
        raise AbletonVerificationError(
            f"No Ableton execution receipt found: {receipt_file}. "
            "Run `kihachi ableton-apply PROJECT` (without --prepare-only) "
            "before `kihachi ableton-verify`. Inspect: missing "
            f"{ABLETON_EXECUTION_NAME}."
        )

    try:
        validated = load_validated_handoff(root)
    except AbletonExecutionError as error:
        raise AbletonVerificationError(
            f"{error} Refusing Live verification until the VS5 handoff is valid."
        ) from error

    try:
        raw = receipt_file.read_text(encoding="utf-8")
        receipt = json.loads(raw)
    except (OSError, UnicodeError) as error:
        raise AbletonVerificationError(
            f"Unable to read Ableton execution receipt: {receipt_file}"
        ) from error
    except json.JSONDecodeError as error:
        raise AbletonVerificationError(
            f"Ableton execution receipt is not valid JSON: {receipt_file} "
            f"({error.msg}). Expected a VS6 ableton_execution.json object. "
            "Inspect the file; do not treat a prepare-only or broken receipt "
            "as Live application."
        ) from error
    if not isinstance(receipt, dict):
        raise AbletonVerificationError(
            f"Ableton execution receipt must be a JSON object: {receipt_file}"
        )

    version = receipt.get("ableton_execution_version")
    if not isinstance(version, str) or version not in SUPPORTED_EXECUTION_VERSIONS:
        raise AbletonVerificationError(
            f"Unsupported ableton_execution_version {version!r} in {receipt_file} "
            f"(supported: {', '.join(sorted(SUPPORTED_EXECUTION_VERSIONS))}). "
            "Refusing Live verification."
        )

    mode = receipt.get("mode")
    if mode == "prepare_only" or receipt.get("execution_state") == "prepared_not_applied":
        raise AbletonVerificationError(
            "Execution receipt is prepare-only (import-kihachi ran, Live job "
            "was not invoked). A prepare-only receipt is not Live application. "
            f"Observed mode={mode!r}, execution_state="
            f"{receipt.get('execution_state')!r}. Run "
            "`kihachi ableton-apply PROJECT` without --prepare-only first."
        )
    if receipt.get("status") != "success":
        raise AbletonVerificationError(
            "Execution receipt does not indicate successful runner completion "
            f"(status={receipt.get('status')!r}, error={receipt.get('error')!r}). "
            "Refusing Live verification. Inspect ableton_execution.json and the "
            "Live Set for a partial arrangement; VS7 does not repair it."
        )
    if mode != "execute":
        raise AbletonVerificationError(
            f"Execution receipt mode is {mode!r}, not 'execute'. "
            "Refusing Live verification."
        )
    if receipt.get("live_applied") is not True:
        raise AbletonVerificationError(
            "Execution receipt live_applied is not true. "
            "AbletonGPT returning an exit code is not the same as verified "
            "Live postconditions. Re-run apply only if a successful execute "
            "receipt is missing."
        )

    source = receipt.get("source_handoff")
    if not isinstance(source, Mapping):
        raise AbletonVerificationError(
            f"Execution receipt is missing source_handoff: {receipt_file}"
        )
    declared_handoff_sha = source.get("sha256")
    if declared_handoff_sha != validated.handoff_sha256:
        raise AbletonVerificationError(
            "Handoff SHA-256 mismatch: the execution receipt was issued for a "
            "different ableton_handoff.json than the one now on disk. "
            f"Receipt {declared_handoff_sha}, on disk {validated.handoff_sha256}. "
            "Never verify a stale receipt against a different handoff."
        )

    plan_row = receipt.get("arrangement_plan")
    if not isinstance(plan_row, Mapping):
        raise AbletonVerificationError(
            f"Execution receipt is missing arrangement_plan provenance: {receipt_file}"
        )
    declared_plan_sha = plan_row.get("sha256")
    if declared_plan_sha != validated.arrangement_plan_sha256:
        raise AbletonVerificationError(
            "Arrangement plan SHA-256 mismatch: the plan changed after the VS6 "
            "execution receipt was written (or does not match the receipt). "
            f"Receipt {declared_plan_sha}, on disk {validated.arrangement_plan_sha256}. "
            "Refusing Live verification."
        )

    adopted_round = receipt.get("adopted_round")
    if adopted_round != validated.adopted_round:
        raise AbletonVerificationError(
            "Adopted round identity mismatch: execution receipt "
            f"adopted_round={adopted_round!r}, handoff "
            f"adopted_round={validated.adopted_round}. Ranking cannot change "
            "the verification target."
        )

    job_plan_file: Path | None = None
    job_plan: dict[str, Any] | None = None
    job_plan_sha: str | None = None
    job_row = receipt.get("job_plan")
    if isinstance(job_row, Mapping) and job_row.get("sha256"):
        job_plan_file = _resolve_receipt_path(
            job_row.get("path"),
            project_dir=root,
            label="job plan",
        )
        if not job_plan_file.is_file():
            raise AbletonVerificationError(
                f"Job plan declared by the execution receipt is missing: "
                f"{job_plan_file}. Refusing Live verification."
            )
        job_plan_sha = _file_sha256(job_plan_file)
        if job_plan_sha != job_row.get("sha256"):
            raise AbletonVerificationError(
                "Job plan SHA-256 mismatch: ableton_job_plan.json changed after "
                "the VS6 execution receipt was written. "
                f"Receipt {job_row.get('sha256')}, on disk {job_plan_sha}. "
                "Refusing Live verification."
            )
        job_plan = _load_json_object(job_plan_file, "AbletonGPT job plan")

    arrangement_plan = _load_json_object(
        validated.arrangement_plan_file, "arrangement plan"
    )

    return VerifiedExecution(
        project_dir=root,
        handoff_file=validated.handoff_file,
        handoff=validated.handoff,
        handoff_sha256=validated.handoff_sha256,
        arrangement_plan_file=validated.arrangement_plan_file,
        arrangement_plan=arrangement_plan,
        arrangement_plan_sha256=validated.arrangement_plan_sha256,
        job_plan_file=job_plan_file,
        job_plan=job_plan,
        job_plan_sha256=job_plan_sha,
        receipt_file=receipt_file.resolve(),
        receipt=receipt,
        receipt_sha256=_file_sha256(receipt_file),
        adopted_round=validated.adopted_round,
    )


def build_expected_live_state(
    arrangement_plan: Mapping[str, Any],
    *,
    job_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive expected Live postconditions from the adopted plan artifacts.

    The human-adopted handoff / arrangement plan remains authoritative.  Ranking,
    preference memory, and review scores are not consulted.
    """

    song = arrangement_plan.get("song")
    if not isinstance(song, Mapping) or not isinstance(song.get("bpm"), (int, float)):
        raise AbletonVerificationError(
            "Arrangement plan is missing song.bpm; cannot build expected Live tempo."
        )
    safety = arrangement_plan.get("safety") if isinstance(arrangement_plan.get("safety"), Mapping) else {}
    operations = arrangement_plan.get("operations")
    if not isinstance(operations, list):
        raise AbletonVerificationError("Arrangement plan is missing operations")

    tracks: list[dict[str, Any]] = []
    for row in arrangement_plan.get("tracks") or []:
        if not isinstance(row, Mapping):
            continue
        index = row.get("live_track_index")
        name = row.get("name")
        if not isinstance(index, int) or not isinstance(name, str):
            continue
        tracks.append(
            {
                "part": row.get("part"),
                "index": index,
                "name": name,
                "notes": row.get("notes"),
                "file": row.get("file"),
            }
        )

    devices: list[dict[str, Any]] = []
    clips: list[dict[str, Any]] = []
    arrangement: list[dict[str, Any]] = []
    audio_imports: list[dict[str, Any]] = []
    created_tracks = 0
    for operation in operations:
        if not isinstance(operation, Mapping):
            continue
        op_name = str(operation.get("op") or "")
        params = operation.get("params") if isinstance(operation.get("params"), Mapping) else {}
        if op_name == "create_track":
            created_tracks += 1
        elif op_name in {"apply_live_instrument_selection", "apply_live_drum_kit"}:
            track_index = params.get("track_index")
            if isinstance(track_index, int):
                devices.append(
                    {
                        "track_index": track_index,
                        "role": params.get("role"),
                        "kind": (
                            "drum_kit"
                            if op_name == "apply_live_drum_kit"
                            else "instrument"
                        ),
                    }
                )
        elif op_name == "create_midi_clip":
            track_index = params.get("track_index")
            clip_index = params.get("clip_index")
            if not isinstance(track_index, int) or not isinstance(clip_index, int):
                continue
            notes = [
                {
                    "pitch": int(note["pitch"]),
                    "start_time": float(note["start_time"]),
                    "duration": float(note["duration"]),
                    "velocity": int(note.get("velocity", 100)),
                }
                for note in (params.get("notes") or [])
                if isinstance(note, Mapping)
            ]
            clips.append(
                {
                    "track_index": track_index,
                    "clip_index": clip_index,
                    "name": params.get("name"),
                    "length_beats": float(params.get("length_beats", 0.0)),
                    "notes": notes,
                    "note_count": len(notes),
                }
            )
        elif op_name == "copy_session_clip_to_arrangement":
            track_index = params.get("track_index")
            if isinstance(track_index, int):
                arrangement.append(
                    {
                        "track_index": track_index,
                        "clip_index": params.get("clip_index"),
                        "destination_time_beats": float(
                            params.get("destination_time_beats", 0.0)
                        ),
                        "name": params.get("name"),
                    }
                )
        elif op_name == "import_vocal_take":
            audio_imports.append(
                {
                    "track_name": params.get("track_name"),
                    "clip_name": params.get("clip_name"),
                    "clip_index": params.get("clip_index"),
                }
            )

    first_track_index = int(safety.get("first_track_index", 0))
    declared_creates = safety.get("creates_tracks")
    if isinstance(declared_creates, int):
        created_tracks = declared_creates
    expected_track_count = (
        max(track["index"] for track in tracks) + 1 if tracks else first_track_index + created_tracks
    )
    return {
        "tempo": float(song["bpm"]),
        "first_track_index": first_track_index,
        "created_track_count": created_tracks,
        "expected_track_count": expected_track_count,
        "tracks": tracks,
        "devices": devices,
        "clips": clips,
        "arrangement": arrangement,
        "audio_imports": audio_imports,
        "job_plan_steps": (
            len(job_plan.get("steps") or [])
            if isinstance(job_plan, Mapping)
            else None
        ),
    }


def collect_live_evidence(
    request: Mapping[str, Any],
    *,
    provider: LiveEvidenceProvider | None = None,
    abletongpt_python: Path | str | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Retrieve read-only Live evidence through AbletonGPT (or an injected fake)."""

    if provider is not None:
        payload = provider(request)
        return _require_evidence_object(payload, source="injected evidence provider")
    return collect_via_abletongpt(
        request,
        abletongpt_python=abletongpt_python,
        runner=runner,
    )


def collect_via_abletongpt(
    request: Mapping[str, Any],
    *,
    abletongpt_python: Path | str | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Invoke AbletonGPT's existing read APIs in a subprocess.  No Live socket here."""

    run = runner if runner is not None else run_command
    python = _python_executable(abletongpt_python)
    with tempfile.TemporaryDirectory(prefix="kihachi-vs7-") as temp:
        request_path = Path(temp) / "live_evidence_request.json"
        request_path.write_text(
            json.dumps(dict(request), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = run([python, "-c", ABLETONGPT_EVIDENCE_COLLECTOR, str(request_path)])
    if _module_unavailable(result):
        raise AbletonVerificationError(
            "AbletonGPT is not available in "
            f"{python}. Install AbletonGPT in that interpreter or pass "
            "--abletongpt-python PATH. Live verification was not performed "
            f"(not_run). Captured stderr: {_bound_text(result.stderr, 400)}"
        )
    if result.returncode != 0:
        blob = f"{result.stderr}\n{result.stdout}"
        if "接続できません" in blob or "Ableton Live" in blob or "connect" in blob.lower():
            raise AbletonVerificationError(
                "Ableton Live is unreachable through AbletonGPT. "
                "Start Live, select the AbletonGPT Control Surface, then re-run "
                "`kihachi ableton-verify`. No Live postconditions were fabricated. "
                f"stderr: {_bound_text(result.stderr, 800)}"
            )
        raise AbletonVerificationError(
            "AbletonGPT read-only evidence collection failed "
            f"(exit {result.returncode}). Live verification was not performed. "
            f"stderr: {_bound_text(result.stderr, 800)}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AbletonVerificationError(
            "AbletonGPT evidence is not valid JSON. "
            f"({error.msg}). stdout: {_bound_text(result.stdout, 400)}. "
            "Inspect the AbletonGPT invocation; VS7 will not invent a Live snapshot."
        ) from error
    return _require_evidence_object(payload, source="AbletonGPT")


def verify_ableton_execution(
    project_dir: Path,
    *,
    abletongpt_python: Path | str | None = None,
    runner: CommandRunner | None = None,
    provider: LiveEvidenceProvider | None = None,
) -> AbletonVerificationManifest:
    """Compare the adopted plan with observed Live state.  Read-only; does not repair."""

    fingerprints = _provenance_fingerprints(Path(project_dir))
    loaded = load_verified_execution(project_dir)
    expected = build_expected_live_state(
        loaded.arrangement_plan, job_plan=loaded.job_plan
    )
    request = _evidence_request(expected)
    try:
        observed = collect_live_evidence(
            request,
            provider=provider,
            abletongpt_python=abletongpt_python,
            runner=runner,
        )
    except AbletonVerificationError as error:
        document = _build_verification_document(
            loaded,
            expected=expected,
            observed=None,
            checks=(),
            state=STATE_NOT_RUN,
            error=str(error),
        )
        _atomic_write_json(ableton_verification_path(loaded.project_dir), document)
        _assert_unchanged(fingerprints)
        raise

    try:
        checks = tuple(_evaluate_checks(expected, observed))
        state = _summarize_state(checks)
        document = _build_verification_document(
            loaded,
            expected=expected,
            observed=_bound_observed(observed),
            checks=checks,
            state=state,
            error=None,
        )
        destination = ableton_verification_path(loaded.project_dir)
        _atomic_write_json(destination, document)
        _assert_unchanged(fingerprints)
        return AbletonVerificationManifest(
            project_dir=loaded.project_dir,
            verification_file=destination,
            document=document,
            expected=expected,
            observed=observed,
            checks=checks,
        )
    except AbletonVerificationError:
        _assert_unchanged(fingerprints)
        raise


def describe_ableton_verification(manifest: AbletonVerificationManifest) -> list[str]:
    """Concise summary lines for the CLI."""

    state = manifest.verification_state
    heading = {
        STATE_VERIFIED: "VERIFIED",
        STATE_PARTIAL: "PARTIALLY VERIFIED",
        STATE_FAILED: "FAILED",
        STATE_NOT_RUN: "NOT RUN",
    }.get(state, state.upper())
    root = manifest.project_dir
    lines = [
        f"Ableton verification: {heading}",
        f"Adopted round: {manifest.document.get('source', {}).get('adopted_round')}",
    ]
    for check in manifest.checks:
        lines.append(_describe_check(check))
    lines.append(
        f"Verification: {_relpath(manifest.verification_file, root)}"
    )
    lines.append("- Live access: AbletonGPT (KIHACHI does not talk to Live)")
    lines.append("- repair attempted: no")
    lines.append("- adoption unchanged: yes")
    lines.append("- preference memory appended: no")
    return lines


def _evidence_request(expected: Mapping[str, Any]) -> dict[str, Any]:
    device_indices = sorted({int(item["track_index"]) for item in expected.get("devices") or []})
    session_clips = [
        {"track_index": int(item["track_index"]), "clip_index": int(item["clip_index"])}
        for item in expected.get("clips") or []
    ]
    arrangement_indices = sorted(
        {int(item["track_index"]) for item in expected.get("arrangement") or []}
    )
    return {
        "read_only": True,
        "device_indices": device_indices,
        "session_clips": session_clips,
        "arrangement_indices": arrangement_indices,
    }


def _evaluate_checks(
    expected: Mapping[str, Any], observed: Mapping[str, Any]
) -> list[dict[str, Any]]:
    live_state = observed.get("live_state")
    if not isinstance(live_state, Mapping):
        raise AbletonVerificationError(
            "AbletonGPT evidence is missing live_state. "
            f"Unsupported evidence schema (abletongpt_evidence_version="
            f"{observed.get('abletongpt_evidence_version')!r}). "
            "Inspect the evidence payload; VS7 will not invent tempo or tracks."
        )
    checks: list[dict[str, Any]] = []
    checks.append(_check_tempo(expected, live_state))
    observed_tracks = live_state.get("tracks")
    if not isinstance(observed_tracks, list):
        raise AbletonVerificationError(
            "AbletonGPT live_state.tracks is missing or not a list. "
            "Unsupported evidence schema. Inspect get_live_state output."
        )
    checks.append(_check_track_count(expected, observed_tracks))
    by_index = _tracks_by_index(observed_tracks)
    for track in expected.get("tracks") or []:
        checks.append(_check_track(track, by_index))
    devices_map = observed.get("devices")
    devices_present = isinstance(devices_map, Mapping)
    for device in expected.get("devices") or []:
        checks.append(_check_device(device, devices_map if devices_present else None))
    clips_map = observed.get("session_clips")
    clips_present = isinstance(clips_map, Mapping)
    for clip in expected.get("clips") or []:
        checks.append(_check_session_clip(clip, clips_map if clips_present else None))
    arrangement_map = observed.get("arrangement_clips")
    arrangement_declared = "arrangement_clips" in observed
    observable_flag = observed.get("arrangement_observable")
    for target in expected.get("arrangement") or []:
        length = _clip_length_for_track(expected, int(target["track_index"]))
        checks.append(
            _check_arrangement(
                target,
                arrangement_map if arrangement_declared else None,
                length_beats=length,
                arrangement_observable=observable_flag,
            )
        )
    return checks


def _check_tempo(expected: Mapping[str, Any], live_state: Mapping[str, Any]) -> dict[str, Any]:
    expected_bpm = float(expected["tempo"])
    observed_bpm = live_state.get("tempo")
    if not isinstance(observed_bpm, (int, float)):
        return _check(
            "tempo",
            "tempo",
            expected_bpm,
            observed_bpm,
            CHECK_NOT_OBSERVABLE,
            "Live tempo was not present in AbletonGPT get_live_state evidence. "
            "Inspect get_live_state(); unknown is not pass.",
        )
    observed_value = float(observed_bpm)
    if abs(expected_bpm - observed_value) <= TEMPO_TOLERANCE_BPM:
        return _check(
            "tempo",
            "tempo",
            expected_bpm,
            observed_value,
            CHECK_PASS,
            f"Tempo matches within {TEMPO_TOLERANCE_BPM:g} BPM",
        )
    return _check(
        "tempo",
        "tempo",
        expected_bpm,
        observed_value,
        CHECK_FAIL,
        f"Tempo mismatch: expected {expected_bpm:g} BPM, observed {observed_value:g} BPM "
        f"(tolerance {TEMPO_TOLERANCE_BPM:g}). Inspect Live's song tempo; VS7 will not set it.",
    )


def _check_track_count(
    expected: Mapping[str, Any], observed_tracks: Sequence[Any]
) -> dict[str, Any]:
    expected_count = int(expected["expected_track_count"])
    first = int(expected["first_track_index"])
    created = int(expected["created_track_count"])
    observed_count = len(observed_tracks)
    if observed_count < expected_count:
        return _check(
            "track_count",
            "tracks",
            expected_count,
            observed_count,
            CHECK_FAIL,
            f"Track count too low: expected at least {expected_count} "
            f"(first_track_index={first} + created={created}), observed {observed_count}. "
            "Inspect Live's track list; VS7 will not create missing tracks.",
        )
    return _check(
        "track_count",
        "tracks",
        expected_count,
        observed_count,
        CHECK_PASS,
        f"Observed {observed_count} track(s); first_track_index={first}, created={created}",
    )


def _check_track(track: Mapping[str, Any], by_index: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    index = int(track["index"])
    expected_name = str(track["name"])
    observed = by_index.get(index)
    found_elsewhere = [
        item_index
        for item_index, item in by_index.items()
        if item.get("name") == expected_name and item_index != index
    ]
    if observed is None:
        message = (
            f"Expected track {expected_name!r} at index {index} is missing. "
        )
        if found_elsewhere:
            message += (
                f"A track with that name exists at index {found_elsewhere[0]} "
                "(index is authoritative; a same-named track elsewhere is not accepted). "
            )
        message += "Inspect Live's track list; VS7 will not create or move tracks."
        return _check(
            f"track:{index}",
            "tracks",
            {"index": index, "name": expected_name},
            None,
            CHECK_FAIL,
            message,
        )
    observed_name = observed.get("name")
    if observed_name != expected_name:
        message = (
            f"Track at index {index}: expected name {expected_name!r}, "
            f"observed {observed_name!r}. "
        )
        if found_elsewhere:
            message += (
                f"The expected name is at index {found_elsewhere[0]}, not {index}. "
            )
        message += "Index is authoritative. Inspect the Set; VS7 will not rename tracks."
        return _check(
            f"track:{index}",
            "tracks",
            {"index": index, "name": expected_name},
            {"index": index, "name": observed_name},
            CHECK_FAIL,
            message,
        )
    return _check(
        f"track:{index}",
        "tracks",
        {"index": index, "name": expected_name},
        {"index": index, "name": observed_name},
        CHECK_PASS,
        f"Track {expected_name} @ {index}",
    )


def _check_device(
    device: Mapping[str, Any], devices_map: Mapping[str, Any] | None
) -> dict[str, Any]:
    index = int(device["track_index"])
    role = device.get("role")
    kind = device.get("kind")
    expected = {"track_index": index, "role": role, "kind": kind}
    check_id = f"device:{index}"
    if devices_map is None:
        return _check(
            check_id,
            "devices",
            expected,
            None,
            CHECK_NOT_OBSERVABLE,
            f"Device list for track {index} was not in AbletonGPT evidence "
            "(get_track_devices not observed). Unknown is not pass.",
        )
    payload = devices_map.get(str(index))
    if isinstance(payload, Mapping) and payload.get("error"):
        return _check(
            check_id,
            "devices",
            expected,
            payload,
            CHECK_FAIL,
            f"Expected a device on track {index} ({kind} role {role!r}) but "
            f"get_track_devices failed: {payload.get('error')}. "
            "Inspect the track's device chain; VS7 will not insert devices.",
        )
    if not isinstance(payload, list):
        return _check(
            check_id,
            "devices",
            expected,
            payload,
            CHECK_NOT_OBSERVABLE,
            f"Device evidence for track {index} has an unsupported shape. "
            "Inspect get_track_devices; unknown is not pass.",
        )
    names = [
        str(item.get("name") or item.get("class_display_name") or item.get("class_name") or "")
        for item in payload
        if isinstance(item, Mapping)
    ]
    if not payload:
        return _check(
            check_id,
            "devices",
            expected,
            [],
            CHECK_FAIL,
            f"Expected a device on track {index} after {kind} "
            f"(role {role!r}) but none was observed. "
            "Inspect the track in Live; VS7 will not load a kit or instrument.",
        )
    rows = [item for item in payload if isinstance(item, Mapping)]
    if len(payload) == 1 and len(rows) == 1 and rows[0].get("is_active") is False:
        observed_device = rows[0]
        name = names[0] if names else ""
        device_index = observed_device.get("index")
        return _check(
            check_id,
            "devices",
            expected,
            {
                "count": 1,
                "names": names[:MAX_DEVICE_EVIDENCE],
                "index": device_index,
                "name": observed_device.get("name") or name,
                "class_name": observed_device.get("class_name"),
                "type": observed_device.get("type"),
                "is_active": False,
            },
            CHECK_FAIL,
            f"Expected an active device on track {index} ({kind} role {role!r}) "
            f"but {name!r} at index {device_index} is inactive. "
            "VS7 will not insert a replacement or invent a parameter repair.",
        )
    return _check(
        check_id,
        "devices",
        expected,
        {"count": len(payload), "names": names[:MAX_DEVICE_EVIDENCE]},
        CHECK_PASS,
        f"Track {index} has {len(payload)} observable device(s)",
    )


def _check_session_clip(
    clip: Mapping[str, Any], clips_map: Mapping[str, Any] | None
) -> dict[str, Any]:
    track_index = int(clip["track_index"])
    clip_index = int(clip["clip_index"])
    key = f"{track_index}:{clip_index}"
    expected_notes = list(clip.get("notes") or [])
    expected = {
        "track_index": track_index,
        "clip_index": clip_index,
        "name": clip.get("name"),
        "length_beats": clip.get("length_beats"),
        "note_count": clip.get("note_count", len(expected_notes)),
    }
    check_id = f"session_clip:{key}"
    if clips_map is None:
        return _check(
            check_id,
            "clips",
            expected,
            None,
            CHECK_NOT_OBSERVABLE,
            f"Session MIDI clip at track {track_index} slot {clip_index} was not "
            "in AbletonGPT evidence (get_midi_clip_notes not observed). "
            "Unknown is not pass.",
        )
    payload = clips_map.get(key)
    if payload is None:
        return _check(
            check_id,
            "clips",
            expected,
            None,
            CHECK_NOT_OBSERVABLE,
            f"No get_midi_clip_notes evidence for track {track_index} slot "
            f"{clip_index}. Unknown is not pass.",
        )
    if isinstance(payload, Mapping) and payload.get("error"):
        return _check(
            check_id,
            "clips",
            expected,
            payload,
            CHECK_FAIL,
            f"Expected Session MIDI clip {clip.get('name')!r} on track "
            f"{track_index} slot {clip_index}, but AbletonGPT reported: "
            f"{payload.get('error')}. Inspect the Session slot; VS7 will not create clips.",
        )
    if not isinstance(payload, Mapping):
        return _check(
            check_id,
            "clips",
            expected,
            payload,
            CHECK_NOT_OBSERVABLE,
            "Session clip evidence has an unsupported shape. Unknown is not pass.",
        )
    observed_length = payload.get("length_beats")
    if not isinstance(observed_length, (int, float)):
        return _check(
            check_id,
            "clips",
            expected,
            _clip_observed_summary(payload),
            CHECK_FAIL,
            f"Session clip on track {track_index} slot {clip_index} did not report "
            "length_beats. Inspect get_midi_clip_notes.",
        )
    if abs(float(clip["length_beats"]) - float(observed_length)) > CLIP_LENGTH_TOLERANCE_BEATS:
        return _check(
            check_id,
            "clips",
            expected,
            _clip_observed_summary(payload),
            CHECK_FAIL,
            f"Clip length mismatch on track {track_index} slot {clip_index}: "
            f"expected {clip['length_beats']:g} beats, observed {observed_length:g} "
            f"(tolerance {CLIP_LENGTH_TOLERANCE_BEATS:g}).",
        )
    observed_notes = payload.get("notes") if isinstance(payload.get("notes"), list) else []
    if payload.get("truncated"):
        return _check(
            check_id,
            "clips",
            expected,
            _clip_observed_summary(payload),
            CHECK_FAIL,
            f"Session clip notes on track {track_index} slot {clip_index} were "
            "truncated by AbletonGPT; full note identity cannot be verified.",
        )
    matched, detail = _notes_match(expected_notes, observed_notes)
    if not matched:
        return _check(
            check_id,
            "clips",
            expected,
            _clip_observed_summary(payload),
            CHECK_FAIL,
            f"MIDI clip mismatch on track {track_index} slot {clip_index}: {detail} "
            f"(timing tolerance {NOTE_TIME_TOLERANCE_BEATS:g} beats). "
            "Inspect the Session clip; VS7 will not rewrite notes.",
        )
    return _check(
        check_id,
        "clips",
        expected,
        _clip_observed_summary(payload),
        CHECK_PASS,
        f"Session MIDI clip on track {track_index} slot {clip_index} matches "
        f"({expected['note_count']} notes)",
    )


def _check_arrangement(
    target: Mapping[str, Any],
    arrangement_map: Mapping[str, Any] | None,
    *,
    length_beats: float | None,
    arrangement_observable: Any,
) -> dict[str, Any]:
    index = int(target["track_index"])
    expected = {
        "track_index": index,
        "destination_time_beats": target.get("destination_time_beats"),
        "name": target.get("name"),
        "length_beats": length_beats,
    }
    check_id = f"arrangement:{index}"
    if arrangement_map is None or arrangement_observable is False:
        return _check(
            check_id,
            "arrangement",
            expected,
            None,
            CHECK_NOT_OBSERVABLE,
            "Arrangement placement is not observable through the present "
            "AbletonGPT read evidence. copy_session_clip_to_arrangement was "
            "planned; unknown is not pass. Inspect Live's Arrangement view.",
        )
    payload = arrangement_map.get(str(index))
    if isinstance(payload, Mapping) and payload.get("not_observable"):
        return _check(
            check_id,
            "arrangement",
            expected,
            payload,
            CHECK_NOT_OBSERVABLE,
            "AbletonGPT could not observe Arrangement clips for track "
            f"{index}: {payload.get('error')}. Unknown is not pass.",
        )
    if not isinstance(payload, Mapping) or not isinstance(payload.get("clips"), list):
        return _check(
            check_id,
            "arrangement",
            expected,
            payload,
            CHECK_NOT_OBSERVABLE,
            f"Arrangement clip evidence for track {index} is missing or unsupported. "
            "Unknown is not pass.",
        )
    clips = [item for item in payload["clips"] if isinstance(item, Mapping)]
    destination = float(target.get("destination_time_beats") or 0.0)
    match = None
    for item in clips:
        start = item.get("start_time")
        if not isinstance(start, (int, float)):
            continue
        if abs(float(start) - destination) <= ARRANGEMENT_TIME_TOLERANCE_BEATS:
            match = item
            break
    if match is None:
        return _check(
            check_id,
            "arrangement",
            expected,
            {"clips": clips[:8], "clip_count": payload.get("clip_count")},
            CHECK_FAIL,
            f"Expected an Arrangement clip on track {index} at "
            f"{destination:g} beats named {target.get('name')!r}; none matched. "
            "Inspect Arrangement view; VS7 will not copy clips.",
        )
    observed_length = match.get("length_beats")
    if (
        length_beats is not None
        and isinstance(observed_length, (int, float))
        and abs(float(length_beats) - float(observed_length)) > CLIP_LENGTH_TOLERANCE_BEATS
    ):
        return _check(
            check_id,
            "arrangement",
            expected,
            match,
            CHECK_FAIL,
            f"Arrangement clip length on track {index} mismatch: expected "
            f"{length_beats:g}, observed {observed_length:g}.",
        )
    return _check(
        check_id,
        "arrangement",
        expected,
        match,
        CHECK_PASS,
        f"Arrangement clip on track {index} at {destination:g} beats",
    )


def _notes_match(
    expected: Sequence[Mapping[str, Any]], observed: Sequence[Any]
) -> tuple[bool, str]:
    if len(expected) != len(observed):
        return False, f"note count expected {len(expected)}, observed {len(observed)}"
    ordered_expected = sorted(
        expected,
        key=lambda note: (
            int(note["pitch"]),
            float(note["start_time"]),
            float(note["duration"]),
            int(note.get("velocity", 100)),
        ),
    )
    ordered_observed: list[Mapping[str, Any]] = []
    for note in observed:
        if not isinstance(note, Mapping):
            return False, "observed note is not an object"
        ordered_observed.append(note)
    ordered_observed.sort(
        key=lambda note: (
            int(note.get("pitch", -1)),
            float(note.get("start_time", -1)),
            float(note.get("duration", -1)),
            float(note.get("velocity", 100)),
        )
    )
    for wanted, found in zip(ordered_expected, ordered_observed):
        if int(wanted["pitch"]) != int(found.get("pitch", -1)):
            return (
                False,
                f"pitch expected {wanted['pitch']}, observed {found.get('pitch')}",
            )
        if abs(float(wanted["start_time"]) - float(found.get("start_time", -1))) > NOTE_TIME_TOLERANCE_BEATS:
            return (
                False,
                f"start_time expected {wanted['start_time']}, observed {found.get('start_time')} "
                f"(tolerance {NOTE_TIME_TOLERANCE_BEATS:g})",
            )
        if abs(float(wanted["duration"]) - float(found.get("duration", -1))) > NOTE_TIME_TOLERANCE_BEATS:
            return (
                False,
                f"duration expected {wanted['duration']}, observed {found.get('duration')} "
                f"(tolerance {NOTE_TIME_TOLERANCE_BEATS:g})",
            )
        if int(wanted.get("velocity", 100)) != int(float(found.get("velocity", 100))):
            return (
                False,
                f"velocity expected {wanted.get('velocity')}, observed {found.get('velocity')}",
            )
    return True, "notes match"


def _summarize_state(checks: Sequence[Mapping[str, Any]]) -> str:
    if any(check.get("status") == CHECK_FAIL for check in checks):
        return STATE_FAILED
    if any(check.get("status") == CHECK_NOT_OBSERVABLE for check in checks):
        return STATE_PARTIAL
    return STATE_VERIFIED


def _build_verification_document(
    loaded: VerifiedExecution,
    *,
    expected: Mapping[str, Any],
    observed: Mapping[str, Any] | None,
    checks: Sequence[Mapping[str, Any]],
    state: str,
    error: str | None,
) -> dict[str, Any]:
    root = loaded.project_dir
    passed = sum(1 for check in checks if check.get("status") == CHECK_PASS)
    failed = sum(1 for check in checks if check.get("status") == CHECK_FAIL)
    not_observable = sum(
        1 for check in checks if check.get("status") == CHECK_NOT_OBSERVABLE
    )
    job_plan = None
    if loaded.job_plan_file is not None:
        job_plan = {
            "path": _relpath(loaded.job_plan_file, root),
            "sha256": loaded.job_plan_sha256,
        }
    document: dict[str, Any] = {
        "ableton_verification_version": ABLETON_VERIFICATION_VERSION,
        "verification_state": state,
        "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "handoff": {
                "path": _relpath(loaded.handoff_file, root),
                "sha256": loaded.handoff_sha256,
            },
            "execution_receipt": {
                "path": _relpath(loaded.receipt_file, root),
                "sha256": loaded.receipt_sha256,
            },
            "arrangement_plan": {
                "path": _relpath(loaded.arrangement_plan_file, root),
                "sha256": loaded.arrangement_plan_sha256,
            },
            "job_plan": job_plan,
            "adopted_round": loaded.adopted_round,
        },
        "expected": {
            "tempo": expected.get("tempo"),
            "first_track_index": expected.get("first_track_index"),
            "created_track_count": expected.get("created_track_count"),
            "expected_track_count": expected.get("expected_track_count"),
            "tracks": expected.get("tracks"),
            "devices": expected.get("devices"),
            "clips": [_clip_expected_summary(item) for item in expected.get("clips") or []],
            "arrangement": expected.get("arrangement"),
        },
        "observed": observed,
        "checks": list(checks),
        "summary": {
            "passed": passed,
            "failed": failed,
            "not_observable": not_observable,
        },
        "boundary": {
            "live_access": "AbletonGPT",
            "kihachi_direct_live_access": False,
            "repair": False,
            "auto_adoption": False,
        },
    }
    if error is not None:
        document["error"] = error
    return document


def _bound_observed(observed: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the durable snapshot small: no device parameter dumps."""

    devices = observed.get("devices")
    bounded_devices: Any = devices
    if isinstance(devices, Mapping):
        bounded_devices = {}
        for key, payload in devices.items():
            if isinstance(payload, list):
                bounded_devices[key] = payload[:MAX_DEVICE_EVIDENCE]
            else:
                bounded_devices[key] = payload
    clips = observed.get("session_clips")
    bounded_clips: Any = clips
    if isinstance(clips, Mapping):
        bounded_clips = {
            key: _clip_observed_summary(payload) if isinstance(payload, Mapping) else payload
            for key, payload in clips.items()
        }
    return {
        "abletongpt_evidence_version": observed.get("abletongpt_evidence_version"),
        "read_only": observed.get("read_only", True),
        "ping": observed.get("ping"),
        "live_state": observed.get("live_state"),
        "devices": bounded_devices,
        "session_clips": bounded_clips,
        "arrangement_clips": observed.get("arrangement_clips"),
        "arrangement_observable": observed.get("arrangement_observable"),
    }


def _clip_expected_summary(clip: Mapping[str, Any]) -> dict[str, Any]:
    notes = list(clip.get("notes") or [])
    return {
        "track_index": clip.get("track_index"),
        "clip_index": clip.get("clip_index"),
        "name": clip.get("name"),
        "length_beats": clip.get("length_beats"),
        "note_count": clip.get("note_count", len(notes)),
        "notes": notes[:MAX_NOTES_IN_MANIFEST],
    }


def _clip_observed_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    notes = payload.get("notes") if isinstance(payload.get("notes"), list) else []
    return {
        "track_index": payload.get("track_index"),
        "clip_index": payload.get("clip_index"),
        "clip": payload.get("clip"),
        "length_beats": payload.get("length_beats"),
        "note_count": payload.get("note_count", len(notes)),
        "truncated": payload.get("truncated"),
        "error": payload.get("error"),
        "notes": notes[:MAX_NOTES_IN_MANIFEST],
    }


def _check(
    check_id: str,
    category: str,
    expected: Any,
    observed: Any,
    status: str,
    message: str,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "category": category,
        "expected": expected,
        "observed": observed,
        "status": status,
        "message": message,
    }


def _describe_check(check: Mapping[str, Any]) -> str:
    status = check.get("status")
    mark = {
        CHECK_PASS: "PASS",
        CHECK_FAIL: "FAIL",
        CHECK_NOT_OBSERVABLE: "NOT OBSERVABLE",
    }.get(str(status), str(status).upper())
    category = check.get("category")
    expected = check.get("expected")
    observed = check.get("observed")
    if check.get("id") == "tempo":
        return f"Tempo: expected {expected} / observed {observed} — {mark}"
    if category == "tracks" and isinstance(expected, Mapping) and "name" in expected:
        return f"Track {expected.get('name')} @ {expected.get('index')} — {mark}"
    if category == "devices":
        role = expected.get("role") if isinstance(expected, Mapping) else None
        kind = expected.get("kind") if isinstance(expected, Mapping) else "device"
        return f"{str(kind).replace('_', ' ').title()} {role or ''} present — {mark}".replace(
            "  ", " "
        )
    if category == "clips":
        return f"Session MIDI clip {check.get('id')} — {mark}"
    if category == "arrangement":
        return f"Arrangement placement — {mark}"
    if category == "tracks":
        return f"Track count — {mark}"
    return f"{check.get('id')} — {mark}"


def _tracks_by_index(tracks: Sequence[Any]) -> dict[int, Mapping[str, Any]]:
    by_index: dict[int, Mapping[str, Any]] = {}
    for item in tracks:
        if not isinstance(item, Mapping):
            continue
        index = item.get("index")
        if isinstance(index, int):
            by_index[index] = item
    return by_index


def _clip_length_for_track(expected: Mapping[str, Any], track_index: int) -> float | None:
    for clip in expected.get("clips") or []:
        if int(clip.get("track_index", -1)) == track_index:
            length = clip.get("length_beats")
            return float(length) if isinstance(length, (int, float)) else None
    return None


def _require_evidence_object(payload: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AbletonVerificationError(
            f"{source} did not return a JSON object. "
            "Inspect the AbletonGPT evidence payload; VS7 will not invent Live state."
        )
    version = payload.get("abletongpt_evidence_version")
    if version not in {None, EVIDENCE_SCHEMA_VERSION}:
        raise AbletonVerificationError(
            f"Unsupported AbletonGPT evidence schema {version!r} "
            f"(expected {EVIDENCE_SCHEMA_VERSION}). "
            "Inspect the collector output; VS7 will not guess field meanings."
        )
    if payload.get("read_only") is False:
        raise AbletonVerificationError(
            "Evidence payload is not marked read_only. Refusing to treat it as "
            "a VS7 postcondition snapshot."
        )
    return payload


def _provenance_fingerprints(project_dir: Path) -> dict[str, str]:
    """Snapshot VS1–VS6 artifacts that VS7 must not modify."""

    root = Path(project_dir).resolve()
    fingerprints: dict[str, str] = {}
    for name in (
        "ableton_handoff.json",
        "ableton_execution.json",
        "ableton_job_plan.json",
        "revision_log.json",
        "preference_memory.json",
        "song_spec.json",
    ):
        path = root / name
        if path.is_file():
            fingerprints[str(path)] = _file_sha256(path)
    handoff_file = ableton_handoff_path(root)
    if handoff_file.is_file():
        try:
            payload = json.loads(handoff_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return fingerprints
        if not isinstance(payload, dict):
            return fingerprints
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
            raise AbletonVerificationError(
                f"VS7 must not remove {path.name}; it disappeared during verification."
            )
        actual = _file_sha256(path)
        if actual != digest:
            raise AbletonVerificationError(
                f"VS7 must not modify {path.name}; it changed during verification."
            )


def _resolve_receipt_path(stored: Any, *, project_dir: Path, label: str) -> Path:
    if not isinstance(stored, str) or not stored.strip():
        raise AbletonVerificationError(f"Execution receipt {label} path is missing")
    path = Path(stored)
    resolved = path.resolve() if path.is_absolute() else (project_dir / path).resolve()
    try:
        resolved.relative_to(project_dir.parent.resolve())
    except ValueError as error:
        raise AbletonVerificationError(
            f"Execution receipt {label} path escapes the project parent: {stored}"
        ) from error
    return resolved


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise AbletonVerificationError(f"Unable to read {label}: {path}") from error
    except json.JSONDecodeError as error:
        raise AbletonVerificationError(
            f"{label} is not valid JSON: {path} ({error.msg})"
        ) from error
    if not isinstance(payload, dict):
        raise AbletonVerificationError(f"{label} must be a JSON object: {path}")
    return payload


def _python_executable(abletongpt_python: Path | str | None) -> str:
    if abletongpt_python is None:
        return sys.executable
    path = Path(abletongpt_python)
    if path.exists():
        return str(path.resolve())
    return str(path)


def _module_unavailable(result: CommandResult) -> bool:
    blob = f"{result.stderr}\n{result.stdout}"
    return (
        "No module named abletongpt" in blob
        or "ModuleNotFoundError: No module named 'abletongpt'" in blob
    )


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
