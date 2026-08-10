"""Keys, chords and progressions.

Until the progression shapes below went in, this module answered "what does
this song play" with one shape and one chord type: four triads, degrees
i-VI-III-VII in minor and I-vi-IV-V in major, whatever the brief said. A jazz
brief got the family's comping articulation and its swung ride, and then comped
Am-Dm-C-G with plain triads -- the arrangement had a genre and the harmony did
not.

Chord symbols are the interchange format between here, the composer, the
analyzer and the audio prompt, so the quality vocabulary is deliberately small
and written out: every suffix a progression can produce is in
:data:`CHORD_QUALITIES`, and anything else is not a chord this project writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NOTE_TO_PC = {
    "C": 0,
    "C#": 1,
    "Db": 1,
    "D": 2,
    "D#": 3,
    "Eb": 3,
    "E": 4,
    "F": 5,
    "F#": 6,
    "Gb": 6,
    "G": 7,
    "G#": 8,
    "Ab": 8,
    "A": 9,
    "A#": 10,
    "Bb": 10,
    "B": 11,
}

SHARP_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
FLAT_NAMES = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
SCALES = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
}

_KEY_RE = re.compile(
    r"(?<![A-Za-z])([A-Ga-g])([#b♯♭]?)(?:\s*(m|min|minor|maj|major))?(?![A-Za-z])",
    re.IGNORECASE,
)


def parse_key(text: str, default: str = "C minor") -> tuple[str, str, int, str]:
    match = _KEY_RE.search(text)
    if match is None:
        match = _KEY_RE.search(default)
    if match is None:  # pragma: no cover - guarded by the known default
        raise ValueError("could not parse musical key")
    accidental = match.group(2).replace("♯", "#").replace("♭", "b")
    tonic = match.group(1).upper() + accidental
    quality = (match.group(3) or "major").lower()
    mode = "minor" if quality in {"m", "min", "minor"} else "major"
    return f"{tonic} {mode}", tonic, NOTE_TO_PC[tonic], mode


CHORD_QUALITIES: dict[str, tuple[int, ...]] = {
    "": (0, 4, 7),
    "m": (0, 3, 7),
    "7": (0, 4, 7, 10),
    "m7": (0, 3, 7, 10),
    "maj7": (0, 4, 7, 11),
    "m7b5": (0, 3, 6, 10),
    "dim": (0, 3, 6),
    "sus4": (0, 5, 7),
    # A power chord really is two notes. Writing the third anyway is what made
    # every metal brief come out as a rock brief.
    "5": (0, 7),
    "6": (0, 4, 9, 7),
    "m6": (0, 3, 9, 7),
}
"""Every chord suffix this project writes, and the semitones it means.

Ordered so that the root comes first: the composer scales velocity down per
voice from the root outwards, and the bass and sub read the root alone.
"""

#: Longest first, so ``maj7`` is not read as ``m`` + ``aj7`` -- which is exactly
#: what the old ``startswith("m")`` test did, and it would have called every
#: major seventh a minor chord.
_QUALITY_ORDER = tuple(sorted(CHORD_QUALITIES, key=len, reverse=True))

_ROOT_RE = re.compile(r"^([A-G](?:#|b)?)")


def split_chord(chord_name: str) -> tuple[str, str]:
    """``"Cmaj7"`` -> ``("C", "maj7")``. Raises on anything else."""

    match = _ROOT_RE.match(chord_name)
    if match is None:
        raise ValueError(f"unsupported chord: {chord_name}")
    root = match.group(1)
    suffix = chord_name[len(root) :]
    if suffix not in CHORD_QUALITIES:
        raise ValueError(f"unsupported chord quality: {chord_name!r}")
    return root, suffix


def chord_root(chord_name: str) -> str:
    match = _ROOT_RE.match(chord_name)
    if match is None:
        raise ValueError(f"unsupported chord: {chord_name}")
    return match.group(1)


def chord_is_minor(chord_name: str) -> bool:
    """Whether the chord's third is minor.

    A plain ``startswith("m")`` answered this everywhere before seventh chords
    existed, and it is wrong the moment ``maj7`` appears.
    """

    root = chord_root(chord_name)
    suffix = chord_name[len(root) :]
    for quality in _QUALITY_ORDER:
        if suffix == quality:
            return 3 in CHORD_QUALITIES[quality]
    # An unknown suffix is not worth raising for here: this is asked by the
    # analyzer about chords *heard in audio*, which are named by an estimator
    # rather than by this module.
    return suffix.startswith("m") and not suffix.startswith("maj")


def chord_pitches(chord_name: str, octave: int = 3) -> tuple[int, ...]:
    """The chord's notes, root first. Two to four of them, by quality."""

    root_name, suffix = split_chord(chord_name)
    root = midi_pitch(root_name, octave)
    return tuple(root + interval for interval in CHORD_QUALITIES[suffix])


@dataclass(frozen=True)
class ProgressionShape:
    """One progression, as scale degrees and chord qualities.

    Degrees index the mode's own scale, so a shape is stated once and works in
    every key. The two modes are stated separately because they are different
    progressions rather than one progression transposed: a ii-V-I in minor
    wants a half-diminished ii and a dominant V borrowed from harmonic minor,
    and deriving that from the major form would be inventing music theory in a
    list comprehension.
    """

    minor: tuple[tuple[int, str], ...]
    major: tuple[tuple[int, str], ...]

    def for_mode(self, mode: str) -> tuple[tuple[int, str], ...]:
        return self.minor if mode == "minor" else self.major


