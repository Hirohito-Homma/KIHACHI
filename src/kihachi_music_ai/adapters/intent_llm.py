"""Ask a model to say a brief in the vocabulary the brain already has.

ADR-0011: the LLM reads briefs, it does not write songs. What it produces is a
translation into `intent.TRAIT_WORDS` plus a list of the phrases it could not
translate -- and the second half is the point. Measured on the six briefs in
`example_output`, one 85-character brief matched none of the nine traits, and
nothing anywhere reported that.

Shaped like the ACE-Step adapter (ADR-0002), for the same reasons:

* `build_request` writes exactly what would be sent, with no network and no key.
  Everything except the call itself is checkable offline, and tested that way.
* The key is read from the environment only. It is never written into the
  request record, the artifact, or the CLI output.
* Nothing is applied. `compose --intent` reads the artifact as an explicit act.

**The `anthropic` SDK is an optional dependency and is imported inside the call.**
ADR-0001 keeps the core on the standard library, so `pyproject.toml` carries it
under `[project.optional-dependencies] llm` and nothing here is imported until
someone actually asks for a reading. Talking to Claude over hand-rolled HTTP to
dodge the dependency would be worse: the SDK is the supported client, and the
boundary that matters is that the *core* stays stdlib, not that this file does.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..intent import (
    EARLIER_HALF_WORDS,
    FIRST_HALF,
    LARGE_WORDS,
    LATER_HALF_WORDS,
    SCOPABLE_TRAITS,
    SECOND_HALF,
    SMALL_WORDS,
    TRAIT_WORDS,
)

INTENT_READING_VERSION = "0.1"
INTENT_READING_NAME = "intent_reading.json"

DEFAULT_MODEL = "claude-opus-5"
API_KEY_ENV = "ANTHROPIC_API_KEY"

MAX_TOKENS = 4096

STRENGTHS = (0.5, 1.0, 1.5)
"""Hedged, plainly stated, insisted on -- `intent.Trait`'s own three values.

