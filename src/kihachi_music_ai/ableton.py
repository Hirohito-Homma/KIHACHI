"""ArrangementPlan: what to build in Live, as operations AbletonGPT can run.

This is the KIHACHI side of the boundary from the design notes:

    KIHACHI Music AI = decides what to make   (Producer)
    AbletonGPT       = operates Ableton Live  (Engineer)

So nothing here talks to Live. It emits an ordered, reviewable operation list
whose names and parameters match AbletonGPT's tool surface exactly, plus the
structure the operations encode, so the plan can be checked before anything is
created.

Why this path at all: rendering a nine-section arrangement from one text prompt
was measured to lose the plan (planned boundary recall 0.375, a drumless dub
breakdown coming back at 0.66 against a 0.28 target). The MIDI does not have
that problem -- it scores 98.92 against the same SongSpec and the breakdown
really is drumless. So the structure is carried by MIDI into Live, and the audio
model goes back to being a source of material rather than the arranger.

Pure and stdlib-only.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .midi import MidiNote
from .models import TRACK_NAMES, SongSpec

ARRANGEMENT_PLAN_VERSION = "0.1"
# AbletonGPT's create_midi_clip validator caps both of these.
MAX_CLIP_BEATS = 4096
MAX_NOTES_PER_CLIP = 4096

TRACK_LABELS = {"bass": "KIHACHI Bass", "drums": "KIHACHI Drums", "chords": "KIHACHI Chords"}


def beats_per_bar(spec: SongSpec) -> float:
    numerator, denominator = (int(part) for part in spec.song.time_signature.split("/", 1))
    return numerator * (4.0 / denominator)


def build_arrangement_plan(
    spec: SongSpec,
    tracks: Mapping[str, Sequence[MidiNote]],
    *,
    first_track_index: int = 0,
    session_slot: int = 0,
    automation: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Operations that lay this song out in Live, plus the structure they encode.

    One full-length clip per part, copied to the Arrangement at beat 0. Per
    *section* clips would be more editable, but ``create_midi_clip`` needs an
    empty Session slot and a default set has eight per track, while this
    arrangement has nine sections -- so per-section clips do not fit through the
    current tool surface. The section structure is not lost: it lives in the
    notes, and a part that rests in a section simply has none there.

    ``automation`` binds a per-section SongSpec field to a Live device parameter,
    e.g. ``{"part": "chords", "field": "fx_amount", "device_index": 1,
    "parameter_index": 52, "low": 0.18, "high": 0.52}``. The binding has to come
    from outside because the field is musical intent while the device layout is a
    fact about the Live set, and this module never talks to Live.

    Ordering matters and is not cosmetic: Live only exposes clip envelopes on
    *Session* clips (``automation_envelope`` returns None for Arrangement clips),
    but an envelope written on a Session clip does travel with it into the
    Arrangement. So each part is created, then automated, then copied.
    """

    bar_beats = beats_per_bar(spec)
    song_beats = round(spec.song.total_bars * bar_beats, 6)
    if song_beats > MAX_CLIP_BEATS:
        raise ValueError(
            f"song is {song_beats:g} beats; Live clips are capped at {MAX_CLIP_BEATS}"
        )

    parts = [name for name in TRACK_NAMES if name in tracks]
    operations: list[dict[str, Any]] = [
        {
            "op": "set_tempo",
            "params": {"bpm": spec.song.bpm},
            "why": f"the whole plan is written against {spec.song.bpm:g} BPM",
        }
    ]
    for offset, name in enumerate(parts):
        operations.append(
            {
                "op": "create_track",
                "params": {
                    "name": TRACK_LABELS.get(name, name),
                    "track_type": "midi",
                    "index": -1,
                },
                "why": f"one Live track per composed part ({name})",
            }
        )

    warnings: list[str] = []
    for offset, name in enumerate(parts):
        track_index = first_track_index + offset
        notes = _clip_notes(tracks[name], song_beats)
        if len(notes) > MAX_NOTES_PER_CLIP:
            warnings.append(
                f"{name} has {len(notes)} notes; Live accepts "
                f"{MAX_NOTES_PER_CLIP} per request, so this clip needs splitting"
            )
        operations.append(
            {
                "op": "create_midi_clip",
                "params": {
                    "track_index": track_index,
                    "clip_index": session_slot,
                    "name": f"{TRACK_LABELS.get(name, name)} (full)",
                    "length_beats": song_beats,
                    "notes": notes,
                },
                "why": f"{len(notes)} notes covering all {spec.song.total_bars} bars",
            }
        )
        # Envelopes go on while the clip is still in the Session slot; copying it
        # to the Arrangement afterwards carries them along, and Live gives no way
        # to write them once the clip is in the Arrangement.
        for binding in automation:
            if binding.get("part") != name:
                continue
            steps = _envelope_steps(spec, binding, bar_beats)
            operations.append(
                {
                    "op": "set_clip_parameter_envelope",
                    "params": {
                        "track_index": track_index,
                        "clip_index": session_slot,
                        "device_index": int(binding["device_index"]),
                        "parameter_index": int(binding["parameter_index"]),
                        "steps": steps,
                    },
                    "why": (
                        f"section {binding['field']} drives this parameter "
                        f"({len(steps)} steps, one per section)"
                    ),
                }
            )
        operations.append(
            {
                "op": "copy_session_clip_to_arrangement",
                "params": {
                    "track_index": track_index,
                    "clip_index": session_slot,
                    "destination_time_beats": 0.0,
                    "name": TRACK_LABELS.get(name, name),
                },
                "why": "place the part on the Arrangement timeline at bar 1",
            }
        )

    return {
        "arrangement_plan_version": ARRANGEMENT_PLAN_VERSION,
        "execution_state": "planned_not_applied",
        "song": {
            "title": spec.song.title,
            "bpm": spec.song.bpm,
            "key": spec.song.key,
            "time_signature": spec.song.time_signature,
            "total_bars": spec.song.total_bars,
            "total_beats": song_beats,
        },
        "tracks": [
            {
                "part": name,
                "live_track_index": first_track_index + offset,
                "name": TRACK_LABELS.get(name, name),
                "notes": len(tracks[name]),
            }
            for offset, name in enumerate(parts)
        ],
        "structure": _structure(spec, bar_beats),
        "operations": operations,
        "clip_strategy": "one full-length clip per part",
        "clip_strategy_reason": (
            "create_midi_clip needs an empty Session slot and a default set has "
            "eight per track; this arrangement has nine sections"
        ),
        "warnings": warnings,
        "safety": {
            "creates_tracks": len(parts),
            "modifies_existing_tracks": False,
            "deletes_nothing": True,
            "sets_tempo": True,
        },
    }


