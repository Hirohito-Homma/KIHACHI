from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.adapters.ace_step import AceStepClient, AceStepConfig
from kihachi_music_ai.chunked import (
    DEFAULT_CHUNK_BARS,
    MIN_CHUNK_BARS,
    build_chunk_plan,
    load_chunk_plan,
    render_chunk_plan,
    song_spec_sha256,
)
from kihachi_music_ai.cli import main
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.pipeline import compose_project
from test_ace_step import ScriptedOpener, build_wav_bytes, wrapped
from test_music_brain import EXAMPLE

LONG_PROMPT = EXAMPLE + "5分程度。"


def build_spec(prompt: str = LONG_PROMPT):
    return MusicBrain(seed=8).analyze(prompt)


class ChunkPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()
        self.plan = build_chunk_plan(self.spec)

    def test_chunks_cover_the_whole_song_without_gaps(self) -> None:
        cursor = 1
        for chunk in self.plan["chunks"]:
            selection = chunk["selection"]
            self.assertEqual(selection["start_bar"], cursor)
            cursor = selection["end_bar"] + 1
        self.assertEqual(cursor - 1, self.spec.song.total_bars)

    def test_sections_are_never_split_across_chunks(self) -> None:
        planned = {section.name for section in self.spec.arrangement}
        seen: list[str] = []
        for chunk in self.plan["chunks"]:
            seen.extend(chunk["sections"])
        self.assertEqual(seen, [section.name for section in self.spec.arrangement])
        self.assertEqual(set(seen), planned)

    def test_chunks_reach_the_requested_size(self) -> None:
        for chunk in self.plan["chunks"]:
            selection = chunk["selection"]
            bars = selection["end_bar"] - selection["start_bar"] + 1
            self.assertGreaterEqual(bars, MIN_CHUNK_BARS)

    def test_a_short_trailing_stub_folds_into_the_chunk_before_it(self) -> None:
        # The 8-bar outro must not become a chunk of its own.
        last = self.plan["chunks"][-1]

        self.assertIn("outro", last["sections"])
        self.assertGreater(len(last["sections"]), 1)

    def test_the_first_chunk_lays_a_bed_and_the_rest_repaint(self) -> None:
        self.assertEqual(self.plan["chunks"][0]["task_type"], "text2music")
        for chunk in self.plan["chunks"][1:]:
            self.assertEqual(chunk["task_type"], "repaint")

    def test_only_the_final_chunk_carries_the_tail_guard(self) -> None:
        guards = [
            chunk["selection"].get("tail_guard_sec", 0.0) for chunk in self.plan["chunks"]
        ]

        self.assertEqual(guards[-1], 4.364)
        self.assertTrue(all(not guard for guard in guards[:-1]))

    def test_each_prompt_names_only_its_own_sections(self) -> None:
        # This is the whole point: nine sections in one prompt is what failed.
        for chunk in self.plan["chunks"]:
            prompt = chunk["revision_prompt"]
            own = set(chunk["sections"])
            for section in self.spec.arrangement:
                spoken = section.name.replace("_", " ")
                if section.name in own:
                    self.assertIn(spoken, prompt, f"{section.name} missing from its chunk")
                else:
                    self.assertNotIn(
                        spoken, prompt, f"{section.name} leaked into chunk {chunk['index']}"
                    )

    def test_prompts_state_quiet_sections_as_quiet(self) -> None:
        breakdown = next(
            chunk for chunk in self.plan["chunks"] if "dub_breakdown" in chunk["sections"]
        )

        self.assertIn("no drums", breakdown["revision_prompt"])
        self.assertIn("clearly quieter", breakdown["revision_prompt"])

    def test_repaint_prompts_ask_for_an_inaudible_splice(self) -> None:
        for chunk in self.plan["chunks"][1:]:
            self.assertIn("Preserve all Audio outside this range", chunk["revision_prompt"])
            self.assertIn("splice is inaudible", chunk["revision_prompt"])

    def test_chunk_size_is_configurable(self) -> None:
        wide = build_chunk_plan(self.spec, target_chunk_bars=64)

        self.assertLess(len(wide["chunks"]), len(self.plan["chunks"]))
        with self.assertRaises(ValueError):
            build_chunk_plan(self.spec, target_chunk_bars=8)

    def test_a_short_song_becomes_a_single_chunk(self) -> None:
        plan = build_chunk_plan(build_spec(EXAMPLE), target_chunk_bars=DEFAULT_CHUNK_BARS)

        self.assertEqual(len(plan["chunks"]), 1)
        self.assertEqual(plan["chunks"][0]["task_type"], "text2music")

    def test_the_plan_is_pinned_to_its_spec_and_renders_nothing(self) -> None:
        self.assertEqual(self.plan["song_spec_sha256"], song_spec_sha256(self.spec))
        self.assertEqual(self.plan["execution_state"], "planned_not_rendered")
        self.assertFalse(self.plan["safety"]["render_started"])


