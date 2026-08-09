"""The exact half of the Critic: MIDI checked against the SongSpec.

The audio Analyzer has to *detect* harmony in a finished mix, and on this
project it hits a hard ceiling doing so -- key confidence around 0.11 and a
progression match stuck at 0.0 across every take, because kick, bass, chords,
vocals and dub delay all overlap in the same spectrum.

But when the MIDI was written from the SongSpec, the harmony does not need
detecting. It is known. This module compares the notes actually on disk against
the SongSpec exactly, so the Critic can tell two very different situations apart:

* the design itself is wrong -- the MIDI does not match the spec;
* the design is right and the audio engine (or the audio detector) did not
  realize it -- the MIDI matches while the audio analysis does not.

Read-only and stdlib-only. Nothing here mutates a project.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .midi import MidiNote, read_midi
from .models import SongSpec
from .theory import NOTE_TO_PC, SCALES, chord_pitches, chord_root

MIDI_REVIEW_VERSION = "0.1"
DRUM_CHANNEL = 9
# Humanize deliberately places a downbeat a few milliseconds either side of the
# bar line, so notes are credited to the bar they were written for.
BAR_TOLERANCE_BEATS = 0.05
MIDI_ALIGNMENT_WEIGHTS = {
    "harmony": 0.35,
    "key": 0.20,
    "section_energy": 0.30,
    "coverage": 0.15,
}
TRACK_FILES = ("bass", "drums", "chords")
"""The parts a SongSpec that names no instruments writes. Specs that do name
them are read through ``spec.parts()`` instead."""


@dataclass(frozen=True)
class MidiReviewManifest:
    project_dir: Path
    midi_files: tuple[Path, ...]
    review: dict[str, Any]


def review_project_midi(project_dir: Path) -> MidiReviewManifest:
    """Read a project's ``.mid`` files and compare them with its SongSpec."""

    project_dir = Path(project_dir)
    spec_path = project_dir / "song_spec.json"
    if not spec_path.is_file():
        raise FileNotFoundError(f"SongSpec not found: {spec_path}")
    spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))

    tracks: dict[str, tuple[MidiNote, ...]] = {}
    files: list[Path] = []
    for name in spec.parts():
        path = project_dir / f"{name}.mid"
        if not path.is_file():
            raise FileNotFoundError(f"MIDI track not found: {path}")
        tracks[name] = read_midi(path).notes
        files.append(path)
    return MidiReviewManifest(project_dir, tuple(files), review_midi_tracks(spec, tracks))


def review_midi_tracks(
    spec: SongSpec,
    tracks: Mapping[str, Sequence[MidiNote]],
) -> dict[str, Any]:
    harmony = _harmony_report(spec, tracks)
    key = _key_report(spec, tracks)
    sections = _section_report(spec, tracks)
    coverage = _coverage_report(spec, tracks)
    component_scores = {
        "harmony": harmony["score"],
        "key": key["score"],
        "section_energy": max(0.0, sections["energy_correlation"]),
        "coverage": coverage["score"],
    }
    weighted = sum(
        component_scores[name] * weight for name, weight in MIDI_ALIGNMENT_WEIGHTS.items()
    )
    total = round(weighted * 100.0, 2)
    return {
        "midi_review_version": MIDI_REVIEW_VERSION,
        "scope": "exact_midi_to_song_spec_comparison_not_audio",
        "tracks": {
            name: {"notes": len(notes)} for name, notes in sorted(tracks.items())
        },
        "harmony": harmony,
        "key": key,
        "sections": sections,
        "coverage": coverage,
        "alignment": {
            "score": total,
            "grade": (
                "aligned" if total >= 80.0 else "partial" if total >= 55.0 else "needs_revision"
            ),
            "score_meaning": (
                "exact comparison of the written MIDI against the SongSpec; "
                "independent of any audio render"
            ),
            "components": {
                name: {
                    "score": round(component_scores[name], 4),
                    "weight": weight,
                    "weighted_points": round(component_scores[name] * weight * 100.0, 2),
                }
                for name, weight in MIDI_ALIGNMENT_WEIGHTS.items()
            },
        },
    }


