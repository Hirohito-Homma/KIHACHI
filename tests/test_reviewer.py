from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.repaint_planner import stage_repaint_project
from kihachi_music_ai.reviewer import review_project
from test_music_brain import EXAMPLE


def write_analysis(
    project: Path,
    *,
    tempo_delta: float,
    key_status: str,
    chord_match: float,
    chord_coverage: float,
    boundary_recall: float,
    energy_correlation: float,
) -> None:
    section_rows = [
        ("minimal_intro", 1, 0.25, 0.4008, 3),
        ("minimal_groove", 9, 0.44, 0.6447, 4),
        ("mutation_build", 17, 0.66, 0.7986, 2),
        ("psychedelic_drop", 25, 0.88, 0.7780, 3),
    ]
    chord_bars = []
    energy_bars = []
    for name, start_bar, target_energy, observed_energy, reliable_count in section_rows:
        for offset in range(8):
            bar = start_bar + offset
            reliable = offset < reliable_count
            chord_bars.append(
                {
                    "bar": bar,
                    "reliable": reliable,
                    "match": chord_match >= 0.5 if reliable else None,
                }
            )
            normalized_energy = observed_energy
            if bar == 32 and energy_correlation < 0.5:
                normalized_energy = 0.0
            energy_bars.append(
                {
                    "bar": bar,
                    "normalized_energy": normalized_energy,
                    "planned_section": name,
                    "target_energy": target_energy,
                }
            )
    payload = {
        "analysis_version": "0.2",
        "sha256": project.name * 4,
        "audio_file": "audio/ace-step-01.wav",
        "harmony": {
            "chords": {
                "confident_bar_coverage": chord_coverage,
                "bars": chord_bars,
            }
        },
        "sections": {
            "bars": energy_bars,
            "planned_sections": [
                {
                    "name": name,
                    "start_bar": start_bar,
                    "length_bars": 8,
                    "target_energy": target_energy,
                    "observed_mean_energy": observed_energy,
                }
                for name, start_bar, target_energy, observed_energy, _reliable in section_rows
            ],
            "planned_boundaries_after_bar": [8, 16, 24],
            "planned_boundary_matches": [
                {
                    "planned_after_bar": boundary,
                    "matched_within_one_bar": boundary_recall >= 0.67,
                }
                for boundary in (8, 16, 24)
            ],
        },
        "song_spec_comparison": {
            "duration_delta_sec": -0.018,
            "tempo_delta_bpm": tempo_delta,
            "target_key": "D# minor",
            "observed_key": "C# minor",
            "key_confidence": 0.12,
            "key_status": key_status,
            "progression_match_ratio": chord_match,
            "section_boundary_recall": boundary_recall,
            "section_energy_correlation": energy_correlation,
        },
    }
    (project / "audio_analysis.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class ReviewerTests(unittest.TestCase):
    def test_review_compares_alignment_and_writes_revision_without_mutating_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = root / "base"
            target = root / "lora"
            compose_project(EXAMPLE, baseline)
            compose_project(EXAMPLE, target)
            write_analysis(
                baseline,
                tempo_delta=0.336,
                key_status="low_confidence",
                chord_match=0.1053,
                chord_coverage=0.5938,
                boundary_recall=0.0,
                energy_correlation=-0.5169,
            )
            write_analysis(
                target,
                tempo_delta=-0.322,
                key_status="low_confidence",
                chord_match=0.0,
                chord_coverage=0.375,
                boundary_recall=1.0,
                energy_correlation=0.4696,
            )
            spec_before = (target / "song_spec.json").read_bytes()
            analysis_before = (target / "audio_analysis.json").read_bytes()

            manifest = review_project(target, against=baseline)

            self.assertTrue(manifest.review_file.is_file())
            self.assertTrue(manifest.revision_prompt_file.is_file())
            self.assertTrue(manifest.repaint_plan_file.is_file())
            self.assertGreater(
                manifest.review["comparison"]["target_alignment_score"],
                manifest.review["comparison"]["baseline_alignment_score"],
            )
            self.assertEqual(manifest.review["comparison"]["preferred_song_spec_alignment"], "target")
            # Audio-only weights: dropping the two components that measured nothing
            # (key sat at a constant 0.350, chords at the detector floor) leaves a
            # score made entirely of things audio can actually establish.
            self.assertEqual(manifest.review["alignment"]["grade"], "aligned")
            codes = {item["code"] for item in manifest.review["findings"]}
            self.assertIn("chord_progression_alignment", codes)
            self.assertIn("section_energy_alignment", codes)
            self.assertNotIn("section_boundary_alignment", codes)
            revision_prompt = manifest.revision_prompt_file.read_text(encoding="utf-8")
            self.assertIn("D#m - B - F# - C#", revision_prompt)
            self.assertIn("bass pedals on D#", revision_prompt)
            self.assertIn("Reduce harmonic masking", revision_prompt)
            repaint_plan = json.loads(manifest.repaint_plan_file.read_text(encoding="utf-8"))
            self.assertEqual(repaint_plan["selection"]["section_name"], "psychedelic_drop")
            self.assertEqual(repaint_plan["selection"]["start_bar"], 25)
            self.assertEqual(repaint_plan["selection"]["end_bar"], 32)
            self.assertFalse(repaint_plan["safety"]["render_started"])
            self.assertIn("silent tail", repaint_plan["revision_prompt"])
            self.assertEqual((target / "song_spec.json").read_bytes(), spec_before)
            self.assertEqual((target / "audio_analysis.json").read_bytes(), analysis_before)
            with self.assertRaises(FileExistsError):
                review_project(target, against=baseline)

    def test_review_cli_reports_score_and_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = root / "base"
            target = root / "target"
            compose_project(EXAMPLE, baseline)
            compose_project(EXAMPLE, target)
            for project, recall in ((baseline, 0.0), (target, 1.0)):
                write_analysis(
                    project,
                    tempo_delta=0.2,
                    key_status="match",
                    chord_match=0.8,
                    chord_coverage=0.8,
                    boundary_recall=recall,
                    energy_correlation=0.8,
                )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(["review", str(target), "--against", str(baseline)])
            self.assertEqual(status, 0)
            self.assertIn("alignment score", stdout.getvalue())
            self.assertIn("preferred alignment target", stdout.getvalue())

    def test_review_can_preserve_authored_revision_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            write_analysis(
                project,
                tempo_delta=0.2,
                key_status="low_confidence",
                chord_match=0.0,
                chord_coverage=0.3,
                boundary_recall=1.0,
                energy_correlation=0.4,
            )
            revision_file = project / "revision_prompt.txt"
            authored = "Keep this exact authored revision.\n"
            revision_file.write_text(authored, encoding="utf-8")

            manifest = review_project(project, preserve_revision_prompt=True)

            self.assertTrue(manifest.review_file.is_file())
            self.assertEqual(revision_file.read_text(encoding="utf-8"), authored)
            self.assertNotEqual(manifest.review["revision_prompt"] + "\n", authored)

    def test_repaint_plan_prepares_ace_step_request_without_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            write_analysis(
                project,
                tempo_delta=-0.3,
                key_status="low_confidence",
                chord_match=0.0,
                chord_coverage=0.375,
                boundary_recall=1.0,
                energy_correlation=0.4696,
            )
            manifest = review_project(project)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "ace-step",
                        "prepare",
                        str(project),
                        "--repaint-plan",
                        "repaint_plan.json",
                    ]
                )

            self.assertEqual(status, 0)
            request_path = project / "ace_step_repaint_revision_request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["task_type"], "repaint")
            self.assertEqual(request["repainting_start"], 52.364)
            # The window reaches the final bar, so it extends into the tail guard
            # (2 bars = 4.364 s at 110 BPM) instead of stopping on the song grid.
            self.assertEqual(request["repainting_end"], 74.182)
            self.assertEqual(request["audio_duration"], 74.182)
            self.assertEqual(request["repaint_strength"], 0.65)
            self.assertTrue(request["prompt"].startswith("Revision constraints"))
            self.assertIn("psychedelic_drop", request["prompt"])
            self.assertIn("repaint bars 25:32", stdout.getvalue())
            self.assertTrue(manifest.repaint_plan_file.is_file())
            self.assertFalse((project / "audio").exists())

    def test_stage_repaint_project_copies_design_but_not_source_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            output = root / "repaint-output"
            compose_project(EXAMPLE, source)
            write_analysis(
                source,
                tempo_delta=-0.3,
                key_status="low_confidence",
                chord_match=0.0,
                chord_coverage=0.375,
                boundary_recall=1.0,
                energy_correlation=0.4696,
            )
            audio = source / "audio" / "ace-step-01.wav"
            audio.parent.mkdir()
            audio_bytes = b"RIFFreviewed-source-audio"
            audio.write_bytes(audio_bytes)
            analysis_path = source / "audio_analysis.json"
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            analysis["sha256"] = hashlib.sha256(audio_bytes).hexdigest()
            analysis_path.write_text(
                json.dumps(analysis, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            review_project(source)
            source_spec_before = (source / "song_spec.json").read_bytes()
            source_audio_before = audio.read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "ace-step",
                        "stage-repaint",
                        str(source),
                        str(output),
                    ]
                )

            self.assertEqual(status, 0)
            self.assertEqual((output / "song_spec.json").read_bytes(), source_spec_before)
            self.assertTrue((output / "repaint_plan.json").is_file())
            self.assertEqual(
                (output / "applied_repaint_plan.json").read_bytes(),
                (output / "repaint_plan.json").read_bytes(),
            )
            self.assertTrue((output / "revision_prompt.txt").is_file())
            self.assertTrue((output / "repaint_stage.json").is_file())
            self.assertFalse((output / "audio").exists())
            self.assertFalse((output / "audio_analysis.json").exists())
            self.assertEqual(audio.read_bytes(), source_audio_before)
            self.assertIn("verified, not copied", stdout.getvalue())
            with self.assertRaises(FileExistsError):
                stage_repaint_project(source, output)

    def test_review_requires_existing_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            with self.assertRaises(FileNotFoundError):
                review_project(project)


if __name__ == "__main__":
    unittest.main()
