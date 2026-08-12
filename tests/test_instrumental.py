from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from kihachi_music_ai.instrumental import (
    plan_instrumental_sections,
    write_instrumental_plan,
)
from kihachi_music_ai.lyrics import TAG_INSTRUMENTAL, build_lyrics
from test_tail_guard import build_spec


def write_project(directory: Path, spec) -> Path:
    project = directory / "project"
    project.mkdir(parents=True)
    (project / "song_spec.json").write_text(spec.to_json(), encoding="utf-8")
    return project


class InstrumentalPlanTest(unittest.TestCase):
    def test_the_plan_matches_the_lyric_sheet_exactly(self):
        """The sheet is the authority; the plan must not decide silence on its own.

        If these ever disagree, a section the writer left wordless would keep its
        vocal, or one with words would be wiped -- so the agreement is the test.
        """
        with tempfile.TemporaryDirectory() as raw:
            spec = build_spec()
            project = write_project(Path(raw), spec)

            plan = plan_instrumental_sections(project)

            from_sheet = [
                entry.section_name
                for entry in build_lyrics(spec).sections
                if entry.tag == TAG_INSTRUMENTAL
            ]
            self.assertEqual([section.name for section in plan.sections], from_sheet)

    def test_bars_are_reported_inclusively_from_the_arrangement(self):
        with tempfile.TemporaryDirectory() as raw:
            spec = build_spec()
            project = write_project(Path(raw), spec)
            by_name = {section.name: section for section in spec.arrangement}

            plan = plan_instrumental_sections(project)

            self.assertTrue(plan.sections, "the fixture is expected to have a wordless section")
            for reported in plan.sections:
                source = by_name[reported.name]
                self.assertEqual(reported.start_bar, source.start_bar)
                self.assertEqual(
                    reported.end_bar, source.start_bar + source.length_bars - 1
                )
                self.assertEqual(reported.length_bars, source.length_bars)

    def test_an_instrumental_song_needs_no_repaint_and_says_why(self):
        with tempfile.TemporaryDirectory() as raw:
            spec = build_spec()
            spec = replace(spec, vocal=replace(spec.vocal, enabled=False))
            project = write_project(Path(raw), spec)

            plan = plan_instrumental_sections(project)

            self.assertFalse(plan.vocal_enabled)
            self.assertIn("no vocal at all", plan.reason)

    def test_commands_name_the_section_and_withhold_the_lyrics(self):
        with tempfile.TemporaryDirectory() as raw:
            project = write_project(Path(raw), build_spec())

            plan = plan_instrumental_sections(project)
            commands = plan.commands(base_url="http://127.0.0.1:8001")

            self.assertEqual(len(commands), len(plan.sections))
            for command, section in zip(commands, plan.sections):
                self.assertIn(f"--repaint-section {section.name}", command)
                # Withholding the words is the whole mechanism; instructing fails.
                self.assertIn("--no-lyrics", command)
                self.assertIn("--task-type repaint", command)

    def test_planning_writes_nothing_unless_asked(self):
        with tempfile.TemporaryDirectory() as raw:
            project = write_project(Path(raw), build_spec())

            plan_instrumental_sections(project)
            self.assertEqual(
                sorted(item.name for item in project.iterdir()), ["song_spec.json"]
            )

            destination = write_instrumental_plan(project)
            stored = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(stored["instrumental_plan_version"], "0.1")
            with self.assertRaises(FileExistsError):
                write_instrumental_plan(project)

    def test_a_missing_song_spec_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(FileNotFoundError):
                plan_instrumental_sections(Path(raw))


if __name__ == "__main__":
    unittest.main()
