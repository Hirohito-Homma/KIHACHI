from __future__ import annotations

import random
from dataclasses import replace

from .midi import PPQ, MidiNote
from .models import SectionSpec, SongSpec
from .mutation import Step, build_pattern, mutation_series
from .theory import chord_pitches, chord_root, midi_pitch

# Groove-ordered slots: the earlier a position appears, the more load-bearing it
# is, so raising a part's density adds inessential notes rather than reshuffling
# the pattern. Positions are quarter-note beats inside one 4/4 bar.
BASS_POSITIONS = (0.0, 1.5, 2.75, 0.75, 3.5, 2.0, 3.25, 1.0, 0.25, 2.25)
KICK_POSITIONS = (0.0, 2.0, 1.5, 3.25, 2.5, 0.75)
CHORD_POSITIONS = (1.5, 0.75, 2.75, 3.5, 2.25)
BACKBEAT_POSITIONS = (1.0, 3.0)

BASS_STEPS = (2, 8)
KICK_STEPS = (1, 5)
CHORD_STEPS = (1, 4)


def _section_at(spec: SongSpec, bar: int) -> SectionSpec:
    for section in spec.arrangement:
        if section.start_bar <= bar < section.start_bar + section.length_bars:
            return section
    raise ValueError(f"bar {bar} is outside the arrangement")


def _groove(start: float, spec: SongSpec, rng: random.Random) -> float:
    subdivision = int(round(start * 2))
    swing_delay = max(0.0, spec.groove.swing - 0.5) * 0.35 if subdivision % 2 else 0.0
    jitter = (rng.random() - 0.5) * spec.groove.humanize * 0.035
    return max(0.0, start + swing_delay + jitter)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _velocity_for(base: int, section: SectionSpec) -> int:
    """Scale a part's nominal velocity by how loud the section is meant to be."""

    return max(1, min(127, round(base * (0.72 + 0.42 * section.energy))))


def _mutation_amount(spec: SongSpec, section: SectionSpec, base: float) -> float:
    """A part's mutation intensity, pushed further in psychedelic sections.

    When the arrangement states a ``mutation`` for the section, that is the
    section's intensity and the part's own SongSpec value is how strongly the
    part responds to it -- so 0.0 really is pure repetition, and "make the drop
    more mutated" is one number on one section. Sections written before the
    arrangement engine carry no value and keep deriving it from ``psychedelic``.
    """

    if section.mutation is not None:
        return _clamp(base * section.mutation)
    return _clamp(base * (0.55 + 0.65 * section.psychedelic))


def _sections_in_bars(spec: SongSpec) -> list[tuple[SectionSpec, int, int]]:
    return [
        (section, section.start_bar, section.start_bar + section.length_bars)
        for section in spec.arrangement
    ]


def _section_rng(spec: SongSpec, track: str, index: int) -> random.Random:
    """A private random stream per (song, track, section).

    One shared stream would make every later section move whenever an earlier
    one changed length, because the stream position would shift. Editing one
    section has to leave the rest of the song bit-identical, so each section
    draws from its own deterministic stream. Seeding from a string is stable
    across runs and platforms (CPython hashes it with SHA-512).
    """

    return random.Random(f"{spec.seed}:{track}:{index}")


MONOPHONIC_GAP_BEATS = 2.0 / PPQ
"""Release gap held open between consecutive notes of a monophonic part.

Two ticks rather than one: note starts carry humanize jitter and are not on tick
boundaries, so a single tick can round away and let the notes touch again.
"""


def _monophonic(notes: list[MidiNote]) -> list[MidiNote]:
    """Stop each note before the next one starts.

    A bass line plays one note at a time, so a note that runs into the next is
    not a musical choice -- it comes from writing a 0.3-beat note onto a
    0.25-beat grid. It also cannot survive the trip through a MIDI file: when two
    overlapping notes share a pitch, the format cannot say which note-off closes
    which note-on, and any reader (Live included) re-pairs them first-in
    first-out and gets durations nobody wrote.
    """

    ordered = sorted(notes, key=lambda note: (note.start_beats, note.pitch))
    trimmed: list[MidiNote] = []
    for index, note in enumerate(ordered):
        duration = note.duration_beats
        if index + 1 < len(ordered):
            room = ordered[index + 1].start_beats - note.start_beats - MONOPHONIC_GAP_BEATS
            duration = min(duration, room)
        if duration < MONOPHONIC_GAP_BEATS:
            # Two notes this close are one note; keeping both would write a flam
            # nobody asked for.
            continue
        trimmed.append(replace(note, duration_beats=duration))
    return trimmed


