"""Durably record the listening decision the generator refuses to automate.

Choosing a take is a human judgement.  This module records that judgement and
the exact audio hashes it was made against; it never copies, replaces, deletes,
or renames audio.  Later changes append to the log so an earlier choice cannot
silently disappear.
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

DECISION_LOG_VERSION = "0.1"
DECISION_LOG_NAME = "decision_log.json"


@dataclass(frozen=True)
class DecisionManifest:
    project_dir: Path
    decision_file: Path
    decision: dict[str, Any]
    entry: dict[str, Any]


def record_decision(
    project_dir: Path,
    *,
    selected_project: Path,
    candidate_projects: Sequence[Path] = (),
    reason: str,
) -> DecisionManifest:
    """Append one human listening choice without touching any candidate audio."""

    project_dir = Path(project_dir)
    selected_project = Path(selected_project)
    reason = reason.strip()
    if not reason:
        raise ValueError("decision reason must not be blank")

    requested = [project_dir, *(Path(item) for item in candidate_projects)]
    unique: list[Path] = []
    resolved: set[Path] = set()
    for item in requested:
        identity = item.resolve()
        if identity not in resolved:
            unique.append(item)
            resolved.add(identity)
    selected_identity = selected_project.resolve()
    if selected_identity not in resolved:
        raise ValueError("selected project must be the base project or one supplied with --also")

    candidates = [load_candidate(path) for path in unique]
    snapshots = [_candidate_snapshot(project_dir, item) for item in candidates]
    selected = next(
        item
        for item, path in zip(snapshots, unique)
        if path.resolve() == selected_identity
    )

    destination = project_dir / DECISION_LOG_NAME
    decision = _load_existing(destination)
    entries = decision["entries"]
    entry = {
        "index": len(entries),
        "action": (
            "retain_base"
            if selected_identity == project_dir.resolve()
            else "select_candidate"
        ),
        "selected": {
            "project": selected["project"],
            "name": selected["name"],
            "audio_sha256": selected["audio_sha256"],
        },
        "reason": reason,
        "candidates": snapshots,
        "effects": {
            "audio_copied": False,
            "audio_overwritten": False,
            "audio_deleted": False,
            "selection_record_only": True,
        },
    }
    entries.append(entry)
    decision["current_decision"] = entry["index"]
    _atomic_write_json(destination, decision)
    return DecisionManifest(project_dir, destination, decision, entry)


def load_decision_log(project_dir: Path) -> dict[str, Any] | None:
    path = Path(project_dir) / DECISION_LOG_NAME
    if not path.is_file():
        return None
    return _load_existing(path)


def current_decision(decision: dict[str, Any] | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    index = decision.get("current_decision")
    entries = decision.get("entries")
    if not isinstance(index, int) or not isinstance(entries, list):
        return None
    if not 0 <= index < len(entries):
        return None
    entry = entries[index]
    return entry if isinstance(entry, dict) else None


def decision_audio_status(project_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Whether the selected file still has the bytes the person listened to."""

    selected = entry.get("selected")
    candidates = entry.get("candidates")
    if not isinstance(selected, dict) or not isinstance(candidates, list):
        return {"status": "unverifiable"}
    project_reference = selected.get("project")
    expected = selected.get("audio_sha256")
    snapshot = next(
        (
            item
            for item in candidates
            if isinstance(item, dict)
            and item.get("project") == project_reference
            and item.get("audio_sha256") == expected
        ),
        None,
    )
    if (
        snapshot is None
        or not isinstance(project_reference, str)
        or not isinstance(snapshot.get("audio_file"), str)
        or not isinstance(expected, str)
    ):
        return {"status": "unverifiable"}
    audio = Path(project_dir) / project_reference / snapshot["audio_file"]
    if not audio.is_file():
        return {
            "status": "missing",
            "audio_file": _relative_for_display(audio, Path(project_dir)),
            "expected_sha256": expected,
            "actual_sha256": None,
        }
    actual = _file_sha256(audio)
    return {
        "status": "current" if actual == expected else "changed",
        "audio_file": _relative_for_display(audio, Path(project_dir)),
        "expected_sha256": expected,
        "actual_sha256": actual,
    }


def _candidate_snapshot(owner: Path, candidate: Candidate) -> dict[str, Any]:
    audio = candidate.audio_file
    if audio is None or not audio.is_file():
        raise FileNotFoundError(f"candidate audio not found: {candidate.project_dir}")
    if not candidate.scanned:
        material_status = "not_scanned"
    elif not candidate.defects:
        material_status = "clean"
    elif candidate.blocking:
        material_status = "blocking"
    else:
        material_status = "warning"
    return {
        "project": _relative(candidate.project_dir, owner),
        "name": candidate.name,
        "audio_file": _relative(audio, candidate.project_dir),
        "audio_sha256": _file_sha256(audio),
        "alignment": candidate.alignment,
        "grade": candidate.grade,
        "material_status": material_status,
        "defects": [
            {"code": str(item["code"]), "severity": str(item["severity"])}
            for item in candidate.defects
        ],
    }


def _relative(target: Path, base: Path) -> str:
    return Path(os.path.relpath(target.resolve(), base.resolve())).as_posix()


def _relative_for_display(target: Path, base: Path) -> str:
    return Path(os.path.relpath(target.absolute(), base.absolute())).as_posix()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "decision_log_version": DECISION_LOG_VERSION,
            "scope": "human_listening_decisions_only",
            "current_decision": None,
            "entries": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise FileExistsError(f"refusing to replace non-decision file: {path}") from error
    if (
        not isinstance(payload, dict)
        or payload.get("decision_log_version") != DECISION_LOG_VERSION
        or payload.get("scope") != "human_listening_decisions_only"
        or not isinstance(payload.get("entries"), list)
    ):
        raise FileExistsError(f"refusing to replace non-decision file: {path}")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as sink:
            json.dump(payload, sink, ensure_ascii=False, indent=2)
            sink.write("\n")
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
