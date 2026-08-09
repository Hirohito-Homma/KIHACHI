from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.ableton import (
    MAX_NOTES_PER_CLIP,
    build_arrangement_plan,
    beats_per_bar,
    parse_automation_binding,
    plan_project_arrangement,
)
from kihachi_music_ai.cli import main
from kihachi_music_ai.midi import read_midi
from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.composer import compose_tracks
from kihachi_music_ai.models import TRACK_NAMES
from kihachi_music_ai.music_brain import MusicBrain
from test_music_brain import EXAMPLE

LONG_PROMPT = EXAMPLE + "5分程度。"


def build_spec(prompt: str = LONG_PROMPT):
    return MusicBrain(seed=8).analyze(prompt)


class ArrangementPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()
        self.tracks = compose_tracks(self.spec)
        self.plan = build_arrangement_plan(self.spec, self.tracks)

    def test_operations_use_real_abletongpt_tool_names(self) -> None:
        allowed = {
            "set_tempo",
            "create_track",
            "create_midi_clip",
            "copy_session_clip_to_arrangement",
        }
        self.assertTrue(all(op["op"] in allowed for op in self.plan["operations"]))

    def test_tempo_is_set_before_anything_is_created(self) -> None:
        self.assertEqual(self.plan["operations"][0]["op"], "set_tempo")
        self.assertEqual(self.plan["operations"][0]["params"]["bpm"], self.spec.song.bpm)

    def test_one_track_and_one_clip_per_composed_part(self) -> None:
        creates = [op for op in self.plan["operations"] if op["op"] == "create_track"]
        clips = [op for op in self.plan["operations"] if op["op"] == "create_midi_clip"]
        copies = [
            op
            for op in self.plan["operations"]
            if op["op"] == "copy_session_clip_to_arrangement"
        ]

        # the spec's own parts, not every part the system can write
        expected = len(self.spec.parts())
        self.assertEqual(len(creates), expected)
        self.assertEqual(len(clips), expected)
        self.assertEqual(len(copies), expected)

    def test_tracks_are_created_before_their_clips(self) -> None:
        ops = [op["op"] for op in self.plan["operations"]]

        self.assertLess(max(i for i, o in enumerate(ops) if o == "create_track"),
                        min(i for i, o in enumerate(ops) if o == "create_midi_clip"))

    def test_clip_length_matches_the_song_grid(self) -> None:
        expected = self.spec.song.total_bars * beats_per_bar(self.spec)

        for op in self.plan["operations"]:
            if op["op"] == "create_midi_clip":
                self.assertEqual(op["params"]["length_beats"], expected)

    def test_every_note_satisfies_lives_validator(self) -> None:
        # Mirrors AbletonGPT's _validate_midi_clip exactly.
        for op in self.plan["operations"]:
            if op["op"] != "create_midi_clip":
                continue
            length = op["params"]["length_beats"]
            notes = op["params"]["notes"]
            self.assertLessEqual(len(notes), MAX_NOTES_PER_CLIP, op["params"]["name"])
            for note in notes:
                self.assertTrue(0 <= note["pitch"] <= 127)
                self.assertGreaterEqual(note["start_time"], 0)
                self.assertLess(note["start_time"], length)
                self.assertGreater(note["duration"], 0)
                self.assertLessEqual(note["start_time"] + note["duration"], length + 1e-6)
                self.assertTrue(0 <= note["velocity"] <= 127)

    def test_a_note_nudged_past_the_last_bar_is_pulled_back_not_dropped(self) -> None:
        total = sum(
            len(op["params"]["notes"])
            for op in self.plan["operations"]
            if op["op"] == "create_midi_clip"
        )

        self.assertEqual(total, sum(len(notes) for notes in self.tracks.values()))

    def test_notes_are_ordered_in_time(self) -> None:
        for op in self.plan["operations"]:
            if op["op"] == "create_midi_clip":
                starts = [note["start_time"] for note in op["params"]["notes"]]
                self.assertEqual(starts, sorted(starts))

    def test_clips_land_on_the_arrangement_at_bar_one(self) -> None:
        for op in self.plan["operations"]:
            if op["op"] == "copy_session_clip_to_arrangement":
                self.assertEqual(op["params"]["destination_time_beats"], 0.0)

    def test_structure_records_where_each_part_rests(self) -> None:
        breakdown = next(
            item for item in self.plan["structure"] if item["name"] == "dub_breakdown"
        )

        self.assertEqual(breakdown["resting_tracks"], ["drums"])
        self.assertNotIn("drums", breakdown["active_tracks"])

    def test_structure_covers_the_song_contiguously(self) -> None:
        cursor = 0.0
        for item in self.plan["structure"]:
            self.assertEqual(item["start_beats"], cursor)
            cursor = item["end_beats"]
        self.assertEqual(cursor, self.plan["song"]["total_beats"])

    def test_the_plan_changes_nothing_by_itself(self) -> None:
        self.assertEqual(self.plan["execution_state"], "planned_not_applied")
        self.assertFalse(self.plan["safety"]["modifies_existing_tracks"])
        self.assertTrue(self.plan["safety"]["deletes_nothing"])

    def test_track_indices_can_be_offset_past_existing_tracks(self) -> None:
        plan = build_arrangement_plan(self.spec, self.tracks, first_track_index=4)

        indices = [item["live_track_index"] for item in plan["tracks"]]
        self.assertEqual(indices, list(range(4, 4 + len(self.spec.parts()))))
        for op in plan["operations"]:
            if op["op"] in {"create_midi_clip", "copy_session_clip_to_arrangement"}:
                self.assertGreaterEqual(op["params"]["track_index"], 4)

    def test_a_song_too_long_for_a_live_clip_is_refused(self) -> None:
        huge = build_spec(EXAMPLE.replace("110 BPM", "110 BPM") + "60分程度。")
        if huge.song.total_bars * beats_per_bar(huge) <= 4096:
            self.skipTest("prompt did not produce an over-long song")
        with self.assertRaises(ValueError):
            build_arrangement_plan(huge, compose_tracks(huge))

    def test_the_plan_serialises(self) -> None:
        json.dumps(self.plan)


