from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path

from kihachi_music_ai.tail_trim import (
    MIN_TRIM_SEC,
    plan_tail_trim,
    trim_project_tail,
)
from test_tail_guard import build_spec, write_tone_wav


def make_project(directory: Path, *, duration: float, music_end: float) -> Path:
    """A project holding one render that stops before its buffer does."""

    project = directory / "project"
    (project / "audio").mkdir(parents=True)
    spec = build_spec()
    (project / "song_spec.json").write_text(spec.to_json(), encoding="utf-8")
    write_tone_wav(
        project / "audio" / "ace-step-01.wav",
        duration=duration,
        music_end=music_end,
    )
    return project


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        return source.getnframes() / source.getframerate()


class TailTrimTest(unittest.TestCase):
    def test_plan_measures_the_tail_without_writing(self):
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw), duration=20.0, music_end=17.0)
            before = sorted(p.name for p in (project / "audio").iterdir())

            plan = plan_tail_trim(project)

            self.assertTrue(plan.worth_trimming)
            self.assertAlmostEqual(plan.music_end_sec, 17.0, delta=0.05)
            self.assertAlmostEqual(plan.kept_duration_sec, 17.25, delta=0.05)
            self.assertGreater(plan.removed_sec, MIN_TRIM_SEC)
            self.assertEqual(before, sorted(p.name for p in (project / "audio").iterdir()))
            self.assertFalse((project / "tail_trim.json").exists())

    def test_trim_writes_beside_the_render_and_never_over_it(self):
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw), duration=20.0, music_end=17.0)
            source = project / "audio" / "ace-step-01.wav"
            source_bytes = source.read_bytes()

            manifest = trim_project_tail(project)

            trimmed = project / "audio" / "ace-step-01.tail-trimmed.wav"
            self.assertTrue(trimmed.is_file())
            # The delivered take is the evidence for how the model behaved.
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertLess(wav_duration(trimmed), wav_duration(source))
            self.assertEqual(manifest["source_audio"], "audio/ace-step-01.wav")
            self.assertEqual(manifest["trimmed_audio"], "audio/ace-step-01.tail-trimmed.wav")

    def test_manifest_records_the_shortfall_against_the_song_grid(self):
        """Cutting the tail makes the take shorter than the spec asked for.

        That is a consequence a caller has to be able to see, not a rounding
        detail, so it is measured in bars as well as seconds.
        """
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw), duration=20.0, music_end=17.0)

            trim_project_tail(project)

            stored = json.loads((project / "tail_trim.json").read_text(encoding="utf-8"))
            plan = stored["plan"]
            self.assertGreater(plan["shortfall_sec"], 0.0)
            self.assertGreater(plan["shortfall_bars"], 0.0)
            self.assertEqual(stored["trim"]["kept_duration_sec"], plan["kept_duration_sec"])

    def test_a_short_tail_is_refused_rather_than_written(self):
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw), duration=20.0, music_end=19.9)

            plan = plan_tail_trim(project)
            self.assertFalse(plan.worth_trimming)
            with self.assertRaises(ValueError):
                trim_project_tail(project)
            self.assertFalse((project / "audio" / "ace-step-01.tail-trimmed.wav").exists())

    def test_silent_audio_is_refused_instead_of_being_trimmed_away(self):
        """A file that never rises above the threshold must not be "fixed" to nothing."""
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw), duration=20.0, music_end=0.0)

            plan = plan_tail_trim(project)

            self.assertFalse(plan.worth_trimming)
            self.assertEqual(plan.kept_duration_sec, plan.source_duration_sec)
            self.assertIn("no music to keep", plan.reason)
            with self.assertRaises(ValueError):
                trim_project_tail(project)

    def test_existing_output_is_refused_without_overwrite(self):
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw), duration=20.0, music_end=17.0)
            trim_project_tail(project)

            with self.assertRaises(FileExistsError):
                trim_project_tail(project)

            manifest = trim_project_tail(project, overwrite=True)
            self.assertEqual(manifest["trimmed_audio"], "audio/ace-step-01.tail-trimmed.wav")


if __name__ == "__main__":
    unittest.main()
