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
    parse_send_binding,
    split_drum_notes,
    build_arrangement_plan,
    beats_per_bar,
    parse_automation_binding,
    plan_project_arrangement,
)
from kihachi_music_ai.cli import main
from kihachi_music_ai.midi import MidiNote, read_midi
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
            "apply_live_instrument_selection",
            "apply_live_drum_kit",
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

    def test_tonal_tracks_get_semantic_instrument_selection_before_clips(self) -> None:
        assignments = [
            op
            for op in self.plan["operations"]
            if op["op"] == "apply_live_instrument_selection"
        ]

        self.assertEqual(
            [(op["params"]["track_index"], op["params"]["role"]) for op in assignments],
            [(1, "bass"), (2, "chords")],
        )
        self.assertTrue(all(op["params"]["genre"] == "edm" for op in assignments))
        self.assertTrue(all(op["params"]["mood"] == "dark" for op in assignments))
        operations = [op["op"] for op in self.plan["operations"]]
        self.assertLess(
            max(i for i, op in enumerate(operations) if op == "apply_live_instrument_selection"),
            min(i for i, op in enumerate(operations) if op == "create_midi_clip"),
        )

    def test_drums_ask_for_a_kit_not_a_silent_empty_sampler(self) -> None:
        instruments = [
            op["params"]
            for op in self.plan["operations"]
            if op["op"] == "apply_live_instrument_selection"
        ]
        kits = [
            op["params"]
            for op in self.plan["operations"]
            if op["op"] == "apply_live_drum_kit"
        ]

        # Drums never travel the device-insertion path: that is what produced a
        # silent Drum Rack.
        self.assertNotIn("drums", [params["role"] for params in instruments])
        self.assertEqual([params["role"] for params in kits], ["drums"])
        self.assertEqual(kits[0]["track_index"], 0)
        self.assertEqual(kits[0]["genre"], "edm")
        self.assertEqual(kits[0]["mood"], "dark")
        self.assertFalse(
            any("silent empty sampler" in warning for warning in self.plan["warnings"])
        )

    def test_kihachi_never_names_a_live_preset_or_browser_path(self) -> None:
        # The boundary this whole operation exists to keep: musical intent only.
        # The key set is also AbletonGPT's `apply_live_drum_kit` signature, which
        # takes no `live_edition` -- sending one failed the call, and this test
        # pinned the wrong set until the plan was run against a real Live Set on
        # 2026-08-16. Instrument selection is the tool that takes an edition.
        for op in self.plan["operations"]:
            if op["op"] != "apply_live_drum_kit":
                continue
            self.assertEqual(
                set(op["params"]),
                {"track_index", "role", "genre", "mood"},
            )
        serialised = json.dumps(self.plan)
        for leaked in ("Core Kit", ".adg", "query:", "Drum Rack", "Impulse"):
            self.assertNotIn(leaked, serialised)

    def test_every_split_drum_track_gets_its_own_kit(self) -> None:
        plan = build_arrangement_plan(self.spec, self.tracks, split_drums=True)
        kits = [
            op["params"]["role"]
            for op in plan["operations"]
            if op["op"] == "apply_live_drum_kit"
        ]

        # A split track with no instrument is silent however few pitches it plays.
        self.assertEqual(kits, ["kick", "snare", "percussion"])

    def test_a_kit_is_requested_before_the_drum_clip_is_created(self) -> None:
        operations = [op["op"] for op in self.plan["operations"]]

        self.assertLess(
            operations.index("apply_live_drum_kit"),
            operations.index("create_midi_clip"),
        )

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

    def test_live_note_onsets_are_unique(self) -> None:
        for op in self.plan["operations"]:
            if op["op"] != "create_midi_clip":
                continue
            notes = op["params"]["notes"]
            onsets = {(note["pitch"], note["start_time"]) for note in notes}
            self.assertEqual(len(notes), len(onsets), op["params"]["name"])

    def test_a_note_nudged_past_the_last_bar_is_pulled_back_not_dropped(self) -> None:
        song_beats = self.spec.song.total_bars * beats_per_bar(self.spec)
        plan = build_arrangement_plan(
            self.spec,
            {"bass": (MidiNote(42, song_beats + 0.01, 0.3, 100),)},
        )
        clip = next(
            op for op in plan["operations"] if op["op"] == "create_midi_clip"
        )

        self.assertEqual(len(clip["params"]["notes"]), 1)
        self.assertLess(clip["params"]["notes"][0]["start_time"], song_beats)

    def test_same_pitch_and_start_keep_velocity_then_duration(self) -> None:
        plan = build_arrangement_plan(
            self.spec,
            {
                "bass": (
                    MidiNote(42, 1.0, 0.125, 42),
                    MidiNote(42, 1.0, 0.3, 100),
                    MidiNote(42, 2.0, 0.2, 100),
                    MidiNote(42, 2.0, 0.3, 100),
                )
            },
        )
        clip = next(
            op for op in plan["operations"] if op["op"] == "create_midi_clip"
        )

        self.assertEqual(
            clip["params"]["notes"],
            [
                {"pitch": 42, "start_time": 1.0, "duration": 0.3, "velocity": 100},
                {"pitch": 42, "start_time": 2.0, "duration": 0.3, "velocity": 100},
            ],
        )
        self.assertEqual(plan["tracks"][0]["notes"], 2)
        self.assertTrue(
            any("2 same-pitch/same-start" in item for item in plan["warnings"])
        )

    def test_same_pitch_overlap_ends_at_the_next_onset(self) -> None:
        plan = build_arrangement_plan(
            self.spec,
            {
                "bass": (
                    MidiNote(42, 1.0, 1.0, 100),
                    MidiNote(42, 1.5, 0.3, 90),
                )
            },
        )
        clip = next(
            op for op in plan["operations"] if op["op"] == "create_midi_clip"
        )

        self.assertEqual(clip["params"]["notes"][0]["duration"], 0.5)
        self.assertTrue(any("1 same-pitch overlap" in item for item in plan["warnings"]))

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
            if op["op"] in {
                "apply_live_instrument_selection",
                "create_midi_clip",
                "copy_session_clip_to_arrangement",
            }:
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

    def test_an_envelope_plan_is_now_inside_the_importer_contract(self) -> None:
        """AbletonGPT0.2#137 added the handler; before it, this plan applied nothing.

        `import-kihachi` rejects the whole document on the first operation it
        does not know, so a plan whose only unsupported operation was an
        envelope created nothing at all -- and said so only at import time.
        """

        plan = build_arrangement_plan(
            self.spec, self.tracks, automation=[ECHO_DRY_WET]
        )

        self.assertFalse(
            any("import-kihachi" in warning for warning in plan["warnings"])
        )

    def test_a_plan_the_importer_accepts_carries_no_such_warning(self) -> None:
        plan = build_arrangement_plan(self.spec, self.tracks)

        self.assertFalse(
            any("import-kihachi" in warning for warning in plan["warnings"])
        )

    def test_an_operation_outside_the_contract_is_still_named(self) -> None:
        """The guard outlives the gap it was written for.

        Nothing the planner emits trips it today. It is kept for the next
        operation added here before the adapter has a handler for it, which is
        exactly how the last two went unnoticed until a plan was run.
        """

        from kihachi_music_ai.ableton import _job_pipeline_warnings

        warnings = _job_pipeline_warnings(
            [{"op": "set_tempo", "params": {}}, {"op": "delete_track", "params": {}}]
        )

        self.assertEqual(len(warnings), 1)
        self.assertIn("delete_track", warnings[0])
        self.assertNotIn("set_tempo", warnings[0])

    def test_no_automation_means_no_envelope_operations(self) -> None:
        plan = build_arrangement_plan(self.spec, self.tracks)

        self.assertNotIn(
            "set_clip_parameter_envelope", [op["op"] for op in plan["operations"]]
        )

    def test_an_inverted_range_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_arrangement_plan(
                self.spec,
                self.tracks,
                automation=[{**ECHO_DRY_WET, "low": 0.6, "high": 0.4}],
            )

    def test_a_device_range_outside_zero_to_one_is_allowed(self) -> None:
        """Live checks the value against the parameter, and few are 0..1.

        A Drum Rack gain runs 0..127 and an Operator transpose -48..48. Forcing
        the range into 0..1 is what made 0.38 land as 0.3% of a gain instead of
        38% of it, silently, because Live accepted it.
        """

        plan = build_arrangement_plan(
            self.spec,
            self.tracks,
            automation=[{**ECHO_DRY_WET, "low": -12.0, "high": 24.0}],
        )

        steps = next(
            op["params"]["steps"]
            for op in plan["operations"]
            if op["op"] == "set_clip_parameter_envelope"
        )
        self.assertTrue(all(-12.0 <= step["value"] <= 24.0 for step in steps))
        self.assertTrue(any(step["value"] > 1.0 for step in steps))

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
            self.assertIn("instrument: track 1 role bass (edm, dark", printed)
            self.assertIn("instrument: track 2 role chords (edm, dark", printed)
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


