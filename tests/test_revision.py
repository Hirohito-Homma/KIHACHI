from __future__ import annotations

import contextlib
import io
import json
import math
import shutil
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.revision import MIN_GAIN, RevisionLog, Round, describe, run_revision_loop
from test_music_brain import EXAMPLE

RATE = 8000
TAKE_SECONDS = 18.0
"""Long enough to carry a hole and several sections' worth of level changes.

Seventy seconds -- the SongSpec's own length -- cost about six seconds an
analysis, and these tests analyse fifteen times between them. None of them
assert on duration or on an absolute alignment score.
"""


def write_take(path: Path, *, seconds: float, gap: tuple[float, float] | None = None) -> None:
    """A plausible take: a tone bed with transients, optionally with a hole in it."""

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


class LoopTests(unittest.TestCase):
    def _project(self, root: Path, name: str = "song", **take) -> Path:
        project = root / name
        compose_project(EXAMPLE, project)
        write_take(project / "audio" / "ace-step-01.wav", **take)
        return project

    def _renderer(self, calls: list[Path], **take):
        def render(project: Path, source_audio: Path) -> None:
            # a repaint is defined against existing audio; the loop must supply it
            assert source_audio.is_file(), source_audio
            calls.append(project)
            write_take(project / "audio" / "ace-step-01.wav", **take)

        return render

    def test_the_loop_keeps_every_take_and_adopts_none(self) -> None:
        """Adoption is a listening decision; the score cannot hear."""

        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp), seconds=TAKE_SECONDS, gap=(12.0, 3.0))
            calls: list[Path] = []

            with contextlib.redirect_stdout(io.StringIO()):
                log = run_revision_loop(
                    project, self._renderer(calls, seconds=TAKE_SECONDS), rounds=2
                )

            self.assertGreaterEqual(len(log.rounds), 2)
            self.assertIsNone(log.adopted)
            self.assertIsNone(log.to_dict()["adopted"])
            for round_ in log.rounds:
                self.assertTrue((round_.project_dir / "audio_analysis.json").is_file())

    def test_the_source_project_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp), seconds=TAKE_SECONDS, gap=(12.0, 3.0))
            spec_before = (project / "song_spec.json").read_bytes()
            audio_before = (project / "audio" / "ace-step-01.wav").read_bytes()

            with contextlib.redirect_stdout(io.StringIO()):
                run_revision_loop(project, self._renderer([], seconds=TAKE_SECONDS), rounds=2)

            self.assertEqual((project / "song_spec.json").read_bytes(), spec_before)
            self.assertEqual(
                (project / "audio" / "ace-step-01.wav").read_bytes(), audio_before
            )

    def test_each_round_writes_a_new_project_beside_the_last(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp), seconds=TAKE_SECONDS, gap=(12.0, 3.0))
            calls: list[Path] = []

            with contextlib.redirect_stdout(io.StringIO()):
                run_revision_loop(project, self._renderer(calls, seconds=TAKE_SECONDS), rounds=2)

            self.assertTrue(calls)
            self.assertEqual(calls[0].name, "song-rev01")
            self.assertEqual(calls[0].parent, project.parent)

    def test_an_existing_round_directory_stops_the_loop_rather_than_replacing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp), seconds=TAKE_SECONDS, gap=(12.0, 3.0))
            occupied = project.parent / "song-rev01"
            occupied.mkdir()
            (occupied / "keep.txt").write_text("mine", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                log = run_revision_loop(project, self._renderer([], seconds=TAKE_SECONDS), rounds=2)

            self.assertIn("already exists", log.stopped_because)
            self.assertEqual(len(log.rounds), 1)
            self.assertEqual((occupied / "keep.txt").read_text(encoding="utf-8"), "mine")

    def test_a_round_that_gains_nothing_ends_the_loop(self) -> None:
        """Seed noise moves this score by tens of points; a fraction is not a win."""

        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp), seconds=TAKE_SECONDS)
            calls: list[Path] = []

            with contextlib.redirect_stdout(io.StringIO()):
                log = run_revision_loop(
                    project, self._renderer(calls, seconds=TAKE_SECONDS), rounds=5
                )

            self.assertLess(len(calls), 5)
            self.assertTrue(
                "floor" in log.stopped_because or "nothing worth" in log.stopped_because,
                log.stopped_because,
            )

    def test_rounds_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp), seconds=TAKE_SECONDS)
            with self.assertRaises(ValueError):
                run_revision_loop(project, self._renderer([], seconds=TAKE_SECONDS), rounds=0)

    def test_a_project_without_a_song_spec_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):
                run_revision_loop(Path(temp), self._renderer([], seconds=TAKE_SECONDS))


