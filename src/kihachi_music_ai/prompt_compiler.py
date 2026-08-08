"""SongSpec to ACE-Step prompt.

Every descriptive clause here is *derived* from a SongSpec number. That was not
true before: the prompt hard-coded "octave pops, ghost notes", "spacious dub
gaps" and "tape-delay tails" regardless of what the spec said, so raising
``chords.dub_delay`` or ``section.fx_amount`` changed nothing the audio model
ever saw. A Spec Diff could report a change that could not possibly land.

The banding helper turns a 0..1 value into the kind of language a music model
responds to, rather than dumping the number. Numbers that the Reviewer quotes
back (section energy) are kept numeric so its revision prompts stay consistent
with the base prompt.

Pure and stdlib-only.
"""

from __future__ import annotations

from typing import Sequence

from .models import TRACK_NAMES, SectionSpec, SongSpec

# Per-section detail is the longest part of the prompt and a long arrangement
# can crowd out everything else, so each section states at most this many traits.
MAX_SECTION_TRAITS = 3


def band(value: float, labels: Sequence[str]) -> str:
    """Pick a descriptive label for a 0..1 value, low to high."""

    if not labels:
        raise ValueError("band requires at least one label")
    index = int(max(0.0, min(1.0, value)) * len(labels))
    return labels[min(index, len(labels) - 1)]


def compile_audio_prompt(spec: SongSpec) -> str:
    genres = ", ".join(
        f"{item.name.replace('_', ' ')} {round(item.weight * 100)}%" for item in spec.style.genres
    )
    return (
        f"Title: {spec.song.title}\n"
        f"Create a {genres} hybrid at {spec.song.bpm:g} BPM in {spec.song.key}.\n"
        f"Mood: {_mood(spec)}.\n"
        f"Groove: {_groove(spec)}.\n"
        f"Bass: {_bass(spec)}.\n"
        f"Drums: {_drums(spec)}.\n"
        f"Harmony: {_harmony(spec)}.\n"
        f"Vocal: {_vocal(spec)}.\n"
        f"Arrangement: {_arrangement(spec)}.\n"
        f"Production: {_production(spec)}.\n"
    )


def _mood(spec: SongSpec) -> str:
    darkness = band(
        spec.style.darkness,
        ("bright and open", "warm", "shadowed", "dark", "pitch-black and menacing"),
    )
    psychedelic = band(
        spec.style.psychedelic,
        ("grounded and direct", "subtly coloured", "hallucinatory", "deeply psychedelic"),
    )
    return f"{darkness}, {psychedelic}"


def _groove(spec: SongSpec) -> str:
    syncopation = band(
        spec.groove.syncopation,
        ("straight and on-grid", "lightly syncopated", "syncopated 16th-note feel",
         "heavily syncopated, off-beat led"),
    )
    humanize = band(
        spec.groove.humanize,
        ("machine-tight", "controlled human feel", "loose and hand-played"),
    )
    swing = "straight" if spec.groove.swing <= 0.52 else f"swung {spec.groove.swing:.2f}"
    return f"{syncopation}, {swing}, {humanize}"


def _bass(spec: SongSpec) -> str:
    role = band(
        _role_weight(spec.bass.role),
        ("supporting", "present", "dominant and up-front"),
    )
    parts = [
        f"{role} {spec.bass.technique} electric bass",
        band(
            spec.bass.syncopation,
            ("steady root notes", "syncopated phrasing", "restlessly syncopated phrasing"),
        ),
    ]
    if spec.bass.ghost_note_probability >= 0.2:
        parts.append(
            band(spec.bass.ghost_note_probability, ("occasional ghost notes", "busy ghost notes"))
        )
    if spec.bass.octave_jump_probability >= 0.2:
        parts.append(
            band(spec.bass.octave_jump_probability, ("occasional octave pops", "wild octave pops"))
        )
    parts.append(f"mutation intensity {spec.bass.mutation:.2f}")
    return ", ".join(parts)


