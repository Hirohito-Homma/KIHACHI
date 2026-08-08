from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.composer import compose_tracks
from kihachi_music_ai.midi import PPQ, MidiNote, build_midi_bytes, read_midi, write_midi
from kihachi_music_ai.midi_review import review_midi_tracks, review_project_midi
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.reviewer import review_project
from test_music_brain import EXAMPLE
from test_reviewer import write_analysis


def build_spec():
    return MusicBrain(seed=8).analyze(EXAMPLE)


def grid(notes) -> Counter:
    """Notes as the file actually stores them, on the PPQ grid."""

    return Counter(
        (round(note.start_beats * PPQ), note.pitch, note.velocity, note.channel)
        for note in notes
    )


class MidiReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()

    def test_every_composed_track_round_trips_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for name, notes in compose_tracks(self.spec).items():
                path = Path(temp) / f"{name}.mid"
                write_midi(
                    path,
                    notes,
                    track_name=f"KIHACHI {name.title()}",
                    bpm=self.spec.song.bpm,
                    key=self.spec.song.key,
                )

                result = read_midi(path)

                self.assertEqual(len(result.notes), len(notes), name)
                self.assertEqual(grid(result.notes), grid(notes), name)
                self.assertEqual(result.ppq, PPQ)
                self.assertEqual(result.track_name, f"KIHACHI {name.title()}")
                self.assertAlmostEqual(result.bpm, self.spec.song.bpm, places=3)

    def test_reader_keeps_drum_channel_and_velocity(self) -> None:
        notes = (
            MidiNote(36, 0.0, 0.25, 108, 9),
            MidiNote(42, 0.5, 0.1, 61, 9),
            MidiNote(51, 1.0, 0.5, 90, 0),
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mixed.mid"
            write_midi(path, notes, track_name="Mixed", bpm=110.0, key="D# minor")

            result = read_midi(path)

        self.assertEqual(grid(result.notes), grid(notes))
        self.assertEqual({note.channel for note in result.notes}, {0, 9})

    def test_overlapping_same_pitch_notes_pair_up_in_order(self) -> None:
        notes = (MidiNote(60, 0.0, 2.0, 100), MidiNote(60, 1.0, 2.0, 80))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "overlap.mid"
            write_midi(path, notes, track_name="Overlap", bpm=120.0, key="C major")

            result = read_midi(path)

        self.assertEqual(len(result.notes), 2)
        self.assertEqual(sorted(note.velocity for note in result.notes), [80, 100])

    def test_corrupt_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "broken.mid"
            path.write_bytes(b"not a midi file at all")
            with self.assertRaises(ValueError):
                read_midi(path)

            truncated = Path(temp) / "truncated.mid"
            payload = build_midi_bytes(
                (MidiNote(60, 0.0, 1.0, 100),), track_name="T", bpm=120.0, key="C major"
            )
            truncated.write_bytes(payload[:-4])
            with self.assertRaises(ValueError):
                read_midi(truncated)


class MidiReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()
        self.tracks = compose_tracks(self.spec)

    def test_the_composed_song_matches_its_own_spec_exactly(self) -> None:
        review = review_midi_tracks(self.spec, self.tracks)

        self.assertEqual(review["harmony"]["bass_root_match_ratio"], 1.0)
        self.assertEqual(review["harmony"]["chord_tone_match_ratio"], 1.0)
        self.assertEqual(review["key"]["out_of_key_notes"], 0)
        self.assertEqual(review["coverage"]["empty_bars"], {})
        self.assertGreater(review["sections"]["energy_correlation"], 0.9)
        self.assertEqual(review["alignment"]["grade"], "aligned")

    def test_written_energy_follows_the_planned_arrangement(self) -> None:
        planned = review_midi_tracks(self.spec, self.tracks)["sections"]["planned_sections"]

        written = [section["written_mean_energy"] for section in planned]
        targets = [section["target_energy"] for section in planned]
        self.assertEqual(written, sorted(written))
        self.assertEqual(targets, sorted(targets))

    def test_a_transposed_bass_is_caught_as_a_harmony_failure(self) -> None:
        # One semitone off is inaudible to a mix detector but exact here.
        detuned = tuple(
            dataclasses.replace(note, pitch=note.pitch + 1) for note in self.tracks["bass"]
        )
        clean = review_midi_tracks(self.spec, self.tracks)
        review = review_midi_tracks(self.spec, {**self.tracks, "bass": detuned})

        self.assertEqual(review["harmony"]["bass_root_match_ratio"], 0.0)
        # The chords are untouched, so harmony lands halfway rather than at zero.
        self.assertEqual(review["harmony"]["chord_tone_match_ratio"], 1.0)
        self.assertEqual(review["alignment"]["components"]["harmony"]["score"], 0.5)
        self.assertEqual(review["key"]["out_of_key_notes"], len(detuned))
        self.assertLess(review["alignment"]["components"]["key"]["score"], 0.7)
        self.assertNotEqual(review["alignment"]["grade"], "aligned")
        self.assertLess(review["alignment"]["score"], clean["alignment"]["score"] - 20.0)

    def test_a_silent_bar_is_reported_as_missing_coverage(self) -> None:
        gapped = tuple(note for note in self.tracks["bass"] if not 0.0 <= note.start_beats < 4.0)
        review = review_midi_tracks(self.spec, {**self.tracks, "bass": gapped})

        self.assertEqual(review["coverage"]["empty_bars"], {"bass": [1]})
        self.assertLess(review["coverage"]["score"], 1.0)

    def test_review_reads_the_files_on_disk_not_a_recomposition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            # Rewrite bass.mid a semitone up; the review must see the file, not the spec.
            damaged = tuple(
                dataclasses.replace(note, pitch=note.pitch + 1)
                for note in read_midi(project / "bass.mid").notes
            )
            write_midi(
                project / "bass.mid",
                damaged,
                track_name="KIHACHI Bass",
                bpm=self.spec.song.bpm,
                key=self.spec.song.key,
            )

            manifest = review_project_midi(project)

        self.assertEqual(manifest.review["harmony"]["bass_root_match_ratio"], 0.0)

    def test_cli_reports_midi_alignment_without_any_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            self.assertFalse((project / "audio_analysis.json").exists())
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = main(["midi-review", str(project)])

            self.assertEqual(status, 0)
            output = stdout.getvalue()
            self.assertIn("midi alignment score", output)
            self.assertIn("bass-root match 1.0", output)
            self.assertIn("0/", output)


class DualChannelReviewTests(unittest.TestCase):
    def _project(self, temp: Path) -> Path:
        project = temp / "project"
        compose_project(EXAMPLE, project)
        write_analysis(
            project,
            tempo_delta=-0.3,
            key_status="low_confidence",
            chord_match=0.0,
            chord_coverage=0.375,
            boundary_recall=1.0,
            energy_correlation=0.4696,
        )
        return project

    def test_review_carries_both_channels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = review_project(self._project(Path(temp)))

        review = manifest.review
        self.assertEqual(review["review_version"], "0.3")
        self.assertIn("midi_alignment", review)
        # The mix hides the harmony; the written MIDI states it exactly.
        self.assertEqual(review["alignment"]["components"]["chords"]["score"], 0.0)
        self.assertEqual(review["midi_alignment"]["harmony"]["bass_root_match_ratio"], 1.0)

    def test_a_detection_limit_is_not_reported_as_a_composition_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = review_project(self._project(Path(temp)))

        codes = {finding["code"]: finding for finding in manifest.review["findings"]}
        self.assertIn("harmony_written_but_not_detected", codes)
        self.assertNotIn("midi_harmony_misaligned", codes)
        finding = codes["harmony_written_but_not_detected"]
        self.assertEqual(finding["severity"], "info")
        self.assertEqual(sorted(finding), ["code", "evidence", "recommendation", "severity"])
        self.assertIn("detection limit", finding["recommendation"])

    def test_a_real_composition_error_is_reported_as_high_severity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            spec = build_spec()
            damaged = tuple(
                dataclasses.replace(note, pitch=note.pitch + 1)
                for note in read_midi(project / "bass.mid").notes
            )
            write_midi(
                project / "bass.mid",
                damaged,
                track_name="KIHACHI Bass",
                bpm=spec.song.bpm,
                key=spec.song.key,
            )

            manifest = review_project(project)

        codes = {finding["code"]: finding for finding in manifest.review["findings"]}
        self.assertIn("midi_harmony_misaligned", codes)
        self.assertEqual(codes["midi_harmony_misaligned"]["severity"], "high")
        self.assertIn("midi_out_of_key_notes", codes)
        self.assertNotIn("harmony_written_but_not_detected", codes)

    def test_midi_findings_do_not_leak_into_the_repaint_revision_prompt(self) -> None:
        # The repaint prompt drives an audio render; MIDI diagnostics are not for it.
        with tempfile.TemporaryDirectory() as temp:
            manifest = review_project(self._project(Path(temp)))

        prompt = manifest.review["repaint_candidate"]["revision_prompt"]
        self.assertNotIn("detection limit", prompt)
        self.assertNotIn("written MIDI", prompt)

    def test_review_still_works_when_a_project_has_no_midi(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            for name in ("bass", "drums", "chords"):
                (project / f"{name}.mid").unlink()

            manifest = review_project(project)

        self.assertNotIn("midi_alignment", manifest.review)
        codes = {finding["code"] for finding in manifest.review["findings"]}
        self.assertNotIn("harmony_written_but_not_detected", codes)


if __name__ == "__main__":
    unittest.main()
