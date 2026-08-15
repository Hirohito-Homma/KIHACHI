from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.select import (
    MARGIN_FLOOR,
    SPREAD_FLOOR,
    build_shortlist,
    decide_command,
    describe,
    write_shortlist,
)
from test_report import make_project


def set_components(project: Path, **scores: float) -> None:
    """Pin this take's audio components, leaving the rest of the review alone.

    The shortlist is about how takes differ, and a synthetic tone analyzed
    twice does not differ. Writing the numbers is what makes a spread testable.
    """

    path = project / "generation_review.json"
    review = json.loads(path.read_text(encoding="utf-8"))
    components = review["alignment"]["components"]
    for name, score in scores.items():
        components[name]["score"] = score
    path.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")


class DimensionTests(unittest.TestCase):
    def test_a_dimension_that_never_varies_cannot_decide_anything(self) -> None:
        """The `key`-at-0.350 failure, as a rule instead of a later discovery."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            set_components(first, tempo=0.90, section_energy=0.50)
            set_components(second, tempo=0.90, section_energy=0.90)

            shortlist = build_shortlist(first, [second])

            standing = {item["name"]: item["standing"] for item in shortlist["dimensions"]}
            self.assertEqual(standing["tempo"], "constant")
            self.assertEqual(standing["section_energy"], "deciding")
            self.assertEqual(shortlist["recommended"], "second")
            self.assertNotIn("tempo", shortlist["ranking"][0]["contributions"])

    def test_a_dimension_that_barely_varies_is_reported_and_not_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            nudge = SPREAD_FLOOR / 2.0
            set_components(first, tempo=0.90, section_energy=0.50)
            set_components(second, tempo=0.90 + nudge, section_energy=0.90)

            shortlist = build_shortlist(first, [second])

            standing = {item["name"]: item["standing"] for item in shortlist["dimensions"]}
            self.assertEqual(standing["tempo"], "flat")
            self.assertNotIn("tempo", shortlist["ranking"][0]["contributions"])

    def test_weights_are_renormalised_over_the_dimensions_that_survive(self) -> None:
        """Otherwise a set with one live dimension scores everyone near zero."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            set_components(first, section_energy=1.0)
            set_components(second, section_energy=0.0)

            shortlist = build_shortlist(first, [second])

            self.assertAlmostEqual(shortlist["ranking"][0]["score"], 100.0, places=2)
            self.assertAlmostEqual(shortlist["ranking"][1]["score"], 0.0, places=2)
            self.assertEqual(shortlist["deciding_dimension_count"], 1)


class QuantisedDimensionTests(unittest.TestCase):
    """`section_boundaries` is a recall over planned boundaries, so it steps.

    Its smallest non-zero spread is one step -- 0.333 for a four-part
    arrangement, six times `SPREAD_FLOOR`. The flatness check cannot catch it,
    so the step is reported instead of being hidden inside a score.
    """

    def dimension(self, shortlist: dict, name: str) -> dict:
        return next(item for item in shortlist["dimensions"] if item["name"] == name)

    def test_the_step_is_read_from_this_song_s_arrangement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            planned = json.loads(
                (first / "audio_analysis.json").read_text(encoding="utf-8")
            )["sections"]["planned_boundaries_after_bar"]
            set_components(first, section_boundaries=0.0)
            set_components(second, section_boundaries=1.0 / len(planned))

            shortlist = build_shortlist(first, [second])

            boundaries = self.dimension(shortlist, "section_boundaries")
            self.assertAlmostEqual(boundaries["quantum"], 1.0 / len(planned), places=3)
            self.assertEqual(boundaries["evidence"], "single_step")
            self.assertIn("narrow evidence", "\n".join(describe(shortlist)))

    def test_a_gap_of_several_steps_is_not_called_narrow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            set_components(first, section_boundaries=0.0)
            set_components(second, section_boundaries=1.0)

            shortlist = build_shortlist(first, [second])

            self.assertEqual(
                self.dimension(shortlist, "section_boundaries")["evidence"], "multi_step"
            )
            self.assertNotIn("narrow evidence", "\n".join(describe(shortlist)))

    def test_a_dimension_that_is_not_a_count_has_no_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            set_components(first, tempo=0.20)
            set_components(second, tempo=0.90)

            shortlist = build_shortlist(first, [second])

            tempo = self.dimension(shortlist, "tempo")
            self.assertIsNone(tempo["quantum"])
            self.assertIsNone(tempo["evidence"])


