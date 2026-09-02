from __future__ import annotations

import dataclasses
import unittest

from kihachi_music_ai.composer import compose_tracks
from kihachi_music_ai.density_diagnostic import (
    BOUNDARY_CONVENTION,
    count_onsets,
    density_diagnostics,
    normalize_observed_rates,
    observed_onsets_per_beat,
    onsets_in_interval,
    section_interval,
)
from kihachi_music_ai.midi import MidiNote
from kihachi_music_ai.midi_review import review_midi_tracks
from kihachi_music_ai.models import SectionSpec, SongSpec
from kihachi_music_ai.music_brain import MusicBrain
from test_music_brain import EXAMPLE


def build_spec() -> SongSpec:
    return MusicBrain(seed=8).analyze(EXAMPLE)


def two_section_spec() -> SongSpec:
    spec = build_spec()
    return dataclasses.replace(
        spec,
        arrangement=(
            SectionSpec(
                name="section_a",
                start_bar=0,
                length_bars=1,
                energy=0.5,
                minimal=False,
                psychedelic=0.2,
            ),
            SectionSpec(
                name="section_b",
                start_bar=1,
                length_bars=1,
                energy=0.8,
                minimal=False,
                psychedelic=0.4,
            ),
        ),
        song=dataclasses.replace(spec.song, total_bars=2),
    )


class OnsetCountingTests(unittest.TestCase):
    def test_single_notes_at_different_times_count_separately(self) -> None:
        notes = (
            MidiNote(60, 0.0, 0.5, 90),
            MidiNote(62, 1.0, 0.5, 90),
            MidiNote(64, 2.0, 0.5, 90),
        )

        self.assertEqual(count_onsets(notes), 3)

    def test_simultaneous_chord_pitches_collapse_to_one_onset(self) -> None:
        notes = (
            MidiNote(48, 0.0, 1.0, 90),
            MidiNote(52, 0.0, 1.0, 88),
            MidiNote(55, 0.0, 1.0, 86),
        )

        self.assertEqual(count_onsets(notes), 1)

    def test_two_chords_at_different_times_count_as_two_onsets(self) -> None:
        notes = (
            MidiNote(48, 0.0, 0.5, 90),
            MidiNote(52, 0.0, 0.5, 88),
            MidiNote(55, 0.0, 0.5, 86),
            MidiNote(50, 1.0, 0.5, 90),
            MidiNote(53, 1.0, 0.5, 88),
            MidiNote(57, 1.0, 0.5, 86),
        )

        self.assertEqual(count_onsets(notes), 2)

    def test_rest_only_space_contributes_zero_onsets(self) -> None:
        self.assertEqual(count_onsets(()), 0)
        self.assertEqual(onsets_in_interval((), 0.0, 4.0), 0)


class SectionBoundaryTests(unittest.TestCase):
    def test_sustained_note_counts_only_in_the_section_where_it_started(self) -> None:
        spec = two_section_spec()
        start_a, end_a = section_interval(spec, spec.arrangement[0])
        start_b, end_b = section_interval(spec, spec.arrangement[1])
        notes = (MidiNote(60, start_a + 3.0, 4.0, 90),)

        self.assertEqual(onsets_in_interval(notes, start_a, end_a), 1)
        self.assertEqual(onsets_in_interval(notes, start_b, end_b), 0)

    def test_onset_exactly_on_section_end_belongs_to_the_next_section(self) -> None:
        spec = two_section_spec()
        _, end_a = section_interval(spec, spec.arrangement[0])
        start_b, end_b = section_interval(spec, spec.arrangement[1])
        notes = (MidiNote(60, end_a, 0.5, 90),)

        self.assertEqual(BOUNDARY_CONVENTION, "[start, end)")
        self.assertEqual(onsets_in_interval(notes, 0.0, end_a), 0)
        self.assertEqual(onsets_in_interval(notes, start_b, end_b), 1)


class NormalizationTests(unittest.TestCase):
    def test_proportional_activity_across_different_section_lengths(self) -> None:
        rates = [observed_onsets_per_beat(4, 4.0), observed_onsets_per_beat(8, 8.0)]
        normalized = normalize_observed_rates(rates)

        self.assertEqual(normalized, [1.0, 1.0])

    def test_slower_section_normalizes_below_a_busier_one(self) -> None:
        normalized = normalize_observed_rates([2.0, 1.0])

        self.assertEqual(normalized, [1.0, 0.5])


class DiagnosticOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()
        self.tracks = compose_tracks(self.spec)

    def test_output_stays_separated_by_section_and_part(self) -> None:
        report = density_diagnostics(self.spec, self.tracks)
        entries = report["entries"]

        self.assertGreater(len(entries), len(self.spec.arrangement))
        pairs = {(entry["section"], entry["part"]) for entry in entries}
        self.assertEqual(len(pairs), len(entries))
        self.assertGreater(len({entry["section"] for entry in entries}), 1)
        self.assertGreater(len({entry["part"] for entry in entries}), 1)

    def test_extra_part_vocoder_is_included(self) -> None:
        self.assertIn("vocoder", self.spec.parts())

        report = density_diagnostics(self.spec, self.tracks)
        vocoder_rows = [entry for entry in report["entries"] if entry["part"] == "vocoder"]

        self.assertEqual(len(vocoder_rows), len(self.spec.arrangement))
        self.assertTrue(any(entry["onset_count"] > 0 for entry in vocoder_rows))

    def test_density_diagnostics_do_not_change_alignment_scores(self) -> None:
        review = review_midi_tracks(self.spec, self.tracks)

        self.assertIn("density", review)
        self.assertEqual(review["alignment"]["score"], review_midi_tracks(self.spec, self.tracks)["alignment"]["score"])
        self.assertEqual(
            review["alignment"]["components"],
            review_midi_tracks(self.spec, self.tracks)["alignment"]["components"],
        )
        self.assertEqual(review["alignment"]["grade"], review_midi_tracks(self.spec, self.tracks)["alignment"]["grade"])

    def test_expected_density_comes_from_the_song_spec(self) -> None:
        section = self.spec.arrangement[0]
        report = density_diagnostics(self.spec, self.tracks)
        bass_row = next(
            entry
            for entry in report["entries"]
            if entry["section"] == section.name and entry["part"] == "bass"
        )

        self.assertEqual(bass_row["expected_density"], round(section.density("bass"), 4))


if __name__ == "__main__":
    unittest.main()
