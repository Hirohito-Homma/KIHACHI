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

from kihachi_music_ai.adapters.ace_step import (
    AceStepClient,
    AceStepConfig,
    AceStepError,
    AceStepGenerationRequest,
    AceStepLoraConfig,
    AceStepOptions,
    prepare_ace_step_request,
    render_with_ace_step,
    resolve_repaint_window,
)
from kihachi_music_ai.cli import main
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.pipeline import compose_project
from test_music_brain import EXAMPLE


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class ScriptedOpener:
    def __init__(self, payloads: list[bytes]) -> None:
        self.payloads = list(payloads)
        self.requests: list[Any] = []

    def __call__(self, request: Any, *, timeout: float) -> FakeResponse:
        self.requests.append(request)
        if not self.payloads:
            raise AssertionError("unexpected request")
        return FakeResponse(self.payloads.pop(0))


class RepeatedLoraOpener(ScriptedOpener):
    def __call__(self, request: Any, *, timeout: float) -> FakeResponse:
        if request.full_url.endswith("/v1/lora/load"):
            self.requests.append(request)
            raise AceStepError("ACE-Step HTTP error 400")
        return super().__call__(request, timeout=timeout)


def wrapped(data: Any) -> bytes:
    return json.dumps({"data": data, "code": 200, "error": None}).encode("utf-8")


def build_wav_bytes(
    *,
    duration: float,
    music_end: float,
    sample_rate: int = 4000,
) -> bytes:
    """Mono PCM that stops at ``music_end`` and then drops to a noise floor."""

    frames = array("h")
    for frame in range(round(duration * sample_rate)):
        seconds = frame / sample_rate
        amplitude = 0.6 if seconds < music_end else 0.0002
        frames.append(int(amplitude * math.sin(2.0 * math.pi * 220.0 * seconds) * 32767))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(sample_rate)
        sink.writeframes(frames.tobytes())
    return buffer.getvalue()


class AceStepAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = MusicBrain(seed=8).analyze(EXAMPLE)

    def test_song_spec_maps_to_stable_ace_step_request(self) -> None:
        request = AceStepGenerationRequest.from_song_spec(self.spec)
        payload = request.to_dict()
        self.assertEqual(payload["bpm"], 110)
        self.assertEqual(payload["key_scale"], "D# minor")
        self.assertEqual(payload["time_signature"], "4")
        self.assertEqual(payload["audio_duration"], 69.818)
        self.assertFalse(payload["thinking"])
        self.assertFalse(payload["use_cot_caption"])
        self.assertFalse(payload["use_cot_language"])
        self.assertFalse(payload["use_random_seed"])
        self.assertEqual(payload["seed"], 8)
        self.assertNotIn("ai_token", payload)

    def test_song_spec_maps_to_cover_request_without_changing_music_fields(self) -> None:
        request = AceStepGenerationRequest.from_song_spec(
            self.spec,
            AceStepOptions(
                task_type="cover",
                audio_cover_strength=1.0,
                cover_noise_strength=0.8,
            ),
        )
        payload = request.to_dict()

        self.assertEqual(payload["task_type"], "cover")
        self.assertEqual(payload["audio_cover_strength"], 1.0)
        self.assertEqual(payload["cover_noise_strength"], 0.8)
        self.assertEqual(payload["bpm"], 110)
        self.assertEqual(payload["key_scale"], "D# minor")
        self.assertEqual(payload["audio_duration"], 69.818)

    def test_song_spec_maps_to_bounded_repaint_request(self) -> None:
        request = AceStepGenerationRequest.from_song_spec(
            self.spec,
            AceStepOptions(
                task_type="repaint",
                cover_noise_strength=0.0,
                repainting_start=52.364,
                repainting_end=69.8,
                repaint_mode="balanced",
                repaint_strength=0.65,
                repaint_latent_crossfade_frames=10,
                repaint_wav_crossfade_sec=0.25,
                chunk_mask_mode="explicit",
            ),
        )
        payload = request.to_dict()

        self.assertEqual(payload["task_type"], "repaint")
        self.assertEqual(payload["repainting_start"], 52.364)
        self.assertEqual(payload["repainting_end"], 69.8)
        self.assertEqual(payload["repaint_mode"], "balanced")
        self.assertEqual(payload["repaint_strength"], 0.65)
        self.assertEqual(payload["repaint_latent_crossfade_frames"], 10)
        self.assertEqual(payload["repaint_wav_crossfade_sec"], 0.25)
        self.assertEqual(payload["chunk_mask_mode"], "explicit")
        self.assertEqual(payload["bpm"], 110)
        self.assertEqual(payload["key_scale"], "D# minor")

        with self.assertRaises(ValueError):
            AceStepOptions(
                task_type="repaint",
                repainting_start=20.0,
                repainting_end=10.0,
            )

    def test_repaint_section_resolves_song_spec_bars_to_seconds(self) -> None:
        window = resolve_repaint_window(self.spec, section_name="psychedelic-drop")

        self.assertEqual(window.selector, "section")
        self.assertEqual(window.section_name, "psychedelic_drop")
        self.assertEqual((window.start_bar, window.end_bar), (25, 32))
        self.assertEqual((window.start_sec, window.end_sec), (52.364, 69.818))

    def test_repaint_bar_range_is_one_based_inclusive_and_validated(self) -> None:
        window = resolve_repaint_window(self.spec, bar_range="17:24")

        self.assertEqual(window.selector, "bars")
        self.assertEqual((window.start_sec, window.end_sec), (34.909, 52.364))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            resolve_repaint_window(
                self.spec,
                section_name="mutation_build",
                bar_range="17:24",
            )
        with self.assertRaisesRegex(ValueError, "START:END"):
            resolve_repaint_window(self.spec, bar_range="17-24")
        with self.assertRaisesRegex(ValueError, "1 <= START"):
            resolve_repaint_window(self.spec, bar_range="0:8")
        with self.assertRaisesRegex(ValueError, "available sections"):
            resolve_repaint_window(self.spec, section_name="missing")

    def test_cover_submit_uploads_source_without_leaking_local_path(self) -> None:
        opener = ScriptedOpener(
            [wrapped({"task_id": "cover-task", "status": "queued"})]
        )
        client = AceStepClient(AceStepConfig(api_key="upload-secret"), opener=opener)
        request = AceStepGenerationRequest.from_song_spec(
            self.spec,
            AceStepOptions(task_type="cover", cover_noise_strength=0.8),
        )
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.wav"
            source.write_bytes(b"RIFFsource-audio")

            task = client.submit(request, source_audio=source)

            self.assertEqual(task.task_id, "cover-task")
            uploaded = opener.requests[0]
            content_type = uploaded.get_header("Content-type") or ""
            self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
            self.assertIn(b'name="src_audio"; filename="source.wav"', uploaded.data)
            self.assertIn(b"RIFFsource-audio", uploaded.data)
            self.assertNotIn(str(source.parent).encode("utf-8"), uploaded.data)
            self.assertNotIn(b"upload-secret", uploaded.data)
            self.assertEqual(uploaded.get_header("Authorization"), "Bearer upload-secret")

    def test_prepare_writes_request_without_network_or_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            request_path, _request = prepare_ace_step_request(project)
            payload = request_path.read_text(encoding="utf-8")
            self.assertIn('"task_type": "text2music"', payload)
            self.assertNotIn("api_key", payload)
            self.assertNotIn("ai_token", payload)

    def test_revision_is_prioritized_without_changing_song_spec_fields(self) -> None:
        revision = "Anchor bass pedals on D# and state D#m - B - F# - C# clearly."
        request = AceStepGenerationRequest.from_song_spec(
            self.spec,
            AceStepOptions(revision=revision),
        )
        self.assertTrue(
            request.prompt.startswith("Revision constraints (highest priority):\n" + revision)
        )
        self.assertIn("\n\nBase song design:\n", request.prompt)
        self.assertEqual(request.bpm, 110)
        self.assertEqual(request.key_scale, "D# minor")
        self.assertEqual(request.audio_duration, 69.818)
        self.assertEqual(request.seed, 8)

    def test_prepare_revision_preserves_original_request_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            base_path, _base_request = prepare_ace_step_request(project)
            revision_path, _revision_request = prepare_ace_step_request(
                project,
                AceStepOptions(revision="Make D# minor unambiguous."),
            )
            self.assertEqual(base_path.name, "ace_step_request.json")
            self.assertEqual(revision_path.name, "ace_step_revision_request.json")
            self.assertNotIn("Revision constraints", base_path.read_text(encoding="utf-8"))
            self.assertIn("Make D# minor unambiguous.", revision_path.read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                prepare_ace_step_request(
                    project,
                    AceStepOptions(revision="Use a different revision."),
                )

    def test_prepare_cli_reads_explicit_revision_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            revision_file = project / "revision_prompt.txt"
            revision_file.write_text("Make every section opening clearly D# minor.\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                status = main(
                    [
                        "ace-step",
                        "prepare",
                        str(project),
                        "--revision-file",
                        str(revision_file),
                    ]
                )
            self.assertEqual(status, 0)
            payload = json.loads((project / "ace_step_revision_request.json").read_text(encoding="utf-8"))
            self.assertIn("Make every section opening clearly D# minor.", payload["prompt"])

    def test_prepare_cli_resolves_repaint_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "ace-step",
                        "prepare",
                        str(project),
                        "--task-type",
                        "repaint",
                        "--repaint-section",
                        "psychedelic_drop",
                    ]
                )

            self.assertEqual(status, 0)
            payload = json.loads(
                (project / "ace_step_repaint_request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["repainting_start"], 52.364)
            self.assertEqual(payload["repainting_end"], 69.818)
            self.assertIn("repaint bars 25:32", stdout.getvalue())

    def test_a_server_error_carries_the_server_s_own_message(self) -> None:
        """A dropped model read as a bare "HTTP error 500" for far too long."""

        import urllib.error

        class Failing:
            def __call__(self, request, timeout=None):
                raise urllib.error.HTTPError(
                    request.full_url, 500, "Internal Server Error", {},
                    io.BytesIO(b'{"detail": "Model not initialized"}'),
                )

        client = AceStepClient(AceStepConfig(request_timeout=3), opener=Failing())

        with self.assertRaises(AceStepError) as caught:
            client.get_lora_status()

        self.assertIn("500", str(caught.exception))
        self.assertIn("Model not initialized", str(caught.exception))

    def test_a_body_that_is_not_json_still_reaches_the_caller(self) -> None:
        import urllib.error

        class Failing:
            def __call__(self, request, timeout=None):
                raise urllib.error.HTTPError(
                    request.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"upstream died")
                )

        client = AceStepClient(AceStepConfig(request_timeout=3), opener=Failing())

        with self.assertRaises(AceStepError) as caught:
            client.get_lora_status()

        self.assertIn("upstream died", str(caught.exception))

    def test_waiting_reports_progress_to_the_caller(self) -> None:
        """A render takes minutes; a client that prints nothing looks hung."""

        task_id = "task-progress"
        pending = wrapped(
            [
                {
                    "task_id": task_id,
                    "status": 0,
                    "result": json.dumps([{"file": "", "status": 0, "stage": "running"}]),
                }
            ]
        )
        done = wrapped(
            [
                {
                    "task_id": task_id,
                    "status": 1,
                    "result": json.dumps(
                        [{"file": "/v1/audio?path=%2Ftmp%2Fa.wav", "status": 1, "seed_value": "8"}]
                    ),
                }
            ]
        )
        client = AceStepClient(
            AceStepConfig(request_timeout=3), opener=ScriptedOpener([pending, pending, done])
        )
        seen: list[tuple[int, float]] = []
        elapsed = iter([0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

        result = client.wait(
            task_id,
            poll_interval=0,
            wait_timeout=60,
            sleep=lambda _seconds: None,
            clock=lambda: next(elapsed),
            on_poll=lambda outcome, waited: seen.append((outcome.status, waited)),
        )

        self.assertEqual(result.status, 1)
        # only the unfinished polls are reported, and the wait is cumulative
        self.assertEqual([status for status, _ in seen], [0, 0])
        self.assertEqual([waited for _, waited in seen], [0.0, 2.0])

    def test_waiting_without_a_progress_hook_still_works(self) -> None:
        task_id = "task-quiet"
        done = wrapped(
            [
                {
                    "task_id": task_id,
                    "status": 1,
                    "result": json.dumps(
                        [{"file": "/v1/audio?path=%2Ftmp%2Fa.wav", "status": 1, "seed_value": "8"}]
                    ),
                }
            ]
        )
        client = AceStepClient(AceStepConfig(request_timeout=3), opener=ScriptedOpener([done]))

        self.assertEqual(client.wait(task_id, poll_interval=0, wait_timeout=5).status, 1)

    def test_submit_wait_and_download_use_official_async_flow(self) -> None:
        task_id = "task-123"
        success_outputs = [
            {
                "file": "/v1/audio?path=%2Ftmp%2Fmutation.wav",
                "status": 1,
                "seed_value": "8",
                "dit_model": "acestep-v15-turbo",
            }
        ]
        opener = ScriptedOpener(
            [
                wrapped({"task_id": task_id, "status": "queued", "queue_position": 1}),
                wrapped(
                    [
                        {
                            "task_id": task_id,
                            "status": 0,
                            "result": json.dumps(
                                [
                                    {
                                        "file": "",
                                        "status": 0,
                                        "progress": 0.0,
                                        "stage": "queued",
                                    }
                                ]
                            ),
                        }
                    ]
                ),
                wrapped([{"task_id": task_id, "status": 1, "result": json.dumps(success_outputs)}]),
                b"RIFFfake-wave",
            ]
        )
        client = AceStepClient(
            AceStepConfig(api_key="top-secret", request_timeout=3),
            opener=opener,
        )
        task = client.submit(AceStepGenerationRequest.from_song_spec(self.spec))
        result = client.wait(task.task_id, poll_interval=0, wait_timeout=1)
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / "result.wav"
            client.download(result.outputs[0].file, audio)
            self.assertEqual(audio.read_bytes(), b"RIFFfake-wave")

        self.assertEqual([request.full_url.rsplit("/", 1)[-1].split("?", 1)[0] for request in opener.requests], ["release_task", "query_result", "query_result", "audio"])
        for request in opener.requests:
            self.assertEqual(request.get_header("Authorization"), "Bearer top-secret")
            if request.data:
                self.assertNotIn(b"top-secret", request.data)

    def test_render_writes_audio_and_result_without_api_key(self) -> None:
        task_id = "task-render"
        output = {
            "file": "/v1/audio?path=%2Ftmp%2Fmutation.wav",
            "status": 1,
            "seed_value": "8",
            "metas": {"bpm": 110, "keyscale": "D# minor"},
        }
        opener = ScriptedOpener(
            [
                wrapped({"task_id": task_id, "status": "queued"}),
                wrapped([{"task_id": task_id, "status": 1, "result": json.dumps([output])}]),
                b"RIFFrendered-wave",
            ]
        )
        client = AceStepClient(AceStepConfig(api_key="never-write-me"), opener=opener)
        revision = "Keep D# minor explicit at each section opening."
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            manifest = render_with_ace_step(
                project,
                client,
                AceStepOptions(revision=revision),
                poll_interval=0,
                wait_timeout=1,
            )
            self.assertEqual(manifest.task_id, task_id)
            self.assertEqual(manifest.audio_files[0].read_bytes(), b"RIFFrendered-wave")
            documents = manifest.request_file.read_text() + manifest.result_file.read_text()
            self.assertNotIn("never-write-me", documents)
            self.assertEqual(manifest.request_file.name, "ace_step_revision_request.json")
            result_document = json.loads(manifest.result_file.read_text(encoding="utf-8"))
            self.assertEqual(
                result_document["revision"]["sha256"],
                hashlib.sha256(revision.encode("utf-8")).hexdigest(),
            )

    def test_render_with_tail_guard_trims_to_the_grid_and_keeps_the_raw_render(self) -> None:
        # ACE-Step composes its ending inside whatever buffer it is given, so a
        # render asked for exactly the song grid leaves the final bar silent. The
        # guard asks for two extra bars and trims the delivery back to the grid.
        rendered_wav = build_wav_bytes(duration=74.182, music_end=72.2)
        output = {
            "file": "/v1/audio?path=%2Ftmp%2Fguarded.wav",
            "status": 1,
            "seed_value": "8",
        }
        opener = ScriptedOpener(
            [
                wrapped({"task_id": "task-guard", "status": "queued"}),
                wrapped(
                    [{"task_id": "task-guard", "status": 1, "result": json.dumps([output])}]
                ),
                rendered_wav,
            ]
        )
        client = AceStepClient(AceStepConfig(), opener=opener)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)

            manifest = render_with_ace_step(
                project,
                client,
                AceStepOptions(tail_guard_bars=2.0),
                poll_interval=0,
                wait_timeout=1,
            )

            request = json.loads(manifest.request_file.read_text(encoding="utf-8"))
            self.assertEqual(request["audio_duration"], 74.182)

            delivered = manifest.audio_files[0]
            untrimmed = project / "audio" / "ace-step-01.untrimmed.wav"
            self.assertEqual(delivered.name, "ace-step-01.wav")
            self.assertEqual(untrimmed.read_bytes(), rendered_wav)
            with wave.open(str(delivered), "rb") as handle:
                self.assertEqual(handle.getnframes(), round(69.818 * 4000))

            document = json.loads(manifest.result_file.read_text(encoding="utf-8"))
            guard = document["tail_guard"]
            self.assertEqual(guard["guard_bars"], 2.0)
            self.assertEqual(guard["guard_sec"], 4.364)
            self.assertEqual(guard["requested_duration_sec"], 74.182)
            self.assertEqual(guard["song_grid_duration_sec"], 69.818)
            self.assertEqual(
                guard["untrimmed_audio_files"], ["audio/ace-step-01.untrimmed.wav"]
            )
            # The delivered final bar now carries music instead of a silent tail.
            self.assertAlmostEqual(guard["delivered_music_end_sec"][0], 69.818, delta=0.05)

    def test_render_without_tail_guard_keeps_the_delivery_untouched(self) -> None:
        output = {"file": "/v1/audio?path=%2Ftmp%2Fplain.wav", "status": 1, "seed_value": "8"}
        opener = ScriptedOpener(
            [
                wrapped({"task_id": "task-plain", "status": "queued"}),
                wrapped(
                    [{"task_id": "task-plain", "status": 1, "result": json.dumps([output])}]
                ),
                b"RIFFrendered-wave",
            ]
        )
        client = AceStepClient(AceStepConfig(), opener=opener)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)

            manifest = render_with_ace_step(
                project, client, AceStepOptions(), poll_interval=0, wait_timeout=1
            )

            self.assertEqual(manifest.audio_files[0].read_bytes(), b"RIFFrendered-wave")
            self.assertFalse((project / "audio" / "ace-step-01.untrimmed.wav").exists())
            self.assertNotIn(
                "tail_guard", json.loads(manifest.result_file.read_text(encoding="utf-8"))
            )

    def test_configure_lora_uses_official_lifecycle_and_verifies_status(self) -> None:
        lora_path = "/workspace/ACE-Step-1.5/output/KIHACHI_LORA_v1/final"
        opener = ScriptedOpener(
            [
                wrapped({"message": "loaded", "lora_path": lora_path, "adapter_name": "kihachi"}),
                wrapped({"message": "scaled", "scale": 0.72, "adapter_name": "kihachi"}),
                wrapped({"message": "enabled", "use_lora": True}),
                wrapped(
                    {
                        "lora_loaded": True,
                        "use_lora": True,
                        "lora_scale": 1.0,
                        "adapter_type": "lora",
                        "scales": {"kihachi": 0.72},
                        "active_adapter": "kihachi",
                        "adapters": ["kihachi"],
                        "synthetic_default_mode": False,
                    }
                ),
            ]
        )
        client = AceStepClient(AceStepConfig(), opener=opener)
        status = client.configure_lora(
            AceStepLoraConfig(
                lora_path=lora_path,
                scale=0.72,
                adapter_name="kihachi",
            )
        )

        self.assertTrue(status.lora_loaded)
        self.assertTrue(status.use_lora)
        self.assertEqual(status.active_adapter, "kihachi")
        self.assertEqual(status.scales, {"kihachi": 0.72})
        self.assertEqual(
            [request.full_url.rsplit("/v1/lora/", 1)[-1] for request in opener.requests],
            ["load", "scale", "toggle", "status"],
        )
        bodies = [json.loads(request.data) for request in opener.requests if request.data]
        self.assertEqual(
            bodies,
            [
                {"lora_path": lora_path, "adapter_name": "kihachi"},
                {"scale": 0.72, "adapter_name": "kihachi"},
                {"use_lora": True},
            ],
        )

    def test_render_with_lora_records_verified_runtime_state(self) -> None:
        task_id = "task-lora-render"
        lora_path = "/workspace/ACE-Step-1.5/output/KIHACHI_LORA_v1/final"
        output = {
            "file": "/v1/audio?path=%2Ftmp%2Fmutation-lora.wav",
            "status": 1,
            "seed_value": "8",
            "dit_model": "acestep-v15-turbo",
        }
        opener = ScriptedOpener(
            [
                wrapped({"message": "loaded", "lora_path": lora_path}),
                wrapped({"message": "scaled", "scale": 0.8}),
                wrapped({"message": "enabled", "use_lora": True}),
                wrapped(
                    {
                        "lora_loaded": True,
                        "use_lora": True,
                        "lora_scale": 0.8,
                        "adapter_type": "lora",
                        "scales": {},
                        "active_adapter": None,
                        "adapters": [],
                        "synthetic_default_mode": False,
                    }
                ),
                wrapped({"task_id": task_id, "status": "queued"}),
                wrapped([{"task_id": task_id, "status": 1, "result": json.dumps([output])}]),
                b"RIFFkihachi-lora-wave",
            ]
        )
        client = AceStepClient(AceStepConfig(), opener=opener)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            manifest = render_with_ace_step(
                project,
                client,
                lora=AceStepLoraConfig(lora_path=lora_path, scale=0.8),
                poll_interval=0,
                wait_timeout=1,
            )
            result = json.loads(manifest.result_file.read_text(encoding="utf-8"))
            self.assertEqual(manifest.audio_files[0].read_bytes(), b"RIFFkihachi-lora-wave")
            self.assertIsNotNone(manifest.lora_status)
            self.assertEqual(result["lora"]["requested"]["lora_path"], lora_path)
            self.assertEqual(result["lora"]["requested"]["scale"], 0.8)
            self.assertTrue(result["lora"]["status"]["lora_loaded"])
            self.assertTrue(result["lora"]["status"]["use_lora"])

    def test_render_cover_records_source_provenance(self) -> None:
        task_id = "task-cover-render"
        output = {
            "file": "/v1/audio?path=%2Ftmp%2Fmutation-cover.wav",
            "status": 1,
            "seed_value": "8",
            "dit_model": "acestep-v15-turbo",
        }
        opener = ScriptedOpener(
            [
                wrapped({"task_id": task_id, "status": "queued"}),
                wrapped([{"task_id": task_id, "status": 1, "result": json.dumps([output])}]),
                b"RIFFcover-wave",
            ]
        )
        client = AceStepClient(AceStepConfig(), opener=opener)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            source = Path(temp) / "source.wav"
            source_bytes = b"RIFFsource-wave"
            source.write_bytes(source_bytes)

            manifest = render_with_ace_step(
                project,
                client,
                AceStepOptions(task_type="cover", cover_noise_strength=0.8),
                source_audio=source,
                poll_interval=0,
                wait_timeout=1,
            )

            result = json.loads(manifest.result_file.read_text(encoding="utf-8"))
            conditioning = result["conditioning"]
            self.assertEqual(manifest.request_file.name, "ace_step_cover_request.json")
            self.assertEqual(manifest.audio_files[0].read_bytes(), b"RIFFcover-wave")
            self.assertEqual(conditioning["task_type"], "cover")
            self.assertEqual(conditioning["cover_noise_strength"], 0.8)
            self.assertEqual(conditioning["source_audio"]["name"], "source.wav")
            self.assertEqual(
                conditioning["source_audio"]["sha256"],
                hashlib.sha256(source_bytes).hexdigest(),
            )
            self.assertNotIn(str(source.parent), manifest.result_file.read_text(encoding="utf-8"))

    def test_render_repaint_records_edit_window(self) -> None:
        task_id = "task-repaint-render"
        output = {
            "file": "/v1/audio?path=%2Ftmp%2Fmutation-repaint.wav",
            "status": 1,
            "seed_value": "8",
            "dit_model": "acestep-v15-turbo",
        }
        opener = ScriptedOpener(
            [
                wrapped({"task_id": task_id, "status": "queued"}),
                wrapped([{"task_id": task_id, "status": 1, "result": json.dumps([output])}]),
                b"RIFFrepaint-wave",
            ]
        )
        client = AceStepClient(AceStepConfig(), opener=opener)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            source = Path(temp) / "source.wav"
            source.write_bytes(b"RIFFsource-wave")
            options = AceStepOptions(
                task_type="repaint",
                cover_noise_strength=0.0,
                repainting_start=52.364,
                repainting_end=69.818,
                repaint_strength=0.65,
                repaint_wav_crossfade_sec=0.25,
                chunk_mask_mode="explicit",
            )

            manifest = render_with_ace_step(
                project,
                client,
                options,
                source_audio=source,
                repaint_selection=resolve_repaint_window(
                    self.spec,
                    section_name="psychedelic_drop",
                ),
                poll_interval=0,
                wait_timeout=1,
            )

            result = json.loads(manifest.result_file.read_text(encoding="utf-8"))
            conditioning = result["conditioning"]
            self.assertEqual(manifest.request_file.name, "ace_step_repaint_request.json")
            self.assertEqual(conditioning["task_type"], "repaint")
            self.assertEqual(conditioning["repainting_start"], 52.364)
            self.assertEqual(conditioning["repainting_end"], 69.818)
            self.assertEqual(conditioning["repaint_strength"], 0.65)
            self.assertEqual(conditioning["repaint_wav_crossfade_sec"], 0.25)
            self.assertEqual(conditioning["chunk_mask_mode"], "explicit")
            self.assertEqual(
                conditioning["repaint_selection"],
                {
                    "selector": "section",
                    "start_bar": 25,
                    "end_bar": 32,
                    "start_sec": 52.364,
                    "end_sec": 69.818,
                    "section_name": "psychedelic_drop",
                },
            )

    def test_configure_lora_reuses_matching_loaded_adapter(self) -> None:
        lora_path = "/workspace/ACE-Step-1.5/output/KIHACHI_LORA_v1/final"
        status = {
            "lora_loaded": True,
            "use_lora": True,
            "lora_scale": 0.8,
            "adapter_type": "lora",
            "scales": {"final": 0.8},
            "active_adapter": "final",
            "adapters": ["final"],
            "synthetic_default_mode": False,
        }
        opener = RepeatedLoraOpener(
            [
                wrapped(status),
                wrapped({"message": "scaled", "scale": 0.8}),
                wrapped({"message": "enabled", "use_lora": True}),
                wrapped(status),
            ]
        )
        client = AceStepClient(AceStepConfig(), opener=opener)

        result = client.configure_lora(AceStepLoraConfig(lora_path=lora_path, scale=0.8))

        self.assertEqual(result.active_adapter, "final")
        self.assertEqual(
            [request.full_url.rsplit("/v1/lora/", 1)[-1] for request in opener.requests],
            ["load", "status", "scale", "toggle", "status"],
        )

    def test_cross_origin_audio_url_is_rejected(self) -> None:
        client = AceStepClient(AceStepConfig(base_url="http://127.0.0.1:8001"))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AceStepError):
                client.download("https://example.com/audio.wav", Path(temp) / "audio.wav")


if __name__ == "__main__":
    unittest.main()
