from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.composer import compose_tracks
from kihachi_music_ai.edit import (
    EditInstructionError,
    apply_edit_to_project,
    apply_spec_edit,
    build_spec_edit,
    parse_edit_instruction,
    song_spec_sha256,
    summarise_regeneration,
)
from kihachi_music_ai.midi import read_midi
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.pipeline import compose_project
from test_music_brain import EXAMPLE

LONG_PROMPT = EXAMPLE + "5分程度。"


def build_spec():
    return MusicBrain(seed=8).analyze(LONG_PROMPT)


class InstructionParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()

    def test_the_design_document_example_parses_as_written(self) -> None:
        intent = parse_edit_instruction("Dropのベースだけもっと変態的に", self.spec)

        self.assertEqual(intent.qualities, ("mutation",))
        self.assertEqual(intent.tracks, ("bass",))
        self.assertEqual(intent.sections, ("psychedelic_drop", "final_drop"))
        self.assertEqual(intent.direction, 1)

    def test_magnitude_words_are_read(self) -> None:
        small = parse_edit_instruction("ベースを少し激しく", self.spec)
        plain = parse_edit_instruction("ベースを激しく", self.spec)
        large = parse_edit_instruction("ベースをかなり激しく", self.spec)

        self.assertLess(small.magnitude, plain.magnitude)
        self.assertLess(plain.magnitude, large.magnitude)

    def test_decrease_words_flip_the_direction(self) -> None:
        self.assertEqual(parse_edit_instruction("ベースの密度を上げて", self.spec).direction, 1)
        self.assertEqual(parse_edit_instruction("ベースの密度を下げて", self.spec).direction, -1)

    def test_a_section_name_containing_a_keyword_does_not_flip_the_direction(self) -> None:
        # "dub_breakdown" contains "down"; the section name is an identifier, not
        # a description, so it must not be read as "decrease".
        intent = parse_edit_instruction("dub_breakdownのディレイを増やして", self.spec)

        self.assertEqual(intent.sections, ("dub_breakdown",))
        self.assertEqual(intent.direction, 1)

    def test_ascii_keywords_need_word_boundaries(self) -> None:
        intent = parse_edit_instruction("group the chords more densely", self.spec)

        # "up" inside "group" must not register as an increase word on its own,
        # and the instruction is still read as a density increase.
        self.assertEqual(intent.qualities, ("density",))
        self.assertEqual(intent.direction, 1)

    def test_half_selectors_pick_a_span(self) -> None:
        later = parse_edit_instruction("後半のドラムを抜いて", self.spec)
        earlier = parse_edit_instruction("前半のドラムを抜いて", self.spec)

        self.assertEqual(len(later.sections) + len(earlier.sections), len(self.spec.arrangement))
        self.assertNotEqual(later.sections, earlier.sections)

    def test_an_unrecognised_instruction_is_refused(self) -> None:
        for text in ("", "   ", "make it better somehow"):
            with self.assertRaises(EditInstructionError):
                parse_edit_instruction(text, self.spec)


class SpecDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()

    def test_naming_a_section_keeps_song_wide_parameters_out_of_the_edit(self) -> None:
        # "だけ" has to mean only: bass.mutation is song-wide and would leak the
        # change into every other section's bass.
        edit = build_spec_edit(self.spec, "Dropのベースだけもっと変態的に")

        scopes = {change["scope"] for change in edit["changes"]}
        self.assertEqual(scopes, {"section"})
        self.assertEqual(
            sorted(edit["sections_touched"]), ["final_drop", "psychedelic_drop"]
        )
        self.assertEqual(edit["scope_warnings"], [])

    def test_a_song_wide_only_quality_is_used_but_called_out(self) -> None:
        edit = build_spec_edit(self.spec, "psychedelic_dropのシンコペーションを上げて")

        self.assertTrue(any(change["scope"] == "song" for change in edit["changes"]))
        self.assertTrue(edit["scope_warnings"])
        self.assertIn("whole song", edit["scope_warnings"][0])
        self.assertIn("no per-section parameter", edit["scope_warnings"][0])

    def test_a_maxed_out_section_value_is_reported_as_such_not_as_missing(self) -> None:
        # dub_breakdown.fx_amount is already 1.0, so the fx edit falls back to the
        # song-wide value. That is a different reason from "there is no such field".
        edit = build_spec_edit(self.spec, "dub_breakdownのディレイをかなり増やして")

        self.assertTrue(edit["scope_warnings"])
        warning = edit["scope_warnings"][0]
        self.assertIn("already at its limit", warning)
        self.assertNotIn("no per-section parameter", warning)

    def test_planning_never_mutates_the_spec(self) -> None:
        before = self.spec.to_json()

        build_spec_edit(self.spec, "Dropのベースだけもっと変態的に")

        self.assertEqual(self.spec.to_json(), before)

    def test_values_are_clamped_and_a_dead_edit_is_refused(self) -> None:
        maxed = dataclasses.replace(
            self.spec,
            arrangement=tuple(
                dataclasses.replace(section, mutation=1.0) for section in self.spec.arrangement
            ),
            bass=dataclasses.replace(self.spec.bass, mutation=1.0),
        )
        with self.assertRaises(EditInstructionError):
            build_spec_edit(maxed, "全部もっと変態的に")

    def test_apply_refuses_a_diff_planned_against_another_spec(self) -> None:
        edit = build_spec_edit(self.spec, "Dropのベースだけもっと変態的に")
        other = dataclasses.replace(
            self.spec, bass=dataclasses.replace(self.spec.bass, mutation=0.1)
        )

        with self.assertRaises(ValueError):
            apply_spec_edit(other, edit)

    def test_apply_moves_exactly_the_planned_parameters(self) -> None:
        edit = build_spec_edit(self.spec, "Dropのベースだけもっと変態的に")

        updated = apply_spec_edit(self.spec, edit)

        by_name = {section.name: section for section in updated.arrangement}
        self.assertEqual(by_name["psychedelic_drop"].mutation, 1.0)
        self.assertEqual(by_name["final_drop"].mutation, 1.0)
        self.assertEqual(updated.bass.mutation, self.spec.bass.mutation)
        for section in self.spec.arrangement:
            if section.name in {"psychedelic_drop", "final_drop"}:
                continue
            self.assertEqual(by_name[section.name], section)


class RegenerationLocalityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()

    def test_editing_one_section_leaves_every_other_section_identical(self) -> None:
        edit = build_spec_edit(self.spec, "Dropのベースだけもっと変態的に")
        updated = apply_spec_edit(self.spec, edit)

        report = summarise_regeneration(
            self.spec, compose_tracks(self.spec), compose_tracks(updated), updated=updated
        )

        self.assertEqual(report["changed_sections"], ["psychedelic_drop", "final_drop"])
        self.assertEqual(len(report["unchanged_sections"]), len(self.spec.arrangement) - 2)
        self.assertEqual(report["tracks"]["drums"]["changed_sections"], [])
        self.assertEqual(report["tracks"]["chords"]["changed_sections"], [])

    def test_untouched_tracks_are_note_for_note_equal(self) -> None:
        edit = build_spec_edit(self.spec, "Dropのベースだけもっと変態的に")
        updated = apply_spec_edit(self.spec, edit)

        before, after = compose_tracks(self.spec), compose_tracks(updated)

        self.assertEqual(before["drums"], after["drums"])
        self.assertEqual(before["chords"], after["chords"])
        self.assertNotEqual(before["bass"], after["bass"])


