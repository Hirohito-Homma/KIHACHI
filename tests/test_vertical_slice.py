"""VS1 minimum executable vertical slice integration tests."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.pipeline import ARTIFACT_NAMES, compose_project, run_vertical_slice
from kihachi_music_ai.project_artifacts import managed_midi_names
from kihachi_music_ai.review_contract import ReviewPhase, detect_review_phase
from kihachi_music_ai.reviewer import review_project_midi_only
from test_music_brain import EXAMPLE

VS1_BRIEF = (
    "Mutation Funk、DUB、Tech House。110 BPM、D#m。"
    "ファンキーなスラップベース。シンコペーション。4つ打ちキック。"
    "タイトなスネア。ダブコード。Vocoder。前半はミニマル、後半はエネルギッシュ。"
)
LEGACY_BRIEF = "Tech House。"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_review_payload(review: dict) -> dict:
    return {
        "review_phase": review["review_phase"],
        "midi_alignment": review["midi_alignment"],
        "critic": review["critic"],
        "findings": review["findings"],
        "revision_prompt": review["revision_prompt"],
    }


class VerticalSliceHappyPathTests(unittest.TestCase):
    def test_natural_language_brief_completes_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            manifest = run_vertical_slice(VS1_BRIEF, output, seed=8)

            self.assertTrue(manifest.compose.output_dir.is_dir())
            self.assertTrue(manifest.review.review_file.is_file())
            self.assertTrue(manifest.review.revision_prompt_file.is_file())
            self.assertFalse((output / "audio_analysis.json").exists())

    def test_resulting_song_spec_is_valid_and_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            manifest = run_vertical_slice(VS1_BRIEF, output, seed=8)
            spec = manifest.compose.spec
            raw = (output / "song_spec.json").read_text(encoding="utf-8")

            self.assertEqual(SongSpec.from_json(raw), spec)
            self.assertEqual(spec.song.bpm, 110.0)
            self.assertEqual(spec.song.key, "D# minor")
            self.assertGreater(len(spec.arrangement), 0)

    def test_all_managed_midi_from_spec_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            manifest = run_vertical_slice(VS1_BRIEF, output, seed=8)
            spec = manifest.compose.spec
            names = managed_midi_names(spec)

            self.assertEqual(names, tuple(f"{part}.mid" for part in spec.parts()))
            for name in names:
                self.assertTrue((output / name).is_file(), name)

    def test_extra_vocoder_part_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            manifest = run_vertical_slice(VS1_BRIEF, output, seed=8)
            spec = manifest.compose.spec

            self.assertIn("vocoder", spec.parts())
            self.assertTrue((output / "vocoder.mid").is_file())
            tracks = set(manifest.review.review["midi_alignment"]["tracks"])
            self.assertIn("vocoder", tracks)

    def test_prompt_compiler_artifacts_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            run_vertical_slice(VS1_BRIEF, output, seed=8)

            prompt_txt = (output / "prompt.txt").read_text(encoding="utf-8")
            prompt_json = json.loads((output / "prompt.json").read_text(encoding="utf-8"))
            self.assertIn("110 BPM", prompt_txt)
            self.assertIn("D# minor", prompt_txt)
            self.assertIn("prompt", prompt_json)

    def test_density_evidence_is_in_review_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            manifest = run_vertical_slice(VS1_BRIEF, output, seed=8)
            density = manifest.review.review["midi_alignment"]["density"]

            self.assertEqual(density["scope"], "section_part_onset_density_diagnostic")
            self.assertGreater(len(density["entries"]), 0)
            row = next(entry for entry in density["entries"] if entry["part"] == "vocoder")
            for key in ("section", "part", "expected_density", "observed_density", "deviation"):
                self.assertIn(key, row)

    def test_midi_only_phase_does_not_require_wav(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            manifest = run_vertical_slice(VS1_BRIEF, output, seed=8)

            self.assertEqual(detect_review_phase(output), ReviewPhase.MIDI_ONLY)
            self.assertEqual(manifest.review.review["review_phase"], "midi_only")
            critic = manifest.review.review["critic"]
            self.assertEqual(critic["evidence_status"]["audio_analysis"], "not_applicable")
            self.assertEqual(critic["evidence_status"]["midi"], "evaluated")
            audio_codes = {
                "duration_alignment",
                "tempo_alignment",
                "chord_progression_alignment",
                "section_energy_alignment",
            }
            codes = {item["code"] for item in manifest.review.review["findings"]}
            self.assertFalse(codes & audio_codes)

    def test_critic_uses_structured_midi_review_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            manifest = run_vertical_slice(VS1_BRIEF, output, seed=8)
            review = manifest.review.review

            self.assertIn("critic", review)
            self.assertIn("midi_alignment", review)
            self.assertIn("findings", review)
            self.assertIn("revision_prompt", review)
            self.assertIn("density", review["midi_alignment"])


class VerticalSliceDeterminismTests(unittest.TestCase):
    def test_identical_inputs_produce_identical_canonical_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            first_manifest = run_vertical_slice(VS1_BRIEF, first, seed=8)
            second_manifest = run_vertical_slice(VS1_BRIEF, second, seed=8)
            spec = first_manifest.compose.spec

            self.assertEqual(
                (first / "song_spec.json").read_text(encoding="utf-8"),
                (second / "song_spec.json").read_text(encoding="utf-8"),
            )
            for name in managed_midi_names(spec):
                self.assertEqual(_sha256(first / name), _sha256(second / name), name)
            for name in ("prompt.txt", "prompt.json"):
                self.assertEqual(
                    (first / name).read_bytes(),
                    (second / name).read_bytes(),
                    name,
                )
            self.assertEqual(
                _canonical_review_payload(
                    json.loads((first / "generation_review.json").read_text(encoding="utf-8"))
                ),
                _canonical_review_payload(
                    json.loads((second / "generation_review.json").read_text(encoding="utf-8"))
                ),
            )


class VerticalSliceSafetyTests(unittest.TestCase):
    def test_existing_output_is_not_silently_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            run_vertical_slice(VS1_BRIEF, output, seed=8)
            marker = output / "user-notes.txt"
            marker.write_text("keep", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                run_vertical_slice(VS1_BRIEF, output, seed=8)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertTrue((output / "generation_review.json").is_file())

    def test_missing_managed_midi_fails_at_review_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            compose_project(EXAMPLE, output)
            (output / "vocoder.mid").unlink()

            with self.assertRaisesRegex(
                FileNotFoundError,
                r"MIDI review project missing managed MIDI artifact\(s\): vocoder\.mid",
            ):
                review_project_midi_only(output)


class VerticalSliceLegacyTests(unittest.TestCase):
    def test_legacy_brief_completes_without_extra_part(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "legacy"
            manifest = run_vertical_slice(LEGACY_BRIEF, output, seed=8)
            spec = manifest.compose.spec

            self.assertEqual(
                tuple(path.name for path in manifest.compose.files),
                ARTIFACT_NAMES,
            )
            self.assertNotIn("vocoder", spec.parts())
            self.assertEqual(manifest.review.review["review_phase"], "midi_only")


class VerticalSliceCliTests(unittest.TestCase):
    def test_local_slice_cli_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "cli-vs1"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "local-slice",
                        VS1_BRIEF,
                        "--output",
                        str(output),
                        "--seed",
                        "8",
                    ]
                )

            self.assertEqual(status, 0)
            text = stdout.getvalue()
            self.assertIn("Local vertical slice:", text)
            self.assertIn("generation_review.json", text)
            self.assertTrue((output / "generation_review.json").is_file())
            self.assertTrue((output / "vocoder.mid").is_file())


if __name__ == "__main__":
    unittest.main()
