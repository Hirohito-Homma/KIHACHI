from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.stems import (
    DEFAULT_MODEL,
    MANIFEST_VERSION,
    STEM_NAMES,
    import_stems,
    load_stem_manifest,
    plan_separation,
    stem_paths,
)


def write_wav(
    path: Path,
    *,
    seconds: float = 2.0,
    frequency: float = 220.0,
    sample_rate: int = 8000,
    channels: int = 1,
) -> None:
    frames = round(sample_rate * seconds)
    samples = array("h")
    for index in range(frames):
        value = round(0.4 * math.sin(2.0 * math.pi * frequency * index / sample_rate) * 32767.0)
        for _ in range(channels):
            samples.append(value)
    if sys.byteorder != "little":
        samples.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.tobytes())


def build_project(temp: str, **stem_overrides: dict) -> Path:
    """A project with a render and a full set of stems beside it."""

    project = Path(temp) / "project"
    write_wav(project / "audio" / "ace-step-01.wav")
    for name, path in zip(STEM_NAMES, stem_paths(project)):
        write_wav(path, **stem_overrides.get(name, {}))
    return project


class SeparationPlanTests(unittest.TestCase):
    def test_the_plan_writes_nothing_and_names_the_contract_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            write_wav(project / "audio" / "ace-step-01.wav")

            plan = plan_separation(project)

            self.assertEqual(plan.model, DEFAULT_MODEL)
            self.assertIn("demucs", plan.command)
            self.assertEqual(
                [path.name for path in plan.expected_stems],
                [f"{name}.wav" for name in STEM_NAMES],
            )
            # The point of this command: it plans, it does not separate.
            self.assertFalse((project / "audio" / "stems").exists())
            self.assertFalse((project / "stem_manifest.json").exists())

    def test_a_missing_render_is_refused_before_a_command_is_offered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):
                plan_separation(Path(temp) / "project")


class ImportStemsTests(unittest.TestCase):
    def test_a_full_set_is_recorded_with_hashes_and_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = build_project(temp)

            manifest = import_stems(project)

            self.assertEqual(manifest["manifest_version"], MANIFEST_VERSION)
            self.assertEqual([entry["stem"] for entry in manifest["stems"]], list(STEM_NAMES))
            self.assertEqual(manifest["source_audio"]["path"], "audio/ace-step-01.wav")
            for entry in manifest["stems"]:
                self.assertEqual(len(entry["sha256"]), 64)
                self.assertEqual(entry["path"], f"audio/stems/{entry['stem']}.wav")
            written = load_stem_manifest(project / "stem_manifest.json")
            self.assertEqual(written["stems"], manifest["stems"])

    def test_the_render_and_the_stems_are_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = build_project(temp)
            before = {
                path: path.read_bytes()
                for path in (project / "audio").rglob("*.wav")
            }

            import_stems(project)

            for path, payload in before.items():
                self.assertEqual(path.read_bytes(), payload)

    def test_a_missing_stem_names_itself_and_points_at_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = build_project(temp)
            (project / "audio" / "stems" / "bass.wav").unlink()

            with self.assertRaises(FileNotFoundError) as caught:
                import_stems(project)

            self.assertIn("bass.wav", str(caught.exception))
            self.assertIn("stems prepare", str(caught.exception))

    def test_a_stem_of_the_wrong_length_is_refused(self) -> None:
        # A drifting stem would quietly misalign every bar-grid measurement,
        # which is worse than failing here.
        with tempfile.TemporaryDirectory() as temp:
            project = build_project(temp, bass={"seconds": 1.5})

            with self.assertRaises(ValueError) as caught:
                import_stems(project)

            self.assertIn("bass", str(caught.exception))

    def test_a_stem_at_another_sample_rate_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = build_project(temp, other={"sample_rate": 16000})

            with self.assertRaises(ValueError) as caught:
                import_stems(project)

            self.assertIn("other", str(caught.exception))

    def test_an_existing_manifest_survives_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = build_project(temp)
            import_stems(project)
            first = (project / "stem_manifest.json").read_text(encoding="utf-8")

            with self.assertRaises(FileExistsError):
                import_stems(project)
            self.assertEqual((project / "stem_manifest.json").read_text(encoding="utf-8"), first)

            import_stems(project, overwrite=True)

    def test_a_manifest_from_another_version_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "stem_manifest.json"
            path.write_text(json.dumps({"manifest_version": "stem-manifest-v0"}), encoding="utf-8")

            with self.assertRaises(ValueError):
                load_stem_manifest(path)


class StemsCliTests(unittest.TestCase):
    def test_prepare_prints_the_command_and_the_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            write_wav(project / "audio" / "ace-step-01.wav")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["stems", "prepare", str(project)])

            self.assertEqual(status, 0)
            printed = output.getvalue()
            self.assertIn("demucs", printed)
            self.assertIn("stems import", printed)
            self.assertIn("nothing written", printed)

    def test_import_reports_every_stem_and_writes_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = build_project(temp)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["stems", "import", str(project)])

            self.assertEqual(status, 0)
            printed = output.getvalue()
            for name in STEM_NAMES:
                self.assertIn(name, printed)
            self.assertTrue((project / "stem_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
