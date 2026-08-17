from __future__ import annotations

import random
from bisect import bisect_right
from dataclasses import replace
from functools import wraps
from typing import Callable

from .groove_tables import KICK, bass_role, chord_articulation, drum_pattern, hat_positions
from .midi import PPQ, MidiNote
from .models import SectionSpec, SongSpec
from .mutation import Step, build_pattern, mutation_series
from .theory import beats_per_bar, chord_pitches, chord_root, midi_pitch

# Groove-ordered slots: the earlier a position appears, the more load-bearing it
# is, so raising a part's density adds inessential notes rather than reshuffling
# the pattern. Positions are quarter-note beats inside one 4/4 bar.
BASS_POSITIONS = (0.0, 1.5, 2.75, 0.75, 3.5, 2.0, 3.25, 1.0, 0.25, 2.25)
# Stabs answer the chords rather than doubling them, so they sit in the gaps the
# chord slots leave: 0.5 and 2.5 are the strongest offbeats not already taken.
SYNTH_POSITIONS = (2.5, 0.5, 3.25, 1.25, 3.75)

BASS_STEPS = (2, 8)
SYNTH_STEPS = (1, 4)

# Registers, in octaves. Each part gets its own so a six-part arrangement does
# not pile every line into the same two octaves and turn to mud.
SUB_OCTAVE = 1
BASS_OCTAVE = 2
CHORD_OCTAVE = 3
SYNTH_OCTAVE = 4
VOCODER_OCTAVE = 4
ARP_OCTAVE = 5

ARP_GRID = 0.25
"""Sixteenth notes: an arpeggio is a continuous line, not a sparse pattern."""


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


def _separate_repeats(notes: list[MidiNote]) -> list[MidiNote]:
    """Stop each note before the *same pitch* sounds again.

    ``_monophonic`` says one note at a time and is right for a bass line. A
    chord part is polyphonic and must not be flattened that way -- but two
    notes of the same pitch overlapping is not polyphony, it is the one thing a
    MIDI file cannot express: the format has no way to say which note-off
    closes which note-on, so any reader (Live included) pairs them
    first-in-first-out and hands back lengths nobody wrote.

    Only sustains can do this, so nothing that was already short is touched.
    """

    trimmed: list[MidiNote] = []
    by_pitch: dict[int, list[MidiNote]] = {}
    for note in notes:
        by_pitch.setdefault(note.pitch, []).append(note)
    for group in by_pitch.values():
        group.sort(key=lambda note: note.start_beats)
        for index, note in enumerate(group):
            duration = note.duration_beats
            if index + 1 < len(group):
                room = group[index + 1].start_beats - note.start_beats - MONOPHONIC_GAP_BEATS
                duration = min(duration, room)
            if duration < MONOPHONIC_GAP_BEATS:
                continue
            trimmed.append(replace(note, duration_beats=duration))
    trimmed.sort(key=lambda note: (note.start_beats, note.pitch))
    return trimmed


def _in_bar(positions: tuple[float, ...], bar_beats: float) -> tuple[float, ...]:
    """Drop slots that fall outside a bar of this length.

    Every position table here is written in 4/4, because that is what this
    project writes. A bar of 3/4 is three beats long, so a slot at 3.5 is not a
    late offbeat -- it is the next bar, and writing it there would overlap the
    downbeat that follows.
    """

    kept = tuple(position for position in positions if position < bar_beats)
    # Never return nothing: a pattern with no slots writes a silent part, which
    # is a worse answer than a downbeat.
    return kept or (0.0,)


def _backbeats(groove, bar_beats: float) -> tuple[float, ...]:
    """The groove's snare slots that still fit the bar. May be empty."""

    return tuple(position for position in groove.backbeat_positions if position < bar_beats)


