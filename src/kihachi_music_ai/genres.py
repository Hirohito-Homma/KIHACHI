"""Recognise genres named in a prompt, using the shipped genre database.

Before this module, ``MusicBrain._parse_genres`` knew three genres by hand --
mutation funk, dub, tech house -- and collapsed everything else to a single
``electronic``. That was the real bottleneck in the genre path: a prompt asking
for bossa nova became ``electronic``, then ``edm`` at the AbletonGPT boundary,
and was handed a 909 drum machine kit. No amount of detail further downstream
could recover from that, because the distinction was already gone.

The database carries 1020 genre names across 37 families. What this module adds
is only *recognition*: it maps prompt text onto those names. Everything the
SongSpec does with a genre afterwards is unchanged, and deliberately so -- the
slugs the database produces for the original three are byte-identical to the
names they already had (``Tech House`` -> ``tech_house``), so the swing, drum
pattern, dub-send and lyric-vocabulary decisions keyed on them keep working.

Pure and stdlib-only, like the rest of the core.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parent / "data" / "genres.json"

#: A surface form made only of ASCII letters/digits/&/'/- and spaces is matched
#: with word boundaries; anything else (Japanese, mostly) is matched as a plain
#: substring, because Japanese does not delimit words.
_LATIN = re.compile(r"^[a-z0-9&'\- ./]+$")


@dataclass(frozen=True)
class Genre:
    """One row of the database, as much of it as recognition needs."""

    slug: str
    name: str
    parent: str | None
    level: str
    aliases: tuple[str, ...]
    bpm_min: float | None
    bpm_max: float | None
    meter: str
    mood_tags: tuple[str, ...]
    region: str


@dataclass(frozen=True)
class GenreMatch:
    genre: Genre
    #: The surface form actually found in the prompt.
    matched: str
    #: Character offset of that form, so callers can keep the prompt's order.
    position: int


@lru_cache(maxsize=1)
def load_database() -> tuple[Genre, ...]:
    payload = json.loads(_DATA.read_text(encoding="utf-8"))
    return tuple(
        Genre(
            slug=entry["slug"],
            name=entry["name"],
            parent=entry["parent"],
            level=entry["level"],
            aliases=tuple(entry["aliases"]),
            bpm_min=entry["bpm_min"],
            bpm_max=entry["bpm_max"],
            meter=entry["meter"],
            mood_tags=tuple(entry["mood_tags"]),
            region=entry["region"],
        )
        for entry in payload["genres"]
    )


@lru_cache(maxsize=1)
def _surface_forms() -> tuple[tuple[str, Genre], ...]:
    """Every name and alias, longest first, with collisions resolved.

    24 surface forms in v0.2 are claimed by two rows at once, always the same
    shape: a family header (``Reggae / Dub / Ska``) and the specific style
    inside it (``Dub``). The specific one wins, because a family is a grouping
    rather than something a person asks for by name -- and because keeping the
    family would have turned the prompt word "dub" into ``reggae_dub_ska`` and
    silently broken the dub send that KIHACHI keys on that exact slug.

    Longest first so ``Tech House`` is preferred over ``House`` and ``Dubstep``
    over ``Dub``.
    """
    claimed: dict[str, Genre] = {}
    for genre in load_database():
        for form in (genre.name, *genre.aliases):
            key = form.strip().lower()
            if not key:
                continue
            previous = claimed.get(key)
            if previous is None:
                claimed[key] = genre
                continue
            # Prefer the row that sits inside a family over the family itself;
            # ties fall back to the name that is not a multi-style header.
            if previous.parent is None and genre.parent is not None:
                claimed[key] = genre
    return tuple(
        sorted(claimed.items(), key=lambda item: (-len(item[0]), item[0]))
    )


def _is_katakana(char: str) -> bool:
    return bool(char) and ("゠" <= char <= "ヿ" or char in "ー・")


def _is_kanji(char: str) -> bool:
    return bool(char) and "一" <= char <= "鿿"


def _continues_run(form: str, before: str, after: str) -> bool:
    """Whether the surrounding text makes this match part of a longer word.

    Japanese writes no spaces, so a bare substring search finds a genre inside
    an unrelated word: ``ラップ`` (rap) sits inside ``スラップベース`` (slap
    bass), which turned a prompt about a bassline into a hip-hop request. There
    is no boundary character to test for, so the test is whether the same script
    simply keeps running on either side. A miss is far cheaper than inventing a
    genre nobody asked for, so this errs towards rejecting.
    """
    if _is_katakana(form[0]) and _is_katakana(before):
        return True
    if _is_katakana(form[-1]) and _is_katakana(after):
        return True
    if _is_kanji(form[0]) and _is_kanji(before):
        return True
    if _is_kanji(form[-1]) and _is_kanji(after):
        return True
    return False


def _spans(text: str, form: str) -> list[tuple[int, int]]:
    if _LATIN.match(form):
        pattern = r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(form)
        return [(m.start(), m.end()) for m in re.finditer(pattern, text)]
    found = []
    start = text.find(form)
    while start != -1:
        end = start + len(form)
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        if not _continues_run(form, before, after):
            found.append((start, end))
        start = text.find(form, start + 1)
    return found


def match_genres(prompt: str) -> tuple[GenreMatch, ...]:
    """Genres named in ``prompt``, in the order they appear.

    A form contained inside a longer match is dropped, so "Tech House" yields
    tech house alone rather than tech house *and* house.
    """
    lowered = prompt.lower()
    hits: list[tuple[int, int, str, Genre]] = []
    for form, genre in _surface_forms():
        haystack = lowered if _LATIN.match(form) else prompt
        for start, end in _spans(haystack, form):
            hits.append((start, end, form, genre))

    kept: list[tuple[int, int, str, Genre]] = []
    for hit in sorted(hits, key=lambda h: (-(h[1] - h[0]), h[0])):
        start, end, _form, genre = hit
        covered = any(k[0] <= start and end <= k[1] for k in kept)
        if covered:
            continue
        if any(k[3].slug == genre.slug for k in kept):
            continue
        kept.append(hit)

    kept.sort(key=lambda h: h[0])
    return tuple(
        GenreMatch(genre=genre, matched=form, position=start)
        for start, _end, form, genre in kept
    )


def find(slug: str) -> Genre | None:
    """The database row for a slug, or ``None`` when it is not a known genre."""
    for genre in load_database():
        if genre.slug == slug:
            return genre
    return None


def describe(slug: str) -> dict[str, Any]:
    """Everything known about a slug, for reports and debugging."""
    genre = find(slug)
    if genre is None:
        return {"slug": slug, "known": False}
    return {
        "slug": genre.slug,
        "known": True,
        "name": genre.name,
        "parent": genre.parent,
        "bpm": [genre.bpm_min, genre.bpm_max],
        "mood_tags": list(genre.mood_tags),
        "region": genre.region,
    }