The model picks one of these rather than a free float, because the brain reads
them through fixed thresholds and a 0.73 would land on whichever side of a
comparison it happened to fall.
"""


def _trait_shape(names: Any, *, scoped: bool) -> dict[str, Any]:
    """One kind of trait entry: the four fields every trait has, plus placement.

    Split in two because a schema that offers `scope` on every trait offers a
    document the brain cannot act on, and :func:`validate_reading` then throws
    the whole reading away -- a paid call returning nothing, at the model's
    discretion rather than the caller's.
    """

    properties: dict[str, Any] = {
        "name": {"type": "string", "enum": sorted(names)},
        "polarity": {"type": "integer", "enum": [1, -1]},
        "strength": {"type": "number", "enum": list(STRENGTHS)},
        "evidence": {"type": "string"},
    }
    if scoped:
        # Omitted rather than null for the whole song: the API rejects an enum
        # carrying null against a union type, and "absent" is already how a
        # reading stored before scopes existed says the same thing.
        properties["scope"] = {"type": "string", "enum": [FIRST_HALF, SECOND_HALF]}
    return {
        "type": "object",
        "properties": properties,
        "required": ["name", "polarity", "strength", "evidence"],
        "additionalProperties": False,
    }


def _schema() -> dict[str, Any]:
    """What the model may return. Every field the brain can act on, and no more.

    Two trait shapes rather than one. `scope` is a field only four traits have
    somewhere to put (ADR-0013), and stating that in the prompt alone left the
    schema admitting the other twenty-one with a place attached: 「ワンコードで
    ずっと同じ和音を引っ張って」 -- a brief with no span word in it at all --
    came back with `slow_changes` scoped to the first half in two runs of five,
    and each of those readings was rejected whole. Under the split the field
    does not exist on that shape, so the model cannot spend a call on it.
    """

    return {
        "type": "object",
        "properties": {
            "traits": {
                "type": "array",
                "items": {
                    "anyOf": [
                        _trait_shape(SCOPABLE_TRAITS, scoped=True),
                        _trait_shape(set(TRAIT_WORDS) - set(SCOPABLE_TRAITS), scoped=False),
                    ]
                },
            },
            "unmapped": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["traits", "unmapped"],
        "additionalProperties": False,
    }


def _system_prompt() -> str:
    """The vocabulary, and the instruction not to exceed it.

    The trait list is generated from `TRAIT_WORDS` rather than restated, so a
    trait added to the brain cannot be missing here -- the drift this whole
    module exists to surface should not start inside it.
    """

    vocabulary = "\n".join(
        f"- {name}: {', '.join(words)}" for name, words in sorted(TRAIT_WORDS.items())
    )
    return (
        "You translate a music brief into a fixed vocabulary. You do not write "
        "music, choose settings, or interpret beyond what the brief says.\n\n"
        "The vocabulary is these traits, with the surface forms the rule-based "
        "reader already recognises:\n\n"
        f"{vocabulary}\n\n"
        "For each trait the brief asks for or refuses, report it with:\n"
        "- polarity: 1 asked for, -1 refused\n"
        f"- strength: {STRENGTHS[0]} hedged (e.g. {', '.join(SMALL_WORDS[:3])}), "
        f"{STRENGTHS[1]} plainly stated, {STRENGTHS[2]} insisted on "
        f"(e.g. {', '.join(LARGE_WORDS[:3])})\n"
        "- evidence: the exact substring of the brief that says so, copied "
        "character for character\n\n"
        "A brief may also say **where** in the song it means, and four traits "
        f"can carry that: {', '.join(sorted(SCOPABLE_TRAITS))}. When the clause "
        f"names the later part of the song ({', '.join(LATER_HALF_WORDS)}) set "
        f"`scope` to {SECOND_HALF!r}; for the earlier part "
        f"({', '.join(EARLIER_HALF_WORDS)}) set it to {FIRST_HALF!r}. Leave it "
        "out otherwise. Any other trait applies to the whole song no matter "
        "where it is said, because the song has only one of that number -- so a "
        "place named around one of those is a real gap, and belongs in "
        "`unmapped`.\n\n"
        "Then list, in `unmapped`, every phrase of the brief that asks for "
        "something musical this vocabulary cannot express. Copy those "
        "character for character too.\n\n"
        "`unmapped` is the more useful half. Do not force a phrase onto a trait "
        "that does not mean the same thing -- reporting that the vocabulary "
        "falls short is the correct answer, and a wrong trait is worse than an "
        "honest gap. Tempo, key, length and genre names are read elsewhere; "
        "leave them out of both lists."
    )


def build_request(brief: str, *, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Exactly what would be sent. No network, no key, no side effects."""

    brief = brief.strip()
    if not brief:
        raise ValueError("brief must not be empty")
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": _system_prompt(),
        "messages": [{"role": "user", "content": brief}],
        "output_config": {"format": {"type": "json_schema", "schema": _schema()}},
    }


def validate_reading(reading: dict[str, Any], brief: str) -> dict[str, Any]:
    """Reject anything the brain could not act on, or that the brief did not say.

    Two checks, and the second is the one that matters. A trait name outside the
    vocabulary is caught by the schema; a trait whose evidence is not in the
    brief is not, and that is what a fabricated reading looks like. The whole
    document is rejected rather than the offending entry dropped -- the same
    grain as `import-kihachi`, where a partly-applied document is the worse
    outcome.
    """

    for entry in reading.get("traits", []):
        name = entry.get("name")
        if name not in TRAIT_WORDS:
            raise ValueError(f"trait {name!r} is outside the vocabulary")
        if entry.get("polarity") not in (1, -1):
            raise ValueError(f"trait {name!r} has polarity {entry.get('polarity')!r}")
        if entry.get("strength") not in STRENGTHS:
            raise ValueError(f"trait {name!r} has strength {entry.get('strength')!r}")
        scope = entry.get("scope")
        if scope is not None:
            if scope not in (FIRST_HALF, SECOND_HALF):
                raise ValueError(f"trait {name!r} has scope {scope!r}")
            if name not in SCOPABLE_TRAITS:
                # The brain has nowhere to put it: `SectionSpec` carries an
                # energy and three densities and nothing else, so accepting a
                # scoped `dark` would promise placement that never arrives.
                raise ValueError(
                    f"trait {name!r} cannot be scoped; only {sorted(SCOPABLE_TRAITS)} can"
                )
        evidence = entry.get("evidence", "")
        if not evidence or evidence not in brief:
            raise ValueError(
                f"trait {name!r} cites {evidence!r}, which is not in the brief"
            )
    for phrase in reading.get("unmapped", []):
        if phrase not in brief:
            raise ValueError(f"unmapped phrase {phrase!r} is not in the brief")
    return reading