def _bar_of(note: MidiNote, beats_per_bar: float) -> int:
    return int((note.start_beats + BAR_TOLERANCE_BEATS) // beats_per_bar)


def _beats_per_bar(spec: SongSpec) -> float:
    numerator, denominator = (int(part) for part in spec.song.time_signature.split("/", 1))
    return numerator * (4.0 / denominator)


def _expected_chord(spec: SongSpec, bar: int) -> str:
    progression = spec.harmony.progression
    return progression[(bar // spec.harmony.harmonic_rhythm_bars) % len(progression)]


def _pitched(tracks: Mapping[str, Sequence[MidiNote]]) -> list[MidiNote]:
    return [
        note
        for notes in tracks.values()
        for note in notes
        if note.channel != DRUM_CHANNEL
    ]


def _harmony_report(
    spec: SongSpec,
    tracks: Mapping[str, Sequence[MidiNote]],
) -> dict[str, Any]:
    """Per bar: does the bass sit on the chord root, are chord tones in the chord?"""

    beats_per_bar = _beats_per_bar(spec)
    bass = tracks.get("bass", ())
    chords = tracks.get("chords", ())
    bars: list[dict[str, Any]] = []
    bass_hits = bass_total = tone_hits = tone_total = 0

    for bar in range(spec.song.total_bars):
        chord = _expected_chord(spec, bar)
        root_pc = NOTE_TO_PC[chord_root(chord)]
        chord_pcs = {pitch % 12 for pitch in chord_pitches(chord)}
        bar_bass = [note for note in bass if _bar_of(note, beats_per_bar) == bar]
        bar_chord = [note for note in chords if _bar_of(note, beats_per_bar) == bar]
        on_root = sum(note.pitch % 12 == root_pc for note in bar_bass)
        in_chord = sum(note.pitch % 12 in chord_pcs for note in bar_chord)
        bass_hits += on_root
        bass_total += len(bar_bass)
        tone_hits += in_chord
        tone_total += len(bar_chord)
        bars.append(
            {
                "bar": bar + 1,
                "expected_chord": chord,
                "bass_notes": len(bar_bass),
                "bass_on_root": on_root,
                "chord_notes": len(bar_chord),
                "chord_tones_in_chord": in_chord,
            }
        )

    bass_ratio = round(bass_hits / bass_total, 4) if bass_total else 0.0
    tone_ratio = round(tone_hits / tone_total, 4) if tone_total else 0.0
    return {
        "grid": "song_spec_bars",
        "progression": list(spec.harmony.progression),
        "harmonic_rhythm_bars": spec.harmony.harmonic_rhythm_bars,
        "bars": bars,
        "bass_root_match_ratio": bass_ratio,
        "chord_tone_match_ratio": tone_ratio,
        "score": round((bass_ratio + tone_ratio) / 2.0, 4),
    }


def _key_report(
    spec: SongSpec,
    tracks: Mapping[str, Sequence[MidiNote]],
) -> dict[str, Any]:
    scale = SCALES.get(spec.song.mode)
    if scale is None:
        raise ValueError(f"unsupported SongSpec mode: {spec.song.mode!r}")
    in_key = {(spec.song.tonic_pitch_class + interval) % 12 for interval in scale}
    pitched = _pitched(tracks)
    outside = [note for note in pitched if note.pitch % 12 not in in_key]
    ratio = round(len(outside) / len(pitched), 4) if pitched else 1.0
    return {
        "key": spec.song.key,
        "scale_pitch_classes": sorted(in_key),
        "observed_pitch_classes": sorted({note.pitch % 12 for note in pitched}),
        "pitched_notes": len(pitched),
        "out_of_key_notes": len(outside),
        "out_of_key_ratio": ratio,
        "score": round(1.0 - ratio, 4),
    }


def _section_report(
    spec: SongSpec,
    tracks: Mapping[str, Sequence[MidiNote]],
) -> dict[str, Any]:
    """Per-bar written intensity, normalized the way the audio side normalizes."""

    beats_per_bar = _beats_per_bar(spec)
    raw = [0.0] * spec.song.total_bars
    counts = [0] * spec.song.total_bars
    for notes in tracks.values():
        for note in notes:
            bar = _bar_of(note, beats_per_bar)
            if 0 <= bar < spec.song.total_bars:
                raw[bar] += note.velocity
                counts[bar] += 1

    normalized = _normalize(raw)
    bars: list[dict[str, Any]] = []
    targets: list[float] = []
    for index, value in enumerate(normalized):
        section = next(
            item
            for item in spec.arrangement
            if item.start_bar <= index < item.start_bar + item.length_bars
        )
        targets.append(section.energy)
        bars.append(
            {
                "bar": index + 1,
                "planned_section": section.name,
                "target_energy": section.energy,
                "notes": counts[index],
                "velocity_sum": round(raw[index], 2),
                "written_energy": round(value, 4),
            }
        )

    planned = []
    for section in spec.arrangement:
        window = normalized[section.start_bar : section.start_bar + section.length_bars]
        planned.append(
            {
                "name": section.name,
                "start_bar": section.start_bar + 1,
                "length_bars": section.length_bars,
                "target_energy": section.energy,
                "written_mean_energy": (
                    round(sum(window) / len(window), 4) if window else 0.0
                ),
            }
        )
    return {
        "grid": "song_spec_bars",
        "bars": bars,
        "planned_sections": planned,
        "energy_correlation": round(_correlation(normalized, targets), 4),
    }


def _coverage_report(
    spec: SongSpec,
    tracks: Mapping[str, Sequence[MidiNote]],
) -> dict[str, Any]:
    """Silent bars, counting only bars where the arrangement asked for sound.

    A dub breakdown that deliberately drops the drums is an arrangement
    decision, not missing coverage, so those bars are excluded from the total
    rather than scored against the composition.
    """

    beats_per_bar = _beats_per_bar(spec)
    empty: dict[str, list[int]] = {}
    rested: dict[str, list[int]] = {}
    expected = 0
    for name, notes in sorted(tracks.items()):
        filled = {_bar_of(note, beats_per_bar) for note in notes}
        missing: list[int] = []
        silent_by_design: list[int] = []
        for bar in range(spec.song.total_bars):
            section = _section_of(spec, bar)
            if section is not None and not section.plays(name):
                silent_by_design.append(bar + 1)
                continue
            expected += 1
            if bar not in filled:
                missing.append(bar + 1)
        if missing:
            empty[name] = missing
        if silent_by_design:
            rested[name] = silent_by_design
    missing_total = sum(len(bars) for bars in empty.values())
    return {
        "expected_bars": spec.song.total_bars,
        "tracks": len(tracks),
        "scored_track_bars": expected,
        "empty_bars": empty,
        "resting_bars": rested,
        "score": round(1.0 - missing_total / expected, 4) if expected else 0.0,
    }


def _section_of(spec: SongSpec, bar: int):
    for section in spec.arrangement:
        if section.start_bar <= bar < section.start_bar + section.length_bars:
            return section
    return None


def _normalize(values: Sequence[float]) -> list[float]:
    """Percentile-normalized curve, matching how the audio side scales energy."""

    if not values:
        return []
    low = _percentile(values, 0.1)
    high = _percentile(values, 0.9)
    span = max(1e-9, high - low)
    return [max(0.0, min(1.0, (value - low) / span)) for value in values]


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    count = min(len(left), len(right))
    if count < 2:
        return 0.0
    left_mean = sum(left[:count]) / count
    right_mean = sum(right[:count]) / count
    numerator = sum(
        (left[index] - left_mean) * (right[index] - right_mean) for index in range(count)
    )
    left_dev = math.sqrt(sum((left[index] - left_mean) ** 2 for index in range(count)))
    right_dev = math.sqrt(sum((right[index] - right_mean) ** 2 for index in range(count)))
    if left_dev == 0.0 or right_dev == 0.0:
        return 0.0
    return numerator / (left_dev * right_dev)
