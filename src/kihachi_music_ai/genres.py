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
from typing import Any, Sequence

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


#: A database tempo range only says something when it is narrow. Measured on
#: v0.2 the median range is 100 BPM wide, because most rows inherit their
#: family's span -- "70 to 180" is not a tempo, it is an absence of one, and
#: acting on its midpoint would dress a guess up as data. 162 of 1020 genres are
#: individualised enough to clear this, and those are the ones worth using.
MAX_INFORMATIVE_BPM_RANGE = 40.0


def typical_bpm(weighted: Sequence[tuple[str, float]]) -> float | None:
    """A tempo for these weighted genre slugs, or ``None`` if the data is mute.

    The midpoint of each usable range, averaged by the genre's weight. Genres
    whose range is too wide to mean anything (or whose ``bpm_min`` is 0, which
    one row is) sit the vote out rather than dragging the result toward the
    middle of nowhere.
    """
    total = 0.0
    weight_used = 0.0
    for slug, weight in weighted:
        genre = find(slug)
        if genre is None or not genre.bpm_min or not genre.bpm_max:
            continue
        if genre.bpm_max - genre.bpm_min > MAX_INFORMATIVE_BPM_RANGE:
            continue
        total += (genre.bpm_min + genre.bpm_max) / 2.0 * weight
        weight_used += weight
    if weight_used <= 0:
        return None
    return round(total / weight_used, 1)


#: mood_tags that push the SongSpec's two timbre axes. Only tags whose musical
#: direction is unambiguous are listed; the other ~60 (``narrative``,
#: ``regional``, ``communal``...) describe context rather than timbre and are
#: deliberately ignored rather than stretched to fit an axis.
_DARK_TAGS = frozenset(
    {"dark", "aggressive", "nocturnal", "melancholic", "raw", "extreme",
     "militant", "menacing", "intense", "uncanny", "confrontational", "urgent"}
)
_BRIGHT_TAGS = frozenset(
    {"sunny", "uplifting", "euphoric", "warm", "playful", "celebratory",
     "festive", "accessible", "catchy", "serene", "calm", "romantic"}
)
_PSYCHEDELIC_TAGS = frozenset(
    {"psychedelic", "hypnotic", "abstract", "dreamy", "transcendental",
     "immersive", "cerebral", "weird", "futuristic", "atmospheric"}
)


def mood_axes(weighted: Sequence[tuple[str, float]]) -> tuple[float | None, float | None]:
    """``(darkness, psychedelic)`` in 0..1 from mood tags, or ``None`` each.

    ``None`` means the tags said nothing about that axis, which is different
    from saying "neutral" -- the caller keeps its own default instead of being
    pulled to 0.5 by silence.
    """
    dark_score = 0.0
    dark_weight = 0.0
    psy_score = 0.0
    total_weight = 0.0
    saw_psychedelic = False
    for slug, weight in weighted:
        genre = find(slug)
        if genre is None:
            continue
        total_weight += weight
        if not genre.mood_tags:
            continue
        tags = {tag.lower() for tag in genre.mood_tags}
        dark = len(tags & _DARK_TAGS)
        bright = len(tags & _BRIGHT_TAGS)
        if dark or bright:
            # An average of ratios: darkness is a direction each genre either
            # has an opinion about or does not, so genres that stay silent are
            # left out of the average rather than counted as neutral.
            dark_score += (dark / (dark + bright)) * weight
            dark_weight += weight
        psychedelic = len(tags & _PSYCHEDELIC_TAGS)
        if psychedelic:
            saw_psychedelic = True
            # Normalised by the *total* weight, not by the genres that carry
            # the tags: this axis asks how much of the song is psychedelic, so
            # one 0.3-weight dub cannot make the whole track 1.0. Saturating
            # within a genre, because five such tags is not five times two.
            psy_score += min(1.0, psychedelic / 3.0) * weight
    darkness = round(dark_score / dark_weight, 3) if dark_weight else None
    psyched = (
        round(psy_score / total_weight, 3)
        if saw_psychedelic and total_weight
        else None
    )
    return darkness, psyched


def find(slug: str) -> Genre | None:
    """The database row for a slug, or ``None`` when it is not a known genre."""
    for genre in load_database():
        if genre.slug == slug:
            return genre
    return None


@lru_cache(maxsize=1)
def families() -> frozenset[str]:
    """Every family name in the database, exactly as the database spells them.

    Two hand-written tables elsewhere -- :data:`.derive.FAMILY_PROFILES` and
    :data:`.ableton.LIVE_GENRE_BY_FAMILY` -- are keyed on these strings. Before
    this existed each table simply repeated the spelling and hoped, and a
    renamed family would have made every lookup miss silently: no error, just
    the caller's default from then on. :func:`unknown_families` is what those
    tables are checked against.
    """
    names = set()
    for genre in load_database():
        names.add(genre.parent if genre.parent else genre.name)
    return frozenset(names)


def family_of(slug: str) -> str | None:
    """The family ``slug`` belongs to, or ``None`` when the slug is unknown.

    A top-level row *is* a family, so it answers with its own name rather than
    with the ``None`` its ``parent`` column holds. Reading ``parent`` directly
    is what made a prompt naming a family outright ("Disco", "Pop") fall
    through every family table to the default -- the one shape of prompt where
    the family is stated most plainly.
    """
    genre = find(slug)
    if genre is None:
        return None
    return genre.parent if genre.parent else genre.name


def unknown_families(names: Sequence[str]) -> tuple[str, ...]:
    """Those of ``names`` that no longer name a family, sorted."""
    return tuple(sorted(set(names) - families()))


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
