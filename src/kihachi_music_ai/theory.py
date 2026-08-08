from __future__ import annotations

import re

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


def progression_for_key(tonic_pc: int, mode: str, *, prefer_flats: bool = False) -> tuple[str, ...]:
    names = FLAT_NAMES if prefer_flats else SHARP_NAMES
    scale = SCALES[mode]
    degrees = (0, 5, 2, 6) if mode == "minor" else (0, 5, 3, 4)
    qualities = ("m", "", "", "") if mode == "minor" else ("", "m", "", "")
    return tuple(names[(tonic_pc + scale[degree]) % 12] + quality for degree, quality in zip(degrees, qualities))


def chord_root(chord_name: str) -> str:
    match = re.match(r"^([A-G](?:#|b)?)", chord_name)
    if match is None:
        raise ValueError(f"unsupported chord: {chord_name}")
    return match.group(1)


def chord_pitches(chord_name: str, octave: int = 3) -> tuple[int, int, int]:
    root_name = chord_root(chord_name)
    root = midi_pitch(root_name, octave)
    is_minor = chord_name[len(root_name) :].startswith("m")
    intervals = (0, 3, 7) if is_minor else (0, 4, 7)
    return tuple(root + interval for interval in intervals)


def midi_pitch(note_name: str, octave: int) -> int:
    return 12 * (octave + 1) + NOTE_TO_PC[note_name]


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

