"""Where the two readers of one vocabulary disagree.

A brief gets read twice. :func:`.intent.read` applies the rules; `intent read`
asks a model (ADR-0011). Both answer in `intent.TRAIT_WORDS`, both report a
polarity and a strength, and **nothing has ever compared them**.

That cost something. On 2026-08-17 the two answered 「暗すぎない感じで」 in
opposite directions -- the model refused `dark`, the rules requested it -- and
that surfaced only because someone happened to run one brief through both while
working on something else. It was the third negation gap found that day by
looking rather than by testing, after adjectival `くない` and verb `ない`.

A disagreement is worth having either way round:

* the rules are missing something the model heard -- every gap so far;
* or the model invented a reading the vocabulary does not support, which is what
  ADR-0011's validation is for and this is a second look at.

**Neither reader is treated as correct here.** This reports the difference and
stops; deciding which one to change is a person's job, and pinning the model as
the answer would make a network call part of the definition of the rules.

Reads a stored `intent_reading.json` rather than calling anything. The artifact
already carries the brief it was produced from, so the comparison needs no key,
no network and no repeat of a paid call -- and it stays runnable over readings
collected months apart.

Pure and stdlib-only.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .intent import (
    EARLIER_HALF_WORDS,
    LATER_HALF_WORDS,
    read as read_intent,
)

AGREEMENT_VERSION = "0.1"

#: What a single trait can be, comparing one reading with the other.
AGREE = "agree"
POLARITY = "polarity_differs"
SCOPE = "scope_differs"
STRENGTH = "strength_differs"
#: A strength difference on a trait **both readers refuse**. Reported, and
#: ranked last, because it cannot reach the song: `Traits.strength_of` returns
#: 0.0 for any refusal, so -1.5 and -1.0 compose byte-identical MIDI. The first
#: sweep to use this tool spent its afternoon on one -- 「ボコーダーは厳禁で」,
#: model -1.5 against rules -1.0 -- which was worth it for what stood behind it
#: and was not, in itself, a difference about the song.
STRENGTH_ON_A_REFUSAL = "strength_differs_on_a_refusal"
MODEL_ONLY = "model_only"
RULES_ONLY = "rules_only"

#: The orders that matter. `polarity_differs` means the two readers disagree
#: about whether the brief asked for a thing or refused it, which is the
#: failure `intent.py` exists to prevent; a strength difference is a difference
#: of degree within the same answer.
#: `scope_differs` sits under a polarity difference and above everything else:
#: the two readers agree the brief asked for this and disagree about **where**
#: it lands, which is a change applied to the wrong half of the song.
#: `strength_differs_on_a_refusal` sits last of the disagreements, below the
#: strength difference that can move a number, because nothing reads it.
SEVERITY = (
    POLARITY,
    SCOPE,
    MODEL_ONLY,
    RULES_ONLY,
    STRENGTH,
    STRENGTH_ON_A_REFUSAL,
    AGREE,
)


#: The marks a brief separates its statements with, as `brief` and `intent` both
#: split on. A scope does not reach across one, so neither does this check.
_CLAUSE_SPLIT = re.compile(r"[、。，．,.;；\n\r]+")


def _clauses(brief: str) -> list[str]:
    return [piece for piece in _CLAUSE_SPLIT.split(brief) if piece.strip()]


def compare_readings(reading: dict[str, Any]) -> dict[str, Any]:
    """Compare a stored model reading with what the rules say about the same brief."""

    brief = str(reading.get("brief", ""))
    if not brief:
        raise ValueError("this reading carries no brief to compare against")
    stored = reading.get("brief_sha256")
    actual = hashlib.sha256(brief.encode("utf-8")).hexdigest()
    if stored and stored != actual:
        raise ValueError(
            "this reading's brief does not match its own sha256; comparing it "
            "would describe two different briefs"
        )

    model = {
        str(entry["name"]): entry
        for entry in reading.get("traits", [])
        if isinstance(entry, dict) and entry.get("name")
    }
    rules = {trait.name: trait for trait in read_intent(brief).traits}

    rows: list[dict[str, Any]] = []
    for name in sorted(set(model) | set(rules)):
        left, right = model.get(name), rules.get(name)
        if left is None:
            rows.append(_row(name, RULES_ONLY, None, right))
            continue
        if right is None:
            rows.append(_row(name, MODEL_ONLY, left, None))
            continue
        if int(left.get("polarity", 1)) != right.polarity:
            rows.append(_row(name, POLARITY, left, right))
        elif left.get("scope") != right.scope:
            rows.append(_row(name, SCOPE, left, right))
        elif float(left.get("strength", 0.0)) != right.strength:
            # Both refuse it, so the number they disagree about is one nothing
            # downstream reads. Still reported -- a reader that cannot be
            # trusted about degree is worth knowing about -- but not ranked
            # with the differences that change a song.
            refused = int(left.get("polarity", 1)) < 0 and right.polarity < 0
            rows.append(
                _row(name, STRENGTH_ON_A_REFUSAL if refused else STRENGTH, left, right)
            )
        else:
            rows.append(_row(name, AGREE, left, right))

    # A phrase the model filed as unmapped that the rules did act on. The model
    # is told to report what the vocabulary cannot say, so this is the two
    # readers contradicting each other about the vocabulary's own reach.
    #
    # Against the *evidence the rules cited*, not against the clause it sits in.
    # Clause-level agreement reported 「疾走感のある」 as contested because the
    # clause around it also contained 「暗くて」: two different statements, one
    # of them genuinely unread, called the same thing.
    evidence = [trait.evidence for trait in rules.values()]
    # A span word the rules used as a scope counts as cited too. Without this,
    # the first scoped brief compared clean while the model was filing 「前半は」
    # under `unmapped` -- the readers contradicting each other about placement,
    # invisible because a span word is not any trait's evidence.
    #
    # Per clause, because a span word is only read where a scopable trait sits
    # beside it. 「テクノ。後半は暗く、終盤はスカスカに。」 scopes the sparse and
    # not the darkness, and the model's 「後半」 there is a real gap: reporting it
    # as contested would call the rules right about a placement they dropped.
    evidence.extend(
        word
        for clause in _clauses(brief)
        if any(
            trait.scope is not None and trait.evidence in clause
            for trait in rules.values()
        )
        for word in LATER_HALF_WORDS + EARLIER_HALF_WORDS
        if word in clause
    )
    contested = sorted(
        phrase
        for phrase in reading.get("unmapped", [])
        if any(cited in phrase for cited in evidence)
    )

    rows.sort(key=lambda row: (SEVERITY.index(row["status"]), row["trait"]))
    return {
        "agreement_version": AGREEMENT_VERSION,
        "scope": "a_difference_between_two_readers_not_a_verdict_on_either",
        "brief": brief,
        "brief_sha256": actual,
        "model": reading.get("model"),
        "traits": rows,
        "contested_unmapped": contested,
        "disagreements": sum(1 for row in rows if row["status"] != AGREE),
        "note": (
            "neither reader is treated as correct. A polarity difference is the "
            "one this vocabulary was written to prevent, so it sorts first"
        ),
    }


def _row(
    name: str, status: str, model: dict[str, Any] | None, rules: Any
) -> dict[str, Any]:
    return {
        "trait": name,
        "status": status,
        "model": None
        if model is None
        else {
            "polarity": int(model.get("polarity", 1)),
            "strength": float(model.get("strength", 0.0)),
            "scope": model.get("scope"),
            "evidence": str(model.get("evidence", "")),
        },
        "rules": None
        if rules is None
        else {
            "polarity": rules.polarity,
            "strength": rules.strength,
            "scope": rules.scope,
            "evidence": rules.evidence,
        },
    }


def describe(comparison: dict[str, Any]) -> list[str]:
    """The comparison as lines, disagreements first."""

    lines = [
        f"Two readings of one brief ({comparison['disagreements']} "
        f"disagreement(s) across {len(comparison['traits'])} trait(s)):"
    ]
    for row in comparison["traits"]:
        if row["status"] == AGREE:
            continue
        lines.append(f"  {row['trait']:<14} {row['status']}")
        lines.append(f"      model: {_side(row['model'])}")
        lines.append(f"      rules: {_side(row['rules'])}")
        if row["status"] == STRENGTH_ON_A_REFUSAL:
            lines.append(
                "      (both refuse it, and a refusal has one strength: "
                "this difference cannot reach the song)"
            )
    for phrase in comparison["contested_unmapped"]:
        lines.append(f"  the model called {phrase!r} unmapped, but the rules read it")
    if not comparison["disagreements"] and not comparison["contested_unmapped"]:
        lines.append("- the two readers agree on every trait in this brief")
    else:
        lines.append(
            "- neither reader is authoritative here. A polarity difference means "
            "one of them is answering the opposite of what was asked"
        )
    return lines


def _side(side: dict[str, Any] | None) -> str:
    if side is None:
        return "said nothing"
    sign = "+" if side["polarity"] > 0 else "-"
    where = f" in {side['scope']}" if side.get("scope") else ""
    return f"{sign}{side['strength']:g} from {side['evidence']!r}{where}"
