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
from .intent import (
    EARLIER_HALF_WORDS,
    ENGLISH_NEGATORS,
    JAPANESE_NEGATORS,
    JAPANESE_VERB_NEGATORS,
    LARGE_WORDS,
    LATER_HALF_WORDS,
    SMALL_WORDS,
    TRAIT_WORDS,
    contains as _contains,
    matches as _matches,
)
from .midi import MidiNote, write_midi
from .models import DENSITY_FIELDS, TRACK_NAMES, SectionSpec, SongSpec
from .prompt_compiler import compile_audio_prompt, render_brief

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
    "swing": (("song", "groove.swing"),),
    "density": (("section", "@density"),),
    "energy": (("section", "energy"),),
    "ghost": (("song", "bass.ghost_note_probability"),),
    "octave": (("song", "bass.octave_jump_probability"),),
    "space": (("section", "@density"), ("song", "drums.dub_space")),
    "fx": (("section", "fx_amount"), ("song", "chords.dub_delay")),
}

QUALITY_WORDS: dict[str, tuple[str, ...]] = {
    "mutation": ("変態", "ミューテーション", "mutation", "mutate", "mutated", "weird", "twisted"),
    # 「跳ね」 and `swing` used to be here, and they name the swing rather than
    # the syncopation: they moved `groove.syncopation` while the brief reader
    # has read them as `groove.swing` since the `swung` trait landed. One word,
    # two knobs, depending on whether you were asking for a song or correcting
    # one.
    "syncopation": ("シンコペ", "syncopat", "うねら"),
    # The brief reader's own list, like the degree words: a correction and a
    # brief must not disagree about what 「シャッフル」 means.
    "swing": TRAIT_WORDS["swung"],
    "density": ("密度", "詰め", "busy", "dense", "thick", "厚く", "細かく"),
    "energy": ("激し", "energy", "エネルギー", "派手", "強烈", "hard", "harder", "intense"),
    "ghost": ("ゴースト", "ghost"),
    "octave": ("オクターブ", "octave"),
    "space": ("スペース", "space", "抜い", "隙間", "sparse", "薄く", "減ら"),
    "fx": ("エフェクト", "fx", "ディレイ", "delay", "リバーブ", "reverb", "dub感"),
}

INCREASE_WORDS = ("もっと", "更に", "さらに", "上げ", "増やし", "強く", "more", "increase", "raise", "up")
DECREASE_WORDS = ("抑え", "減ら", "弱く", "下げ", "less", "reduce", "lower", "down", "薄く", "控え")

#: 「抜い」 is one of `intent`'s verb negators and this module's own word for
#: *making* space (「ドラムを抜いて」), so it names a quality here rather than
#: refusing one. Reading it both ways would turn one instruction into two
#: readings that disagree about which direction it asks for.
_NOT_A_REFUSAL_HERE = ("抜い",)

#: Words that refuse a quality rather than nudging it. **The brief reader's
#: own lists**, for the reason the degree words are shared: 「無しで」 must mean
#: the same thing when asking for a song and when correcting one, and until
#: now it meant nothing at all here -- 「ゴーストノートは無しで」 raised the ghost
#: notes from 0.34 to 0.54, because no word in it was a decrease word and the
#: direction defaults to up. That is the failure `intent` fixed in #82 and #84,
#: still live on this side of the vocabulary.
#:
#: The suffix negators are deliberately left out. This parser is a bag of words
#: with no positions in it, so a bare 「ない」 cannot tell 「入れないで」 from
#: 「減らさないで」, and the second is a refusal of the *edit* rather than of the
#: quality.
REFUSAL_WORDS = (
    JAPANESE_NEGATORS
    + tuple(word for word in JAPANESE_VERB_NEGATORS if word not in _NOT_A_REFUSAL_HERE)
    + ENGLISH_NEGATORS
)

#: A refusal lands on the low pole and does not go past it -- the same rule the
#: brief reader states, and here it falls out of the clamp in :func:`_move`:
#: any current value minus a full magnitude is the bottom of its range.
REFUSAL_MAGNITUDE = 1.0