class TwelveTrackLayoutTests(unittest.TestCase):
    """The Live layout from the architecture diagram."""

    def setUp(self) -> None:
        self.spec = MusicBrain(seed=8).analyze(
            "Mutation Funk、DUB。110 BPM、D#m。5分程度。"
            "スラップベース、サブベース、シンセスタブ、アルペジオ、ボコーダー。"
        )
        self.tracks = compose_tracks(self.spec)

    def test_the_drum_part_becomes_three_tracks_without_losing_a_note(self) -> None:
        """The .mid stays one channel-10 track; only the Live layout splits."""

        plan = build_arrangement_plan(self.spec, self.tracks, split_drums=True)

        drum_rows = [t for t in plan["tracks"] if t["part"] in {"kick", "snare", "percussion"}]
        self.assertEqual(
            [row["part"] for row in drum_rows], ["kick", "snare", "percussion"]
        )
        self.assertEqual(
            [row["name"] for row in drum_rows],
            ["KIHACHI Kick", "KIHACHI Drums", "KIHACHI Percussion"],
        )
        self.assertEqual(
            sum(row["notes"] for row in drum_rows), len(self.tracks["drums"])
        )

    def test_the_split_sorts_by_role_not_by_accident(self) -> None:
        rows = split_drum_notes(self.tracks["drums"])
        by_role = {role: notes for role, _label, notes in rows}

        self.assertTrue(all(n.pitch in (35, 36) for n in by_role["kick"]))
        self.assertTrue(all(n.pitch in (37, 38, 39, 40) for n in by_role["snare"]))
        self.assertTrue(all(n.pitch not in (35, 36, 37, 38, 39, 40) for n in by_role["percussion"]))

    def test_without_the_flag_the_kit_stays_one_track(self) -> None:
        plan = build_arrangement_plan(self.spec, self.tracks)

        self.assertEqual([t["part"] for t in plan["tracks"]].count("drums"), 1)
        self.assertNotIn("kick", [t["part"] for t in plan["tracks"]])

    def test_tracks_come_out_in_the_order_the_layout_asks_for(self) -> None:
        plan = build_arrangement_plan(self.spec, self.tracks, split_drums=True)

        self.assertEqual(
            [t["part"] for t in plan["tracks"]],
            ["kick", "snare", "percussion", "bass", "sub", "chords", "synth", "arp", "vocoder"],
        )

    def test_extended_tonal_roles_are_mapped_without_faking_the_vocoder(self) -> None:
        plan = build_arrangement_plan(self.spec, self.tracks, split_drums=True)
        assignments = [
            op["params"]
            for op in plan["operations"]
            if op["op"] == "apply_live_instrument_selection"
        ]

        self.assertEqual(
            [params["role"] for params in assignments],
            ["bass", "bass", "chords", "chords", "pluck"],
        )
        # Drums are no longer a gap in the layout -- they take the kit path --
        # but the vocoder still is, and inventing an instrument for it would be
        # the same mistake in a new place.
        self.assertFalse(any("silent empty sampler" in item for item in plan["warnings"]))
        self.assertTrue(any("carrier/modulator" in item for item in plan["warnings"]))

    def test_automation_that_targets_nothing_is_refused_not_ignored(self) -> None:
        """Silently applying a whole-kit binding to the snare is worse than failing."""

        binding = {**ECHO_DRY_WET, "part": "drums"}
        with self.assertRaises(ValueError) as caught:
            build_arrangement_plan(
                self.spec, self.tracks, split_drums=True, automation=[binding]
            )

        message = str(caught.exception)
        self.assertIn("drums", message)
        self.assertIn("kick", message)

    def test_the_same_binding_is_fine_when_the_kit_is_one_track(self) -> None:
        binding = {**ECHO_DRY_WET, "part": "drums"}

        plan = build_arrangement_plan(self.spec, self.tracks, automation=[binding])

        self.assertIn(
            "set_clip_parameter_envelope", [op["op"] for op in plan["operations"]]
        )

    def test_a_typo_in_a_binding_is_caught(self) -> None:
        with self.assertRaises(ValueError):
            build_arrangement_plan(
                self.spec, self.tracks, automation=[{**ECHO_DRY_WET, "part": "chord"}]
            )


class AudioTrackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = MusicBrain(seed=8).analyze(LONG_PROMPT)
        self.tracks = compose_tracks(self.spec)

    def test_audio_tracks_land_after_every_midi_track(self) -> None:
        """Inserting them earlier would shift the indices the clips were written for."""

        with tempfile.TemporaryDirectory() as temp:
            take = Path(temp) / "take.wav"
            take.write_bytes(b"RIFF____WAVEfmt ")

            plan = build_arrangement_plan(
                self.spec,
                self.tracks,
                audio_tracks=[{"role": "reference", "name": "ACE-Step Ref", "file": take}],
            )

            rows = plan["tracks"]
            self.assertEqual(rows[-1]["part"], "reference")
            self.assertEqual(rows[-1]["live_track_index"], len(rows) - 1)
            self.assertTrue(all("notes" in row for row in rows[:-1]))

    def test_imported_audio_reaches_the_arrangement_like_every_part(self) -> None:
        """It did not, and only running a plan into Live showed it.

        Three MIDI clips landed on the timeline and the imported sample stayed in
        its Session slot, because `import_vocal_take` had no copy after it. The
        tests all passed: none of them looked.
        """

        with tempfile.TemporaryDirectory() as temp:
            take = Path(temp) / "groove-a.wav"
            take.write_bytes(b"RIFF....WAVEfmt ")

            plan = build_arrangement_plan(
                self.spec,
                self.tracks,
                audio_tracks=[{"role": "reference", "name": "ACE-Step Ref", "file": take}],
            )

            ops = [op["op"] for op in plan["operations"]]
            imported = ops.index("import_vocal_take")
            self.assertEqual(ops[imported + 1], "copy_session_clip_to_arrangement")
            copy = plan["operations"][imported + 1]["params"]
            audio_row = plan["tracks"][-1]
            self.assertEqual(copy["track_index"], audio_row["live_track_index"])
            self.assertEqual(copy["destination_time_beats"], 0.0)

    def test_an_empty_audio_track_has_no_clip_to_copy(self) -> None:
        plan = build_arrangement_plan(
            self.spec, self.tracks, audio_tracks=[{"role": "fx", "name": "KIHACHI FX"}]
        )

        copies = [
            op for op in plan["operations"]
            if op["op"] == "copy_session_clip_to_arrangement"
        ]
        audio_row = plan["tracks"][-1]
        self.assertNotIn(
            audio_row["live_track_index"], [op["params"]["track_index"] for op in copies]
        )

    def test_a_reference_import_names_a_file_that_exists(self) -> None:
        with self.assertRaises(FileNotFoundError):
            build_arrangement_plan(
                self.spec,
                self.tracks,
                audio_tracks=[{"name": "ACE-Step Ref", "file": "/no/such/take.wav"}],
            )

    def test_an_fx_track_is_created_empty(self) -> None:
        plan = build_arrangement_plan(
            self.spec, self.tracks, audio_tracks=[{"role": "fx", "name": "KIHACHI FX"}]
        )

        created = [
            op for op in plan["operations"]
            if op["op"] == "create_track" and op["params"]["track_type"] == "audio"
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["params"]["name"], "KIHACHI FX")
        self.assertNotIn("import_vocal_take", [op["op"] for op in plan["operations"]])