def compose_bass(spec: SongSpec) -> tuple[MidiNote, ...]:
    notes: list[MidiNote] = []
    progression = spec.harmony.progression
    for index, (section, start_bar, end_bar) in enumerate(_sections_in_bars(spec)):
        if not section.plays("bass"):
            continue
        rng = _section_rng(spec, "bass", index)
        density = _clamp(section.density("bass") * (0.55 + 0.55 * spec.bass.syncopation))
        base = build_pattern(
            BASS_POSITIONS,
            density=density,
            minimum=BASS_STEPS[0],
            maximum=BASS_STEPS[1],
            duration=0.3,
            velocity=_velocity_for(102, section),
            anchors=(0.0,),
        )
        generations = mutation_series(
            base,
            bars=end_bar - start_bar,
            amount=_mutation_amount(spec, section, spec.bass.mutation),
            rng=rng,
            syncopation=spec.bass.syncopation,
            ghost_probability=spec.bass.ghost_note_probability,
            octave_jump_probability=spec.bass.octave_jump_probability,
            space=_clamp(0.35 * (1.0 - section.energy)),
            minimum_steps=BASS_STEPS[0],
        )
        for offset, pattern in enumerate(generations):
            bar = start_bar + offset
            chord = progression[(bar // spec.harmony.harmonic_rhythm_bars) % len(progression)]
            base_pitch = midi_pitch(chord_root(chord), 2)
            for step in pattern:
                start = _groove(bar * 4.0 + step.position, spec, rng)
                notes.append(
                    MidiNote(
                        max(0, min(127, base_pitch + step.octave)),
                        start,
                        step.duration,
                        step.velocity,
                    )
                )
    return tuple(_monophonic(notes))


def compose_drums(spec: SongSpec) -> tuple[MidiNote, ...]:
    notes: list[MidiNote] = []
    for index, (section, start_bar, end_bar) in enumerate(_sections_in_bars(spec)):
        if not section.plays("drums"):
            continue
        rng = _section_rng(spec, "drums", index)
        # A plain product, not an offset blend: the old `0.45 + 0.75 * density`
        # floor squeezed every section into three of the five kick levels, so a
        # deliberate change to drum_density quantized away to no change at all.
        kick_density = _clamp(spec.drums.kick_density * section.density("drums"))
        base = build_pattern(
            KICK_POSITIONS,
            density=kick_density,
            minimum=KICK_STEPS[0],
            maximum=KICK_STEPS[1],
            duration=0.16,
            velocity=_velocity_for(108, section),
            anchors=(0.0,),
        )
        generations = mutation_series(
            base,
            bars=end_bar - start_bar,
            amount=_mutation_amount(spec, section, spec.groove.syncopation * 0.6),
            rng=rng,
            syncopation=spec.groove.syncopation,
            ghost_probability=0.0,
            octave_jump_probability=0.0,
            # Dub leaves holes in the kick; that is the point of the genre.
            space=_clamp(spec.drums.dub_space * (1.0 - section.energy)),
            minimum_steps=KICK_STEPS[0],
        )
        hat_step = 0.5 if spec.drums.hat_density * section.density("drums") >= 0.3 else 1.0
        for offset, pattern in enumerate(generations):
            bar_start = (start_bar + offset) * 4.0
            for step in pattern:
                notes.append(
                    MidiNote(36, _groove(bar_start + step.position, spec, rng), 0.16, step.velocity, 9)
                )
            for position in BACKBEAT_POSITIONS:
                notes.append(
                    MidiNote(
                        39,
                        _groove(bar_start + position, spec, rng),
                        0.14,
                        _velocity_for(98, section),
                        9,
                    )
                )
            position = 0.5
            hat_index = 0
            while position < 4.0:
                velocity = _velocity_for(72 if hat_index % 2 == 0 else 61, section)
                notes.append(MidiNote(42, _groove(bar_start + position, spec, rng), 0.09, velocity, 9))
                position += hat_step
                hat_index += 1
            if section.energy >= 0.5:
                notes.append(
                    MidiNote(
                        46,
                        _groove(bar_start + 3.75, spec, rng),
                        0.18,
                        _velocity_for(78, section),
                        9,
                    )
                )
    return tuple(notes)


def compose_chords(spec: SongSpec) -> tuple[MidiNote, ...]:
    notes: list[MidiNote] = []
    progression = spec.harmony.progression
    for index, (section, start_bar, end_bar) in enumerate(_sections_in_bars(spec)):
        if not section.plays("chords"):
            continue
        rng = _section_rng(spec, "chords", index)
        base = build_pattern(
            CHORD_POSITIONS,
            density=_clamp(section.density("chords")),
            minimum=CHORD_STEPS[0],
            maximum=CHORD_STEPS[1],
            duration=0.28 if section.minimal else 0.2,
            velocity=_velocity_for(82, section),
            anchors=(),
        )
        generations = mutation_series(
            base,
            bars=end_bar - start_bar,
            amount=_mutation_amount(spec, section, spec.groove.syncopation * 0.5),
            rng=rng,
            syncopation=spec.groove.syncopation,
            ghost_probability=0.0,
            octave_jump_probability=0.0,
            # Long delay tails need room, so a dubby chord part stays sparse.
            space=_clamp(spec.chords.dub_delay * (1.0 - section.energy)),
            minimum_steps=CHORD_STEPS[0],
        )
        for offset, pattern in enumerate(generations):
            bar = start_bar + offset
            chord = progression[(bar // spec.harmony.harmonic_rhythm_bars) % len(progression)]
            for step in pattern:
                start = _groove(bar * 4.0 + step.position, spec, rng)
                for voice, pitch in enumerate(chord_pitches(chord, octave=3)):
                    notes.append(
                        MidiNote(
                            max(0, min(127, pitch + step.octave)),
                            start,
                            step.duration,
                            max(1, step.velocity - voice * 4),
                        )
                    )
    return tuple(notes)


def compose_tracks(spec: SongSpec) -> dict[str, tuple[MidiNote, ...]]:
    return {
        "bass": compose_bass(spec),
        "drums": compose_drums(spec),
        "chords": compose_chords(spec),
    }