DEFAULT_PROGRESSION = "axis"

PROGRESSIONS: dict[str, ProgressionShape] = {
    # The incumbent, and what every one of the 1020 genres used to play.
    "axis": ProgressionShape(
        minor=((0, "m"), (5, ""), (2, ""), (6, "")),
        major=((0, ""), (5, "m"), (3, ""), (4, "")),
    ),
    # One chord, or nearly: the point of a techno or ambient harmony is that it
    # does not move. Four slots so ``harmonic_rhythm_bars`` still means what it
    # means everywhere else.
    "modal_vamp": ProgressionShape(
        minor=((0, "m"), (0, "m"), (6, ""), (6, "")),
        major=((0, ""), (0, ""), (3, ""), (3, "")),
    ),
    "minor_seven_vamp": ProgressionShape(
        minor=((0, "m7"), (0, "m7"), (3, "m7"), (0, "m7")),
        major=((0, "maj7"), (0, "maj7"), (3, "maj7"), (0, "maj7")),
    ),
    "ii_v_i": ProgressionShape(
        minor=((1, "m7b5"), (4, "7"), (0, "m7"), (0, "m7")),
        major=((1, "m7"), (4, "7"), (0, "maj7"), (0, "maj7")),
    ),
    "blues_shuffle": ProgressionShape(
        minor=((0, "m7"), (3, "m7"), (0, "m7"), (4, "7")),
        major=((0, "7"), (3, "7"), (0, "7"), (4, "7")),
    ),
    "bossa": ProgressionShape(
        minor=((0, "m7"), (1, "m7b5"), (4, "7"), (0, "m7")),
        major=((0, "maj7"), (1, "m7"), (4, "7"), (0, "maj7")),
    ),
    "montuno_latin": ProgressionShape(
        minor=((0, "m7"), (3, "m7"), (4, "7"), (0, "m7")),
        major=((0, ""), (3, ""), (4, "7"), (0, "")),
    ),
    "one_four_five": ProgressionShape(
        minor=((0, "m"), (3, "m"), (4, ""), (0, "m")),
        major=((0, ""), (3, ""), (4, ""), (0, "")),
    ),
    # Power chords have no third, so the mode lives in the roots alone.
    "power_riff": ProgressionShape(
        minor=((0, "5"), (5, "5"), (2, "5"), (4, "5")),
        major=((0, "5"), (3, "5"), (4, "5"), (0, "5")),
    ),
    "hip_hop_loop": ProgressionShape(
        minor=((0, "m7"), (5, "maj7"), (0, "m7"), (4, "m7")),
        major=((0, "maj7"), (5, "m7"), (3, "maj7"), (4, "7")),
    ),
}


def progression_for_key(
    tonic_pc: int,
    mode: str,
    *,
    prefer_flats: bool = False,
    shape: str = DEFAULT_PROGRESSION,
) -> tuple[str, ...]:
    """Chord symbols for ``shape`` in this key.

    An unknown shape name falls back to the default rather than raising, for
    the reason the groove tables do: the name reaches here from a profile
    table, and a song that will not compose is worse than an ordinary
    progression.
    """

    names = FLAT_NAMES if prefer_flats else SHARP_NAMES
    scale = SCALES[mode]
    steps = PROGRESSIONS.get(shape, PROGRESSIONS[DEFAULT_PROGRESSION]).for_mode(mode)
    return tuple(
        names[(tonic_pc + scale[degree]) % 12] + quality for degree, quality in steps
    )


def midi_pitch(note_name: str, octave: int) -> int:
    return 12 * (octave + 1) + NOTE_TO_PC[note_name]


def beats_per_bar(time_signature: str) -> float:
    """How many quarter-note beats one bar of this signature holds.

    Everything in the composer is measured in quarter-note beats, because that
    is the unit a MIDI file's PPQ counts. ``6/8`` is three beats, not six: six
    eighth notes.

    The composer assumed 4.0 everywhere -- literally, as ``bar * 4.0`` -- while
    the analyzer, the Ableton bridge, the tail guard and the report all parsed
    ``song.time_signature`` properly. A spec in 3/4 therefore produced a MIDI
    file in 4/4 that every downstream reader then measured as 3/4.
    """

    numerator, denominator = (int(part) for part in time_signature.split("/", 1))
    if numerator <= 0 or denominator <= 0:
        raise ValueError(f"invalid time signature: {time_signature!r}")
    return numerator * 4.0 / denominator


def key_signature_value(key: str) -> tuple[int, int]:
    signatures = {
        "C major": 0,
        "G major": 1,
        "D major": 2,
        "A major": 3,
        "E major": 4,
        "B major": 5,
        "F# major": 6,
        "C# major": 7,
        "F major": -1,
        "Bb major": -2,
        "Eb major": -3,
        "Ab major": -4,
        "Db major": -5,
        "Gb major": -6,
        "Cb major": -7,
        "A minor": 0,
        "E minor": 1,
        "B minor": 2,
        "F# minor": 3,
        "C# minor": 4,
        "G# minor": 5,
        "D# minor": 6,
        "A# minor": 7,
        "D minor": -1,
        "G minor": -2,
        "C minor": -3,
        "F minor": -4,
        "Bb minor": -5,
        "Eb minor": -6,
        "Ab minor": -7,
    }
    return signatures.get(key, 0), 1 if key.endswith("minor") else 0

