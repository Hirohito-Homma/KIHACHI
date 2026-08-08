from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.lyrics import (
    CHANT,
    INSTRUMENTAL,
    SPOKEN,
    SUNG,
    VOCODER,
    build_lyrics,
    compile_lyrics,
    detect_mode,
)
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.pipeline import ARTIFACT_NAMES, compose_project
from test_music_brain import EXAMPLE

LONG_PROMPT = EXAMPLE + "5分程度。"


def build_spec(prompt: str = LONG_PROMPT):
    return MusicBrain(seed=8).analyze(prompt)


def with_vocal(spec, **fields):
    return dataclasses.replace(spec, vocal=dataclasses.replace(spec.vocal, **fields))


def with_sections(spec, **fields):
    return dataclasses.replace(
        spec,
        arrangement=tuple(
            dataclasses.replace(section, **fields) for section in spec.arrangement
        ),
    )


class ModeDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()

    def test_the_example_song_is_a_vocoder_song(self) -> None:
        self.assertEqual(detect_mode(self.spec), VOCODER)

    def test_mode_follows_the_vocal_spec(self) -> None:
        cases = {
            VOCODER: with_vocal(self.spec, vocoder=True, character="anything"),
            CHANT: with_vocal(self.spec, vocoder=False, character="ritual chant"),
            SPOKEN: with_vocal(self.spec, vocoder=False, character="spoken word"),
            SUNG: with_vocal(self.spec, vocoder=False, character="warm soulful"),
            INSTRUMENTAL: with_vocal(self.spec, enabled=False),
        }
        for expected, spec in cases.items():
            with self.subTest(mode=expected):
                self.assertEqual(detect_mode(spec), expected)

    def test_a_robot_character_counts_as_a_vocoder_even_without_the_flag(self) -> None:
        spec = with_vocal(self.spec, vocoder=False, character="dark robotic phrases")

        self.assertEqual(detect_mode(spec), VOCODER)


class SheetShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()
        self.sheet = build_lyrics(self.spec)

    def test_one_block_per_arrangement_section(self) -> None:
        self.assertEqual(
            [block.section_name for block in self.sheet.sections],
            [section.name for section in self.spec.arrangement],
        )

    def test_a_silent_section_becomes_an_instrumental_tag(self) -> None:
        intro = self.sheet.sections[0]

        self.assertEqual(self.spec.arrangement[0].vocal_probability, 0.0)
        self.assertEqual(intro.tag, "[inst]")
        self.assertEqual(intro.lines, ())

    def test_high_energy_sections_are_choruses_and_carry_the_hook(self) -> None:
        choruses = [block for block in self.sheet.sections if block.tag == "[chorus]"]

        self.assertGreaterEqual(len(choruses), 2)
        for chorus in choruses:
            self.assertEqual(chorus.lines[0], self.sheet.hook)

    def test_a_vocoder_sheet_keeps_its_phrases_short(self) -> None:
        for block in self.sheet.sections:
            for line in block.lines:
                self.assertLessEqual(
                    len(line.split()), 4, f"vocoder line too long: {line!r}"
                )

    def test_a_sung_sheet_is_allowed_longer_lines(self) -> None:
        sung = with_vocal(self.spec, vocoder=False, character="warm soulful")

        longest = max(
            len(line.split())
            for block in build_lyrics(sung).sections
            for line in block.lines
        )

        self.assertGreater(longest, 4)

    def test_lines_within_a_section_do_not_repeat_except_by_design(self) -> None:
        for block in self.sheet.sections:
            if block.tag == "[chorus]":
                continue
            self.assertEqual(len(block.lines), len(set(block.lines)), block.section_name)

    def test_a_chant_repeats_its_opening_phrase(self) -> None:
        chant = with_vocal(self.spec, vocoder=False, character="ritual chant")

        blocks = [b for b in build_lyrics(chant).sections if len(b.lines) >= 2]

        self.assertTrue(blocks)
        self.assertTrue(any(block.lines[0] == block.lines[1] for block in blocks))

    def test_an_instrumental_song_writes_no_words_at_all(self) -> None:
        spec = with_vocal(self.spec, enabled=False)

        sheet = build_lyrics(spec)

        self.assertEqual(sheet.mode, INSTRUMENTAL)
        self.assertEqual(sheet.line_count, 0)
        self.assertIsNone(sheet.hook)
        self.assertTrue(all(block.tag == "[inst]" for block in sheet.sections))

    def test_vocal_probability_controls_how_much_is_sung(self) -> None:
        quiet = build_lyrics(with_sections(self.spec, vocal_probability=0.1))
        loud = build_lyrics(with_sections(self.spec, vocal_probability=1.0))
        silent = build_lyrics(with_sections(self.spec, vocal_probability=0.0))

        self.assertEqual(silent.line_count, 0)
        self.assertLess(quiet.line_count, loud.line_count)


