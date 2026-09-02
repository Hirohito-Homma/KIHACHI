"""Section × part density diagnostics: SongSpec intent vs written MIDI onsets.

Compares the arrangement's per-section, per-part density targets against note
onset activity in managed MIDI artifacts. Diagnostic only — no scoring, grading,
or composition changes.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .midi import PPQ, MidiNote
from .models import SongSpec

DENSITY_DIAGNOSTIC_VERSION = "0.1"
BOUNDARY_CONVENTION = "[start, end)"
"""Section intervals in beats; an onset exactly on ``end`` belongs to the next section."""


def beats_per_bar(spec: SongSpec) -> float:
    numerator, denominator = (int(part) for part in spec.song.time_signature.split("/", 1))
    return numerator * (4.0 / denominator)


def section_interval(spec: SongSpec, section) -> tuple[float, float]:
    bar_beats = beats_per_bar(spec)
    start = section.start_bar * bar_beats
    end = (section.start_bar + section.length_bars) * bar_beats
    return start, end


def onset_tick(note: MidiNote) -> int:
    """Musical instant for an onset, on the repository's PPQ grid."""

    return int(round(note.start_beats * PPQ))


def count_onsets(notes: Sequence[MidiNote]) -> int:
    """Count note starts, collapsing simultaneous pitches to one onset."""

    if not notes:
        return 0
    return len({onset_tick(note) for note in notes})


def onsets_in_interval(
    notes: Sequence[MidiNote],
    start_beats: float,
    end_beats: float,
) -> int:
    """Onsets whose start falls in ``[start_beats, end_beats)``.

    Rests contribute nothing. A sustained note counted here belongs only to the
    section where it started; its continuation into a later section does not add
    another onset there.
    """

    return len(
        {
            onset_tick(note)
            for note in notes
            if start_beats <= note.start_beats < end_beats
        }
    )


def observed_onsets_per_beat(onset_count: int, section_beats: float) -> float:
    if section_beats <= 0:
        return 0.0
    return onset_count / section_beats


def normalize_observed_rates(rates: Sequence[float]) -> list[float]:
    """Map onsets-per-beat values to 0–1 against the peak rate in the set."""

    if not rates:
        return []
    peak = max(rates)
    if peak <= 0.0:
        return [0.0 for _ in rates]
    return [min(1.0, rate / peak) for rate in rates]


def expected_density(section, part: str) -> float:
    """Design intent for ``part`` in ``section``.

    Resting parts are expected silent; active parts use ``SectionSpec.density``,
    which reads explicit per-part fields when set and otherwise follows energy.
    """

    if not section.plays(part):
        return 0.0
    return section.density(part)


def density_diagnostics(
    spec: SongSpec,
    tracks: Mapping[str, Sequence[MidiNote]],
) -> dict[str, Any]:
    """Compare expected and observed density for every section × declared part."""

    parts = spec.parts()
    rows: list[dict[str, Any]] = []
    rate_index: dict[str, list[tuple[int, float]]] = {part: [] for part in parts}

    for section in spec.arrangement:
        start_beats, end_beats = section_interval(spec, section)
        section_beats = end_beats - start_beats
        for part in parts:
            notes = tracks.get(part, ())
            onset_count = onsets_in_interval(notes, start_beats, end_beats)
            rate = observed_onsets_per_beat(onset_count, section_beats)
            row_index = len(rows)
            rows.append(
                {
                    "section": section.name,
                    "part": part,
                    "start_bar": section.start_bar + 1,
                    "length_bars": section.length_bars,
                    "section_beats": round(section_beats, 6),
                    "plays": section.plays(part),
                    "expected_density": round(expected_density(section, part), 4),
                    "onset_count": onset_count,
                    "observed_onsets_per_beat": round(rate, 6),
                    "observed_density": None,
                    "deviation": None,
                }
            )
            if section.plays(part):
                rate_index[part].append((row_index, rate))

    for part, indexed_rates in rate_index.items():
        normalized = normalize_observed_rates([rate for _, rate in indexed_rates])
        for (row_index, _), observed in zip(indexed_rates, normalized, strict=True):
            rows[row_index]["observed_density"] = round(observed, 4)
            rows[row_index]["deviation"] = round(
                observed - rows[row_index]["expected_density"],
                4,
            )

    for row in rows:
        if not row["plays"]:
            row["observed_density"] = 0.0
            row["deviation"] = round(0.0 - row["expected_density"], 4)

    return {
        "density_version": DENSITY_DIAGNOSTIC_VERSION,
        "scope": "section_part_onset_density_diagnostic",
        "expected_source": "SectionSpec.density with SectionSpec.plays rest semantics",
        "observed_source": "managed_midi_onsets",
        "boundary_convention": BOUNDARY_CONVENTION,
        "onset_rules": {
            "count_note_starts": True,
            "collapse_simultaneous_pitches": True,
            "exclude_rests": True,
            "sustained_notes_count_once_at_start": True,
        },
        "normalization": {
            "expected_unit": "0-1 design density from SongSpec",
            "observed_rate_unit": "onsets_per_beat",
            "observed_density_unit": "0-1 normalized against peak onsets_per_beat per part",
        },
        "entries": rows,
    }