class SendTests(unittest.TestCase):
    """A dub delay throw follows the SongSpec section by section."""

    def setUp(self) -> None:
        self.spec = MusicBrain(seed=8).analyze(LONG_PROMPT)
        self.tracks = compose_tracks(self.spec)

    def _sends(self, plan):
        return [op for op in plan["operations"] if op["op"] == "set_clip_send_envelope"]

    def test_a_send_range_outside_zero_to_one_is_still_refused(self) -> None:
        """Unlike a device parameter, a send really is 0..1, so the bound holds."""

        for bad in ({"low": -0.1, "high": 0.5}, {"low": 0.2, "high": 1.4}):
            with self.assertRaises(ValueError):
                build_arrangement_plan(
                    self.spec,
                    self.tracks,
                    sends=[{"part": "chords", "send_index": 0, **bad}],
                )

    def test_a_send_targets_the_track_the_part_landed_on(self) -> None:
        plan = build_arrangement_plan(
            self.spec, self.tracks, first_track_index=4, sends=[{"part": "chords", "send_index": 1}]
        )

        chords = next(t for t in plan["tracks"] if t["part"] == "chords")
        self.assertEqual(self._sends(plan)[0]["params"]["track_index"], chords["live_track_index"])
        self.assertEqual(self._sends(plan)[0]["params"]["send_index"], 1)

    def test_one_step_per_section_uses_the_song_grid(self) -> None:
        plan = build_arrangement_plan(
            self.spec, self.tracks, sends=[{"part": "chords", "send_index": 1}]
        )
        steps = self._sends(plan)[0]["params"]["steps"]

        self.assertEqual(len(steps), len(self.spec.arrangement))
        for step, section in zip(steps, self.spec.arrangement):
            self.assertEqual(step["start"], section.start_bar * 4.0)
            self.assertEqual(step["length"], section.length_bars * 4.0)

    def test_values_come_from_each_section_and_respect_the_range(self) -> None:
        plain = build_arrangement_plan(
            self.spec, self.tracks, sends=[{"part": "chords", "send_index": 0}]
        )
        scaled = build_arrangement_plan(
            self.spec,
            self.tracks,
            sends=[{"part": "chords", "send_index": 0, "low": 0.0, "high": 0.5}],
        )

        full = [step["value"] for step in self._sends(plain)[0]["params"]["steps"]]
        half = [step["value"] for step in self._sends(scaled)[0]["params"]["steps"]]
        self.assertEqual(
            len(set(full)),
            len(set(section.fx_amount for section in self.spec.arrangement)),
        )
        for full_value, half_value in zip(full, half):
            self.assertAlmostEqual(half_value, full_value * 0.5, places=5)

    def test_the_envelope_is_written_before_the_clip_is_copied(self) -> None:
        plan = build_arrangement_plan(
            self.spec, self.tracks, sends=[{"part": "chords", "send_index": 1}]
        )
        operations = plan["operations"]
        envelope = operations.index(self._sends(plan)[0])

        self.assertEqual(operations[envelope - 1]["op"], "create_midi_clip")
        self.assertEqual(operations[envelope + 1]["op"], "copy_session_clip_to_arrangement")
        self.assertFalse(any("one level for the whole song" in warning for warning in plan["warnings"]))

    def test_a_send_to_a_part_that_is_not_in_the_plan_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            build_arrangement_plan(
                # this brief asks for no synth, so no such track exists
                self.spec, self.tracks, sends=[{"part": "synth", "send_index": 0}]
            )

        self.assertIn("targets no track", str(caught.exception))

    def test_a_bad_range_is_refused(self) -> None:
        for bad in ({"low": 0.6, "high": 0.4}, {"low": -0.1, "high": 0.5}):
            with self.assertRaises(ValueError):
                build_arrangement_plan(
                    self.spec,
                    self.tracks,
                    sends=[{"part": "chords", "send_index": 0, **bad}],
                )

    def test_no_fx_amount_is_refused_rather_than_guessed(self) -> None:
        bare = dataclasses.replace(
            self.spec,
            arrangement=tuple(
                dataclasses.replace(section, fx_amount=None)
                for section in self.spec.arrangement
            ),
        )
        with self.assertRaises(ValueError):
            build_arrangement_plan(
                bare,
                compose_tracks(bare),
                sends=[{"part": "chords", "send_index": 0}],
            )

    def test_binding_syntax(self) -> None:
        self.assertEqual(
            parse_send_binding("chords:1"), {"part": "chords", "send_index": 1}
        )
        self.assertEqual(parse_send_binding("bass:0:0.2:0.8")["high"], 0.8)
        for bad in ("chords", "chords:x", "chords:-1", "guitar:0", "chords:0:0.2"):
            with self.assertRaises(ValueError, msg=bad):
                parse_send_binding(bad)