if __name__ == "__main__":
    unittest.main()


ECHO_DRY_WET = {
    "part": "chords",
    "field": "fx_amount",
    "device_index": 1,
    "parameter_index": 52,
    "low": 0.18,
    "high": 0.52,
}


class AutomationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()
        self.tracks = compose_tracks(self.spec)
        self.plan = build_arrangement_plan(
            self.spec, self.tracks, automation=[ECHO_DRY_WET]
        )

    def _ops(self):
        return [op["op"] for op in self.plan["operations"]]

    def test_the_envelope_is_written_before_the_clip_leaves_the_session(self) -> None:
        # Live only exposes clip envelopes on Session clips, so this ordering is
        # the whole reason the automation reaches the Arrangement at all.
        ops = self._ops()
        envelope = ops.index("set_clip_parameter_envelope")

        self.assertEqual(ops[envelope - 1], "create_midi_clip")
        self.assertEqual(ops[envelope + 1], "copy_session_clip_to_arrangement")

    def test_one_step_per_section_aligned_to_the_bar_grid(self) -> None:
        steps = next(
            op["params"]["steps"]
            for op in self.plan["operations"]
            if op["op"] == "set_clip_parameter_envelope"
        )

        self.assertEqual(len(steps), len(self.spec.arrangement))
        cursor = 0.0
        for step, section in zip(steps, self.spec.arrangement):
            self.assertEqual(step["start"], cursor)
            self.assertEqual(step["length"], section.length_bars * 4.0)
            cursor += step["length"]
        self.assertEqual(cursor, self.plan["song"]["total_beats"])

    def test_values_are_mapped_into_the_requested_range(self) -> None:
        steps = next(
            op["params"]["steps"]
            for op in self.plan["operations"]
            if op["op"] == "set_clip_parameter_envelope"
        )

        for step, section in zip(steps, self.spec.arrangement):
            expected = 0.18 + section.fx_amount * (0.52 - 0.18)
            self.assertAlmostEqual(step["value"], expected, places=6)
        # the drumless dub breakdown is the wettest point of the song
        breakdown = next(
            index
            for index, section in enumerate(self.spec.arrangement)
            if section.name == "dub_breakdown"
        )
        self.assertEqual(max(steps, key=lambda s: s["value"]), steps[breakdown])

    def test_automation_targets_only_the_named_part(self) -> None:
        envelopes = [
            op for op in self.plan["operations"] if op["op"] == "set_clip_parameter_envelope"
        ]

        self.assertEqual(len(envelopes), 1)
        chords_track = next(
            item["live_track_index"] for item in self.plan["tracks"] if item["part"] == "chords"
        )
        self.assertEqual(envelopes[0]["params"]["track_index"], chords_track)

    def test_no_automation_means_no_envelope_operations(self) -> None:
        plan = build_arrangement_plan(self.spec, self.tracks)

        self.assertNotIn(
            "set_clip_parameter_envelope", [op["op"] for op in plan["operations"]]
        )

    def test_a_bad_range_is_refused(self) -> None:
        for bad in ({"low": 0.6, "high": 0.4}, {"low": -0.1, "high": 0.5}, {"low": 0.2, "high": 1.4}):
            with self.assertRaises(ValueError):
                build_arrangement_plan(
                    self.spec, self.tracks, automation=[{**ECHO_DRY_WET, **bad}]
                )

    def test_a_field_no_section_carries_is_refused_rather_than_guessed(self) -> None:
        bare = dataclasses.replace(
            self.spec,
            arrangement=tuple(
                dataclasses.replace(section, fx_amount=None)
                for section in self.spec.arrangement
            ),
        )
        with self.assertRaises(ValueError):
            build_arrangement_plan(bare, compose_tracks(bare), automation=[ECHO_DRY_WET])


