from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.agreement import (
    AGREE,
    MODEL_ONLY,
    POLARITY,
    RULES_ONLY,
    SCOPE,
    STRENGTH,
    compare_readings,
    describe,
)
from kihachi_music_ai.cli import main


def reading(brief: str, traits: list[dict], unmapped: list[str] | None = None) -> dict:
    return {
        "intent_reading_version": "0.1",
        "model": "claude-opus-5",
        "brief": brief,
        "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        "traits": traits,
        "unmapped": unmapped or [],
    }


def status_of(comparison: dict, name: str) -> str:
    for row in comparison["traits"]:
        if row["trait"] == name:
            return row["status"]
        continue
    raise AssertionError(f"{name} is not in this comparison")


class ScopeComparisonTests(unittest.TestCase):
    """Where a trait lands is a thing the two readers can disagree about."""

    BRIEF = "テクノ。後半は手数を多く。"

    def test_the_same_trait_in_a_different_place_is_a_disagreement(self) -> None:
        comparison = compare_readings(
            reading(
                self.BRIEF,
                [{"name": "busy", "polarity": 1, "strength": 1.0, "evidence": "手数を多く"}],
            )
        )

        self.assertEqual(status_of(comparison, "busy"), SCOPE)

    def test_agreeing_on_the_place_is_agreement(self) -> None:
        comparison = compare_readings(
            reading(
                self.BRIEF,
                [
                    {
                        "name": "busy",
                        "polarity": 1,
                        "strength": 1.0,
                        "evidence": "手数を多く",
                        "scope": "second_half",
                    }
                ],
            )
        )

        self.assertEqual(status_of(comparison, "busy"), AGREE)

    def test_a_span_word_the_rules_used_counts_as_contested(self) -> None:
        """The hole this was written for: a span word is nobody's evidence.

        The first scoped brief compared clean while the model was still filing
        「後半は」 under `unmapped`, because the contest check only looked at the
        phrases traits cited.
        """

        comparison = compare_readings(
            reading(
                self.BRIEF,
                [{"name": "busy", "polarity": 1, "strength": 1.0, "evidence": "手数を多く"}],
                unmapped=["後半は"],
            )
        )

        self.assertEqual(comparison["contested_unmapped"], ["後半は"])

    def test_a_place_named_around_an_unscopable_trait_is_a_real_gap(self) -> None:
        """「後半は暗く」 -- darkness is one number for the whole song, so the
        rules drop that placement and the model is right to call it unread."""

        brief = "テクノ。後半は暗く、終盤はスカスカに。"
        comparison = compare_readings(
            reading(
                brief,
                [
                    {"name": "dark", "polarity": 1, "strength": 1.0, "evidence": "暗く"},
                    {
                        "name": "sparse",
                        "polarity": 1,
                        "strength": 1.0,
                        "evidence": "スカスカ",
                        "scope": "second_half",
                    },
                ],
                unmapped=["後半"],
            )
        )

        self.assertEqual(comparison["contested_unmapped"], [])


class ComparisonTests(unittest.TestCase):
    def test_the_two_readers_agreeing_is_reported_as_agreement(self) -> None:
        brief = "サイケに。"
        comparison = compare_readings(
            reading(brief, [{"name": "psychedelic", "polarity": 1, "strength": 1.0, "evidence": "サイケ"}])
        )

        self.assertEqual(status_of(comparison, "psychedelic"), AGREE)
        self.assertEqual(comparison["disagreements"], 0)

    def test_opposite_answers_sort_first_and_are_named(self) -> None:
        """The failure `intent.py` exists to prevent, now visible across readers."""

        brief = "サイケじゃなくて、スラップで。"
        comparison = compare_readings(
            reading(
                brief,
                [
                    {"name": "psychedelic", "polarity": 1, "strength": 1.0, "evidence": "サイケ"},
                    {"name": "slap", "polarity": 1, "strength": 1.0, "evidence": "スラップ"},
                ],
            )
        )

        self.assertEqual(status_of(comparison, "psychedelic"), POLARITY)
        self.assertEqual(comparison["traits"][0]["trait"], "psychedelic")

    def test_a_degree_difference_is_a_smaller_thing_than_a_polarity_one(self) -> None:
        brief = "かなりサイケに。"
        comparison = compare_readings(
            reading(brief, [{"name": "psychedelic", "polarity": 1, "strength": 1.0, "evidence": "サイケ"}])
        )

        self.assertEqual(status_of(comparison, "psychedelic"), STRENGTH)

    def test_each_reader_can_be_alone(self) -> None:
        brief = "サイケに。"
        model_extra = compare_readings(
            reading(
                brief,
                [
                    {"name": "psychedelic", "polarity": 1, "strength": 1.0, "evidence": "サイケ"},
                    {"name": "dub", "polarity": 1, "strength": 1.0, "evidence": "サイケ"},
                ],
            )
        )
        rules_alone = compare_readings(reading(brief, []))

        self.assertEqual(status_of(model_extra, "dub"), MODEL_ONLY)
        self.assertEqual(status_of(rules_alone, "psychedelic"), RULES_ONLY)

    def test_unmapped_is_contested_only_when_the_rules_read_that_phrase(self) -> None:
        """Clause-level agreement called two different statements the same thing.

        「暗くて疾走感のある」 is one clause. The rules read `暗` in it and read
        nothing in `疾走感`, so only the first phrase is contested.
        """

        brief = "暗くて疾走感のあるテクノ。"
        comparison = compare_readings(
            reading(brief, [], unmapped=["暗くて", "疾走感のある"])
        )

        self.assertEqual(comparison["contested_unmapped"], ["暗くて"])

    def test_a_brief_that_does_not_match_its_own_hash_is_refused(self) -> None:
        record = reading("サイケに。", [])
        record["brief"] = "まったく別のブリーフ。"

        with self.assertRaises(ValueError):
            compare_readings(record)

    def test_a_reading_with_no_brief_cannot_be_compared(self) -> None:
        with self.assertRaises(ValueError):
            compare_readings({"traits": [], "unmapped": []})


class ReportTests(unittest.TestCase):
    def test_agreement_says_so_rather_than_printing_nothing(self) -> None:
        lines = "\n".join(
            describe(
                compare_readings(
                    reading(
                        "サイケに。",
                        [{"name": "psychedelic", "polarity": 1, "strength": 1.0, "evidence": "サイケ"}],
                    )
                )
            )
        )

        self.assertIn("agree on every trait", lines)

    def test_a_disagreement_shows_both_sides(self) -> None:
        lines = "\n".join(
            describe(
                compare_readings(
                    reading(
                        "サイケじゃない。",
                        [{"name": "psychedelic", "polarity": 1, "strength": 1.0, "evidence": "サイケ"}],
                    )
                )
            )
        )

        self.assertIn("polarity_differs", lines)
        self.assertIn("model:", lines)
        self.assertIn("rules:", lines)
        self.assertIn("neither reader is authoritative", lines)


class CommandTests(unittest.TestCase):
    def test_the_command_reads_a_file_or_the_directory_holding_it(self) -> None:
        record = reading(
            "サイケに。", [{"name": "psychedelic", "polarity": 1, "strength": 1.0, "evidence": "サイケ"}]
        )
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            path = project / "intent_reading.json"
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

            self.assertEqual(main(["compare-readings", str(path)]), 0)
            self.assertEqual(main(["compare-readings", str(project)]), 0)

    def test_a_missing_reading_is_named_rather_than_traced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(main(["compare-readings", str(Path(temp))]), 2)


if __name__ == "__main__":
    unittest.main()
