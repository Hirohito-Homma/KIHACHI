from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kihachi_music_ai.adapters import intent_llm
from kihachi_music_ai.adapters.intent_llm import (
    API_KEY_ENV,
    DEFAULT_MODEL,
    STRENGTHS,
    _explain_api_error,
    build_request,
    read_brief,
    validate_reading,
    write_reading,
)
from kihachi_music_ai.cli import main
from kihachi_music_ai.intent import SCOPABLE_TRAITS, TRAIT_WORDS

BRIEF = "サイケデリックに。きらびやかで高域中心、繊細。ベースは控えめで薄い。"


class _FakeStatusError(Exception):
    """The shape `_explain_api_error` reads, without needing the SDK installed.

    The tests stay runnable on a core-only install (ADR-0001), which is the whole
    reason the SDK is imported inside the call rather than at module level.
    """

    def __init__(self, status_code: int, body: dict | None) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        self.body = body


class RequestTests(unittest.TestCase):
    """Everything except the call itself is checkable without a key."""

    def test_the_schema_admits_only_traits_the_brain_can_act_on(self) -> None:
        request = build_request(BRIEF)

        shapes = request["output_config"]["format"]["schema"]["properties"]["traits"][
            "items"
        ]["anyOf"]
        names = {
            name for shape in shapes for name in shape["properties"]["name"]["enum"]
        }
        self.assertEqual(names, set(TRAIT_WORDS))

    def test_the_vocabulary_is_generated_from_the_brain_not_restated(self) -> None:
        """A trait added to the brain must not go missing from the prompt."""

        with mock.patch.dict(
            "kihachi_music_ai.intent.TRAIT_WORDS", {"kalimba": ("カリンバ",)}, clear=False
        ):
            request = build_request(BRIEF)

        self.assertIn("kalimba", request["system"])
        self.assertIn("カリンバ", request["system"])

    def test_the_request_names_the_default_model_and_carries_no_key(self) -> None:
        request = build_request(BRIEF)

        self.assertEqual(request["model"], DEFAULT_MODEL)
        self.assertNotIn("api_key", json.dumps(request))

    def test_an_empty_brief_is_refused_before_anything_is_built(self) -> None:
        with self.assertRaises(ValueError):
            build_request("   ")


class ValidationTests(unittest.TestCase):
    """The check the schema cannot make: did the brief actually say this?"""

    def reading(self, **overrides) -> dict:
        trait = {
            "name": "psychedelic",
            "polarity": 1,
            "strength": 1.0,
            "evidence": "サイケデリック",
        }
        trait.update(overrides)
        return {"traits": [trait], "unmapped": []}

    def test_a_reading_grounded_in_the_brief_passes(self) -> None:
        self.assertIsNotNone(validate_reading(self.reading(), BRIEF))

    def test_evidence_that_is_not_in_the_brief_is_refused(self) -> None:
        """What a fabricated reading looks like, and the schema cannot catch it."""

        with self.assertRaises(ValueError) as caught:
            validate_reading(self.reading(evidence="ダブっぽく"), BRIEF)

        self.assertIn("not in the brief", str(caught.exception))

    def test_an_unmapped_phrase_must_be_quoted_from_the_brief_too(self) -> None:
        with self.assertRaises(ValueError):
            validate_reading(
                {"traits": [], "unmapped": ["ふくよかな低音"]}, BRIEF
            )

    def test_a_trait_outside_the_vocabulary_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            validate_reading(self.reading(name="shimmer"), BRIEF)

    def test_polarity_and_strength_are_the_brain_s_own_values(self) -> None:
        for bad in ({"polarity": 0}, {"strength": 0.73}):
            with self.subTest(**bad):
                with self.assertRaises(ValueError):
                    validate_reading(self.reading(**bad), BRIEF)
        for strength in STRENGTHS:
            with self.subTest(strength=strength):
                validate_reading(self.reading(strength=strength), BRIEF)

    def test_the_whole_document_is_refused_not_the_bad_entry(self) -> None:
        """A partly-applied reading is the worse outcome, as with import-kihachi."""

        grounded = self.reading()["traits"][0]
        fabricated = dict(grounded, name="dub", evidence="ダブ")

        with self.assertRaises(ValueError):
            validate_reading({"traits": [grounded, fabricated], "unmapped": []}, BRIEF)