def _api_error_message(exc: Any) -> str:
    """Dig the API's own sentence out of an SDK error, or fall back to str()."""

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
    return str(exc)


def _explain_api_error(exc: Any, model: str = DEFAULT_MODEL) -> str:
    """Say what the caller has to fix, in one line.

    The SDK raises these with a full traceback through `client.messages.create`,
    which reads as a crash in KIHACHI. None of them are: every one is something
    the caller changes outside this program. A refused call is reported the same
    way a refused brief is -- named, not dumped. Measured on a real 400 for an
    empty credit balance, which arrived as 25 lines of traceback.
    """

    status = getattr(exc, "status_code", None)
    message = _api_error_message(exc)
    hints = {
        400: "the request was rejected; if this mentions the credit balance, "
        "the account needs credit before any brief can be read",
        401: f"{API_KEY_ENV} was not accepted. Check the value rather than "
        "whether it is set -- a placeholder is set too",
        403: "this key is not allowed to make this request",
        # The model has to be the one that was asked for. Naming DEFAULT_MODEL
        # here instead reported a model the caller had just overridden, which is
        # a wrong answer dressed as a helpful one -- caught by sending a bogus
        # --model at the real API.
        404: f"the API does not know the model {model!r}; --model picks another",
        429: "rate limited; the brief is unchanged, so this can simply be run "
        "again",
    }
    hint = hints.get(status if isinstance(status, int) else -1)
    if hint is None and isinstance(status, int) and status >= 500:
        hint = "the API is having trouble; this is worth retrying unchanged"
    prefix = f"the API refused the request ({status})" if status else "the API refused the request"
    return f"{prefix}: {message}" + (f". {hint}" if hint else "")


def read_brief(brief: str, *, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Ask the model, validate what comes back, and return the artifact.

    Needs `ANTHROPIC_API_KEY` in the environment and the `llm` extra installed.
    """

    brief = brief.strip()
    request = build_request(brief, model=model)
    if not os.environ.get(API_KEY_ENV):
        raise RuntimeError(
            f"{API_KEY_ENV} is not set. It is read from the environment only and "
            "never stored; `intent prepare` writes the request without it"
        )
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "the anthropic SDK is not installed. It is an optional dependency so "
            "the core stays standard-library only (ADR-0001): "
            "pip install 'kihachi-music-ai[llm]'"
        ) from exc

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(**request)
    except anthropic.APIStatusError as exc:
        raise RuntimeError(_explain_api_error(exc, model)) from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError(
            f"could not reach the API: {exc}. Nothing was read; "
            "`intent prepare` still works offline"
        ) from exc
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to read this brief")
    text = next((block.text for block in response.content if block.type == "text"), "")
    reading = validate_reading(json.loads(text), brief)
    return {
        "intent_reading_version": INTENT_READING_VERSION,
        "scope": "a_translation_of_the_brief_not_a_judgement_about_it",
        "model": model,
        "brief": brief,
        "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        "traits": reading["traits"],
        "unmapped": reading["unmapped"],
        "note": (
            "traits are named in the brain's own vocabulary and were checked "
            "against it; every evidence string was found in the brief. unmapped "
            "is what the vocabulary could not say -- the reason to read this file"
        ),
    }


def write_reading(
    project_dir: Path, reading: dict[str, Any], *, overwrite: bool = False
) -> Path:
    destination = Path(project_dir) / INTENT_READING_NAME
    if destination.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite intent reading: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(reading, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return destination