class SafetyDeclarationTests(unittest.TestCase):
    """The safety block must describe what the plan does, not what it intended."""

    def setUp(self) -> None:
        self.spec = build_spec()
        self.tracks = compose_tracks(self.spec)

    def _plan(self, **kwargs):
        return build_arrangement_plan(self.spec, self.tracks, **kwargs)

    def _assert_count_matches(self, plan) -> None:
        actual = sum(1 for op in plan["operations"] if op["op"] == "create_track")
        self.assertEqual(plan["safety"]["creates_tracks"], actual)

    def test_creates_tracks_matches_the_operations(self) -> None:
        self._assert_count_matches(self._plan())

    def test_creates_tracks_counts_every_split_drum_track(self) -> None:
        # One composed part becomes three Live tracks; the count used to say one.
        plan = self._plan(split_drums=True)

        self._assert_count_matches(plan)
        self.assertEqual(
            plan["safety"]["creates_tracks"], self._plan()["safety"]["creates_tracks"] + 2
        )

    def test_creates_tracks_counts_an_empty_audio_track(self) -> None:
        plan = self._plan(audio_tracks=[{"name": "Reference", "role": "reference"}])

        self._assert_count_matches(plan)

    def test_creates_tracks_matches_with_both_at_once(self) -> None:
        self._assert_count_matches(
            self._plan(
                split_drums=True,
                audio_tracks=[{"name": "Reference", "role": "reference"}],
            )
        )

    def test_the_track_offset_is_declared_so_the_no_modify_claim_is_checkable(self) -> None:
        plan = self._plan(first_track_index=7)

        self.assertEqual(plan["safety"]["first_track_index"], 7)
        self.assertFalse(plan["safety"]["modifies_existing_tracks"])
        # and it must agree with where the plan actually writes
        targeted = [
            op["params"]["track_index"]
            for op in plan["operations"]
            if "track_index" in op.get("params", {})
        ]
        self.assertEqual(min(targeted), 7)