class ApplyEditProjectTests(unittest.TestCase):
    def _source(self, root: Path, instruction: str) -> Path:
        project = root / "source"
        compose_project(LONG_PROMPT, project)
        spec = build_spec()
        edit = build_spec_edit(spec, instruction)
        (project / "spec_edit.json").write_text(
            json.dumps(edit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return project

    def test_apply_writes_a_new_project_and_leaves_the_source_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root, "Dropのベースだけもっと変態的に")
            before = {path.name: path.read_bytes() for path in sorted(source.iterdir())}

            manifest = apply_edit_to_project(source, root / "edited")

            after = {path.name: path.read_bytes() for path in sorted(source.iterdir())}
            self.assertEqual(before, after)
            self.assertTrue((manifest.output_project / "song_spec.json").is_file())
            self.assertTrue((manifest.output_project / "applied_spec_edit.json").is_file())
            applied = json.loads(
                (manifest.output_project / "applied_spec_edit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(applied["execution_state"], "applied")

    def test_the_written_midi_matches_the_edited_spec_outside_the_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root, "Dropのベースだけもっと変態的に")

            manifest = apply_edit_to_project(source, root / "edited")

            for track in ("drums", "chords"):
                self.assertEqual(
                    (source / f"{track}.mid").read_bytes(),
                    (manifest.output_project / f"{track}.mid").read_bytes(),
                    f"{track}.mid should be byte-identical",
                )
            self.assertNotEqual(
                (source / "bass.mid").read_bytes(),
                (manifest.output_project / "bass.mid").read_bytes(),
            )
            self.assertGreater(len(read_midi(manifest.output_project / "bass.mid").notes), 0)

    def test_apply_refuses_to_replace_an_existing_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root, "Dropのベースだけもっと変態的に")
            (root / "edited").mkdir()

            with self.assertRaises(FileExistsError):
                apply_edit_to_project(source, root / "edited")

    def test_apply_refuses_to_write_over_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root, "Dropのベースだけもっと変態的に")

            with self.assertRaises(ValueError):
                apply_edit_to_project(source, source)

    def test_an_fx_edit_reaches_the_audio_prompt_even_though_no_note_moves(self) -> None:
        # dub_delay and fx_amount steer the render, not the notes. They used to
        # reach nothing at all; the report has to show they land somewhere.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root, "dub_breakdownのディレイをかなり増やして")

            manifest = apply_edit_to_project(source, root / "edited")

            self.assertEqual(manifest.report["changed_sections"], [])
            self.assertTrue(manifest.report["audio_prompt_changed"])
            self.assertFalse(manifest.report["no_effect"])
            self.assertNotEqual(
                (source / "prompt.txt").read_text(encoding="utf-8"),
                (manifest.output_project / "prompt.txt").read_text(encoding="utf-8"),
            )

    def test_a_change_too_small_to_land_anywhere_is_reported_as_no_effect(self) -> None:
        # Densities are quantized into step counts and are not named in the
        # prompt, so a nudge below one step really does nothing.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "source"
            compose_project(LONG_PROMPT, project)
            spec = build_spec()
            drop = next(s for s in spec.arrangement if s.name == "psychedelic_drop")
            (project / "spec_edit.json").write_text(
                json.dumps(
                    {
                        "edit_version": "0.1",
                        "instruction": "nudge the drop chords",
                        "song_spec_sha256": song_spec_sha256(spec),
                        "changes": [
                            {
                                "scope": "section",
                                "section": "psychedelic_drop",
                                "path": "chord_density",
                                "from": drop.chord_density,
                                "to": round(drop.chord_density + 0.01, 4),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            manifest = apply_edit_to_project(project, root / "edited")

            self.assertTrue(manifest.report["no_effect"])
            self.assertEqual(manifest.report["changed_sections"], [])
            self.assertFalse(manifest.report["audio_prompt_changed"])


class EditCliTests(unittest.TestCase):
    def test_plan_then_apply_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            compose_project(LONG_PROMPT, source)
            planned = io.StringIO()

            with contextlib.redirect_stdout(planned):
                status = main(["edit", str(source), "Dropのベースだけもっと変態的に"])

            self.assertEqual(status, 0)
            self.assertIn("mutation increase by 0.2", planned.getvalue())
            self.assertIn("nothing regenerated yet", planned.getvalue())
            self.assertTrue((source / "spec_edit.json").is_file())
            self.assertFalse((root / "edited").exists())

            applied = io.StringIO()
            with contextlib.redirect_stdout(applied):
                status = main(["apply-edit", str(source), str(root / "edited")])

            self.assertEqual(status, 0)
            output = applied.getvalue()
            self.assertIn("sections regenerated: ['psychedelic_drop', 'final_drop']", output)
            self.assertIn("sections byte-identical: 7", output)
            self.assertIn("drums: ", output)
            self.assertIn("unchanged", output)

    def test_plan_refuses_to_overwrite_without_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            compose_project(LONG_PROMPT, source)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                main(["edit", str(source), "Dropのベースだけもっと変態的に"])

                # The CLI reports the refusal as a non-zero exit, not a traceback.
                refused = main(["edit", str(source), "Dropのベースだけ少し変態的に"])

                status = main(
                    ["edit", str(source), "Dropのベースだけ少し変態的に", "--overwrite"]
                )
            self.assertNotEqual(refused, 0)
            self.assertEqual(status, 0)
            plan = json.loads((source / "spec_edit.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["interpretation"]["magnitude"], 0.1)

    def test_the_plan_records_the_spec_it_was_planned_against(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            manifest = compose_project(LONG_PROMPT, source)
            with contextlib.redirect_stdout(io.StringIO()):
                main(["edit", str(source), "Dropのベースだけもっと変態的に"])

            plan = json.loads((source / "spec_edit.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["song_spec_sha256"], song_spec_sha256(manifest.spec))
            self.assertEqual(plan["execution_state"], "planned_not_applied")
            self.assertFalse(plan["safety"]["song_spec_mutated"])


if __name__ == "__main__":
    unittest.main()
