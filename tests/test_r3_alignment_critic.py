"""R3 alignment / reviewer / critic reconstruction regression tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.alignment import ALIGNMENT_WEIGHTS, audio_alignment
from kihachi_music_ai.critic import critique_evidence
from kihachi_music_ai.midi_review import review_midi_tracks, review_project_midi
from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.review_contract import (
    EvidenceStatus,
    ReviewPhase,
    collect_generation_review_evidence,
    collect_midi_review_evidence,
    detect_review_phase,
    require_audio_analysis,
)
from kihachi_music_ai.reviewer import review_project
from test_music_brain import EXAMPLE
from test_reviewer import write_analysis


class R3PhaseAwareValidationTests(unittest.TestCase):
    def test_midi_only_project_runs_midi_review_without_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            self.assertFalse((project / "audio_analysis.json").exists())

            manifest = review_project_midi(project)

            self.assertEqual(detect_review_phase(project), ReviewPhase.MIDI_ONLY)
            self.assertIn("vocoder", manifest.review["tracks"])
            self.assertIn("density", manifest.review)
            self.assertEqual(manifest.review["alignment"]["grade"], "aligned")

    def test_generation_review_requires_audio_analysis_at_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)

            with self.assertRaisesRegex(
                FileNotFoundError,
                r"generation review requires audio analysis artifact: audio_analysis\.json",
            ):
                review_project(project)

            with self.assertRaisesRegex(
                FileNotFoundError,
                r"generation review requires audio analysis artifact",
            ):
                require_audio_analysis(project, context="generation review")

    def test_audio_present_path_includes_audio_alignment(self) -> None:
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

            self.assertEqual(manifest.review["review_phase"], "generation_review")
            self.assertIn("alignment", manifest.review)
            self.assertIn("midi_alignment", manifest.review)
            self.assertIn("critic", manifest.review)
            self.assertEqual(
                manifest.review["critic"]["evidence_status"]["audio_analysis"],
                "evaluated",
            )

    def test_unavailable_future_phase_does_not_become_audio_failure(self) -> None:
        from kihachi_music_ai.models import SongSpec

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            manifest = review_project_midi(project)
            spec = SongSpec.from_json((project / "song_spec.json").read_text(encoding="utf-8"))
            critique = critique_evidence(
                spec,
                phase=ReviewPhase.MIDI_ONLY,
                analysis=None,
                audio_alignment=None,
                midi_review=manifest.review,
                defects=None,
                audio_analysis_status=EvidenceStatus.NOT_APPLICABLE,
                midi_status=EvidenceStatus.EVALUATED,
                defects_status=EvidenceStatus.NOT_APPLICABLE,
            )
            codes = {item["code"] for item in critique["findings"]}
            self.assertNotIn("chord_progression_alignment", codes)
            self.assertNotIn("section_energy_alignment", codes)
            self.assertNotIn("duration_alignment", codes)


class R3ExtraPartTests(unittest.TestCase):
    def test_extra_vocoder_part_survives_reviewer_to_critic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            write_analysis(
                project,
                tempo_delta=0.2,
                key_status="match",
                chord_match=0.8,
                chord_coverage=0.8,
                boundary_recall=1.0,
                energy_correlation=0.8,
            )

            manifest = review_project(project)
            tracks = set(manifest.review["midi_alignment"]["tracks"])
            density_parts = {entry["part"] for entry in manifest.review["midi_alignment"]["density"]["entries"]}

            self.assertIn("vocoder", tracks)
            self.assertIn("vocoder", density_parts)


class R3BoundaryTests(unittest.TestCase):
    def test_missing_managed_midi_fails_at_midi_review_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            (project / "vocoder.mid").unlink()

            with self.assertRaisesRegex(
                FileNotFoundError,
                r"MIDI review project missing managed MIDI artifact\(s\): vocoder\.mid",
            ):
                review_project_midi(project)

    def test_generation_review_without_midi_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            write_analysis(
                project,
                tempo_delta=0.2,
                key_status="match",
                chord_match=0.8,
                chord_coverage=0.8,
                boundary_recall=1.0,
                energy_correlation=0.8,
            )
            from kihachi_music_ai.models import SongSpec

            spec = SongSpec.from_json((project / "song_spec.json").read_text(encoding="utf-8"))
            for part in spec.parts():
                (project / f"{part}.mid").unlink(missing_ok=True)

            manifest = review_project(project)

            self.assertNotIn("midi_alignment", manifest.review)
            self.assertEqual(
                manifest.review["critic"]["evidence_status"]["midi"],
                "unavailable",
            )


class R3ReviewerCriticSeparationTests(unittest.TestCase):
    def test_critic_consumes_precomputed_alignment_without_rescoring(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            write_analysis(
                project,
                tempo_delta=0.2,
                key_status="match",
                chord_match=0.8,
                chord_coverage=0.8,
                boundary_recall=1.0,
                energy_correlation=0.8,
            )
            evidence = collect_generation_review_evidence(project)
            assert evidence.analysis is not None
            alignment = audio_alignment(evidence.analysis)
            from kihachi_music_ai.midi import read_midi
            from kihachi_music_ai.midi_review import review_midi_tracks

            assert evidence.midi_paths is not None
            tracks = {
                name: read_midi(path).notes
                for name, path in zip(evidence.spec.parts(), evidence.midi_paths, strict=True)
            }
            midi_review = review_midi_tracks(evidence.spec, tracks)
            critique = critique_evidence(
                evidence.spec,
                phase=evidence.phase,
                analysis=evidence.analysis,
                audio_alignment=alignment,
                midi_review=midi_review,
                defects=evidence.defects,
                audio_analysis_status=evidence.audio_analysis_status,
                midi_status=evidence.midi_status,
                defects_status=evidence.defects_status,
            )
            self.assertIs(critique["audio_alignment"], alignment)

    def test_density_diagnostics_preserved_in_midi_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            evidence = collect_midi_review_evidence(project)
            from kihachi_music_ai.midi import read_midi

            assert evidence.midi_paths is not None
            tracks = {
                name: read_midi(path).notes
                for name, path in zip(evidence.spec.parts(), evidence.midi_paths, strict=True)
            }
            review = review_midi_tracks(evidence.spec, tracks)
            density = review["density"]

            self.assertEqual(density["scope"], "section_part_onset_density_diagnostic")
            self.assertEqual(density["boundary_convention"], "[start, end)")
            parts = {entry["part"] for entry in density["entries"]}
            self.assertIn("vocoder", parts)


class R3DeterminismTests(unittest.TestCase):
    def test_same_inputs_produce_identical_review(self) -> None:
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

            first = review_project(project, overwrite=True)
            second = review_project(project, overwrite=True)

            for key in ("alignment", "findings", "midi_alignment", "critic"):
                self.assertEqual(first.review[key], second.review[key])


class R3LegacyCompatibilityTests(unittest.TestCase):
    def test_alignment_weights_unchanged(self) -> None:
        self.assertEqual(
            ALIGNMENT_WEIGHTS,
            {
                "duration": 0.15,
                "tempo": 0.30,
                "section_boundaries": 0.25,
                "section_energy": 0.30,
            },
        )

    def test_legacy_reviewer_aliases_still_work(self) -> None:
        from kihachi_music_ai.reviewer import _alignment, _balance_findings, _defect_findings

        analysis = {
            "song_spec_comparison": {
                "duration_delta_sec": 0.0,
                "tempo_delta_bpm": 0.0,
                "section_boundary_recall": 1.0,
                "section_energy_correlation": 1.0,
            }
        }
        self.assertEqual(_alignment(analysis)["grade"], "aligned")
        self.assertEqual(_balance_findings(None), [])
        self.assertEqual(_defect_findings(None), [])


class R3ContractScoreTests(unittest.TestCase):
    def test_same_analysis_inputs_produce_same_alignment_score(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            write_analysis(
                project,
                tempo_delta=-0.322,
                key_status="low_confidence",
                chord_match=0.0,
                chord_coverage=0.375,
                boundary_recall=1.0,
                energy_correlation=0.4696,
            )
            analysis = json.loads((project / "audio_analysis.json").read_text(encoding="utf-8"))
            direct = audio_alignment(analysis)
            manifest = review_project(project)

            self.assertEqual(manifest.review["alignment"]["score"], direct["score"])
            self.assertEqual(manifest.review["alignment"]["grade"], direct["grade"])
            self.assertEqual(
                manifest.review["alignment"]["components"],
                direct["components"],
            )


if __name__ == "__main__":
    unittest.main()