#: Parameters whose usable range is **not** 0 to 1, with the range they have.
#:
#: `groove.swing` is the only one so far and it is not close: 0.5 is straight
#: and 0.667 is triplet swing, so the whole feel lives in a sixth of the unit
#: interval and a 0.2 move would leave music behind entirely. The poles are the
#: brief reader's (`music_brain._SWUNG_POLE`, `_STRAIGHT_POLE`), so 「もっと
#: 跳ねさせて」 walks the same axis whether it is asked for or corrected.
#:
#: A magnitude is read as a **share of the range**, which leaves every other
#: parameter exactly where it was -- the range is 1.0 wide, so the arithmetic is
#: unchanged -- and gives a refusal the right meaning here too: refusing the
#: swing lands on 0.5, which is straight.
PARAMETER_RANGE: dict[str, tuple[float, float]] = {"groove.swing": (0.5, 0.66)}
# ``SMALL_WORDS`` / ``LARGE_WORDS`` now live in :mod:`.intent` and are imported
# above, unchanged. They are the same words a *brief* uses, and keeping two
# copies meant "少し" could mean one thing when asking for a song and another
# when correcting it. The magnitudes below stay here: how far an edit moves is
# an edit's business.

# "後半" / "前半" select a span of the arrangement rather than one section. They
# live in `intent` now, with the degree words and for the same reason: a brief
# can name a place too, and 「後半」 must not mean one span when asking for a
# song and another when correcting it.


class EditInstructionError(ValueError):
    """The instruction did not name anything the engine can act on."""


@dataclass(frozen=True)
class EditIntent:
    sections: tuple[str, ...]
    tracks: tuple[str, ...]
    qualities: tuple[str, ...]
    direction: int
    magnitude: float
    refusal: bool = False


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

    refusal = _matches(lowered, REFUSAL_WORDS)
    if refusal and "space" in qualities:
        # Every other quality has a low pole that "none of it" plainly means.
        # This one is already spelled as an absence, so refusing it is asking
        # for the opposite of less -- and this parser cannot tell
        # 「スペースは要らない」 (denser) from 「隙間は無しで」 (denser) from a
        # refusal of the thing that made the space. It says so instead of
        # picking one.
        raise EditInstructionError(
            "refusing 'space' is ambiguous: it already means less of something. "
            "Say which way to move instead, as in 「密度を上げて」"
        )
    if refusal:
        direction, magnitude = -1, REFUSAL_MAGNITUDE
    else:
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
    return EditIntent(
        sections, tracks or TRACK_NAMES, qualities, direction, magnitude, refusal
    )


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
            "direction": (
                "refuse" if intent.refusal else "increase" if intent.direction > 0 else "decrease"
            ),
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
        "prompt.json",
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
        # Rewritten, not carried over: ``prompt.json`` states the SHA-256 of the
        # spec it was compiled from, so a copied one would claim to describe the
        # song before the edit.
        (stage / "prompt.json").write_text(
            json.dumps(render_brief(updated), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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


# ``_matches`` / ``_contains`` are imported from :mod:`.intent`; the brief and
# the correction now agree on what counts as a mention.


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
        # Only three of the seven tracks carry a density of their own; the four
        # in `EXTRA_TRACKS` fall back to the section energy and have no field to
        # move. An instruction that names a track always resolves to one of the
        # three, because those are the only keys `TRACK_WORDS` has -- but one
        # that names none defaults to every track there is, and 「密度を上げて」
        # raised `KeyError: 'sub'` from inside the planner. The same lookup in
        # `SectionSpec.density` has always been a `.get`.
        fields = (
            [DENSITY_FIELDS[track] for track in intent.tracks if track in DENSITY_FIELDS]
            if path == "@density"
            else [path]
        )
        for field in fields:
            current = _read_section(section, field)
            if current is None:
                current = section.energy
            moved = _move(current, intent.direction, intent.magnitude, field)
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
    moved = _move(current, intent.direction, intent.magnitude, path)
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


def _move(current: float, direction: int, magnitude: float, path: str = "") -> float:
    """Move ``current`` by a share of its parameter's range, and clamp to it."""

    low, high = PARAMETER_RANGE.get(path, (0.0, 1.0))
    moved = current + direction * magnitude * (high - low)
    return round(max(low, min(high, moved)), 4)


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
