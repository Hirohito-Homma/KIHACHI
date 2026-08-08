"""Difference instructions: change one thing without re-rolling the song.

This is the "Dropのベースだけもっと変態的に" path. A short instruction is parsed
into a *Spec Diff* -- an explicit, reviewable list of parameter moves with their
before and after values -- and applying it rewrites only the parameters named,
so only the affected part of the arrangement is recomposed.

Localised regeneration is a property of the composer, not of this module: each
section draws from its own random stream, so editing one section leaves every
other section bit-identical. :func:`summarise_regeneration` proves that after
the fact by diffing the written notes section by section.

Planning is read-only and stdlib-only. Nothing here writes a project; the CLI
plans to ``spec_edit.json`` first and applies into a *new* project.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .composer import compose_tracks
from .midi import MidiNote, write_midi
from .models import DENSITY_FIELDS, TRACK_NAMES, SectionSpec, SongSpec
from .prompt_compiler import compile_audio_prompt

EDIT_VERSION = "0.1"
DEFAULT_MAGNITUDE = 0.2
SMALL_MAGNITUDE = 0.1
LARGE_MAGNITUDE = 0.35
BAR_TOLERANCE_BEATS = 0.05

# Words that name a part. Order matters only for reporting.
TRACK_WORDS: dict[str, tuple[str, ...]] = {
    "bass": ("bass", "ベース", "低音", "スラップ", "slap", "sub"),
    "drums": ("drum", "ドラム", "kick", "キック", "percussion", "パーカッション", "hat", "ハット"),
    "chords": ("chord", "コード", "和音", "synth", "シンセ", "stab", "スタブ"),
}

# Words that name a musical quality, mapped to the parameters that carry it.
# ``section`` targets a per-section field; ``song`` targets a SongSpec field.
QUALITY_TARGETS: dict[str, tuple[tuple[str, str], ...]] = {
    "mutation": (("section", "mutation"), ("song", "bass.mutation")),
    "syncopation": (("song", "groove.syncopation"), ("song", "bass.syncopation")),
    "density": (("section", "@density"),),
    "energy": (("section", "energy"),),
    "ghost": (("song", "bass.ghost_note_probability"),),
    "octave": (("song", "bass.octave_jump_probability"),),
    "space": (("section", "@density"), ("song", "drums.dub_space")),
    "fx": (("section", "fx_amount"), ("song", "chords.dub_delay")),
}

QUALITY_WORDS: dict[str, tuple[str, ...]] = {
    "mutation": ("変態", "ミューテーション", "mutation", "mutate", "mutated", "weird", "twisted"),
    "syncopation": ("シンコペ", "syncopat", "跳ね", "うねら", "swing"),
    "density": ("密度", "詰め", "busy", "dense", "thick", "厚く", "細かく"),
    "energy": ("激し", "energy", "エネルギー", "派手", "強烈", "hard", "harder", "intense"),
    "ghost": ("ゴースト", "ghost"),
    "octave": ("オクターブ", "octave"),
    "space": ("スペース", "space", "抜い", "隙間", "sparse", "薄く", "減ら"),
    "fx": ("エフェクト", "fx", "ディレイ", "delay", "リバーブ", "reverb", "dub感"),
}

INCREASE_WORDS = ("もっと", "更に", "さらに", "上げ", "増やし", "強く", "more", "increase", "raise", "up")
DECREASE_WORDS = ("抑え", "減ら", "弱く", "下げ", "less", "reduce", "lower", "down", "薄く", "控え")
SMALL_WORDS = ("少し", "ちょっと", "やや", "slightly", "a bit", "軽く")
LARGE_WORDS = ("かなり", "大幅", "ずっと", "much", "far", "way", "大胆")

# "後半" / "前半" select a span of the arrangement rather than one section.
LATER_HALF_WORDS = ("後半", "second half", "later half", "終盤")
EARLIER_HALF_WORDS = ("前半", "first half", "earlier half", "序盤")


class EditInstructionError(ValueError):
    """The instruction did not name anything the engine can act on."""


@dataclass(frozen=True)
class EditIntent:
    sections: tuple[str, ...]
    tracks: tuple[str, ...]
    qualities: tuple[str, ...]
    direction: int
    magnitude: float


def parse_edit_instruction(instruction: str, spec: SongSpec) -> EditIntent:
    """Turn a short instruction into an explicit, checkable intent."""

    text = instruction.strip()
    if not text:
        raise EditInstructionError("edit instruction must not be empty")
    raw = text.casefold()
    sections = _resolve_sections(raw, spec)
    # Section names are identifiers, not description. Leaving them in would let
    # "dub_breakdown" match the decrease word "down", so they are removed before
    # anything else is read out of the instruction.
    lowered = _without_section_names(raw, spec)

    qualities = tuple(
        quality
        for quality, words in QUALITY_WORDS.items()
        if _matches(lowered, words)
    )
    if not qualities:
        raise EditInstructionError(
            "no musical quality recognised in the instruction; expected one of: "
            + ", ".join(sorted(QUALITY_WORDS))
        )

    tracks = tuple(
        track for track, words in TRACK_WORDS.items() if _matches(lowered, words)
    )

    direction = -1 if _matches(lowered, DECREASE_WORDS) else 1
    if _matches(lowered, LARGE_WORDS):
        magnitude = LARGE_MAGNITUDE
    elif _matches(lowered, SMALL_WORDS):
        magnitude = SMALL_MAGNITUDE
    else:
        magnitude = DEFAULT_MAGNITUDE
    # "space" and "sparse" already mean less, so they invert a plain "more".
    if "space" in qualities and not _matches(lowered, DECREASE_WORDS):
        direction = -1 if len(qualities) == 1 else direction
    return EditIntent(sections, tracks or TRACK_NAMES, qualities, direction, magnitude)


def build_spec_edit(spec: SongSpec, instruction: str) -> dict[str, Any]:
    """Plan the parameter moves for ``instruction``; never mutates ``spec``."""

    intent = parse_edit_instruction(instruction, spec)
    changes: list[dict[str, Any]] = []
    warnings: list[str] = []
    for quality in intent.qualities:
        section_targets = [p for scope, p in QUALITY_TARGETS[quality] if scope == "section"]
        song_targets = [p for scope, p in QUALITY_TARGETS[quality] if scope == "song"]
        local: list[dict[str, Any]] = []
        for path in section_targets:
            local.extend(_section_changes(spec, intent, quality, path))
        changes.extend(local)
        # Naming a section means "only there". A song-wide parameter would leak
        # the edit into every other section, so it is only used when the quality
        # has no per-section field at all -- and then it is called out.
        if intent.sections and local:
            continue
        song_changes: list[dict[str, Any]] = []
        for path in song_targets:
            song_changes.extend(_song_changes(spec, intent, quality, path))
        if intent.sections and song_changes:
            reason = (
                f"{quality} has no per-section parameter"
                if not section_targets
                else f"the per-section {quality} value is already at its limit in "
                + ", ".join(intent.sections)
            )
            warnings.append(
                f"{reason}, so "
                + ", ".join(sorted(change["path"] for change in song_changes))
                + " changes for the whole song, not only "
                + ", ".join(intent.sections)
            )
        changes.extend(song_changes)

    changes = _deduplicate(changes)
    if not changes:
        raise EditInstructionError(
            "the instruction resolved to no parameter change; every target is "
            "already at its limit"
        )
    touched = sorted({change["section"] for change in changes if change.get("section")})
    return {
        "edit_version": EDIT_VERSION,
        "execution_state": "planned_not_applied",
        "instruction": instruction.strip(),
        "song_spec_sha256": song_spec_sha256(spec),
        "target": {
            "sections": list(intent.sections) if intent.sections else "all",
            "tracks": list(intent.tracks),
        },
        "interpretation": {
            "qualities": list(intent.qualities),
            "direction": "increase" if intent.direction > 0 else "decrease",
            "magnitude": intent.magnitude,
        },
        "changes": changes,
        "scope_warnings": warnings,
        "sections_touched": touched,
        "sections_untouched": [
            section.name for section in spec.arrangement if section.name not in touched
        ],
        "safety": {
            "song_spec_mutated": False,
            "midi_regenerated": False,
            "requires_separate_output_project": True,
        },
    }


def apply_spec_edit(spec: SongSpec, edit: Mapping[str, Any]) -> SongSpec:
    """Return a new SongSpec with the planned moves applied."""

    if edit.get("edit_version") != EDIT_VERSION:
        raise ValueError(f"unsupported spec edit version: {edit.get('edit_version')!r}")
    if edit.get("song_spec_sha256") != song_spec_sha256(spec):
        raise ValueError("spec edit does not match this SongSpec")
    changes = edit.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("spec edit requires a non-empty changes list")

    sections = {section.name: section for section in spec.arrangement}
    song_updates: dict[str, dict[str, float]] = {}
    for change in changes:
        scope = change["scope"]
        if scope == "section":
            name = change["section"]
            if name not in sections:
                raise ValueError(f"spec edit names an unknown section: {name}")
            current = _read_section(sections[name], change["path"])
            if current is not None and abs(current - float(change["from"])) > 1e-9:
                raise ValueError(
                    f"section {name}.{change['path']} moved since the edit was planned"
                )
            sections[name] = dataclasses.replace(
                sections[name], **{change["path"]: float(change["to"])}
            )
        elif scope == "song":
            group, field = change["path"].split(".", 1)
            current = float(getattr(getattr(spec, group), field))
            if abs(current - float(change["from"])) > 1e-9:
                raise ValueError(f"{change['path']} moved since the edit was planned")
            song_updates.setdefault(group, {})[field] = float(change["to"])
        else:
            raise ValueError(f"unsupported spec edit scope: {scope!r}")

    updated = dataclasses.replace(
        spec, arrangement=tuple(sections[section.name] for section in spec.arrangement)
    )
    for group, fields in song_updates.items():
        updated = dataclasses.replace(
            updated, **{group: dataclasses.replace(getattr(updated, group), **fields)}
        )
    return updated


def summarise_regeneration(
    spec: SongSpec,
    before: Mapping[str, Sequence[MidiNote]],
    after: Mapping[str, Sequence[MidiNote]],
    *,
    updated: SongSpec | None = None,
) -> dict[str, Any]:
    """Which sections of which tracks actually moved, note for note.

    Some parameters (``fx_amount``, ``dub_delay``) are production hints that
    reach the audio prompt and never the notes, so pass ``updated`` to also
    report whether the ACE-Step prompt changed. Reporting only MIDI would
    otherwise say "nothing happened" about an edit that did land.
    """

    beats_per_bar = _beats_per_bar(spec)
    report: dict[str, Any] = {"tracks": {}, "changed_sections": [], "unchanged_sections": []}
    if updated is not None:
        before_prompt = compile_audio_prompt(spec)
        after_prompt = compile_audio_prompt(updated)
        report["audio_prompt_changed"] = before_prompt != after_prompt
    changed: set[str] = set()
    for track in sorted(set(before) | set(after)):
        per_section: dict[str, bool] = {}
        for section in spec.arrangement:
            low = section.start_bar
            high = section.start_bar + section.length_bars
            same = _slice(before.get(track, ()), beats_per_bar, low, high) == _slice(
                after.get(track, ()), beats_per_bar, low, high
            )
            per_section[section.name] = not same
            if not same:
                changed.add(section.name)
        report["tracks"][track] = {
            "changed_sections": [name for name, moved in per_section.items() if moved],
            "notes_before": len(before.get(track, ())),
            "notes_after": len(after.get(track, ())),
        }
    report["changed_sections"] = [
        section.name for section in spec.arrangement if section.name in changed
    ]
    report["unchanged_sections"] = [
        section.name for section in spec.arrangement if section.name not in changed
    ]
    return report


def song_spec_sha256(spec: SongSpec) -> str:
    return hashlib.sha256(spec.to_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EditApplyManifest:
    source_project: Path
    output_project: Path
    spec: SongSpec
    files: tuple[Path, ...]
    report: dict[str, Any]


def apply_edit_to_project(
    source_project: Path,
    output_project: Path,
    *,
    edit_path: Path | None = None,
) -> EditApplyManifest:
    """Write a new project with the edit applied; never touches the source."""

    source_project = Path(source_project)
    output_project = Path(output_project)
    if source_project.resolve() == output_project.resolve():
        raise ValueError("edit output project must differ from the source project")
    if output_project.exists():
        raise FileExistsError(f"refusing to replace edit output project: {output_project}")

    spec_path = source_project / "song_spec.json"
    if not spec_path.is_file():
        raise FileNotFoundError(f"SongSpec not found: {spec_path}")
    spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))

    requested = Path(edit_path) if edit_path is not None else Path("spec_edit.json")
    if not requested.is_absolute():
        requested = source_project / requested
    if not requested.is_file():
        raise FileNotFoundError(f"spec edit not found: {requested}")
    edit = json.loads(requested.read_text(encoding="utf-8"))

    updated = apply_spec_edit(spec, edit)
    before = compose_tracks(spec)
    after = compose_tracks(updated)
    report = summarise_regeneration(spec, before, after, updated=updated)
    report["instruction"] = edit.get("instruction")
    report["source_project"] = source_project.name
    report["source_song_spec_sha256"] = edit["song_spec_sha256"]
    report["edited_song_spec_sha256"] = song_spec_sha256(updated)
    report["no_effect"] = not report["changed_sections"] and not report.get(
        "audio_prompt_changed", False
    )

    output_project.parent.mkdir(parents=True, exist_ok=True)
    stage: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{output_project.name}-", dir=output_project.parent)
    )
    names = (
        "song_spec.json",
        "bass.mid",
        "drums.mid",
        "chords.mid",
        "prompt.txt",
        "applied_spec_edit.json",
        "edit_report.json",
    )
    try:
        updated.write_json(stage / "song_spec.json")
        for track, notes in after.items():
            write_midi(
                stage / f"{track}.mid",
                notes,
                track_name=f"KIHACHI {track.title()}",
                bpm=updated.song.bpm,
                key=updated.song.key,
            )
        (stage / "prompt.txt").write_text(compile_audio_prompt(updated), encoding="utf-8")
        applied = dict(edit)
        applied["execution_state"] = "applied"
        (stage / "applied_spec_edit.json").write_text(
            json.dumps(applied, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (stage / "edit_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(stage, output_project)
        stage = None
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)

    return EditApplyManifest(
        source_project=source_project,
        output_project=output_project,
        spec=updated,
        files=tuple(output_project / name for name in names),
        report=report,
    )


def _matches(lowered: str, words: Sequence[str]) -> bool:
    return any(_contains(lowered, word) for word in words)


def _contains(lowered: str, word: str) -> bool:
    """Substring match, but an ASCII word has to start a word.

    Japanese has no word boundaries, so substring is the only option there. A
    bare ASCII substring would match inside unrelated words ("up" in "group"),
    while requiring a boundary at *both* ends would miss ordinary inflection
    ("dense" in "densely"), so only the start is anchored.
    """

    folded = word.casefold()
    if folded.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(folded)}", lowered) is not None
    return folded in lowered


def _without_section_names(lowered: str, spec: SongSpec) -> str:
    stripped = lowered
    names = sorted(
        {section.name.casefold() for section in spec.arrangement}
        | {_bare_name(section.name).casefold() for section in spec.arrangement},
        key=len,
        reverse=True,
    )
    for name in names:
        stripped = stripped.replace(name, " ")
    return stripped


def _resolve_sections(lowered: str, spec: SongSpec) -> tuple[str, ...]:
    named = [
        section.name
        for section in spec.arrangement
        if section.name.casefold() in lowered
        or _bare_name(section.name).casefold() in lowered
    ]
    if named:
        return tuple(dict.fromkeys(named))
    midpoint = len(spec.arrangement) // 2
    if _matches(lowered, LATER_HALF_WORDS):
        return tuple(section.name for section in spec.arrangement[midpoint:])
    if _matches(lowered, EARLIER_HALF_WORDS):
        return tuple(section.name for section in spec.arrangement[:midpoint])
    for keyword, wanted in (("drop", "drop"), ("ドロップ", "drop"), ("breakdown", "breakdown"),
                            ("ブレイク", "breakdown"), ("intro", "intro"), ("イントロ", "intro"),
                            ("build", "build"), ("ビルド", "build"), ("outro", "outro")):
        if keyword.casefold() in lowered:
            hits = [
                section.name for section in spec.arrangement if wanted in section.name.casefold()
            ]
            if hits:
                return tuple(hits)
    return ()


def _bare_name(name: str) -> str:
    return re.sub(r"_\d+$", "", name)


def _target_sections(spec: SongSpec, intent: EditIntent) -> list[SectionSpec]:
    if not intent.sections:
        return list(spec.arrangement)
    wanted = set(intent.sections)
    return [section for section in spec.arrangement if section.name in wanted]


def _section_changes(
    spec: SongSpec,
    intent: EditIntent,
    quality: str,
    path: str,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for section in _target_sections(spec, intent):
        fields = (
            [DENSITY_FIELDS[track] for track in intent.tracks]
            if path == "@density"
            else [path]
        )
        for field in fields:
            current = _read_section(section, field)
            if current is None:
                current = section.energy
            moved = _move(current, intent.direction, intent.magnitude)
            if abs(moved - current) < 1e-9:
                continue
            changes.append(
                {
                    "scope": "section",
                    "section": section.name,
                    "path": field,
                    "quality": quality,
                    "from": round(current, 4),
                    "to": round(moved, 4),
                }
            )
    return changes


def _song_changes(
    spec: SongSpec,
    intent: EditIntent,
    quality: str,
    path: str,
) -> list[dict[str, Any]]:
    group, field = path.split(".", 1)
    # A song-wide bass parameter is only relevant if the bass is a target.
    if group in TRACK_NAMES and group not in intent.tracks:
        return []
    current = float(getattr(getattr(spec, group), field))
    moved = _move(current, intent.direction, intent.magnitude)
    if abs(moved - current) < 1e-9:
        return []
    return [
        {
            "scope": "song",
            "section": None,
            "path": path,
            "quality": quality,
            "from": round(current, 4),
            "to": round(moved, 4),
        }
    ]


def _read_section(section: SectionSpec, field: str) -> float | None:
    value = getattr(section, field)
    return None if value is None else float(value)


def _move(current: float, direction: int, magnitude: float) -> float:
    return round(max(0.0, min(1.0, current + direction * magnitude)), 4)


def _deduplicate(changes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for change in changes:
        key = (change["scope"], change.get("section"), change["path"])
        if key not in seen:
            seen[key] = dict(change)
    return list(seen.values())


def _beats_per_bar(spec: SongSpec) -> float:
    numerator, denominator = (int(part) for part in spec.song.time_signature.split("/", 1))
    return numerator * (4.0 / denominator)


def _slice(
    notes: Sequence[MidiNote],
    beats_per_bar: float,
    low_bar: int,
    high_bar: int,
) -> list[tuple[float, int, int, int]]:
    low = low_bar * beats_per_bar - BAR_TOLERANCE_BEATS
    high = high_bar * beats_per_bar - BAR_TOLERANCE_BEATS
    return [
        (round(note.start_beats, 6), note.pitch, note.velocity, note.channel)
        for note in notes
        if low <= note.start_beats < high
    ]
