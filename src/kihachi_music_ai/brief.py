"""What a brief said that nothing here heard.

Measured across the six briefs in `example_output`: the vocabulary was nine
traits in thirty-four surface forms, and one real 85-character brief matched
none of them.

    アンビエント。110 BPM、D#m。2分程度。きらびやかで高域中心、繊細。
    ベースは控えめで薄い。パーカッションは軽く、シェイカーとハイハット中心。

The genre matcher caught `ambient`, and the regexes caught the tempo, the key
and the length. Everything describing the *sound* -- bright, delicate, a thin
bass, light percussion -- reached nothing, and the SongSpec came out carrying
its genre's default `darkness` of 0.48.

`きらびやか` reaches the `bright` trait as of 2026-08-17, and that brief now
composes at darkness 0.227 instead of 0.48 -- the first time a word about the
sound moved the spec. The other four clauses are unread exactly as before, so
this measurement is still the point: **half** of that brief, not none of it.

The gap is not that the brief was unreasonable. It is that a brief can be
ignored **silently**: `MusicBrain` has no way to say "I did not use this". So
this reads a brief the same way the brain does, and reports the clauses that
produced nothing.

No LLM, and deliberately so (ADR-0011). Which phrases go unread is a fact about
this vocabulary, decidable here, and worth having before anything is asked to
fill them in.

Pure and stdlib-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .genres import match_genres
from .intent import TRAIT_WORDS, read as read_intent
from .theory import _KEY_RE

BRIEF_COVERAGE_VERSION = "0.1"

#: The brain's own readers, re-run here so coverage cannot drift from behaviour.
#: Importing them rather than restating the patterns is the point: a new regex in
#: `music_brain` that is not listed here shows up as a false "unread", which is
#: the failure that gets noticed.
_BPM_RE = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*BPM", re.IGNORECASE)
_MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*分")
_TIME_SIGNATURE_RE = re.compile(r"(?<!\d)([2-9]|1[0-2])\s*/\s*(2|4|8)(?!\d)")
_BEATS_RE = re.compile(r"([2-9])\s*拍子")

_READERS = (
    ("bpm", _BPM_RE),
    ("duration", _MINUTES_RE),
    ("time_signature", _TIME_SIGNATURE_RE),
    ("time_signature", _BEATS_RE),
    ("key", _KEY_RE),
)

CLAUSE_SEPARATORS = "。、\n;"
"""Where one statement ends and the next begins.

Not a sentence splitter and not trying to be. A brief is a list of requests
separated by these marks, and the unit worth reporting is the request.
"""


PARTIAL_BELOW = 0.5
"""A clause this much covered or less is called partly read.

Clause granularity alone hides a real case: `ダブの32小節` contains a genre, so
the whole clause counts as read and the fact that nothing reads a bar count
disappears. Reporting how much of the clause was actually touched surfaces that
without claiming the clause was ignored.
"""


@dataclass(frozen=True)
class Clause:
    text: str
    start: int
    read_as: tuple[str, ...]
    #: Share of the clause's characters some reader actually matched.
    covered: float

    @property
    def unread(self) -> bool:
        return not self.read_as

    @property
    def partly_read(self) -> bool:
        return bool(self.read_as) and self.covered <= PARTIAL_BELOW


def _spans(prompt: str) -> list[tuple[int, int, str]]:
    """Every stretch of the brief something acted on, with what acted."""

    found: list[tuple[int, int, str]] = []
    for label, pattern in _READERS:
        for match in pattern.finditer(prompt):
            found.append((match.start(), match.end(), label))
    for match in match_genres(prompt):
        found.append((match.position, match.position + len(match.matched), "genre"))
    for trait in read_intent(prompt).traits:
        found.append(
            (trait.position, trait.position + len(trait.evidence), f"trait:{trait.name}")
        )
    return found


def _clauses(prompt: str) -> list[tuple[int, str]]:
    clauses: list[tuple[int, str]] = []
    start = 0
    for index, character in enumerate(prompt):
        if character in CLAUSE_SEPARATORS:
            text = prompt[start:index].strip()
            if text:
                clauses.append((start, text))
            start = index + 1
    tail = prompt[start:].strip()
    if tail:
        clauses.append((start, tail))
    return clauses


def read_coverage(prompt: str) -> dict[str, Any]:
    """Which of a brief's statements the brain acts on, and which it drops."""

    spans = _spans(prompt)
    clauses: list[Clause] = []
    for start, text in _clauses(prompt):
        end = start + len(text)
        # A reader that fired anywhere inside the clause counts as having read
        # it. Deliberately generous: the claim being made is only "something
        # here was used", and overstating the gap would be its own kind of lie.
        labels = sorted(
            {
                label
                for span_start, span_end, label in spans
                if span_start < end and span_end > start
            }
        )
        touched = set()
        for span_start, span_end, _ in spans:
            for position in range(max(span_start, start), min(span_end, end)):
                touched.add(position)
        clauses.append(
            Clause(
                text=text,
                start=start,
                read_as=tuple(labels),
                covered=round(len(touched) / len(text), 4) if text else 0.0,
            )
        )

    unread = [clause for clause in clauses if clause.unread]
    return {
        "brief_coverage_version": BRIEF_COVERAGE_VERSION,
        "scope": "which_statements_this_vocabulary_acts_on_not_whether_it_should",
        "clauses": [
            {
                "text": clause.text,
                "at": clause.start,
                "read_as": list(clause.read_as),
                "covered": clause.covered,
            }
            for clause in clauses
        ],
        "unread": [clause.text for clause in unread],
        "partly_read": [clause.text for clause in clauses if clause.partly_read],
        "read_fraction": (
            round((len(clauses) - len(unread)) / len(clauses), 4) if clauses else 0.0
        ),
        "vocabulary": {
            "traits": len(TRAIT_WORDS),
            "surface_forms": sum(len(words) for words in TRAIT_WORDS.values()),
        },
        "note": (
            "an unread clause is not a rejected one: nothing here judges whether "
            "the request was reasonable, only that no reader acted on it. The "
            "brief still composes -- with defaults where this had nothing to say"
        ),
    }


def describe(coverage: dict[str, Any]) -> list[str]:
    """The coverage as lines to print."""

    lines = [
        f"Brief coverage: {coverage['read_fraction']:.0%} of "
        f"{len(coverage['clauses'])} statements acted on "
        f"({coverage['vocabulary']['traits']} traits, "
        f"{coverage['vocabulary']['surface_forms']} surface forms)"
    ]
    for clause in coverage["clauses"]:
        if clause["read_as"]:
            mark = f"{', '.join(clause['read_as'])} ({clause['covered']:.0%} of the text)"
        else:
            mark = "nothing acted on this"
        lines.append(f"  {clause['text']}")
        lines.append(f"      -> {mark}")
    for text in coverage["partly_read"]:
        lines.append(
            f"- only part of \u300c{text}\u300d was read; the rest of that statement "
            "reached nothing"
        )
    if coverage["unread"]:
        lines.append(
            "- these went unread. The song still gets made, with defaults where "
            "the brief had something to say and this had nothing to hear it with"
        )
    return lines
