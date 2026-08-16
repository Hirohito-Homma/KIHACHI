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