class RankingTests(unittest.TestCase):
    def _round(self, index, alignment, blocking=0, codes=()):
        return Round(
            index=index,
            project_dir=Path(f"/tmp/take{index}"),
            alignment=alignment,
            grade="aligned",
            blocking=blocking,
            warnings=0,
            defect_codes=tuple(codes),
            planned_action=None,
            audio_file=Path(f"/tmp/take{index}/audio/ace-step-01.wav"),
        )

    def test_a_take_with_a_hole_does_not_win_on_points(self) -> None:
        """88.69 "aligned" with 2.28 s of silence is not the best take."""

        log = RevisionLog(
            (
                self._round(0, 88.69, blocking=1, codes=("silent_gap",)),
                self._round(1, 35.38),
            ),
            "reached the round limit",
        )

        self.assertEqual([item.index for item in log.ranked()], [1, 0])

    def test_among_usable_takes_the_higher_score_leads(self) -> None:
        log = RevisionLog(
            (self._round(0, 60.0), self._round(1, 84.55), self._round(2, 72.0)),
            "reached the round limit",
        )

        self.assertEqual([item.index for item in log.ranked()], [1, 2, 0])

    def test_the_report_says_nothing_was_adopted(self) -> None:
        log = RevisionLog((self._round(0, 60.0),), "reached the round limit")

        text = "\n".join(describe(log))

        self.assertIn("Nothing adopted", text)
        self.assertIn("candidates", text)


if __name__ == "__main__":
    unittest.main()

class LogAndResumeTests(unittest.TestCase):
    """A round is a render, and a render is minutes. Losing one is expensive."""

    def _project(self, root: Path) -> Path:
        project = root / "song"
        compose_project(EXAMPLE, project)
        write_take(project / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS, gap=(12.0, 3.0))
        return project

    def _log(self, project: Path) -> dict:
        return json.loads((project / "revision_log.json").read_text(encoding="utf-8"))

    def _renderer(self):
        def render(destination: Path, source_audio: Path) -> None:
            write_take(destination / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)

        return render

    def test_the_log_is_written_by_the_loop_not_by_its_caller(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))

            def render(destination: Path, source_audio: Path) -> None:
                write_take(destination / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)

            with contextlib.redirect_stdout(io.StringIO()):
                run_revision_loop(project, render, rounds=1)

            log = self._log(project)
            self.assertEqual(log["execution_state"], "complete")
            self.assertTrue(log["rounds"])

    def test_a_failed_round_still_leaves_the_takes_that_were_measured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))

            def render(destination: Path, source_audio: Path) -> None:
                raise RuntimeError("the renderer timed out")

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    run_revision_loop(project, render, rounds=2)

            log = self._log(project)
            self.assertEqual(log["execution_state"], "failed")
            self.assertEqual(len(log["rounds"]), 1)
            self.assertIn("the renderer timed out", log["stopped_because"])

    def test_resume_measures_a_round_that_already_has_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            # A round that rendered before the run died.
            done = project.parent / "song-rev01"
            shutil.copytree(project, done)
            write_take(done / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)
            for stale in ("audio_analysis.json", "revision_log.json"):
                (done / stale).unlink(missing_ok=True)
            calls: list[Path] = []

            def render(destination: Path, source_audio: Path) -> None:
                calls.append(destination)
                write_take(destination / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)

            with contextlib.redirect_stdout(io.StringIO()):
                log = run_revision_loop(project, render, rounds=1, resume=True)

            self.assertEqual(len(log.rounds), 2)
            self.assertEqual(calls, [])
            self.assertEqual(log.rounds[1].project_dir, done)

    def test_resume_still_refuses_a_round_directory_with_no_audio(self) -> None:
        """A half-staged project is not a take, whatever the flag says."""

        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            (project.parent / "song-rev01").mkdir()

            with contextlib.redirect_stdout(io.StringIO()):
                log = run_revision_loop(
                    project, lambda *_: None, rounds=1, resume=True
                )

            self.assertIn("already exists", log.stopped_because)

    def test_a_stale_analysis_is_not_trusted(self) -> None:
        """The audio was re-rendered; judging it on the old numbers is wrong."""

        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            with contextlib.redirect_stdout(io.StringIO()):
                run_revision_loop(project, self._renderer(), rounds=1)
            first = json.loads(
                (project / "audio_analysis.json").read_text(encoding="utf-8")
            )["sha256"]

            # Same project, different take.
            write_take(project / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)
            shutil.rmtree(project.parent / "song-rev01", ignore_errors=True)
            with contextlib.redirect_stdout(io.StringIO()):
                run_revision_loop(project, self._renderer(), rounds=1)
            second = json.loads(
                (project / "audio_analysis.json").read_text(encoding="utf-8")
            )["sha256"]

            self.assertNotEqual(first, second)

    def test_an_analysis_that_still_describes_the_audio_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            with contextlib.redirect_stdout(io.StringIO()):
                run_revision_loop(project, self._renderer(), rounds=1)
            analysis_before = (project / "audio_analysis.json").read_bytes()

            shutil.rmtree(project.parent / "song-rev01", ignore_errors=True)
            with contextlib.redirect_stdout(io.StringIO()):
                run_revision_loop(project, self._renderer(), rounds=1)

            self.assertEqual(
                (project / "audio_analysis.json").read_bytes(), analysis_before
            )