class AutomationBindingTests(unittest.TestCase):
    def test_the_short_form_leaves_the_range_to_the_plan(self) -> None:
        binding = parse_automation_binding("chords:fx_amount:1:52")

        self.assertEqual(binding["part"], "chords")
        self.assertEqual(binding["field"], "fx_amount")
        self.assertEqual((binding["device_index"], binding["parameter_index"]), (1, 52))
        self.assertNotIn("low", binding)

    def test_the_long_form_carries_the_range(self) -> None:
        binding = parse_automation_binding("chords:fx_amount:1:52:0.18:0.52")

        self.assertEqual((binding["low"], binding["high"]), (0.18, 0.52))

    def test_malformed_bindings_are_refused_with_the_format_in_the_message(self) -> None:
        for bad in (
            "chords:fx_amount:1",            # too few fields
            "chords:fx_amount:1:52:0.1",     # a range needs both ends
            "guitar:fx_amount:1:52",         # not a composed part
            "chords:fx_amount:one:52",       # indices are integers
            "chords:fx_amount:-1:52",        # and not negative
            "chords:fx_amount:1:52:low:high",
        ):
            with self.assertRaises(ValueError, msg=bad):
                parse_automation_binding(bad)


class ProjectPlanTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "project"
        compose_project(LONG_PROMPT, project)
        return project

    def test_the_plan_describes_the_notes_on_disk_not_the_ones_composed(self) -> None:
        """MIDI is what Live imports, so the plan has to agree with the file.

        Writing quantizes to 480 PPQ, and the format cannot represent two
        overlapping notes of the same pitch unambiguously -- the bass part has
        129 such overlaps. Recomposing in memory would describe notes that never
        reached disk.
        """

        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))

            manifest = plan_project_arrangement(project)

            for operation in manifest.plan["operations"]:
                if operation["op"] != "create_midi_clip":
                    continue
                part = next(
                    track["part"]
                    for track in manifest.plan["tracks"]
                    if track["live_track_index"] == operation["params"]["track_index"]
                )
                on_disk = read_midi(project / f"{part}.mid").notes
                self.assertEqual(len(operation["params"]["notes"]), len(on_disk))
                for planned, actual in zip(
                    sorted(operation["params"]["notes"], key=lambda n: (n["start_time"], n["pitch"])),
                    sorted(on_disk, key=lambda n: (n.start_beats, n.pitch)),
                ):
                    self.assertEqual(planned["pitch"], actual.pitch)
                    self.assertEqual(planned["velocity"], actual.velocity)

    def test_an_existing_plan_is_not_replaced_without_being_asked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            plan_project_arrangement(project)
            authored = (project / "arrangement_plan.json").read_bytes()

            with self.assertRaises(FileExistsError):
                plan_project_arrangement(project)
            self.assertEqual((project / "arrangement_plan.json").read_bytes(), authored)

            plan_project_arrangement(project, overwrite=True)

    def test_a_missing_midi_track_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            (project / "drums.mid").unlink()

            with self.assertRaises(FileNotFoundError) as caught:
                plan_project_arrangement(project)
            self.assertIn("drums.mid", str(caught.exception))

    def test_the_first_track_index_offsets_every_operation(self) -> None:
        """A Live set with existing tracks does not start at index 0."""

        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))

            manifest = plan_project_arrangement(
                project, first_track_index=1, automation=[ECHO_DRY_WET], overwrite=False
            )

            parts = manifest.plan["tracks"]
            self.assertEqual(
                [track["live_track_index"] for track in parts],
                list(range(1, 1 + len(parts))),
            )
            envelope = next(
                op for op in manifest.plan["operations"]
                if op["op"] == "set_clip_parameter_envelope"
            )
            chords = next(t for t in parts if t["part"] == "chords")
            self.assertEqual(envelope["params"]["track_index"], chords["live_track_index"])

    def test_the_cli_reports_the_resting_tracks_and_the_automation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = main(
                    [
                        "ableton-plan",
                        str(project),
                        "--automate",
                        "chords:fx_amount:1:52:0.18:0.52",
                    ]
                )

            self.assertEqual(status, 0)
            printed = out.getvalue()
            # the drumless breakdown is the whole reason for the MIDI path
            self.assertIn("resting: drums", printed)
            self.assertIn("automation: track 2 device 1 parameter 52", printed)
            self.assertIn("planned_not_applied", printed)
            self.assertTrue((project / "arrangement_plan.json").is_file())

    def test_the_cli_refuses_a_malformed_binding_before_writing_anything(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            with contextlib.redirect_stderr(io.StringIO()):
                status = main(["ableton-plan", str(project), "--automate", "chords:fx_amount:1"])

            self.assertEqual(status, 2)
            self.assertFalse((project / "arrangement_plan.json").exists())
