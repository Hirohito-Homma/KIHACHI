from __future__ import annotations

import json
import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.analyzer import analyze_project
from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.report import Candidate, build_report, load_candidate, rank
from kihachi_music_ai.reviewer import review_project
from test_music_brain import EXAMPLE

RATE = 8000


def write_take(path: Path, *, seconds: float = 70.0, gap: tuple[float, float] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = array("h")
    for frame in range(int(seconds * RATE)):
        second = frame / RATE
        if gap is not None and gap[0] <= second < gap[0] + gap[1]:
            samples.extend((0, 0))
            continue
        value = 0.12 * math.sin(2 * math.pi * 110 * frame / RATE)
        phase = frame % 4000
        if phase < 240:
            value += 0.8 * math.exp(-phase / 45.0) * math.sin(2 * math.pi * 200 * frame / RATE)
        sample = max(-32767, min(32767, int(value * 32767)))
        samples.extend((sample, sample))
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(2)
        sink.setsampwidth(2)
        sink.setframerate(RATE)
        sink.writeframes(samples.tobytes())


def make_project(root: Path, name: str, **take) -> Path:
    project = root / name
    compose_project(EXAMPLE, project)
    write_take(project / "audio" / "ace-step-01.wav", **take)
    analyze_project(project)
    review_project(project, overwrite=True)
    return project


def fake(name: str, alignment: float, *, scanned=True, blocking=0) -> Candidate:
    findings = (
        ({"code": "silent_gap", "severity": "blocking", "detail": "a hole"},) if blocking else ()
    )
    return Candidate(
        project_dir=Path("/tmp") / name,
        name=name,
        alignment=alignment,
        grade="aligned",
        defects=findings,
        scanned=scanned,
        measurements={},
        audio_file=None,
        playable=None,
        duration_sec=70.0,
        peaks=(),
        section_marks=(),
        defect_marks=(),
    )


class RankingTests(unittest.TestCase):
    def test_a_take_with_a_hole_loses_to_a_lower_scoring_clean_one(self) -> None:
        ordered = rank([fake("holed", 88.69, blocking=1), fake("clean", 35.38)])

        self.assertEqual([item.name for item in ordered], ["clean", "holed"])

    def test_never_measured_is_not_the_same_as_no_defects(self) -> None:
        """Reporting an unscanned take as clean would launder a missing check."""

        ordered = rank([fake("unscanned", 90.0, scanned=False), fake("checked", 40.0)])

        self.assertEqual([item.name for item in ordered], ["checked", "unscanned"])
        self.assertFalse(ordered[1].usable)
        self.assertEqual(ordered[1].standing, 1)


class CandidateTests(unittest.TestCase):
    def test_a_take_reports_its_defects_and_where_they_are(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = make_project(Path(temp), "holed", gap=(60.0, 3.0))

            candidate = load_candidate(project)

            self.assertTrue(candidate.scanned)
            self.assertIn("silent_gap", [d["code"] for d in candidate.defects])
            at, code, severity = candidate.defect_marks[0]
            self.assertEqual(code, "silent_gap")
            self.assertEqual(severity, "blocking")
            self.assertAlmostEqual(at, 60.0, delta=1.0)

    def test_section_marks_survive_a_take_that_was_never_scanned(self) -> None:
        """Length used to come only from the scan, so unscanned takes lost every mark."""

        with tempfile.TemporaryDirectory() as temp:
            project = make_project(Path(temp), "song")
            (project / "material_defects.json").unlink()
            review_project(project, overwrite=True)

            candidate = load_candidate(project)

            self.assertFalse(candidate.scanned)
            self.assertGreater(candidate.duration_sec, 60.0)
            self.assertTrue(candidate.section_marks)

    def test_the_untrimmed_render_is_never_offered_for_listening(self) -> None:
        """It is the material the tail guard cut down -- playing it shows the bug."""

        with tempfile.TemporaryDirectory() as temp:
            project = make_project(Path(temp), "song")
            audio = project / "audio"
            write_take(audio / "ace-step-01.untrimmed.wav", seconds=74.0)

            candidate = load_candidate(project)

            self.assertIsNotNone(candidate.playable)
            self.assertNotIn(".untrimmed.", candidate.playable.name)

    def test_a_project_without_a_review_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):
                load_candidate(Path(temp))


class PageTests(unittest.TestCase):
    def test_takes_in_sibling_directories_get_a_path_the_page_can_follow(self) -> None:
        """Rounds are written beside their source, so links have to walk upwards."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_project(root, "song")
            second = make_project(root, "song-rev01")

            page = build_report(
                [load_candidate(first), load_candidate(second)], base_dir=first
            )

            self.assertIn('src="../song-rev01/audio/ace-step-01.wav"', page)
            self.assertIn('src="audio/ace-step-01.wav"', page)
            self.assertNotIn("file://", page)

    def test_the_page_says_nothing_was_adopted(self) -> None:
        page = build_report([fake("a", 60.0)], base_dir=Path("/tmp"))

        self.assertIn("Nothing is adopted here; choose by listening.", " ".join(page.split()))

    def test_audio_is_linked_not_embedded(self) -> None:
        """A take is 13 MB; three of them inside a page is not a page."""

        with tempfile.TemporaryDirectory() as temp:
            project = make_project(Path(temp), "song")

            page = build_report([load_candidate(project)], base_dir=project)

            self.assertNotIn("data:audio", page)
            self.assertLess(len(page.encode("utf-8")), 400_000)

    def test_a_take_name_cannot_inject_markup(self) -> None:
        page = build_report([fake("<script>alert(1)</script>", 50.0)], base_dir=Path("/tmp"))

        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
