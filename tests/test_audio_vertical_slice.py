"""VS2 audio vertical slice integration tests."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from typing import Any
from unittest import mock

from kihachi_music_ai.adapters.ace_step import (
    AceStepClient,
    AceStepConfig,
    AceStepError,
    AceStepGenerationRequest,
    AceStepOptions,
    prepare_ace_step_request,
    render_with_ace_step,
)
from kihachi_music_ai.analyzer import analyze_project
from kihachi_music_ai.cli import main
from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.pipeline import compose_project, run_audio_vertical_slice
from kihachi_music_ai.project_artifacts import managed_midi_names
from kihachi_music_ai.review_contract import ReviewPhase, detect_review_phase
from kihachi_music_ai.reviewer import review_project
from test_ace_step import ScriptedOpener, build_wav_bytes, wrapped
from test_music_brain import EXAMPLE

VS2_BRIEF = (
    "Mutation Funk、DUB、Tech House。110 BPM、D#m。"
    "ファンキーなスラップベース。シンコペーション。4つ打ちキック。"
    "タイトなスネア。ダブコード。Vocoder。前半はミニマル、後半はエネルギッシュ。"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_tone_wav(path: Path, *, duration: float = 8.0, sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = array("h")
    for frame in range(round(duration * sample_rate)):
        seconds = frame / sample_rate
        frames.append(int(0.5 * math.sin(2.0 * math.pi * 220.0 * seconds) * 32767))
    with wave.open(path, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(sample_rate)
        sink.writeframes(frames.tobytes())


class AlwaysPendingOpener:
    """Submit once, then keep returning queued status until payloads run out."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.submitted = False

    def __call__(self, request: Any, *, timeout: float) -> Any:
        from test_ace_step import FakeResponse

        if not self.submitted:
            self.submitted = True
            return FakeResponse(wrapped({"task_id": self.task_id, "status": "queued"}))
        return FakeResponse(
            wrapped(
                [
                    {
                        "task_id": self.task_id,
                        "status": 0,
                        "result": json.dumps([{"file": "", "status": 0}]),
                    }
                ]
            )
        )


def _fake_ace_client(
    *,
    wav_bytes: bytes | None = None,
    task_id: str = "vs2-task",
    fail_status: int | None = None,
    succeed_without_outputs: bool = False,
    timeout: bool = False,
) -> AceStepClient:
    wav_bytes = wav_bytes or build_wav_bytes(duration=12.0, music_end=10.0)
    output = {"file": "/v1/audio?path=%2Ftmp%2Fvs2.wav", "status": 1, "seed_value": "8"}
    if fail_status == 2:
        done = wrapped([{"task_id": task_id, "status": 2, "result": None}])
    elif succeed_without_outputs:
        done = wrapped([{"task_id": task_id, "status": 1, "result": json.dumps([])}])
    elif timeout:
        return AceStepClient(
            AceStepConfig(request_timeout=3),
            opener=AlwaysPendingOpener(task_id),
        )
    else:
        done = wrapped([{"task_id": task_id, "status": 1, "result": json.dumps([output])}])
    opener = ScriptedOpener(
        [
            wrapped({"task_id": task_id, "status": "queued"}),
            done,
            wav_bytes,
        ]
    )
    return AceStepClient(AceStepConfig(request_timeout=3), opener=opener)


