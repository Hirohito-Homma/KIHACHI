"""VS5 — Adopted Take → Ableton Handoff."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.ableton_handoff import (
    AbletonHandoffError,
    ableton_handoff_path,
    build_ableton_handoff,
    resolve_adopted_take,
)
from kihachi_music_ai.cli import build_parser, main
from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.preference_memory import load_preference_memory
from kihachi_music_ai.project_artifacts import managed_midi_names
from kihachi_music_ai.repaint_planner import song_spec_sha256
from kihachi_music_ai.revision import (
    adopt_revision,
    load_revision_log,
    run_revision_loop,
)
from test_music_brain import EXAMPLE
from test_revision import TAKE_SECONDS, write_take


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_fingerprints(project: Path) -> dict[str, str]:
    spec = SongSpec.from_json((project / "song_spec.json").read_text(encoding="utf-8"))
    names = ("song_spec.json", *managed_midi_names(spec))
    fingerprints = {name: _sha256(project / name) for name in names}
    audio = project / "audio" / "ace-step-01.wav"
    if audio.is_file():
        fingerprints[str(audio.relative_to(project))] = _sha256(audio)
    return fingerprints


class AbletonHandoffTests(unittest.TestCase):
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

    def _adopted(self, root: Path, round_number: int, *, rounds: int = 1) -> Path:
        project = self._project_with_revisions(root, rounds=rounds)
        adopt_revision(project, round_number, reason=f"adopt round {round_number}")
        return project

    def test_adopted_round_0_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 0)
            manifest = build_ableton_handoff(project)
            take = manifest.adopted_take
            self.assertEqual(take.adopted_round, 0)
            self.assertEqual(take.adopted_project.resolve(), project.resolve())
            self.assertTrue(manifest.handoff_file.is_file())
            self.assertEqual(manifest.handoff["adopted_round"], 0)
            self.assertEqual(manifest.handoff["adopted_project"], "song")

    def test_adopted_revision_round_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            manifest = build_ableton_handoff(project)
            take = manifest.adopted_take
            self.assertEqual(take.adopted_round, 1)
            self.assertEqual(take.adopted_project.name, "song-rev01")
            self.assertEqual(manifest.handoff["adopted_project"], "song-rev01")
            self.assertTrue(
                (take.adopted_project / "arrangement_plan.json").is_file()
            )

    def test_no_adoption_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            with self.assertRaisesRegex(AbletonHandoffError, "No human-adopted take"):
                resolve_adopted_take(project)
            self.assertFalse(ableton_handoff_path(project).exists())

    def test_nonexistent_adopted_round_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            payload = json.loads((project / "revision_log.json").read_text())
            payload["adopted"]["round"] = 99
            payload["adopted"]["project"] = "song-rev99"
            (project / "revision_log.json").write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(AbletonHandoffError, "no longer exists|does not exist"):
                resolve_adopted_take(project)

    def test_missing_adopted_project_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            rev01 = project.parent / "song-rev01"
            # Rename away so path resolution cannot recover the directory.
            rev01.rename(project.parent / "song-rev01-gone")
            with self.assertRaisesRegex(AbletonHandoffError, "missing|not found"):
                resolve_adopted_take(project)

    def test_missing_adopted_wav_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            rev01 = project.parent / "song-rev01"
            for wav in (rev01 / "audio").glob("*.wav"):
                wav.unlink()
            with self.assertRaisesRegex(AbletonHandoffError, "WAV is missing|audio"):
                resolve_adopted_take(project)

    def test_modified_adopted_wav_sha_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            rev01 = project.parent / "song-rev01"
            audio = rev01 / "audio" / "ace-step-01.wav"
            audio.write_bytes(audio.read_bytes() + b"\x00tampered")
            with self.assertRaisesRegex(AbletonHandoffError, "SHA-256 mismatch"):
                resolve_adopted_take(project)

    def test_song_spec_lineage_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            rev01 = project.parent / "song-rev01"
            foreign_dir = Path(temp) / "foreign"
            compose_project(
                "Ambient drone, 70 BPM, C major. Soft pads only.",
                foreign_dir,
            )
            (rev01 / "song_spec.json").write_bytes(
                (foreign_dir / "song_spec.json").read_bytes()
            )
            with self.assertRaisesRegex(AbletonHandoffError, "lineage|SongSpec"):
                resolve_adopted_take(project)

    def test_missing_managed_midi_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            rev01 = project.parent / "song-rev01"
            (rev01 / "vocoder.mid").unlink()
            with self.assertRaisesRegex(AbletonHandoffError, "managed MIDI"):
                resolve_adopted_take(project)

    def test_extra_midi_vocoder_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            take = resolve_adopted_take(project)
            parts = {item.part for item in take.midi}
            self.assertIn("vocoder", parts)
            self.assertTrue(any(item.path.name == "vocoder.mid" for item in take.midi))
            handoff = build_ableton_handoff(project).handoff
            self.assertIn("vocoder", [row["part"] for row in handoff["midi"]])

    def test_arrangement_plan_from_adopted_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            manifest = build_ableton_handoff(project)
            plan_file = manifest.arrangement.plan_file
            self.assertEqual(plan_file.parent.name, "song-rev01")
            plan = manifest.arrangement.plan
            self.assertEqual(plan["execution_state"], "planned_not_applied")
            self.assertGreater(len(plan["operations"]), 0)
            # Structure comes from adopted MIDI, not ranking.
            adopted_spec = SongSpec.from_json(
                (manifest.adopted_take.adopted_project / "song_spec.json").read_text()
            )
            self.assertEqual(plan["song"]["bpm"], adopted_spec.song.bpm)

    def test_handoff_manifest_includes_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            manifest = build_ableton_handoff(project)
            doc = manifest.handoff
            self.assertEqual(doc["ableton_handoff_version"], "0.1")
            self.assertEqual(doc["adopted_round"], 1)
            self.assertEqual(doc["adoption"]["selection_mode"], "human")
            self.assertEqual(
                doc["audio"]["sha256"],
                manifest.adopted_take.audio_sha256,
            )
            self.assertFalse(doc["audio"]["authoritative_for_structure"])
            self.assertEqual(
                doc["song_spec"]["sha256"],
                song_spec_sha256(
                    SongSpec.from_json(
                        manifest.adopted_take.song_spec_file.read_text()
                    )
                ),
            )
            self.assertGreaterEqual(len(doc["midi"]), 4)
            self.assertTrue(doc["arrangement_plan"]["path"].endswith("arrangement_plan.json"))
            self.assertFalse(doc["boundary"]["live_execution"])
            self.assertFalse(doc["boundary"]["auto_adoption"])

    def test_second_handoff_does_not_silently_overwrite_different_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            first = build_ableton_handoff(project)
            path = first.handoff_file
            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["adopted_round"] = 0
            tampered["audio"]["sha256"] = "0" * 64
            path.write_text(json.dumps(tampered, indent=2) + "\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                build_ableton_handoff(project)
            # Explicit overwrite regenerates from the adopted take.
            second = build_ableton_handoff(project, overwrite=True)
            self.assertEqual(second.handoff["adopted_round"], 1)
            self.assertEqual(
                second.handoff["audio"]["sha256"],
                first.handoff["audio"]["sha256"],
            )

    def test_identical_second_handoff_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            first = build_ableton_handoff(project)
            second = build_ableton_handoff(project)
            self.assertTrue(second.unchanged)
            self.assertEqual(second.handoff["audio"]["sha256"], first.handoff["audio"]["sha256"])

    def test_handoff_does_not_mutate_audio_midi_song_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            rev01 = project.parent / "song-rev01"
            before_root = _artifact_fingerprints(project)
            before_rev = _artifact_fingerprints(rev01)
            build_ableton_handoff(project)
            self.assertEqual(_artifact_fingerprints(project), before_root)
            self.assertEqual(_artifact_fingerprints(rev01), before_rev)

    def test_handoff_does_not_alter_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            before = json.loads((project / "revision_log.json").read_text())["adopted"]
            build_ableton_handoff(project)
            after = json.loads((project / "revision_log.json").read_text())["adopted"]
            self.assertEqual(before, after)
            self.assertEqual(load_revision_log(project).adopted.round, 1)
            self.assertEqual(load_revision_log(project).adopted.selection_mode, "human")

    def test_handoff_does_not_append_preference_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            before = len(load_preference_memory(project).entries)
            build_ableton_handoff(project)
            build_ableton_handoff(project)
            self.assertEqual(len(load_preference_memory(project).entries), before)

    def test_best_ranked_is_not_used_when_another_round_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp), rounds=2)
            log = load_revision_log(project)
            best = log.ranked()[0].index
            # Explicitly adopt a non-best round when possible.
            chosen = 0 if best != 0 else (1 if len(log.rounds) > 1 else 0)
            if chosen == best and len(log.rounds) > 1:
                chosen = next(r.index for r in log.rounds if r.index != best)
            adopt_revision(project, chosen, reason="not the ranked winner")
            manifest = build_ableton_handoff(project)
            self.assertEqual(manifest.adopted_take.adopted_round, chosen)
            self.assertEqual(manifest.handoff["adopted_round"], chosen)
            if best != chosen:
                self.assertNotEqual(manifest.handoff["adopted_round"], best)

    def test_cli_ableton_handoff_integration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._adopted(Path(temp), 1)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                status = main(["ableton-handoff", str(project)])
            self.assertEqual(status, 0)
            text = buffer.getvalue()
            self.assertIn("Adopted round: 1", text)
            self.assertIn("Project: song-rev01", text)
            self.assertIn("Handoff:", text)
            self.assertTrue((project / "ableton_handoff.json").is_file())
            self.assertEqual(load_revision_log(project).adopted.round, 1)

    def test_cli_refuses_without_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                status = main(["ableton-handoff", str(project)])
            self.assertEqual(status, 2)
            self.assertIn("No human-adopted take", buffer.getvalue())

    def test_parser_accepts_ableton_handoff(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["ableton-handoff", "projects/song", "--overwrite", "--split-drums"]
        )
        self.assertEqual(args.command, "ableton-handoff")
        self.assertTrue(args.overwrite)
        self.assertTrue(args.split_drums)


if __name__ == "__main__":
    unittest.main()
