"""VS4 — Human Take Selection & Preference Memory."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kihachi_music_ai.analyzer import analyze_project
from kihachi_music_ai.cli import build_parser, main
from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.preference_memory import load_preference_memory
from kihachi_music_ai.project_artifacts import managed_midi_names
from kihachi_music_ai.reviewer import review_project
from kihachi_music_ai.revision import (
    RevisionLog,
    Round,
    adopt_revision,
    compare_rounds,
    load_revision_log,
    revision_log_from_dict,
    run_revision_loop,
)
from kihachi_music_ai.models import SongSpec
from test_music_brain import EXAMPLE
from test_revision import TAKE_SECONDS, write_take

RATE = 8000


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_fingerprints(project: Path) -> dict[str, str]:
    spec = SongSpec.from_json((project / "song_spec.json").read_text(encoding="utf-8"))
    names = ("song_spec.json", *managed_midi_names(spec))
    audio = project / "audio" / "ace-step-01.wav"
    fingerprints = {name: _sha256(project / name) for name in names}
    if audio.is_file():
        fingerprints[str(audio.relative_to(project))] = _sha256(audio)
    return fingerprints


class HumanTakeSelectionTests(unittest.TestCase):
    def _project_with_revisions(self, root: Path, *, rounds: int = 1) -> Path:
        project = root / "song"
        compose_project(EXAMPLE, project)
        write_take(
            project / "audio" / "ace-step-01.wav",
            seconds=TAKE_SECONDS,
            gap=(12.0, 3.0),
        )

        def render(destination: Path, source_audio: Path) -> None:
            write_take(destination / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)

        with contextlib.redirect_stdout(io.StringIO()):
            run_revision_loop(project, render, rounds=rounds)
        return project

    def test_explicit_human_adoption_records_selected_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            before = json.loads((project / "revision_log.json").read_text(encoding="utf-8"))
            self.assertIsNone(before["adopted"])

            manifest = adopt_revision(
                project,
                1,
                reason="better groove and cleaner intro",
                tags=("groove", "intro"),
                selected_at="2026-09-03T01:00:00Z",
            )

            self.assertFalse(manifest.unchanged)
            self.assertEqual(manifest.adoption.round, 1)
            self.assertEqual(manifest.adoption.selection_mode, "human")
            self.assertEqual(manifest.adoption.reason, "better groove and cleaner intro")
            self.assertEqual(manifest.adoption.tags, ("groove", "intro"))
            after = json.loads((project / "revision_log.json").read_text(encoding="utf-8"))
            self.assertEqual(after["adopted"]["round"], 1)
            self.assertEqual(after["adopted"]["selection_mode"], "human")
            self.assertEqual(after["adopted"]["project"], "song-rev01")

    def test_adoption_preserves_source_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            before = _artifact_fingerprints(project)
            adopt_revision(project, 1, reason="keep source intact")
            self.assertEqual(_artifact_fingerprints(project), before)

    def test_adoption_preserves_all_revision_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            rev01 = project.parent / "song-rev01"
            before_source = (project / "audio" / "ace-step-01.wav").read_bytes()
            before_rev = (rev01 / "audio" / "ace-step-01.wav").read_bytes()
            adopt_revision(project, 1, reason="preserve audio")
            self.assertEqual(
                (project / "audio" / "ace-step-01.wav").read_bytes(),
                before_source,
            )
            self.assertEqual(
                (rev01 / "audio" / "ace-step-01.wav").read_bytes(),
                before_rev,
            )

    def test_nonexistent_round_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            with self.assertRaisesRegex(ValueError, "does not exist"):
                adopt_revision(project, 99, reason="missing")
            self.assertIsNone(
                json.loads((project / "revision_log.json").read_text())["adopted"]
            )

    def test_missing_candidate_audio_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            rev01 = project.parent / "song-rev01"
            for wav in (rev01 / "audio").glob("*.wav"):
                wav.unlink()
            with self.assertRaises(FileNotFoundError):
                adopt_revision(project, 1, reason="no audio")

    def test_invalid_candidate_provenance_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            rev01 = project.parent / "song-rev01"
            stage = json.loads((rev01 / "repaint_stage.json").read_text(encoding="utf-8"))
            stage["source_project"] = "foreign-song"
            (rev01 / "repaint_stage.json").write_text(
                json.dumps(stage, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "lineage|provenance|foreign"):
                adopt_revision(project, 1, reason="bad provenance")

    def test_existing_null_adoption_logs_remain_compatible(self) -> None:
        payload = {
            "revision_log_version": "0.1",
            "execution_state": "complete",
            "stopped_because": "reached the round limit",
            "rounds": [
                {
                    "index": 0,
                    "project": "song",
                    "alignment": 80.0,
                    "grade": "aligned",
                    "blocking": 0,
                    "warnings": 0,
                    "defects": [],
                    "planned_action": None,
                    "audio_file": "song/audio/ace-step-01.wav",
                    "usable": True,
                }
            ],
            "ranking": [0],
            "adopted": None,
            "adoption_note": "Nothing was adopted.",
        }
        log = revision_log_from_dict(payload)
        self.assertIsNone(log.adopted)
        self.assertIsNone(log.to_dict()["adopted"])

    def test_revision_loop_never_auto_adopts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp), rounds=2)
            log = load_revision_log(project)
            self.assertIsNone(log.adopted)
            self.assertIsNone(log.to_dict()["adopted"])

    def test_better_scoring_round_is_not_auto_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "song"
            compose_project(EXAMPLE, project)
            write_take(project / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)

            initial = Round(
                index=0,
                project_dir=project,
                alignment=70.0,
                grade="partial",
                blocking=1,
                warnings=0,
                defect_codes=("discontinuity",),
                planned_action="repaint bars 1:4",
                audio_file=project / "audio" / "ace-step-01.wav",
            )
            better = Round(
                index=1,
                project_dir=project.parent / "song-rev01",
                alignment=95.0,
                grade="aligned",
                blocking=0,
                warnings=0,
                defect_codes=(),
                planned_action=None,
                audio_file=project.parent / "song-rev01" / "audio" / "ace-step-01.wav",
            )

            def fake_measure(project_dir: Path, index: int) -> Round:
                return initial if index == 0 else better

            def fake_render(destination: Path, source_audio: Path) -> None:
                (destination / "audio").mkdir(parents=True, exist_ok=True)
                write_take(destination / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)

            with patch("kihachi_music_ai.revision._measure", side_effect=fake_measure):
                with patch("kihachi_music_ai.revision.stage_repaint_project", lambda *_: None):
                    with contextlib.redirect_stdout(io.StringIO()):
                        log = run_revision_loop(project, fake_render, rounds=1)

            self.assertEqual(log.ranked()[0].index, 1)
            self.assertGreater(log.rounds[1].alignment, log.rounds[0].alignment)
            self.assertIsNone(log.adopted)
            self.assertIsNone(
                json.loads((project / "revision_log.json").read_text())["adopted"]
            )

    def test_same_adoption_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            first = adopt_revision(
                project,
                1,
                reason="pocket",
                tags=("groove",),
                selected_at="2026-09-03T01:00:00Z",
            )
            memory_before = load_preference_memory(project)
            second = adopt_revision(
                project,
                1,
                reason="pocket",
                tags=("groove",),
            )
            self.assertTrue(second.unchanged)
            self.assertFalse(second.preference_recorded)
            self.assertEqual(second.adoption.round, first.adoption.round)
            self.assertEqual(
                len(load_preference_memory(project).entries),
                len(memory_before.entries),
            )

    def test_reselection_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp), rounds=2)
            adopt_revision(project, 1, reason="first listen", tags=("groove",))
            second = adopt_revision(project, 0, reason="changed mind", tags=("source",))
            self.assertEqual(second.adoption.round, 0)
            self.assertFalse(second.unchanged)
            memory = load_preference_memory(project)
            self.assertEqual(len(memory.entries), 2)
            self.assertEqual(memory.entries[0].selected_round, 1)
            self.assertEqual(memory.entries[1].selected_round, 0)
            log = load_revision_log(project)
            self.assertEqual(log.adopted.round, 0)

    def test_preference_memory_records_selected_and_rejected_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            adopt_revision(project, 1, reason="selected over source")
            memory = load_preference_memory(project)
            self.assertEqual(len(memory.entries), 1)
            entry = memory.entries[0]
            self.assertEqual(entry.selected_round, 1)
            self.assertIn(0, entry.candidate_rounds)
            self.assertIn(1, entry.candidate_rounds)
            self.assertEqual(entry.rejected_rounds, (0,))
            self.assertIn("alignment_delta", entry.comparison)
            self.assertIn("blocking_delta", entry.comparison)

    def test_preference_reason_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            adopt_revision(project, 1, reason="rev01 has a stronger bass pocket")
            entry = load_preference_memory(project).entries[0]
            self.assertEqual(entry.reason, "rev01 has a stronger bass pocket")
            log = load_revision_log(project)
            self.assertEqual(log.adopted.reason, "rev01 has a stronger bass pocket")

    def test_preference_tags_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            adopt_revision(project, 1, tags=("groove", "bass", "intro"))
            entry = load_preference_memory(project).entries[0]
            self.assertEqual(entry.tags, ("groove", "bass", "intro"))
            self.assertEqual(
                load_revision_log(project).adopted.tags,
                ("groove", "bass", "intro"),
            )

    def test_preference_memory_does_not_change_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            review_project(project, overwrite=True)
            before = json.loads(
                (project / "generation_review.json").read_text(encoding="utf-8")
            )["alignment"]
            adopt_revision(
                project,
                1,
                reason="should not move scores",
                tags=("scoring-check",),
            )
            review_project(project, overwrite=True)
            after = json.loads(
                (project / "generation_review.json").read_text(encoding="utf-8")
            )["alignment"]
            self.assertEqual(before, after)
            memory = load_preference_memory(project)
            self.assertFalse(memory.to_dict()["affects_scoring"])
            self.assertFalse(memory.to_dict()["affects_generation"])
            # Preference memory is evidence only — Analyzer / alignment code
            # must not import it.
            import kihachi_music_ai.analyzer as analyzer_mod
            import kihachi_music_ai.alignment as alignment_mod
            import kihachi_music_ai.critic as critic_mod

            for module in (analyzer_mod, alignment_mod, critic_mod):
                self.assertNotIn("preference_memory", getattr(module, "__file__", ""))
                source = Path(module.__file__).read_text(encoding="utf-8")
                self.assertNotIn("preference_memory", source)
                self.assertNotIn("adopt_revision", source)

    def test_extra_managed_midi_survives_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            before = {
                name: _sha256(project / name)
                for name in managed_midi_names(
                    SongSpec.from_json((project / "song_spec.json").read_text())
                )
            }
            self.assertIn("vocoder.mid", before)
            adopt_revision(project, 1, reason="midi intact")
            after = {
                name: _sha256(project / name)
                for name in managed_midi_names(
                    SongSpec.from_json((project / "song_spec.json").read_text())
                )
            }
            self.assertEqual(before, after)

    def test_cli_adopt_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            status = main(
                [
                    "adopt",
                    str(project),
                    "--round",
                    "1",
                    "--reason",
                    "better groove and cleaner intro",
                    "--tag",
                    "groove",
                    "--tag",
                    "intro",
                ]
            )
            self.assertEqual(status, 0)
            log = load_revision_log(project)
            self.assertEqual(log.adopted.round, 1)
            self.assertEqual(log.adopted.tags, ("groove", "intro"))
            memory = load_preference_memory(project)
            self.assertEqual(memory.entries[0].reason, "better groove and cleaner intro")

    def test_cli_revisions_lists_candidates_without_adopting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                status = main(["revisions", str(project)])
            self.assertEqual(status, 0)
            text = buffer.getvalue()
            self.assertIn("Round 0", text)
            self.assertIn("adopted: null", text)
            self.assertIsNone(load_revision_log(project).adopted)

    def test_parser_accepts_adopt_and_revisions(self) -> None:
        parser = build_parser()
        adopt_args = parser.parse_args(
            ["adopt", "projects/song", "--round", "1", "--reason", "x", "--tag", "groove"]
        )
        self.assertEqual(adopt_args.command, "adopt")
        self.assertEqual(adopt_args.round_number, 1)
        self.assertEqual(adopt_args.tags, ["groove"])
        revisions_args = parser.parse_args(["revisions", "projects/song"])
        self.assertEqual(revisions_args.command, "revisions")

    def test_pipelines_leave_adopted_null_without_human_api(self) -> None:
        """No automatic path may populate adopted — only adopt_revision may."""

        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            log = load_revision_log(project)
            self.assertIsNone(log.adopted)

            # compare / describe never write adoption
            _ = compare_rounds(log.rounds[0], log.rounds[-1])
            self.assertIsNone(load_revision_log(project).adopted)

            analyze_project(project, overwrite=True)
            review_project(project, overwrite=True)
            self.assertIsNone(
                json.loads((project / "revision_log.json").read_text())["adopted"]
            )


if __name__ == "__main__":
    unittest.main()