class ChunkPlanLoadingTests(unittest.TestCase):
    def test_a_plan_round_trips(self) -> None:
        plan = build_chunk_plan(build_spec())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "chunk_plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")

            self.assertEqual(load_chunk_plan(path)["chunks"], plan["chunks"])

    def test_a_malformed_plan_is_refused(self) -> None:
        plan = build_chunk_plan(build_spec())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad_version = dict(plan, chunk_plan_version="9.9")
            (root / "a.json").write_text(json.dumps(bad_version), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_chunk_plan(root / "a.json")

            no_bed = dict(plan)
            no_bed["chunks"] = [dict(plan["chunks"][0], task_type="repaint")]
            (root / "b.json").write_text(json.dumps(no_bed), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_chunk_plan(root / "b.json")

            with self.assertRaises(FileNotFoundError):
                load_chunk_plan(root / "missing.json")


def scripted_chunk_opener(chunk_count: int) -> ScriptedOpener:
    """Submit + poll + download, repeated once per chunk."""

    payloads: list[bytes] = []
    for index in range(chunk_count):
        task = f"task-chunk-{index + 1}"
        output = {"file": f"/v1/audio?path=%2Ftmp%2F{task}.wav", "status": 1, "seed_value": "8"}
        payloads.append(wrapped({"task_id": task, "status": "queued"}))
        payloads.append(
            wrapped([{"task_id": task, "status": 1, "result": json.dumps([output])}])
        )
        payloads.append(build_wav_bytes(duration=301.2, music_end=299.0, sample_rate=800))
    return ScriptedOpener(payloads)


class ChunkRenderTests(unittest.TestCase):
    def _project(self, root: Path) -> tuple[Path, dict]:
        project = root / "project"
        compose_project(LONG_PROMPT, project)
        plan = build_chunk_plan(build_spec())
        (project / "chunk_plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return project, plan

    def test_each_chunk_renders_from_the_previous_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, plan = self._project(Path(temp))
            opener = scripted_chunk_opener(len(plan["chunks"]))
            client = AceStepClient(AceStepConfig(), opener=opener)

            manifest = render_chunk_plan(
                project, client, plan, poll_interval=0, wait_timeout=1
            )

            self.assertEqual(len(manifest.steps), len(plan["chunks"]))
            self.assertIsNone(manifest.steps[0]["source_audio_sha256"])
            for previous, step in zip(manifest.steps, manifest.steps[1:]):
                self.assertEqual(
                    step["source_audio_sha256"], previous["rendered_audio_sha256"]
                )

    def test_the_final_audio_is_the_last_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, plan = self._project(Path(temp))
            client = AceStepClient(
                AceStepConfig(), opener=scripted_chunk_opener(len(plan["chunks"]))
            )

            manifest = render_chunk_plan(
                project, client, plan, poll_interval=0, wait_timeout=1
            )

            log = json.loads(manifest.log_file.read_text(encoding="utf-8"))
            self.assertEqual(log["execution_state"], "rendered")
            self.assertEqual(
                log["final_audio_sha256"], manifest.steps[-1]["rendered_audio_sha256"]
            )
            self.assertTrue(manifest.audio_file.is_file())

    def test_every_step_keeps_its_own_audit_trail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, plan = self._project(Path(temp))
            client = AceStepClient(
                AceStepConfig(), opener=scripted_chunk_opener(len(plan["chunks"]))
            )

            render_chunk_plan(project, client, plan, poll_interval=0, wait_timeout=1)

            steps = sorted((project / "chunks").iterdir())
            self.assertEqual(len(steps), len(plan["chunks"]))
            for step in steps:
                self.assertTrue((step / "ace_step_result.json").is_file())
                self.assertTrue((step / "song_spec.json").is_file())

    def test_a_plan_from_another_spec_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, plan = self._project(Path(temp))
            client = AceStepClient(AceStepConfig(), opener=scripted_chunk_opener(1))

            with self.assertRaises(ValueError):
                render_chunk_plan(
                    project,
                    client,
                    dict(plan, song_spec_sha256="0" * 64),
                    poll_interval=0,
                    wait_timeout=1,
                )

    def test_rendering_refuses_to_replace_existing_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, plan = self._project(Path(temp))
            audio = project / "audio"
            audio.mkdir()
            (audio / "ace-step-01.wav").write_bytes(b"RIFFkeep")
            client = AceStepClient(AceStepConfig(), opener=scripted_chunk_opener(1))

            with self.assertRaises(FileExistsError):
                render_chunk_plan(project, client, plan, poll_interval=0, wait_timeout=1)

            self.assertEqual((audio / "ace-step-01.wav").read_bytes(), b"RIFFkeep")


class ChunkCliTests(unittest.TestCase):
    def test_plan_chunks_writes_a_plan_and_no_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(LONG_PROMPT, project)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = main(["plan-chunks", str(project)])

            self.assertEqual(status, 0)
            output = stdout.getvalue()
            self.assertIn("4 chunks over 136 bars", output)
            self.assertIn("(+tail guard)", output)
            self.assertIn("nothing rendered yet", output)
            self.assertTrue((project / "chunk_plan.json").is_file())
            self.assertFalse((project / "audio").exists())

    def test_plan_chunks_refuses_to_overwrite_without_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(LONG_PROMPT, project)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                main(["plan-chunks", str(project)])
                refused = main(["plan-chunks", str(project)])
                allowed = main(["plan-chunks", str(project), "--overwrite"])

            self.assertNotEqual(refused, 0)
            self.assertEqual(allowed, 0)


if __name__ == "__main__":
    unittest.main()