class VerdictTests(unittest.TestCase):
    def test_a_lead_under_the_floor_is_not_called_a_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            set_components(first, section_energy=0.50, tempo=0.20)
            set_components(second, section_energy=0.51, tempo=0.20)

            shortlist = build_shortlist(first, [second])

            self.assertEqual(shortlist["verdict"], "too_close_to_call")
            self.assertLess(shortlist["margin"], MARGIN_FLOOR)
            self.assertCountEqual(shortlist["tied_with"], ["first", "second"])

    def test_a_tie_does_not_prefill_the_decide_command(self) -> None:
        """Handing back a runnable choice would undo the sentence above it."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            set_components(first, section_energy=0.50)
            set_components(second, section_energy=0.51)

            command = decide_command(build_shortlist(first, [second]))

            self.assertIn("--selected <take>", command)

    def test_a_clear_lead_is_named_and_offered_to_decide(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            set_components(first, section_energy=0.20)
            set_components(second, section_energy=0.95)

            shortlist = build_shortlist(first, [second])

            self.assertEqual(shortlist["verdict"], "recommended")
            self.assertEqual(shortlist["recommended"], "second")
            self.assertIn(str(second), decide_command(shortlist))

    def test_nothing_is_recommended_when_no_dimension_varies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")

            shortlist = build_shortlist(first, [second])

            self.assertEqual(shortlist["verdict"], "too_close_to_call")
            self.assertEqual(shortlist["verdict_reason"], "no_deciding_dimension")
            self.assertIsNone(shortlist["recommended"])
            self.assertCountEqual(shortlist["tied_with"], ["first", "second"])
            self.assertIn(
                "nothing measurable separates these takes",
                "\n".join(describe(shortlist)),
            )


class GateTests(unittest.TestCase):
    def test_a_take_of_a_different_design_is_not_ranked_against_this_one(self) -> None:
        """ADR-0005's identical-SongSpec rule, applied to more than two takes."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            other = make_project(root, "other")
            spec_path = other / "song_spec.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["song"]["bpm"] = spec["song"]["bpm"] + 7
            spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

            shortlist = build_shortlist(first, [other])

            self.assertEqual([item["name"] for item in shortlist["ranking"]], ["first"])
            self.assertEqual(shortlist["excluded"][0]["reason"], "different_song_spec")

    def test_a_take_with_a_hole_is_excluded_rather_than_ranked_last(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            holed = make_project(root, "holed", gap=(12.0, 3.0))
            set_components(first, section_energy=0.20)
            set_components(holed, section_energy=0.95)

            shortlist = build_shortlist(first, [holed])

            self.assertEqual([item["name"] for item in shortlist["ranking"]], ["first"])
            self.assertEqual(shortlist["excluded"][0]["reason"], "blocking_defect")

    def test_an_unscanned_take_is_excluded_rather_than_assumed_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            unscanned = make_project(root, "unscanned")
            (unscanned / "material_defects.json").unlink()
            review_path = unscanned / "generation_review.json"
            review = json.loads(review_path.read_text(encoding="utf-8"))
            review.pop("material_defects", None)
            review_path.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")

            shortlist = build_shortlist(first, [unscanned])

            self.assertEqual(shortlist["excluded"][0]["reason"], "not_scanned")


class TieBreakTests(unittest.TestCase):
    def test_the_cleaner_of_two_inseparable_takes_is_named_but_not_scored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            set_components(first, section_energy=0.50)
            set_components(second, section_energy=0.50)
            # A warning-level finding: material enough to mention, not to score.
            path = second / "generation_review.json"
            review = json.loads(path.read_text(encoding="utf-8"))
            review["material_defects"]["findings"].append(
                {"code": "discontinuity", "severity": "warning", "detail": "a click"}
            )
            path.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")

            shortlist = build_shortlist(first, [second])

            self.assertEqual(shortlist["tie_break"]["name"], "first")
            self.assertEqual(shortlist["ranking"][0]["score"], shortlist["ranking"][1]["score"])


class MixedTrimTests(unittest.TestCase):
    """Found the hard way: four re-rolls trimmed, one not, `duration` spread 1.000."""

    def trim(self, project: Path) -> None:
        source = project / "audio" / "ace-step-01.wav"
        trimmed = source.with_name("ace-step-01.tail-trimmed.wav")
        trimmed.write_bytes(source.read_bytes())
        analysis_path = project / "audio_analysis.json"
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        analysis["audio_file"] = "audio/ace-step-01.tail-trimmed.wav"
        analysis_path.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")

    def test_a_half_trimmed_set_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            self.trim(second)

            shortlist = build_shortlist(first, [second])

            self.assertEqual(shortlist["mixed_tail_trim"]["trimmed"], ["second"])
            self.assertEqual(shortlist["mixed_tail_trim"]["untrimmed"], ["first"])
            self.assertIn("confounded", "\n".join(describe(shortlist)))

    def test_trimming_all_of_them_is_not_a_confound(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            self.trim(first)
            self.trim(second)

            shortlist = build_shortlist(first, [second])

            self.assertIsNone(shortlist["mixed_tail_trim"])
            self.assertNotIn("confounded", "\n".join(describe(shortlist)))


class OutputTests(unittest.TestCase):
    def test_the_file_says_what_it_did_not_judge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")
            set_components(first, section_energy=0.20)
            set_components(second, section_energy=0.95)

            manifest = write_shortlist(first, [second])
            written = json.loads(manifest.shortlist_file.read_text(encoding="utf-8"))

            self.assertTrue(any("timbre" in item for item in written["not_judged"]))
            self.assertIn("adopts_nothing", written["scope"])

    def test_an_existing_shortlist_is_not_replaced_without_being_asked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            write_shortlist(first)

            with self.assertRaises(FileExistsError):
                write_shortlist(first)

            write_shortlist(first, overwrite=True)

    def test_the_command_ranks_and_writes_nothing_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "first")
            second = make_project(root, "second")

            exit_code = main(["shortlist", str(first), "--also", str(second)])

            self.assertEqual(exit_code, 0)
            self.assertFalse((first / "take_shortlist.json").exists())


if __name__ == "__main__":
    unittest.main()
