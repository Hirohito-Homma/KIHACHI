from __future__ import annotations

import dataclasses
import json
import unittest

from kihachi_music_ai.ableton import (
    MAX_NOTES_PER_CLIP,
    build_arrangement_plan,
    beats_per_bar,
)
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

        self.assertEqual(len(creates), len(TRACK_NAMES))
        self.assertEqual(len(clips), len(TRACK_NAMES))
        self.assertEqual(len(copies), len(TRACK_NAMES))

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
        self.assertEqual(indices, [4, 5, 6])
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
