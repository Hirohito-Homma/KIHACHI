from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.midi import inspect_midi
from kihachi_music_ai.pipeline import ARTIFACT_NAMES, compose_project
from test_music_brain import EXAMPLE


class PipelineTests(unittest.TestCase):
    def test_pipeline_generates_all_requested_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "mutation-signal"
            manifest = compose_project(EXAMPLE, output)
            # EXAMPLE asks for a vocoder, so the project writes that part too
            self.assertEqual(
                tuple(path.name for path in manifest.files),
                ARTIFACT_NAMES + ("vocoder.mid",),
            )
            self.assertTrue(all(path.is_file() for path in manifest.files))
            for name in ("bass.mid", "drums.mid", "chords.mid"):
                inspect_midi(output / name)
            prompt = (output / "prompt.txt").read_text(encoding="utf-8")
            self.assertIn("110 BPM", prompt)
            self.assertIn("D# minor", prompt)
            self.assertIn("dark robotic phrases", prompt)

    def test_pipeline_refuses_implicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "mutation-signal"
            compose_project(EXAMPLE, output)
            marker = output / "user-notes.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                compose_project(EXAMPLE, output)
            compose_project(EXAMPLE, output, overwrite=True)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_cli_reports_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(["compose", EXAMPLE, "--output", str(Path(temp) / "out")])
            self.assertEqual(status, 0)
            self.assertIn("song_spec.json", stdout.getvalue())
            self.assertIn("prompt.txt", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