def compose_bass(spec: SongSpec) -> tuple[MidiNote, ...]:
    """The bass line, playing the part ``spec.bass.role`` gives it.

    The role used to reach only the audio prompt, so a bass marked
    ``supporting`` was written exactly as loud and as busy as one marked
    ``dominant``.
    """

    notes: list[MidiNote] = []
    progression = spec.harmony.progression
    role = bass_role(spec.bass.role)
    bar_beats = beats_per_bar(spec.song.time_signature)
    for index, (section, start_bar, end_bar) in enumerate(_sections_in_bars(spec)):
        if not section.plays("bass"):
            continue
        rng = _section_rng(spec, "bass", index)
        density = _clamp(
            section.density("bass")
            * (0.55 + 0.55 * spec.bass.syncopation)
            * role.density_scale
        )
        base = build_pattern(
            _in_bar(BASS_POSITIONS, bar_beats),
            density=density,
            minimum=BASS_STEPS[0],
            maximum=BASS_STEPS[1],
            duration=0.3,
            velocity=_velocity_for(role.velocity, section),
            anchors=(0.0,),
        )
        generations = mutation_series(
            base,
            bars=end_bar - start_bar,
            amount=_mutation_amount(spec, section, spec.bass.mutation),
            rng=rng,
            syncopation=spec.bass.syncopation,
            ghost_probability=spec.bass.ghost_note_probability,
            octave_jump_probability=_clamp(
                spec.bass.octave_jump_probability * role.octave_jump_scale
            ),
            bar_beats=bar_beats,
            space=_clamp(0.35 * (1.0 - section.energy)),
            minimum_steps=BASS_STEPS[0],
        )
        for offset, pattern in enumerate(generations):
            bar = start_bar + offset
            chord = progression[(bar // spec.harmony.harmonic_rhythm_bars) % len(progression)]
            base_pitch = midi_pitch(chord_root(chord), 2)
            for step in pattern:
                start = _groove(bar * bar_beats + step.position, spec, rng)
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
    """The groove ``spec.drums.pattern`` names, mutated section by section.

    The pattern name used to reach only the audio prompt, so every genre played
    the same four-on-the-floor here whatever the SongSpec said. What the name
    means in notes now lives in :mod:`.groove_tables`; what happens to those notes --
    density, mutation, dub space, humanize -- is unchanged and stays here.
    """

    notes: list[MidiNote] = []
    groove = drum_pattern(spec.drums.pattern)
    bar_beats = beats_per_bar(spec.song.time_signature)
    for index, (section, start_bar, end_bar) in enumerate(_sections_in_bars(spec)):
        if not section.plays("drums"):
            continue
        rng = _section_rng(spec, "drums", index)
        # A plain product, not an offset blend: the old `0.45 + 0.75 * density`
        # floor squeezed every section into three of the five kick levels, so a
        # deliberate change to drum_density quantized away to no change at all.
        kick_density = _clamp(spec.drums.kick_density * section.density("drums"))
        base = build_pattern(
            _in_bar(groove.kick_positions, bar_beats),
            density=kick_density,
            minimum=groove.kick_steps[0],
            maximum=groove.kick_steps[1],
            duration=0.16,
            velocity=_velocity_for(108, section),
            anchors=groove.kick_anchors,
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
            bar_beats=bar_beats,
            space=_clamp(spec.drums.dub_space * (1.0 - section.energy)),
            minimum_steps=groove.kick_steps[0],
        )
        hats = hat_positions(
            groove,
            _clamp(spec.drums.hat_density * section.density("drums")),
            bar_beats,
        )
        for offset, pattern in enumerate(generations):
            bar_start = (start_bar + offset) * bar_beats
            for step in pattern:
                notes.append(
                    MidiNote(KICK, _groove(bar_start + step.position, spec, rng), 0.16, step.velocity, 9)
                )
            for position in _backbeats(groove, bar_beats):
                notes.append(
                    MidiNote(
                        groove.backbeat_pitch,
                        _groove(bar_start + position, spec, rng),
                        0.14,
                        _velocity_for(98, section),
                        9,
                    )
                )
            for hat_index, position in enumerate(hats):
                velocity = _velocity_for(72 if hat_index % 2 == 0 else 61, section)
                notes.append(
                    MidiNote(
                        groove.hat_pitch,
                        _groove(bar_start + position, spec, rng),
                        0.09,
                        velocity,
                        9,
                    )
                )
            if groove.accent_position is not None and section.energy >= 0.5:
                notes.append(
                    MidiNote(
                        groove.accent_pitch,
                        _groove(bar_start + groove.accent_position, spec, rng),
                        0.18,
                        _velocity_for(78, section),
                        9,
                    )
                )
    return tuple(notes)


def compose_chords(spec: SongSpec) -> tuple[MidiNote, ...]:
    """The chords, played the way ``spec.chords.articulation`` names.

    Like the drum pattern, the articulation used to reach only the audio
    prompt: a sustained pad and a clipped stab were both written as a 0.2-beat
    hit on the same offbeat slots. :mod:`.groove_tables` says what each name means.
    """

    notes: list[MidiNote] = []
    progression = spec.harmony.progression
    played = chord_articulation(spec.chords.articulation)
    bar_beats = beats_per_bar(spec.song.time_signature)
    for index, (section, start_bar, end_bar) in enumerate(_sections_in_bars(spec)):
        if not section.plays("chords"):
            continue
        rng = _section_rng(spec, "chords", index)
        base = build_pattern(
            _in_bar(played.positions, bar_beats),
            density=_clamp(section.density("chords")),
            minimum=played.steps[0],
            maximum=played.steps[1],
            duration=played.minimal_duration if section.minimal else played.duration,
            velocity=_velocity_for(round(82 * played.velocity_scale), section),
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
            bar_beats=bar_beats,
            space=_clamp(spec.chords.dub_delay * (1.0 - section.energy)),
            minimum_steps=played.steps[0],
        )
        for offset, pattern in enumerate(generations):
            bar = start_bar + offset
            chord = progression[(bar // spec.harmony.harmonic_rhythm_bars) % len(progression)]
            for step in pattern:
                start = _groove(bar * bar_beats + step.position, spec, rng)
                for voice, pitch in enumerate(chord_pitches(chord, octave=3)):
                    notes.append(
                        MidiNote(
                            max(0, min(127, pitch + step.octave)),
                            start,
                            step.duration,
                            max(1, step.velocity - voice * played.voice_falloff),
                        )
                    )
    return tuple(_separate_repeats(notes))


def compose_tracks(spec: SongSpec) -> dict[str, tuple[MidiNote, ...]]:
    """Every part the SongSpec names, in a stable order.

    A spec that names no instruments composes the core three, so nothing written
    before the extra parts existed changes.
    """

    return {name: COMPOSERS[name](spec) for name in spec.parts()}


def compose_synth(spec: SongSpec) -> tuple[MidiNote, ...]:
    """Chord stabs an octave above the chords, in the gaps the chords leave."""

    notes: list[MidiNote] = []
    progression = spec.harmony.progression
    bar_beats = beats_per_bar(spec.song.time_signature)
    for index, (section, start_bar, end_bar) in enumerate(_sections_in_bars(spec)):
        if not section.plays("synth"):
            continue
        rng = _section_rng(spec, "synth", index)
        base = build_pattern(
            _in_bar(SYNTH_POSITIONS, bar_beats),
            density=_clamp(section.density("synth")),
            minimum=SYNTH_STEPS[0],
            maximum=SYNTH_STEPS[1],
            duration=0.18,
            velocity=_velocity_for(88, section),
            anchors=(),
        )
        generations = mutation_series(
            base,
            bars=end_bar - start_bar,
            amount=_mutation_amount(spec, section, spec.groove.syncopation * 0.7),
            rng=rng,
            syncopation=spec.groove.syncopation,
            ghost_probability=0.0,
            octave_jump_probability=0.0,
            bar_beats=bar_beats,
            space=_clamp(0.4 * (1.0 - section.energy)),
            minimum_steps=SYNTH_STEPS[0],
        )
        for offset, pattern in enumerate(generations):
            bar = start_bar + offset
            chord = progression[(bar // spec.harmony.harmonic_rhythm_bars) % len(progression)]
            for step in pattern:
                start = _groove(bar * bar_beats + step.position, spec, rng)
                # First inversion. The vocoder carrier sits in the same octave in
                # root position, and two parts playing the identical notes is
                # not two parts -- it is one, louder.
                #
                # Sevenths and power chords mean this is no longer always three
                # notes: rotating the tuple states the inversion for any size,
                # where unpacking root/third/fifth raised ValueError the moment
                # a genre asked for a maj7.
                tones = chord_pitches(chord, octave=SYNTH_OCTAVE)
                inverted = tones[1:] + (tones[0] + 12,)
                for voice, pitch in enumerate(inverted):
                    notes.append(
                        MidiNote(
                            max(0, min(127, pitch + step.octave)),
                            start,
                            step.duration,
                            max(1, step.velocity - voice * 5),
                        )
                    )
    return tuple(notes)


def compose_arp(spec: SongSpec) -> tuple[MidiNote, ...]:
    """A sixteenth-note line walking the chord tones.

    Not built from ``build_pattern``: that picks a handful of slots out of a bar,
    which is the right shape for a stab or a kick and the wrong one for an
    arpeggio. Density here decides how much of the continuous line sounds, and
    the rests fall on the weakest sixteenths first so the line keeps its shape as
    it thins.
    """

    notes: list[MidiNote] = []
    progression = spec.harmony.progression
    bar_beats = beats_per_bar(spec.song.time_signature)
    steps_per_bar = int(round(bar_beats / ARP_GRID))
    # Weakest sixteenths first, so thinning removes filler before structure.
    rest_order = tuple(
        sorted(range(steps_per_bar), key=lambda step: (step % 4 == 0, step % 2 == 0, step))
    )
    for index, (section, start_bar, end_bar) in enumerate(_sections_in_bars(spec)):
        if not section.plays("arp"):
            continue
        rng = _section_rng(spec, "arp", index)
        density = _clamp(section.density("arp"))
        sounding = max(4, round(steps_per_bar * (0.35 + 0.65 * density)))
        resting = set(rest_order[: max(0, steps_per_bar - sounding)])
        for bar in range(start_bar, end_bar):
            chord = progression[(bar // spec.harmony.harmonic_rhythm_bars) % len(progression)]
            tones = chord_pitches(chord, octave=ARP_OCTAVE)
            # Up over one octave, then back down: a plain ascending loop restates
            # the root every three notes and reads as a stutter.
            contour = tones + (tones[0] + 12,) + tuple(reversed(tones[1:]))
            for step in range(steps_per_bar):
                if step in resting:
                    continue
                pitch = contour[step % len(contour)]
                start = _groove(bar * bar_beats + step * ARP_GRID, spec, rng)
                accent = 12 if step % 4 == 0 else 0
                notes.append(
                    MidiNote(
                        max(0, min(127, pitch)),
                        start,
                        ARP_GRID * 0.8,
                        _velocity_for(64 + accent, section),
                    )
                )
    return tuple(_monophonic(notes))


def compose_vocoder(spec: SongSpec) -> tuple[MidiNote, ...]:
    """Sustained carrier chords for a vocoder.

    A vocoder needs something held to shape, so this part does not mutate: it
    states the harmony and holds it for the whole bar.

    Where it rests is ``active_tracks``, like every other part. An earlier
    version skipped sections whose ``vocal_probability`` was low, which put a
    second, invisible resting rule next to the arrangement's -- the MIDI review
    then reported those bars as missing coverage, because as far as the SongSpec
    was concerned the part was supposed to be playing.
    """

    notes: list[MidiNote] = []
    progression = spec.harmony.progression
    bar_beats = beats_per_bar(spec.song.time_signature)
    for index, (section, start_bar, end_bar) in enumerate(_sections_in_bars(spec)):
        if not section.plays("vocoder"):
            continue
        rng = _section_rng(spec, "vocoder", index)
        for bar in range(start_bar, end_bar):
            chord = progression[(bar // spec.harmony.harmonic_rhythm_bars) % len(progression)]
            start = _groove(bar * bar_beats, spec, rng)
            length = bar_beats - (start - bar * bar_beats) - MONOPHONIC_GAP_BEATS
            for voice, pitch in enumerate(chord_pitches(chord, octave=VOCODER_OCTAVE)):
                notes.append(
                    MidiNote(
                        max(0, min(127, pitch)),
                        start,
                        length,
                        max(1, _velocity_for(74, section) - voice * 4),
                    )
                )
    return tuple(notes)


def compose_sub(spec: SongSpec) -> tuple[MidiNote, ...]:
    """The root, an octave under the bass, held.

    A sub is not a second bass line. The slap bass is busy by design -- ghost
    notes, octave jumps, displaced sixteenths -- and doubling that an octave down
    puts two moving parts in the range where the ear reads pitch worst and the
    speaker has least headroom. So this part states the root of the chord and
    holds it, changing only when the harmony does.
    """

    notes: list[MidiNote] = []
    progression = spec.harmony.progression
    rhythm_bars = spec.harmony.harmonic_rhythm_bars
    bar_beats = beats_per_bar(spec.song.time_signature)
    for index, (section, start_bar, end_bar) in enumerate(_sections_in_bars(spec)):
        if not section.plays("sub"):
            continue
        rng = _section_rng(spec, "sub", index)
        bar = start_bar
        while bar < end_bar:
            chord = progression[(bar // rhythm_bars) % len(progression)]
            # Hold until the harmony moves or the section ends, whichever is first.
            span_end = min(end_bar, ((bar // rhythm_bars) + 1) * rhythm_bars)
            span_end = max(span_end, bar + 1)
            start = _groove(bar * bar_beats, spec, rng)
            length = (
                (span_end - bar) * bar_beats - (start - bar * bar_beats) - MONOPHONIC_GAP_BEATS
            )
            notes.append(
                MidiNote(
                    max(0, min(127, midi_pitch(chord_root(chord), SUB_OCTAVE))),
                    start,
                    length,
                    _velocity_for(96, section),
                )
            )
            bar = span_end
    return tuple(_monophonic(notes))


SHORTEST_NOTE_BEATS = 0.02
"""Floor for a shortened note. At 110 BPM this is 11 ms -- short enough to read
as a click and long enough that a synth still opens its envelope. Below it a
"note" is an event nothing can play."""


def _shaped(notes: tuple[MidiNote, ...], note_length: float) -> tuple[MidiNote, ...]:
    """Hold every note for `note_length` times as long as its part wrote it.

    Lengthening is capped at the next note **in the same part**, which is what
    legato means and what keeps a held chord from crossing into the next one.
    `compose_bass` and `compose_sub` already trim themselves monophonically, so
    the cap is doing the same job here for the polyphonic parts.
    """

    if note_length == 1.0:
        return notes
    starts = sorted({note.start_beats for note in notes})
    shaped: list[MidiNote] = []
    for note in notes:
        duration = note.duration_beats * note_length
        if note_length > 1.0:
            index = bisect_right(starts, note.start_beats)
            if index < len(starts):
                duration = min(duration, starts[index] - note.start_beats)
        shaped.append(replace(note, duration_beats=max(duration, SHORTEST_NOTE_BEATS)))
    return tuple(shaped)


def _holding(compose: Callable[[SongSpec], tuple[MidiNote, ...]]):
    @wraps(compose)
    def composed(spec: SongSpec) -> tuple[MidiNote, ...]:
        return _shaped(compose(spec), spec.groove.note_length)

    return composed


COMPOSERS = {
    "bass": compose_bass,
    "sub": compose_sub,
    "drums": compose_drums,
    "chords": compose_chords,
    "synth": compose_synth,
    "arp": compose_arp,
    "vocoder": compose_vocoder,
}
COMPOSERS = {name: _holding(compose) for name, compose in COMPOSERS.items()}
