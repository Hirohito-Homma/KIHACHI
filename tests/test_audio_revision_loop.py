"""VS3 audio revision loop integration tests."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import shutil
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import patch

from kihachi_music_ai.analyzer import analyze_project
from kihachi_music_ai.adapters.ace_step import (
    AceStepClient,
    AceStepConfig,
    AceStepError,
    render_with_ace_step,
)
from kihachi_music_ai.cli import main
from kihachi_music_ai.pipeline import (
    compose_project,
    make_ace_step_repaint_renderer,
    run_audio_revision_loop,
    run_audio_vertical_slice,
    run_generate_and_revise,
    run_vertical_slice,
)
from kihachi_music_ai.project_artifacts import managed_midi_names
from kihachi_music_ai.repaint_planner import stage_repaint_project
from kihachi_music_ai.reviewer import review_project
from kihachi_music_ai.revision import (
    RevisionLog,
    Round,
    compare_rounds,
    describe_comparison,
    run_revision_loop,
)
from test_ace_step import ScriptedOpener, build_wav_bytes, wrapped
from test_audio_vertical_slice import VS2_BRIEF, _fake_ace_client, _sha256
from test_music_brain import EXAMPLE
from test_revision import TAKE_SECONDS, write_take

RATE = 8000


def _multi_task_client(*task_wavs: bytes) -> AceStepClient:
    payloads: list[bytes] = []
    for index, wav in enumerate(task_wavs):
        task_id = f"vs3-task-{index}"
        payloads.append(wrapped({"task_id": task_id, "status": "queued"}))
        payloads.append(
            wrapped(
                [
                    {
                        "task_id": task_id,
                        "status": 1,
                        "result": json.dumps(
                            [
                                {
                                    "file": f"/v1/audio?path=%2Ftmp%2F{task_id}.wav",
                                    "status": 1,
                                }
                            ]
                        ),
                    }
                ]
            )
        )
        payloads.append(wav)
    return AceStepClient(AceStepConfig(request_timeout=3), opener=ScriptedOpener(payloads))


def _defective_initial_wav() -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        path = Path(handle.name)
    write_take(path, seconds=TAKE_SECONDS, gap=(12.0, 3.0))
    data = path.read_bytes()
    path.unlink(missing_ok=True)
    return data


class AudioRevisionLoopIntegrationTests(unittest.TestCase):
    def test_brief_through_revision_loop_records_both_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs3-song"
            client = _multi_task_client(
                _defective_initial_wav(),
                build_wav_bytes(duration=TAKE_SECONDS, music_end=TAKE_SECONDS - 1.0),
            )
            log = run_generate_and_revise(
                VS2_BRIEF,
                output,
                client=client,
                seed=8,
                rounds=1,
            ).revision_log

            self.assertGreaterEqual(len(log.rounds), 1)
            self.assertEqual(log.rounds[0].index, 0)
            self.assertIsNone(log.adopted)
            self.assertIsNone(log.to_dict()["adopted"])

            rev01 = output.parent / f"{output.name}-rev01"
            if len(log.rounds) > 1:
                self.assertEqual(log.rounds[1].index, 1)
                self.assertTrue(rev01.is_dir())
                self.assertTrue((rev01 / "audio" / "ace-step-01.wav").is_file())
                self.assertTrue((rev01 / "audio_analysis.json").is_file())
                self.assertTrue((rev01 / "generation_review.json").is_file())
                review = json.loads(
                    (rev01 / "generation_review.json").read_text(encoding="utf-8")
                )
                self.assertIn("critic", review)
                self.assertEqual(review["review_phase"], "generation_review")

            from kihachi_music_ai.models import SongSpec

            spec = SongSpec.from_json((output / "song_spec.json").read_text(encoding="utf-8"))
            self.assertIn("vocoder", spec.parts())
            for name in managed_midi_names_from_spec(output):
                self.assertTrue((output / name).is_file())
                if rev01.is_dir():
                    self.assertEqual(
                        _sha256(output / name),
                        _sha256(rev01 / name),
                        name,
                    )

            log_doc = json.loads((output / "revision_log.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(log_doc["rounds"]), 1)
            self.assertIsNone(log_doc["adopted"])

    def test_source_project_and_audio_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs3-song"
            client = _multi_task_client(
                _defective_initial_wav(),
                build_wav_bytes(duration=TAKE_SECONDS, music_end=TAKE_SECONDS - 1.0),
            )
            run_generate_and_revise(VS2_BRIEF, output, client=client, seed=8, rounds=1)
            spec_before = (output / "song_spec.json").read_bytes()
            audio_before = (output / "audio" / "ace-step-01.wav").read_bytes()
            rev01 = output.parent / f"{output.name}-rev01"
            self.assertNotEqual(
                (rev01 / "audio" / "ace-step-01.wav").read_bytes(),
                audio_before,
            )
            self.assertEqual((output / "song_spec.json").read_bytes(), spec_before)
            self.assertEqual(
                (output / "audio" / "ace-step-01.wav").read_bytes(),
                audio_before,
            )


def managed_midi_names_from_spec(project_dir: Path) -> tuple[str, ...]:
    from kihachi_music_ai.models import SongSpec

    spec = SongSpec.from_json((project_dir / "song_spec.json").read_text(encoding="utf-8"))
    return managed_midi_names(spec)


class AudioRevisionLoopRegressionTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "song"
        compose_project(EXAMPLE, project)
        write_take(project / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS, gap=(12.0, 3.0))
        analyze_project(project, overwrite=True)
        review_project(project, overwrite=True)
        return project

    def test_no_repaint_when_reviewer_has_no_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            measured = Round(
                index=0,
                project_dir=project,
                alignment=80.0,
                grade="aligned",
                blocking=0,
                warnings=0,
                defect_codes=(),
                planned_action=None,
                audio_file=project / "audio" / "ace-step-01.wav",
            )

            with patch("kihachi_music_ai.revision._measure", return_value=measured):
                with contextlib.redirect_stdout(io.StringIO()):
                    log = run_revision_loop(project, lambda *_: None, rounds=3)

            self.assertEqual(len(log.rounds), 1)
            self.assertIn("nothing worth repainting", log.stopped_because)

    def test_ace_step_failure_during_revision_keeps_completed_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))

            class FailingRepaintClient(AceStepClient):
                def submit(self, request, *, source_audio=None, reference_audio=None):
                    raise AceStepError("ACE-Step unavailable")

            renderer = make_ace_step_repaint_renderer(FailingRepaintClient(AceStepConfig()))

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(AceStepError):
                    run_revision_loop(project, renderer, rounds=2)

            log = json.loads((project / "revision_log.json").read_text(encoding="utf-8"))
            self.assertEqual(log["execution_state"], "failed")
            self.assertEqual(len(log["rounds"]), 1)
            self.assertIn("unavailable", log["stopped_because"])

    def test_invalid_source_audio_is_refused_at_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            plan_path = project / "repaint_plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["source_audio"]["relative_path"] = "audio/missing.wav"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            destination = project.parent / "song-rev01"

            with self.assertRaises(FileNotFoundError):
                stage_repaint_project(project, destination)

    def test_source_audio_sha_mismatch_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            plan_path = project / "repaint_plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["source_audio"]["sha256"] = "0" * 64
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            destination = project.parent / "song-rev01"

            with self.assertRaisesRegex(ValueError, "SHA-256 does not match"):
                stage_repaint_project(project, destination)

    def test_existing_rev01_stops_without_replacing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            occupied = project.parent / "song-rev01"
            occupied.mkdir()
            (occupied / "keep.txt").write_text("mine", encoding="utf-8")
            client = _multi_task_client(build_wav_bytes(duration=TAKE_SECONDS, music_end=9.0))

            with contextlib.redirect_stdout(io.StringIO()):
                log = run_audio_revision_loop(project, client, rounds=2)

            self.assertIn("already exists", log.stopped_because)
            self.assertEqual((occupied / "keep.txt").read_text(encoding="utf-8"), "mine")

    def test_multi_round_managed_midi_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            calls: list[Path] = []

            def render(destination: Path, source_audio: Path) -> None:
                calls.append(destination)
                write_take(destination / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)

            with contextlib.redirect_stdout(io.StringIO()):
                log = run_revision_loop(project, render, rounds=2)

            if len(calls) >= 2:
                rev02 = calls[1]
                for name in managed_midi_names_from_spec(project):
                    self.assertEqual(
                        _sha256(project / name),
                        _sha256(calls[0] / name),
                    )
                    self.assertEqual(
                        _sha256(project / name),
                        _sha256(rev02 / name),
                    )
            self.assertIn("vocoder.mid", managed_midi_names_from_spec(project))

    def test_resume_after_interrupted_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            done = project.parent / "song-rev01"
            shutil.copytree(project, done)
            write_take(done / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)
            for stale in ("audio_analysis.json", "revision_log.json"):
                (done / stale).unlink(missing_ok=True)
            client = _multi_task_client(build_wav_bytes(duration=TAKE_SECONDS, music_end=9.0))

            with contextlib.redirect_stdout(io.StringIO()):
                log = run_audio_revision_loop(project, client, rounds=1, resume=True)

            self.assertEqual(len(log.rounds), 2)
            self.assertEqual(log.rounds[1].project_dir, done)

    def test_partial_failure_keeps_completed_round_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            calls: list[Path] = []

            def render(destination: Path, source_audio: Path) -> None:
                calls.append(destination)
                if len(calls) > 1:
                    raise RuntimeError("repaint failed on round 2")
                write_take(destination / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    run_revision_loop(project, render, rounds=3)

            log = json.loads((project / "revision_log.json").read_text(encoding="utf-8"))
            self.assertEqual(log["execution_state"], "failed")
            self.assertGreaterEqual(len(log["rounds"]), 2)

    def test_no_automatic_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            client = _multi_task_client(
                build_wav_bytes(duration=TAKE_SECONDS, music_end=9.0),
                build_wav_bytes(duration=TAKE_SECONDS, music_end=9.5),
            )
            log = run_audio_revision_loop(project, client, rounds=1)
            self.assertIsNone(log.adopted)
            self.assertIsNone(json.loads((project / "revision_log.json").read_text())["adopted"])

    def test_compare_rounds_reports_actual_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            client = _multi_task_client(
                build_wav_bytes(duration=TAKE_SECONDS, music_end=9.0),
                build_wav_bytes(duration=TAKE_SECONDS, music_end=9.5),
            )
            log = run_audio_revision_loop(project, client, rounds=1)
            if len(log.rounds) >= 2:
                delta = compare_rounds(log.rounds[0], log.rounds[1])
                self.assertIn("alignment", delta)
                self.assertIn("blocking", delta)
                text = "\n".join(describe_comparison(log))
                self.assertIn("Delta", text)
                self.assertIn("Nothing adopted", text)

    def test_orchestration_reuses_existing_revision_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            client = _fake_ace_client()
            with patch(
                "kihachi_music_ai.pipeline.run_revision_loop",
                wraps=run_revision_loop,
            ) as loop:
                run_audio_revision_loop(project, client, rounds=1)
                loop.assert_called_once()

    def test_repaint_renderer_uses_repaint_plan_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "song"
            compose_project(EXAMPLE, project)
            source_audio = project / "audio" / "ace-step-01.wav"
            write_take(source_audio, seconds=TAKE_SECONDS)
            plan = {
                "plan_version": "0.1",
                "selection": {"selector": "bars", "start_bar": 7, "end_bar": 10},
                "revision_prompt": "Remove the measured click without changing the groove.",
                "ace_step_options": {
                    "task_type": "repaint",
                    "audio_cover_strength": 0.9,
                    "cover_noise_strength": 0.1,
                    "repaint_mode": "conservative",
                    "repaint_strength": 0.35,
                    "repaint_latent_crossfade_frames": 14,
                    "repaint_wav_crossfade_sec": 0.5,
                    "chunk_mask_mode": "explicit",
                    "tail_guard_bars": 2.0,
                },
            }
            (project / "repaint_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            captured: dict[str, Any] = {}

            def fake_render(_project, _client, options, **kwargs):
                captured["options"] = options
                captured["source_audio"] = kwargs["source_audio"]

            client = _fake_ace_client(task_id="repaint-plan")
            renderer = make_ace_step_repaint_renderer(client)
            with patch(
                "kihachi_music_ai.pipeline.render_with_ace_step",
                side_effect=fake_render,
            ):
                renderer(project, source_audio)

            options = captured["options"]
            self.assertEqual(options.revision, plan["revision_prompt"])
            self.assertEqual(options.repaint_strength, 0.35)
            self.assertEqual(captured["source_audio"], source_audio)

    def test_vs1_local_slice_remains_network_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs1"
            manifest = run_vertical_slice(EXAMPLE, output, seed=8)
            self.assertFalse((output / "ace_step_request.json").exists())
            self.assertEqual(manifest.review.review["review_phase"], "midi_only")

    def test_vs2_audio_slice_module_is_importable_without_revision_side_effects(self) -> None:
        from kihachi_music_ai.pipeline import run_audio_vertical_slice as entrypoint

        self.assertTrue(callable(entrypoint))


class AudioRevisionLoopCliTests(unittest.TestCase):
    def test_generate_and_revise_cli_runs_end_to_end(self) -> None:
        client = _multi_task_client(
            _defective_initial_wav(),
            build_wav_bytes(duration=TAKE_SECONDS, music_end=TAKE_SECONDS - 1.0),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "cli-vs3"
            stdout = io.StringIO()
            with (
                mock.patch("kihachi_music_ai.cli.song.ace_client", return_value=client),
                contextlib.redirect_stdout(stdout),
            ):
                status = main(
                    [
                        "generate-and-revise",
                        VS2_BRIEF,
                        "--output",
                        str(output),
                        "--seed",
                        "8",
                        "--rounds",
                        "1",
                        "--base-url",
                        "http://127.0.0.1:8001",
                    ]
                )

            self.assertEqual(status, 0)
            text = stdout.getvalue()
            self.assertIn("Generate and revise:", text)
            self.assertIn("no take adopted", text)
            self.assertTrue((output / "revision_log.json").is_file())


if __name__ == "__main__":
    unittest.main()
