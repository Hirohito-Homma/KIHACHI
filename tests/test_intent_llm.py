from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kihachi_music_ai.adapters.intent_llm import (
    API_KEY_ENV,
    DEFAULT_MODEL,
    STRENGTHS,
    build_request,
    read_brief,
    validate_reading,
    write_reading,
)
from kihachi_music_ai.cli import main
from kihachi_music_ai.intent import TRAIT_WORDS

BRIEF = "サイケデリックに。きらびやかで高域中心、繊細。ベースは控えめで薄い。"


class RequestTests(unittest.TestCase):
    """Everything except the call itself is checkable without a key."""

    def test_the_schema_admits_only_traits_the_brain_can_act_on(self) -> None:
        request = build_request(BRIEF)

        names = request["output_config"]["format"]["schema"]["properties"]["traits"][
            "items"
        ]["properties"]["name"]["enum"]
        self.assertEqual(set(names), set(TRAIT_WORDS))

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

    def test_prepare_needs_no_key_at_all(self) -> None:
        with mock.patch.dict(os.environ, {API_KEY_ENV: ""}, clear=False):
            self.assertEqual(main(["intent", "prepare", BRIEF]), 0)


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
