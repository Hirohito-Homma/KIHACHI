"""``prompt.json``: the prompt in a form a machine can read, with no vendor in it.

Written because there is no ACE-Step server to talk to yet, and the only
structured form of the prompt was ACE-Step's own request body.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from kihachi_music_ai.adapters.ace_step import AceStepGenerationRequest
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.pipeline import ARTIFACT_NAMES, compose_project
from kihachi_music_ai.prompt_compiler import (
    brief_matches_spec,
    compile_audio_prompt,
    load_render_brief,
    render_brief,
)


class RenderBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = MusicBrain(seed=8).analyze("レゲエ。Am。2分程度。")

    def test_it_carries_the_same_prompt_text_as_prompt_txt(self) -> None:
        self.assertEqual(
            render_brief(self.spec)["prompt"], compile_audio_prompt(self.spec).strip()
        )

    def test_it_ties_itself_to_the_spec_it_was_compiled_from(self) -> None:
        digest = sha256(self.spec.to_json().encode("utf-8")).hexdigest()

        self.assertEqual(render_brief(self.spec)["song_spec_sha256"], digest)

    def test_it_names_no_renderer_and_no_renderer_option(self) -> None:
        """The point of the file: it is not one vendor's request body."""

        brief = render_brief(self.spec)
        flat = json.dumps(brief, ensure_ascii=False).lower()

        self.assertNotIn("ace", flat.replace("space", "").replace("trace", ""))
        vendor_fields = set(AceStepGenerationRequest.__annotations__) - {
            "prompt",
            "lyrics",
            "seed",
            "bpm",
            "key_scale",
            "time_signature",
        }
        self.assertEqual(vendor_fields & set(brief), set())

    def test_it_is_plain_json_data(self) -> None:
        # No dataclasses, no tuples: something else has to read this.
        json.loads(json.dumps(render_brief(self.spec)))

    def test_the_tail_guard_only_lengthens_the_duration_when_asked(self) -> None:
        plain = render_brief(self.spec)["song"]["duration_sec"]
        guarded = render_brief(self.spec, tail_guard_bars=2)["song"]["duration_sec"]

        self.assertEqual(plain, self.spec.song.target_duration_sec)
        self.assertGreater(guarded, plain)

    def test_compose_writes_it_next_to_the_text_prompt(self) -> None:
        self.assertIn("prompt.json", ARTIFACT_NAMES)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "song"
            compose_project("レゲエ。Am。2分程度。", output, seed=8)
            brief = json.loads((output / "prompt.json").read_text(encoding="utf-8"))
            spec_text = (output / "song_spec.json").read_text(encoding="utf-8")

            self.assertEqual(
                brief["song_spec_sha256"], sha256(spec_text.encode("utf-8")).hexdigest()
            )
            self.assertEqual(
                brief["prompt"], (output / "prompt.txt").read_text(encoding="utf-8").strip()
            )




class LoadRenderBriefTests(unittest.TestCase):
    def _write(self, temp: Path, **changes) -> Path:
        brief = render_brief(MusicBrain(seed=8).analyze("レゲエ。Am。2分程度。"))
        brief.update(changes)
        path = temp / "prompt.json"
        path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        return path

    def test_a_brief_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(Path(temp))

            self.assertEqual(load_render_brief(path)["version"], "0.1")

    def test_a_hand_edited_prompt_is_accepted(self) -> None:
        """The whole reason the file exists while no renderer is connected."""

        with tempfile.TemporaryDirectory() as temp:
            path = self._write(Path(temp), prompt="Whatever I want to say instead.")

            self.assertEqual(
                load_render_brief(path)["prompt"], "Whatever I want to say instead."
            )

    def test_a_brief_that_cannot_be_rendered_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for changes, expected in (
                ({"prompt": "   "}, "empty"),
                ({"version": "9.9"}, "version"),
                ({"song": {"bpm": 120}}, "missing"),
            ):
                with self.subTest(changes=changes):
                    path = self._write(Path(temp), **changes)
                    with self.assertRaises(ValueError) as caught:
                        load_render_brief(path)
                    self.assertIn(expected, str(caught.exception))

    def test_a_missing_field_is_named_in_the_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "prompt.json"
            path.write_text('{"version": "0.1", "prompt": "x"}', encoding="utf-8")

            with self.assertRaises(ValueError) as caught:
                load_render_brief(path)

            self.assertIn("seed", str(caught.exception))

    def test_a_bad_time_signature_is_caught_here_rather_than_at_the_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            brief = render_brief(MusicBrain(seed=8).analyze("レゲエ。Am。2分程度。"))
            brief["song"]["time_signature"] = "4"
            path = Path(temp) / "prompt.json"
            path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")

            with self.assertRaises(ValueError):
                load_render_brief(path)

    def test_it_notices_a_brief_that_belongs_to_another_spec(self) -> None:
        spec = MusicBrain(seed=8).analyze("レゲエ。Am。2分程度。")
        other = MusicBrain(seed=8).analyze("テクノ。Am。2分程度。")

        self.assertTrue(brief_matches_spec(render_brief(spec), spec))
        self.assertFalse(brief_matches_spec(render_brief(other), spec))


class AceStepFromBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = MusicBrain(seed=8).analyze("レゲエ。Am。2分程度。")

    def test_the_brief_and_the_spec_produce_the_same_request(self) -> None:
        """Nothing changes for a brief that was not touched."""

        from kihachi_music_ai.adapters.ace_step import AceStepGenerationRequest

        from_spec = AceStepGenerationRequest.from_song_spec(self.spec)
        from_brief = AceStepGenerationRequest.from_render_brief(render_brief(self.spec))

        self.assertEqual(from_brief.to_dict(), from_spec.to_dict())

    def test_a_hand_written_prompt_reaches_the_request_unrecompiled(self) -> None:
        from kihachi_music_ai.adapters.ace_step import AceStepGenerationRequest

        brief = render_brief(self.spec)
        brief["prompt"] = "A completely different instruction."
        brief["song"]["duration_sec"] = 95.0

        request = AceStepGenerationRequest.from_render_brief(brief)

        self.assertEqual(request.prompt, "A completely different instruction.")
        self.assertEqual(request.audio_duration, 95.0)

    def test_prepare_writes_the_request_from_a_brief(self) -> None:
        from kihachi_music_ai.adapters.ace_step import prepare_ace_step_request

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "song"
            compose_project("レゲエ。Am。2分程度。", output, seed=8)
            brief_path = output / "prompt.json"
            brief = json.loads(brief_path.read_text(encoding="utf-8"))
            brief["prompt"] += "\nExtra: hand-written, must survive."
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")

            path, request = prepare_ace_step_request(
                output, overwrite=True, brief=brief_path
            )

            self.assertTrue(path.is_file())
            self.assertIn("must survive", request.prompt)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("must survive", written["prompt"])


class RenderFromBriefTests(unittest.TestCase):
    """`render` has to send the brief too, or --from-brief stops at prepare."""

    def test_the_edited_prompt_is_what_gets_submitted(self) -> None:
        from test_ace_step import ScriptedOpener, wrapped

        from kihachi_music_ai.adapters.ace_step import (
            AceStepClient,
            AceStepConfig,
            AceStepOptions,
            render_with_ace_step,
        )

        task_id = "task-brief"
        output = {"file": "/v1/audio?path=%2Ftmp%2Fx.wav", "status": 1, "seed_value": "8"}
        opener = ScriptedOpener(
            [
                wrapped({"task_id": task_id, "status": "queued"}),
                wrapped([{"task_id": task_id, "status": 1, "result": json.dumps([output])}]),
                b"RIFFrendered-wave",
            ]
        )
        client = AceStepClient(AceStepConfig(), opener=opener)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project("レゲエ。Am。2分程度。", project, seed=8)
            brief_path = project / "prompt.json"
            brief = json.loads(brief_path.read_text(encoding="utf-8"))
            brief["prompt"] += "\nExtra: hand-written, must survive."
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")

            manifest = render_with_ace_step(
                project,
                client,
                AceStepOptions(),
                poll_interval=0,
                wait_timeout=1,
                brief=brief_path,
            )

            submitted = json.loads(manifest.request_file.read_text(encoding="utf-8"))
            self.assertIn("must survive", submitted["prompt"])
            result = json.loads(manifest.result_file.read_text(encoding="utf-8"))
            self.assertEqual(result["render_brief"]["path"], str(brief_path.resolve()))
            # Still true: the digest names the spec the brief was compiled
            # from, and editing the prompt does not change that spec. The
            # record is there to catch a brief belonging to another song.
            self.assertTrue(result["render_brief"]["matches_project_spec"])

    def test_the_tail_is_trimmed_to_the_briefs_grid_not_the_specs(self) -> None:
        """A brief with an edited duration must not be cut back to the spec."""

        from kihachi_music_ai.prompt_compiler import brief_grid_duration

        spec = MusicBrain(seed=8).analyze("レゲエ。Am。2分程度。")
        guarded = render_brief(spec, tail_guard_bars=2)

        self.assertGreater(guarded["song"]["duration_sec"], spec.song.target_duration_sec)
        self.assertEqual(brief_grid_duration(guarded), spec.song.target_duration_sec)

    def test_a_brief_with_no_grid_cannot_be_tail_guarded(self) -> None:
        from kihachi_music_ai.prompt_compiler import brief_grid_duration

        brief = render_brief(MusicBrain(seed=8).analyze("レゲエ。Am。2分程度。"))
        del brief["song"]["total_bars"]

        self.assertIsNone(brief_grid_duration(brief))


if __name__ == "__main__":
    unittest.main()
