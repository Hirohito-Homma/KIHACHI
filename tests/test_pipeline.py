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

    def test_legacy_core_three_project_keeps_its_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = compose_project("Tech House。", Path(temp) / "legacy")

            self.assertEqual(
                tuple(path.name for path in manifest.files),
                ARTIFACT_NAMES,
            )
            self.assertTrue(all(path.is_file() for path in manifest.files))

    def test_overwrite_removes_only_midi_managed_by_the_previous_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "mutation-signal"
            compose_project(EXAMPLE, output)
            imported = output / "reference.mid"
            imported.write_bytes(b"user-owned reference MIDI")

            compose_project("Tech House。vocoderなしで。", output, overwrite=True)

            self.assertFalse((output / "vocoder.mid").exists())
            self.assertEqual(imported.read_bytes(), b"user-owned reference MIDI")
            self.assertTrue(all((output / name).is_file() for name in ARTIFACT_NAMES))

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