class VocabularyTests(unittest.TestCase):
    def test_words_come_from_the_song_genres(self) -> None:
        text = compile_lyrics(build_spec()).casefold()

        # mutation funk / dub / tech house banks
        self.assertTrue(
            any(word in text for word in ("funk", "signal", "code", "circuit", "pattern"))
        )
        self.assertTrue(any(word in text for word in ("bass", "night", "shadow", "dub")))

    def test_a_different_genre_writes_different_words(self) -> None:
        techno_only = MusicBrain(seed=8).analyze(
            "Tech House。110 BPM、D#m。Vocoderを使用。"
        )

        self.assertNotEqual(compile_lyrics(techno_only), compile_lyrics(build_spec()))

    def test_writing_is_deterministic_in_the_seed(self) -> None:
        self.assertEqual(compile_lyrics(build_spec()), compile_lyrics(build_spec()))
        other = MusicBrain(seed=99).analyze(LONG_PROMPT)
        self.assertNotEqual(compile_lyrics(other), compile_lyrics(build_spec()))


class SheetTextTests(unittest.TestCase):
    def test_text_is_tag_then_lines(self) -> None:
        text = compile_lyrics(build_spec())

        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("["))
        tags = [line for line in lines if line.startswith("[")]
        self.assertEqual(len(tags), len(build_spec().arrangement))
        self.assertTrue(text.endswith("\n"))

    def test_an_instrumental_sheet_is_only_tags(self) -> None:
        spec = with_vocal(build_spec(), enabled=False)

        text = compile_lyrics(spec)

        self.assertTrue(all(line.startswith("[inst]") for line in text.splitlines()))

    def test_the_sheet_serialises_for_auditing(self) -> None:
        payload = build_lyrics(build_spec()).to_dict()

        json.dumps(payload)
        self.assertEqual(payload["lyrics_version"], "0.1")
        self.assertEqual(payload["mode"], VOCODER)


class LyricsWiringTests(unittest.TestCase):
    def test_compose_writes_a_lyric_sheet(self) -> None:
        self.assertIn("lyrics.txt", ARTIFACT_NAMES)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"

            compose_project(LONG_PROMPT, project)

            sheet = (project / "lyrics.txt").read_text(encoding="utf-8")
            self.assertEqual(sheet, compile_lyrics(build_spec()))
            self.assertIn("[chorus]", sheet)

    def test_a_render_request_sings_the_project_sheet_without_being_asked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(LONG_PROMPT, project)

            with contextlib.redirect_stdout(io.StringIO()):
                status = main(["ace-step", "prepare", str(project)])

            self.assertEqual(status, 0)
            request = json.loads(
                (project / "ace_step_request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                request["lyrics"], (project / "lyrics.txt").read_text(encoding="utf-8")
            )

    def test_no_lyrics_renders_instrumental(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(LONG_PROMPT, project)

            with contextlib.redirect_stdout(io.StringIO()):
                status = main(["ace-step", "prepare", str(project), "--no-lyrics"])

            self.assertEqual(status, 0)
            request = json.loads(
                (project / "ace_step_request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(request["lyrics"], "")

    def test_a_project_without_a_sheet_still_prepares(self) -> None:
        # Projects composed before this module have no lyrics.txt.
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(LONG_PROMPT, project)
            (project / "lyrics.txt").unlink()

            with contextlib.redirect_stdout(io.StringIO()):
                status = main(["ace-step", "prepare", str(project)])

            self.assertEqual(status, 0)
            request = json.loads(
                (project / "ace_step_request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(request["lyrics"], "")

    def test_repaint_staging_carries_the_sheet_but_does_not_require_it(self) -> None:
        import hashlib

        from kihachi_music_ai.repaint_planner import (
            song_spec_sha256,
            stage_repaint_project,
        )

        for keep_sheet in (True, False):
            with self.subTest(has_lyrics=keep_sheet), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "source"
                manifest = compose_project(LONG_PROMPT, source)
                if not keep_sheet:
                    (source / "lyrics.txt").unlink()
                audio = source / "audio"
                audio.mkdir()
                (audio / "a.wav").write_bytes(b"RIFFx")
                plan = {
                    "plan_version": "0.1",
                    "song_spec_sha256": song_spec_sha256(manifest.spec),
                    "selection": {
                        "selector": "section",
                        "section_name": "psychedelic_drop",
                        "start_bar": 25,
                        "end_bar": 32,
                    },
                    "ace_step_options": {"task_type": "repaint"},
                    "revision_prompt": "keep it",
                    "source_audio": {
                        "sha256": hashlib.sha256(b"RIFFx").hexdigest(),
                        "relative_path": "audio/a.wav",
                    },
                }
                (source / "repaint_plan.json").write_text(json.dumps(plan), encoding="utf-8")

                staged = stage_repaint_project(source, root / "staged")

                names = [path.name for path in staged.files]
                self.assertEqual("lyrics.txt" in names, keep_sheet)
                self.assertEqual(
                    (staged.output_project / "lyrics.txt").is_file(), keep_sheet
                )

    def test_cli_lyrics_command_reports_the_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(LONG_PROMPT, project)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = main(["lyrics", str(project)])

            self.assertEqual(status, 0)
            output = stdout.getvalue()
            self.assertIn("vocal mode: vocoder", output)
            self.assertIn("hook:", output)
            self.assertIn("[chorus]", output)
            self.assertIn("(no vocal)", output)


if __name__ == "__main__":
    unittest.main()
