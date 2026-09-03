"""Append-only evidence of which revision takes a human explicitly chose.

This is not training, not scoring, and not automatic optimisation.  Each entry
records what was selected, what it was selected over, the measurable deltas at
the time, and any optional human reason or tags.  Nothing here feeds Composer,
Analyzer, Reviewer, Critic, alignment weights, or ACE-Step prompts.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

PREFERENCE_MEMORY_VERSION = "0.1"
PREFERENCE_MEMORY_NAME = "preference_memory.json"


@dataclass(frozen=True)
class PreferenceEntry:
    source_project: str
    selected_round: int
    candidate_rounds: tuple[int, ...]
    rejected_rounds: tuple[int, ...]
    reason: str | None
    tags: tuple[str, ...]
    comparison: Mapping[str, Any]
    selected_at: str
    selection_mode: str
    selected_project: str
    audio_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_project": self.source_project,
            "selected_round": self.selected_round,
            "selected_project": self.selected_project,
            "candidate_rounds": list(self.candidate_rounds),
            "rejected_rounds": list(self.rejected_rounds),
            "reason": self.reason,
            "tags": list(self.tags),
            "comparison": dict(self.comparison),
            "selected_at": self.selected_at,
            "selection_mode": self.selection_mode,
            "audio_sha256": self.audio_sha256,
        }


@dataclass(frozen=True)
class PreferenceMemory:
    entries: tuple[PreferenceEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "preference_memory_version": PREFERENCE_MEMORY_VERSION,
            "scope": "human_revision_take_selection_evidence_only",
            "affects_scoring": False,
            "affects_generation": False,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def preference_memory_path(project_dir: Path) -> Path:
    return Path(project_dir) / PREFERENCE_MEMORY_NAME


def load_preference_memory(project_dir: Path) -> PreferenceMemory:
    """Load preference evidence, or an empty memory when none has been written."""

    path = preference_memory_path(project_dir)
    if not path.is_file():
        return PreferenceMemory(())
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise FileExistsError(
            f"refusing to replace non-preference-memory file: {path}"
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("preference_memory_version") != PREFERENCE_MEMORY_VERSION
        or payload.get("scope") != "human_revision_take_selection_evidence_only"
        or not isinstance(payload.get("entries"), list)
    ):
        raise FileExistsError(f"refusing to replace non-preference-memory file: {path}")
    entries = tuple(_entry_from_dict(item) for item in payload["entries"])
    return PreferenceMemory(entries)


def record_preference(
    project_dir: Path,
    entry: PreferenceEntry,
) -> PreferenceMemory:
    """Append one human selection evidence record.  Never mutates audio."""

    if entry.selection_mode != "human":
        raise ValueError("preference memory only records human selection evidence")
    memory = load_preference_memory(project_dir)
    updated = PreferenceMemory(memory.entries + (entry,))
    _atomic_write_json(preference_memory_path(project_dir), updated.to_dict())
    return updated


def _entry_from_dict(payload: Mapping[str, Any]) -> PreferenceEntry:
    if not isinstance(payload, Mapping):
        raise ValueError("preference memory entry must be an object")
    tags = payload.get("tags") or ()
    candidates = payload.get("candidate_rounds") or ()
    rejected = payload.get("rejected_rounds") or ()
    comparison = payload.get("comparison") or {}
    if not isinstance(comparison, Mapping):
        raise ValueError("preference memory comparison must be an object")
    return PreferenceEntry(
        source_project=str(payload["source_project"]),
        selected_round=int(payload["selected_round"]),
        candidate_rounds=tuple(int(item) for item in candidates),
        rejected_rounds=tuple(int(item) for item in rejected),
        reason=None if payload.get("reason") is None else str(payload["reason"]),
        tags=tuple(str(item) for item in tags),
        comparison=dict(comparison),
        selected_at=str(payload["selected_at"]),
        selection_mode=str(payload.get("selection_mode", "human")),
        selected_project=str(payload["selected_project"]),
        audio_sha256=(
            None
            if payload.get("audio_sha256") is None
            else str(payload["audio_sha256"])
        ),
    )


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