def _drums(spec: SongSpec) -> str:
    parts = [
        spec.drums.pattern.replace("_", " "),
        band(
            spec.drums.kick_density,
            ("sparse kick", "steady kick", "driving four-on-the-floor kick"),
        ),
        band(spec.drums.hat_density, ("sparse hats", "crisp hats", "relentless 16th hats")),
    ]
    if spec.drums.dub_space >= 0.3:
        parts.append(
            band(spec.drums.dub_space, ("some space between hits", "spacious dub gaps",
                                        "huge dub drop-outs"))
        )
    return ", ".join(parts)


def _harmony(spec: SongSpec) -> str:
    progression = " - ".join(spec.harmony.progression)
    delay = band(
        spec.chords.dub_delay,
        ("dry", "short slapback", "tape-delay tails", "long dub tape-delay tails"),
    )
    instrument = spec.chords.instrument.replace("_", " ")
    articulation = spec.chords.articulation.replace("_", " ")
    bars = spec.harmony.harmonic_rhythm_bars
    rhythm = "one chord per bar" if bars == 1 else f"one chord every {bars} bars"
    return f"{progression}, {rhythm}; {instrument}, {articulation}, {delay}"


def _vocal(spec: SongSpec) -> str:
    if not spec.vocal.enabled:
        return "instrumental, no vocal"
    treatment = "vocoded" if spec.vocal.vocoder else "natural"
    return f"{treatment} {spec.vocal.character}"


def _arrangement(spec: SongSpec) -> str:
    sections = "; ".join(_section_phrase(spec, section) for section in spec.arrangement)
    return f"{sections}. Overall: {_energy_arc(spec)}, never losing the dance groove"


def _energy_arc(spec: SongSpec) -> str:
    """Describe the shape by its peak, not by where it happens to end.

    Comparing the last section with the first calls a song that climbs to 0.95
    and then lands on a quiet outro "one level throughout", which is the
    opposite of what it does.
    """

    first = spec.arrangement[0]
    peak = max(spec.arrangement, key=lambda section: section.energy)
    last = spec.arrangement[-1]
    rise = band(
        peak.energy - first.energy,
        ("hold one level throughout", "lift steadily", "build hard from a quiet opening"),
    )
    arc = (
        f"{rise} to a peak of {peak.energy:.2f} at {peak.name.replace('_', ' ')}"
        if peak is not first
        else f"{rise} from {first.energy:.2f}"
    )
    if last is not peak and last.energy <= peak.energy - 0.25:
        arc += f", then land on {last.name.replace('_', ' ')} at {last.energy:.2f}"
    return arc


def _section_phrase(spec: SongSpec, section: SectionSpec) -> str:
    traits: list[str] = []
    silent = [track for track in TRACK_NAMES if not section.plays(track)]
    if silent:
        traits.append("no " + " or ".join(silent))
    if section.minimal:
        traits.append("minimal")
    if section.fx_amount is not None and section.fx_amount >= 0.6:
        # Only the loud half of the range is worth a trait; below that the
        # song-level production line already covers the effect character.
        traits.append(
            "drenched in dub fx" if section.fx_amount >= 0.85 else "wide dub echoes"
        )
    if spec.vocal.enabled and section.vocal_probability is not None:
        if section.vocal_probability <= 0.05:
            traits.append("no vocal")
        elif section.vocal_probability >= 0.5:
            traits.append("vocal-led")
    if section.psychedelic >= 0.8:
        traits.append("heavily psychedelic")
    detail = "".join(f", {trait}" for trait in traits[:MAX_SECTION_TRAITS])
    return (
        f"{section.name.replace('_', ' ')} ({section.length_bars} bars, "
        f"energy {section.energy:.2f}{detail})"
    )


def _production(spec: SongSpec) -> str:
    parts = ["deep sub control"]
    parts.append(
        band(
            spec.chords.dub_delay,
            ("tight dry space", "moderate echoes", "wide dub echoes", "cavernous dub echoes"),
        )
    )
    parts.append(
        band(spec.style.darkness, ("clean", "restrained distortion", "heavy saturation"))
    )
    parts.append("club-ready dynamics")
    parts.append("clear separation between kick and bass")
    return ", ".join(parts)


def _role_weight(role: str) -> float:
    return {"supporting": 0.1, "present": 0.5, "dominant": 0.9}.get(role.casefold(), 0.5)
