"""Lyrics as a musical part, not as writing.

The distinction this module is built around is the one from the design notes:
a *literary* lyric and a *usable* lyric are different things. A vocoder does not
want sentences. It wants two- and three-word imperatives that survive being
squeezed through a carrier and repeated every four bars:

    Mutate the funk
    Break the code
    Push pull
    Signal mutation

So the writer is driven by the vocal treatment first and the theme second. The
mode decides phrase length, repetition and how the hook returns; the SongSpec's
genres and mood decide the vocabulary; ``section.vocal_probability`` decides
where anything is sung at all.

Deterministic, pure and stdlib-only: the same seed always writes the same sheet.
The output uses ACE-Step's bracketed structure tags. That convention has not
been re-verified against a live server in this session -- see README.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

from .models import SectionSpec, SongSpec

LYRICS_VERSION = "0.1"

VOCODER = "vocoder"
CHANT = "chant"
SPOKEN = "spoken"
SUNG = "sung"
INSTRUMENTAL = "instrumental"

# How many lines a section gets, by how present the vocal is meant to be.
SILENT_BELOW = 0.05
SPARSE_BELOW = 0.4
LINES_BY_MODE = {
    VOCODER: (2, 4),
    CHANT: (2, 4),
    SPOKEN: (2, 3),
    SUNG: (3, 4),
}

# Structure tags, chosen from what the section *does* rather than its name.
TAG_INSTRUMENTAL = "[inst]"
TAG_VERSE = "[verse]"
TAG_CHORUS = "[chorus]"
TAG_BRIDGE = "[bridge]"

GENRE_WORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "mutation_funk": {
        "verbs": ("mutate", "twist", "warp", "bend", "switch", "flip"),
        "nouns": ("funk", "groove", "signal", "code", "circuit", "pattern"),
        "adverbs": ("again", "sideways", "in reverse"),
    },
    "dub": {
        "verbs": ("echo", "drop", "dub", "fade", "drift", "delay"),
        "nouns": ("bass", "night", "shadow", "space", "dread", "weight"),
        "adverbs": ("underground", "deeper", "low", "downtown"),
    },
    "tech_house": {
        "verbs": ("push", "pull", "move", "drive", "lock", "run"),
        "nouns": ("machine", "floor", "pulse", "engine", "wire", "room"),
        "adverbs": ("all night", "forward", "harder"),
    },
    "electronic": {
        "verbs": ("move", "turn", "hold", "break"),
        "nouns": ("light", "wave", "line", "current"),
        "adverbs": ("onward", "again"),
    },
}

MOOD_NOUNS: dict[str, tuple[str, ...]] = {
    "dark": ("shadow", "hollow", "ghost", "static", "iron"),
    "psychedelic": ("spiral", "mirror", "fracture", "prism", "echo"),
}

# Two-word forms are title-cased; they read as slogans rather than sentences.
COMPOUND_FORMS = ("{verb} {verb2}", "{noun} {noun2}")
SHORT_FORMS = ("{verb} the {noun}", "{verb} {verb2}", "{noun} {noun2}", "{verb} {adverb}")
LONG_FORMS = (
    "{verb} the {noun} {adverb}",
    "{verb} the {noun} and {verb2}",
    "no {noun}, only {noun2}",
    "{verb} the {noun}",
)


@dataclass(frozen=True)
class LyricSection:
    section_name: str
    tag: str
    lines: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "section_name": self.section_name,
            "tag": self.tag,
            "lines": list(self.lines),
        }


@dataclass(frozen=True)
class LyricSheet:
    mode: str
    hook: str | None
    sections: tuple[LyricSection, ...]

    def to_text(self) -> str:
        """The sheet as ACE-Step expects it: a structure tag, then its lines."""

        blocks: list[str] = []
        for section in self.sections:
            body = "\n".join(section.lines)
            blocks.append(f"{section.tag}\n{body}" if body else section.tag)
        return "\n".join(blocks) + "\n" if blocks else ""

    def to_dict(self) -> dict[str, object]:
        return {
            "lyrics_version": LYRICS_VERSION,
            "mode": self.mode,
            "hook": self.hook,
            "sections": [section.to_dict() for section in self.sections],
        }

    @property
    def line_count(self) -> int:
        return sum(len(section.lines) for section in self.sections)


def detect_mode(spec: SongSpec) -> str:
    """Pick the vocal treatment, which is what decides the writing style."""

    if not spec.vocal.enabled:
        return INSTRUMENTAL
    character = spec.vocal.character.casefold()
    if spec.vocal.vocoder or "vocoder" in character or "robot" in character:
        return VOCODER
    if "chant" in character:
        return CHANT
    if "spoken" in character or "spoken word" in character:
        return SPOKEN
    return SUNG


def build_lyrics(spec: SongSpec) -> LyricSheet:
    """Write a sheet for ``spec``; deterministic in ``spec.seed``."""

    mode = detect_mode(spec)
    if mode == INSTRUMENTAL:
        return LyricSheet(
            mode,
            None,
            tuple(
                LyricSection(section.name, TAG_INSTRUMENTAL, ())
                for section in spec.arrangement
            ),
        )

    vocabulary = _vocabulary(spec)
    hook = _write_line(random.Random(f"{spec.seed}:lyrics:hook"), vocabulary, mode)
    sections: list[LyricSection] = []
    for index, section in enumerate(spec.arrangement):
        rng = random.Random(f"{spec.seed}:lyrics:{index}")
        presence = _presence(section)
        if presence <= SILENT_BELOW:
            sections.append(LyricSection(section.name, TAG_INSTRUMENTAL, ()))
            continue
        tag = _tag_for(section, presence)
        lines = _write_section(rng, vocabulary, mode, presence, hook, tag)
        sections.append(LyricSection(section.name, tag, lines))
    return LyricSheet(mode, hook, tuple(sections))


def compile_lyrics(spec: SongSpec) -> str:
    """The sheet as the plain text ACE-Step is given."""

    return build_lyrics(spec).to_text()


def _presence(section: SectionSpec) -> float:
    """How present the vocal is here; energy stands in when unset."""

    return section.energy if section.vocal_probability is None else section.vocal_probability


def _tag_for(section: SectionSpec, presence: float) -> str:
    if section.energy >= 0.8:
        return TAG_CHORUS
    if section.energy <= 0.35:
        return TAG_BRIDGE
    return TAG_VERSE


def _write_section(
    rng: random.Random,
    vocabulary: dict[str, tuple[str, ...]],
    mode: str,
    presence: float,
    hook: str,
    tag: str,
) -> tuple[str, ...]:
    low, high = LINES_BY_MODE[mode]
    count = low if presence < SPARSE_BELOW else high
    lines: list[str] = []
    # The hook opens every chorus. Repetition is the point of a hook, and a
    # vocoder part with no recurring phrase reads as random noise.
    if tag == TAG_CHORUS:
        lines.append(hook)
    while len(lines) < count:
        line = _write_line(rng, vocabulary, mode)
        if line not in lines:
            lines.append(line)
    if mode == CHANT and len(lines) > 1:
        # A chant repeats rather than develops.
        lines = [lines[0], lines[0], *lines[1:]][:count]
    return tuple(lines)


def _write_line(
    rng: random.Random,
    vocabulary: dict[str, tuple[str, ...]],
    mode: str,
) -> str:
    forms = LONG_FORMS if mode in {SUNG, SPOKEN} else SHORT_FORMS
    form = rng.choice(forms)
    verbs = list(vocabulary["verbs"])
    nouns = list(vocabulary["nouns"])
    rng.shuffle(verbs)
    rng.shuffle(nouns)
    text = form.format(
        verb=verbs[0],
        verb2=verbs[1 % len(verbs)],
        noun=nouns[0],
        noun2=nouns[1 % len(nouns)],
        adverb=rng.choice(vocabulary["adverbs"]),
    )
    return text.title() if form in COMPOUND_FORMS else text[0].upper() + text[1:]


def _vocabulary(spec: SongSpec) -> dict[str, tuple[str, ...]]:
    """Words drawn from the song's own genres and mood, heaviest genre first."""

    verbs: list[str] = []
    nouns: list[str] = []
    adverbs: list[str] = []
    ordered = sorted(spec.style.genres, key=lambda item: item.weight, reverse=True)
    for genre in ordered:
        bank = GENRE_WORDS.get(genre.name, GENRE_WORDS["electronic"])
        verbs.extend(bank["verbs"])
        nouns.extend(bank["nouns"])
        adverbs.extend(bank["adverbs"])
    if spec.style.darkness >= 0.5:
        nouns.extend(MOOD_NOUNS["dark"])
    if spec.style.psychedelic >= 0.5:
        nouns.extend(MOOD_NOUNS["psychedelic"])
    if not verbs:
        bank = GENRE_WORDS["electronic"]
        verbs, nouns, adverbs = list(bank["verbs"]), list(bank["nouns"]), list(bank["adverbs"])
    return {
        "verbs": tuple(_unique(verbs)),
        "nouns": tuple(_unique(nouns)),
        "adverbs": tuple(_unique(adverbs)),
    }


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