class CallTests(unittest.TestCase):
    def test_a_missing_key_is_named_rather_than_guessed_at(self) -> None:
        with mock.patch.dict(os.environ, {API_KEY_ENV: ""}, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                read_brief(BRIEF)

        self.assertIn(API_KEY_ENV, str(caught.exception))

    def test_the_command_reports_it_without_a_traceback(self) -> None:
        with mock.patch.dict(os.environ, {API_KEY_ENV: ""}, clear=False):
            self.assertEqual(main(["intent", "read", BRIEF]), 2)

    def test_a_refused_call_says_what_to_fix_rather_than_dumping_a_stack(self) -> None:
        """The real 400 that prompted this arrived as 25 lines of traceback."""

        refusal = _explain_api_error(
            _FakeStatusError(
                400,
                {"error": {"message": "Your credit balance is too low to access "
                                      "the Anthropic API."}},
            )
        )

        self.assertIn("credit balance is too low", refusal)
        self.assertIn("400", refusal)
        self.assertEqual(refusal.count("\n"), 0)

    def test_a_rejected_key_is_told_apart_from_a_missing_one(self) -> None:
        """A placeholder is set, so `is it set` is the wrong question for a 401."""

        refusal = _explain_api_error(
            _FakeStatusError(401, {"error": {"message": "invalid x-api-key"}})
        )

        self.assertIn(API_KEY_ENV, refusal)
        self.assertIn("placeholder", refusal)

    def test_an_unknown_model_names_the_one_that_was_asked_for(self) -> None:
        """Naming DEFAULT_MODEL here reported a model the caller had overridden."""

        refusal = _explain_api_error(
            _FakeStatusError(404, {"error": {"message": "model: made-up-7"}}),
            "made-up-7",
        )

        self.assertIn("made-up-7", refusal)
        self.assertNotIn(DEFAULT_MODEL, refusal)

    def test_an_error_with_no_body_still_reads_as_a_sentence(self) -> None:
        refusal = _explain_api_error(_FakeStatusError(529, None))

        self.assertIn("529", refusal)
        self.assertIn("retrying", refusal)

    def test_prepare_needs_no_key_at_all(self) -> None:
        with mock.patch.dict(os.environ, {API_KEY_ENV: ""}, clear=False):
            self.assertEqual(main(["intent", "prepare", BRIEF]), 0)


class ScopeTests(unittest.TestCase):
    """The model can name a place, for the traits that have one (ADR-0013)."""

    BRIEF = "テクノ。後半は手数を多く。"

    def test_the_schema_offers_both_halves_and_nothing_else(self) -> None:
        scoped, plain = self._shapes()
        scope = scoped["properties"]["scope"]

        self.assertEqual(scope["enum"], ["first_half", "second_half"])
        # Absent, not null: the API rejects an enum carrying null against a
        # union type, and absent is how a pre-scope reading already says this.
        self.assertNotIn("scope", scoped["required"])
        self.assertNotIn("scope", plain["properties"])

    def _shapes(self):
        """The two trait shapes, scopable first."""

        shapes = intent_llm._schema()["properties"]["traits"]["items"]["anyOf"]
        return sorted(shapes, key=lambda shape: "scope" not in shape["properties"])

    def test_the_field_does_not_exist_on_a_trait_that_cannot_carry_it(self) -> None:
        """The prompt said which four traits take a place; the schema did not.

        So the model could attach one to any of the twenty-five, and
        `validate_reading` answered by rejecting the reading whole -- the
        caller pays for the call and receives an error, decided by a sampling
        accident rather than by anything in the brief. Measured on
        「ワンコードでずっと同じ和音を引っ張って。」, which names no part of the
        song: `slow_changes` came back scoped to the first half in two runs of
        five, and clean in the other three.

        `additionalProperties` is what makes the split bind. Without it the
        second shape merely fails to *mention* `scope` and still accepts it.
        """

        scoped, plain = self._shapes()

        self.assertEqual(set(scoped["properties"]["name"]["enum"]), set(SCOPABLE_TRAITS))
        self.assertEqual(
            set(plain["properties"]["name"]["enum"]),
            set(TRAIT_WORDS) - set(SCOPABLE_TRAITS),
        )
        self.assertFalse(plain["additionalProperties"])
        self.assertFalse(scoped["additionalProperties"])

    def test_a_scope_on_a_trait_the_brain_cannot_place_is_rejected(self) -> None:
        """`darkness` is one number for the whole song. Accepting a scoped one
        would promise a placement that never arrives."""

        with self.assertRaises(ValueError) as caught:
            intent_llm.validate_reading(
                {
                    "traits": [
                        {
                            "name": "dark",
                            "polarity": 1,
                            "strength": 1.0,
                            "evidence": "暗く",
                            "scope": "second_half",
                        }
                    ],
                    "unmapped": [],
                },
                "テクノ。後半は暗く。",
            )

        self.assertIn("cannot be scoped", str(caught.exception))

    def test_a_scope_on_a_trait_that_has_one_is_kept(self) -> None:
        reading = intent_llm.validate_reading(
            {
                "traits": [
                    {
                        "name": "busy",
                        "polarity": 1,
                        "strength": 1.0,
                        "evidence": "手数を多く",
                        "scope": "second_half",
                    }
                ],
                "unmapped": [],
            },
            self.BRIEF,
        )

        self.assertEqual(reading["traits"][0]["scope"], "second_half")

    def test_an_invented_scope_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            intent_llm.validate_reading(
                {
                    "traits": [
                        {
                            "name": "busy",
                            "polarity": 1,
                            "strength": 1.0,
                            "evidence": "手数を多く",
                            "scope": "the_drop",
                        }
                    ],
                    "unmapped": [],
                },
                self.BRIEF,
            )

    def test_the_prompt_names_the_scopable_traits_from_the_brain(self) -> None:
        prompt = intent_llm._system_prompt()

        for name in ("busy", "sparse", "contrast", "flat"):
            self.assertIn(name, prompt)
        self.assertIn("後半", prompt)
        self.assertIn("second_half", prompt)


class ArtifactTests(unittest.TestCase):
    def record(self) -> dict:
        return {
            "intent_reading_version": "0.1",
            "model": DEFAULT_MODEL,
            "brief": BRIEF,
            "traits": [],
            "unmapped": ["きらびやかで高域中心"],
        }

    def test_the_reading_is_written_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)

            written = write_reading(project, self.record())

            self.assertEqual(
                json.loads(written.read_text(encoding="utf-8"))["unmapped"],
                ["きらびやかで高域中心"],
            )
            with self.assertRaises(FileExistsError):
                write_reading(project, self.record())
            write_reading(project, self.record(), overwrite=True)


if __name__ == "__main__":
    unittest.main()