def _envelope_steps(
    spec: SongSpec,
    binding: Mapping[str, Any],
    bar_beats: float,
) -> list[dict[str, Any]]:
    """One envelope step per section, mapping a 0..1 SongSpec field into a range.

    ``low``/``high`` exist because a musical 0..1 rarely means the parameter's
    own 0..1: a fully wet delay at ``fx_amount`` 1.0 would erase the dry part it
    is supposed to decorate. Sections whose field is unset are skipped rather
    than guessed, so an incomplete arrangement leaves those bars untouched.
    """

    field = str(binding["field"])
    low = float(binding.get("low", 0.0))
    high = float(binding.get("high", 1.0))
    if not 0.0 <= low <= 1.0 or not 0.0 <= high <= 1.0:
        raise ValueError("automation low/high must be between 0.0 and 1.0")
    if high <= low:
        raise ValueError("automation high must be greater than low")

    steps: list[dict[str, Any]] = []
    for section in spec.arrangement:
        value = getattr(section, field, None)
        if value is None:
            continue
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError(f"section {field} must be between 0.0 and 1.0")
        steps.append(
            {
                "start": round(section.start_bar * bar_beats, 6),
                "length": round(section.length_bars * bar_beats, 6),
                "value": round(low + float(value) * (high - low), 6),
            }
        )
    if not steps:
        raise ValueError(f"no section carries {field!r}; nothing to automate")
    return steps


def _structure(spec: SongSpec, bar_beats: float) -> list[dict[str, Any]]:
    """Section boundaries in beats, for locators and for checking the layout."""

    return [
        {
            "name": section.name,
            "start_bar": section.start_bar + 1,
            "length_bars": section.length_bars,
            "start_beats": round(section.start_bar * bar_beats, 6),
            "end_beats": round((section.start_bar + section.length_bars) * bar_beats, 6),
            "energy": section.energy,
            "active_tracks": [track for track in TRACK_NAMES if section.plays(track)],
            "resting_tracks": [track for track in TRACK_NAMES if not section.plays(track)],
        }
        for section in spec.arrangement
    ]


def _clip_notes(notes: Sequence[MidiNote], song_beats: float) -> list[dict[str, Any]]:
    """Notes in AbletonGPT's shape, clamped to stay inside the clip.

    Humanize can push a note a hair past the final bar line; Live rejects a note
    whose start is not strictly inside the clip, so the last sliver is pulled
    back rather than dropped.
    """

    limit = song_beats - 1e-4
    payload: list[dict[str, Any]] = []
    for note in sorted(notes, key=lambda item: (item.start_beats, item.pitch)):
        start = min(max(0.0, note.start_beats), limit)
        duration = min(note.duration_beats, song_beats - start)
        if duration <= 0:
            continue
        payload.append(
            {
                "pitch": note.pitch,
                "start_time": round(start, 6),
                "duration": round(duration, 6),
                "velocity": note.velocity,
            }
        )
    return payload