class AudioVerticalSliceHappyPathTests(unittest.TestCase):
    def test_natural_language_brief_completes_through_critic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            manifest = run_audio_vertical_slice(
                VS2_BRIEF,
                output,
                client=_fake_ace_client(),
                seed=8,
            )

            self.assertTrue((output / "audio" / "ace-step-01.wav").is_file())
            self.assertTrue((output / "audio_analysis.json").is_file())
            self.assertTrue((output / "generation_review.json").is_file())
            self.assertEqual(manifest.review.review["review_phase"], "generation_review")

    def test_ace_step_request_derives_from_project_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            run_audio_vertical_slice(VS2_BRIEF, output, client=_fake_ace_client(), seed=8)
            request = json.loads((output / "ace_step_request.json").read_text(encoding="utf-8"))
            prompt_txt = (output / "prompt.txt").read_text(encoding="utf-8")

            self.assertIn(prompt_txt.strip(), request["prompt"])
            self.assertEqual(request["bpm"], 110)
            self.assertEqual(request["key_scale"], "D# minor")
            self.assertEqual(request["task_type"], "text2music")

    def test_lyrics_reach_adapter_for_vocoder_brief(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            run_audio_vertical_slice(VS2_BRIEF, output, client=_fake_ace_client(), seed=8)
            request = json.loads((output / "ace_step_request.json").read_text(encoding="utf-8"))
            lyrics_file = (output / "lyrics.txt").read_text(encoding="utf-8")

            self.assertTrue(lyrics_file.strip())
            self.assertEqual(request["lyrics"], lyrics_file)

    def test_generated_wav_is_canonical_project_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            manifest = run_audio_vertical_slice(VS2_BRIEF, output, client=_fake_ace_client(), seed=8)
            canonical = output / "audio" / "ace-step-01.wav"
            result = json.loads((output / "ace_step_result.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest.render.audio_files[0], canonical)
            self.assertEqual(result["audio_files"], ["audio/ace-step-01.wav"])
            self.assertGreater(canonical.stat().st_size, 0)

    def test_real_analyzer_output_is_produced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            manifest = run_audio_vertical_slice(VS2_BRIEF, output, client=_fake_ace_client(), seed=8)
            analysis = manifest.analysis.analysis

            self.assertIn("tempo", analysis)
            self.assertIn("estimated_bpm", analysis["tempo"])
            self.assertIn("confidence", analysis["tempo"])
            self.assertIn("harmony", analysis)
            self.assertIn("sections", analysis)

    def test_audio_aware_reviewer_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            manifest = run_audio_vertical_slice(VS2_BRIEF, output, client=_fake_ace_client(), seed=8)

            self.assertEqual(detect_review_phase(output), ReviewPhase.GENERATION_REVIEW)
            self.assertEqual(manifest.review.review["review_phase"], "generation_review")
            self.assertIn("alignment", manifest.review.review)
            self.assertNotEqual(manifest.review.review["review_phase"], "midi_only")

    def test_density_evidence_preserved_with_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            manifest = run_audio_vertical_slice(VS2_BRIEF, output, client=_fake_ace_client(), seed=8)
            density = manifest.review.review["midi_alignment"]["density"]

            self.assertEqual(density["scope"], "section_part_onset_density_diagnostic")
            parts = {entry["part"] for entry in density["entries"]}
            self.assertIn("vocoder", parts)

    def test_extra_vocoder_part_survives_to_critic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            manifest = run_audio_vertical_slice(VS2_BRIEF, output, client=_fake_ace_client(), seed=8)
            spec = manifest.compose.spec
            review = manifest.review.review

            self.assertIn("vocoder", spec.parts())
            self.assertTrue((output / "vocoder.mid").is_file())
            self.assertIn("vocoder", set(review["midi_alignment"]["tracks"]))
            self.assertEqual(review["critic"]["evidence_status"]["audio_analysis"], "evaluated")
            self.assertEqual(review["critic"]["evidence_status"]["midi"], "evaluated")

    def test_critic_receives_structured_audio_aware_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            manifest = run_audio_vertical_slice(VS2_BRIEF, output, client=_fake_ace_client(), seed=8)
            review = manifest.review.review
            codes = {item["code"] for item in review["findings"]}

            self.assertIn("alignment", review)
            self.assertIn("midi_alignment", review)
            self.assertTrue(codes & {"duration_alignment", "tempo_alignment"})


class AudioVerticalSliceFailureTests(unittest.TestCase):
    def test_provider_failure_does_not_claim_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            with self.assertRaises(AceStepError):
                run_audio_vertical_slice(
                    VS2_BRIEF,
                    output,
                    client=_fake_ace_client(fail_status=2),
                    seed=8,
                )
            self.assertFalse((output / "audio_analysis.json").exists())
            self.assertFalse((output / "generation_review.json").exists())

    def test_polling_timeout_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            with self.assertRaisesRegex(AceStepError, "timed out"):
                run_audio_vertical_slice(
                    VS2_BRIEF,
                    output,
                    client=_fake_ace_client(timeout=True),
                    seed=8,
                    poll_interval=0,
                    wait_timeout=1.0,
                )
            self.assertFalse((output / "generation_review.json").exists())

    def test_missing_audio_in_success_response_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            with self.assertRaises(AceStepError):
                run_audio_vertical_slice(
                    VS2_BRIEF,
                    output,
                    client=_fake_ace_client(succeed_without_outputs=True),
                    seed=8,
                )

    def test_empty_downloaded_audio_fails_before_review(self) -> None:
        class EmptyDownloadClient(AceStepClient):
            def download(self, file_url: str, destination: Path) -> None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"")

        task_id = "empty-audio"
        opener = ScriptedOpener(
            [
                wrapped({"task_id": task_id, "status": "queued"}),
                wrapped(
                    [
                        {
                            "task_id": task_id,
                            "status": 1,
                            "result": json.dumps(
                                [{"file": "/v1/audio?path=%2Ftmp%2Fempty.wav", "status": 1}]
                            ),
                        }
                    ]
                ),
            ]
        )
        client = EmptyDownloadClient(AceStepConfig(request_timeout=3), opener=opener)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            with self.assertRaises((AceStepError, EOFError)):
                run_audio_vertical_slice(
                    VS2_BRIEF,
                    output,
                    client=client,
                    seed=8,
                    tail_guard_bars=0.0,
                )
            self.assertFalse((output / "generation_review.json").exists())

    def test_invalid_audio_fails_before_audio_aware_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            compose_project(EXAMPLE, output)
            bad_wav = output / "audio" / "ace-step-01.wav"
            bad_wav.parent.mkdir(parents=True, exist_ok=True)
            bad_wav.write_bytes(b"not-a-wav")
            with self.assertRaises(Exception):
                analyze_project(output, overwrite=True)
            self.assertFalse((output / "generation_review.json").exists())

    def test_existing_output_is_not_silently_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            run_audio_vertical_slice(VS2_BRIEF, output, client=_fake_ace_client(), seed=8)
            marker = output / "user-notes.txt"
            marker.write_text("keep", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                run_audio_vertical_slice(VS2_BRIEF, output, client=_fake_ace_client(), seed=8)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


class AudioVerticalSliceDeterminismTests(unittest.TestCase):
    def test_identical_inputs_produce_identical_pre_generation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            run_audio_vertical_slice(VS2_BRIEF, first, client=_fake_ace_client(task_id="a"), seed=8)
            run_audio_vertical_slice(VS2_BRIEF, second, client=_fake_ace_client(task_id="b"), seed=8)
            spec = SongSpec.from_json((first / "song_spec.json").read_text(encoding="utf-8"))

            self.assertEqual(
                (first / "song_spec.json").read_text(encoding="utf-8"),
                (second / "song_spec.json").read_text(encoding="utf-8"),
            )
            for name in managed_midi_names(spec):
                self.assertEqual(_sha256(first / name), _sha256(second / name), name)
            for name in ("prompt.txt", "prompt.json", "lyrics.txt"):
                self.assertEqual(
                    (first / name).read_bytes(),
                    (second / name).read_bytes(),
                    name,
                )
            first_request = json.loads((first / "ace_step_request.json").read_text(encoding="utf-8"))
            second_request = json.loads((second / "ace_step_request.json").read_text(encoding="utf-8"))
            self.assertEqual(first_request, second_request)

    def test_generated_wav_bytes_are_not_assumed_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            run_audio_vertical_slice(
                VS2_BRIEF,
                first,
                client=_fake_ace_client(wav_bytes=build_wav_bytes(duration=10.0, music_end=9.0)),
                seed=8,
            )
            run_audio_vertical_slice(
                VS2_BRIEF,
                second,
                client=_fake_ace_client(wav_bytes=build_wav_bytes(duration=11.0, music_end=9.5)),
                seed=8,
            )
            first_sha = _sha256(first / "audio" / "ace-step-01.wav")
            second_sha = _sha256(second / "audio" / "ace-step-01.wav")
            self.assertNotEqual(first_sha, second_sha)


class AudioVerticalSliceCliTests(unittest.TestCase):
    def test_audio_slice_cli_runs_end_to_end_with_substituted_boundary(self) -> None:
        task_id = "cli-vs2"
        client_patch = _fake_ace_client(task_id=task_id)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "cli-vs2"
            stdout = io.StringIO()
            with (
                mock.patch("kihachi_music_ai.cli.song.ace_client", return_value=client_patch),
                contextlib.redirect_stdout(stdout),
            ):
                status = main(
                    [
                        "audio-slice",
                        VS2_BRIEF,
                        "--output",
                        str(output),
                        "--seed",
                        "8",
                        "--base-url",
                        "http://127.0.0.1:8001",
                    ]
                )

            self.assertEqual(status, 0)
            text = stdout.getvalue()
            self.assertIn("Audio vertical slice:", text)
            self.assertIn("generation_review", text)
            self.assertIn("generation_review", text)
            self.assertIn("no take adopted", text)
            self.assertEqual(
                json.loads((output / "generation_review.json").read_text(encoding="utf-8"))[
                    "review_phase"
                ],
                "generation_review",
            )


class AudioVerticalSliceAdapterReuseTests(unittest.TestCase):
    def test_orchestration_reuses_render_with_ace_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "vs2"
            client = _fake_ace_client()
            with mock.patch(
                "kihachi_music_ai.pipeline.render_with_ace_step",
                wraps=render_with_ace_step,
            ) as render:
                run_audio_vertical_slice(VS2_BRIEF, output, client=client, seed=8)
                render.assert_called_once()

    def test_prepare_request_matches_render_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            lyrics = (project / "lyrics.txt").read_text(encoding="utf-8")
            _path, request = prepare_ace_step_request(
                project,
                AceStepOptions(task_type="text2music", lyrics=lyrics),
            )
            self.assertEqual(
                request.to_dict(),
                AceStepGenerationRequest.from_song_spec(
                    SongSpec.from_json((project / "song_spec.json").read_text(encoding="utf-8")),
                    AceStepOptions(task_type="text2music", lyrics=lyrics),
                ).to_dict(),
            )


if __name__ == "__main__":
    unittest.main()
