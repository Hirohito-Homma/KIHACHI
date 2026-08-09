from __future__ import annotations

import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.arrangement import (
    ARCHETYPES,
    BLOCK_BARS,
    build_arrangement,
    describe_arrangement,
    distribute_bars,
    select_arc,
)
from kihachi_music_ai.composer import compose_bass, compose_drums, compose_tracks
from kihachi_music_ai.midi_review import review_midi_tracks
from kihachi_music_ai.models import TRACK_NAMES, SectionSpec, SongSpec
from kihachi_music_ai.music_brain import MusicBrain
from test_music_brain import EXAMPLE

# The SongSpec that shipped example_output/mutation-signal-lora. Its repaint
# plans are pinned to this digest, so the arrangement engine must keep producing
# a byte-identical document for the same prompt.
LEGACY_SPEC_SHA256 = "bc83dfdb3e8a2ee6df61d4c4f9978c599ad528262951594e0609428edf420f93"
LEGACY_LAYOUT = (
    ("minimal_intro", 0, 8, 0.25, True, 0.08),
    ("minimal_groove", 8, 8, 0.44, True, 0.18),
    ("mutation_build", 16, 8, 0.66, False, 0.58),
    ("psychedelic_drop", 24, 8, 0.88, False, 0.96),
)
PROTECTED_SPEC = (
    Path(__file__).resolve().parents[1]
    / "example_output"
    / "mutation-signal-lora"
    / "song_spec.json"
)


def section_tuple(section: SectionSpec):
    return (
        section.name,
        section.start_bar,
        section.length_bars,
        section.energy,
        section.minimal,
        section.psychedelic,
    )


class BackwardCompatibilityTests(unittest.TestCase):
    def test_thirty_two_bars_reproduces_the_original_four_section_layout(self) -> None:
        sections = build_arrangement(32)

        self.assertEqual(tuple(section_tuple(s) for s in sections), LEGACY_LAYOUT)

    def test_a_pre_engine_song_spec_still_serializes_to_its_pinned_digest(self) -> None:
        if not PROTECTED_SPEC.is_file():
            self.skipTest("example_output baseline is not present")
        raw = PROTECTED_SPEC.read_text(encoding="utf-8")

        spec = SongSpec.from_json(raw)

        self.assertEqual(spec.to_json(), raw)
        self.assertEqual(
            hashlib.sha256(spec.to_json().encode("utf-8")).hexdigest(), LEGACY_SPEC_SHA256
        )

    def test_sections_without_engine_detail_fall_back_to_energy(self) -> None:
        bare = SectionSpec(
            name="legacy", start_bar=0, length_bars=8, energy=0.6,
            minimal=False, psychedelic=0.5,
        )

        for track in TRACK_NAMES:
            self.assertEqual(bare.density(track), 0.6)
            self.assertTrue(bare.plays(track))
        self.assertIsNone(bare.mutation)
        self.assertNotIn("bass_density", bare.to_dict())
        self.assertNotIn("active_tracks", bare.to_dict())


class ArcSelectionTests(unittest.TestCase):
    def test_longer_songs_earn_a_breakdown_and_a_second_drop(self) -> None:
        short = select_arc(32)
        medium = select_arc(64)
        long_form = select_arc(136)

        self.assertEqual(len(short), 4)
        self.assertNotIn("dub_breakdown", short)
        self.assertIn("dub_breakdown", medium)
        self.assertIn("final_drop", medium)
        self.assertIn("outro", long_form)
        self.assertGreater(len(long_form), len(medium))

    def test_a_very_short_song_never_plans_more_sections_than_bars(self) -> None:
        for bars in range(1, 9):
            self.assertLessEqual(len(select_arc(bars)), bars)

    def test_unknown_archetypes_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_arrangement(32, arc=("minimal_intro", "no_such_section"))

    def test_every_arc_only_names_known_archetypes(self) -> None:
        for bars in (8, 16, 32, 64, 96, 136, 240):
            for name in select_arc(bars):
                self.assertIn(name, ARCHETYPES)


class BarDistributionTests(unittest.TestCase):
    def test_long_forms_land_on_eight_bar_phrases(self) -> None:
        for bars in (64, 96, 136, 160, 240):
            sections = build_arrangement(bars)
            lengths = [section.length_bars for section in sections]
            self.assertEqual(sum(lengths), bars)
            self.assertTrue(
                all(length % BLOCK_BARS == 0 for length in lengths),
                f"{bars} bars produced off-phrase sections {lengths}",
            )

    def test_drops_are_given_more_room_than_intros(self) -> None:
        sections = {s.name: s for s in build_arrangement(136)}

        self.assertGreaterEqual(
            sections["psychedelic_drop"].length_bars, sections["minimal_intro"].length_bars
        )

    def test_short_songs_fall_back_to_an_even_split(self) -> None:
        # Below one block per section there are not enough phrases to align.
        self.assertEqual(distribute_bars(12, 4), (3, 3, 3, 3))
        self.assertEqual(distribute_bars(32, 4), (8, 8, 8, 8))
        self.assertEqual(sum(distribute_bars(10, 3)), 10)

    def test_distribution_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            distribute_bars(4, 8)
        with self.assertRaises(ValueError):
            distribute_bars(32, 0)
        with self.assertRaises(ValueError):
            distribute_bars(32, 4, [1, 2])

    def test_arrangement_always_covers_the_song_exactly(self) -> None:
        for bars in (8, 12, 32, 64, 100, 136, 200):
            sections = build_arrangement(bars)
            cursor = 0
            for section in sections:
                self.assertEqual(section.start_bar, cursor)
                cursor += section.length_bars
            self.assertEqual(cursor, bars)


class SectionIdentityTests(unittest.TestCase):
    def test_a_repeated_archetype_gets_a_unique_section_name(self) -> None:
        # --repaint-section resolves by name, so duplicates would be ambiguous.
        sections = build_arrangement(136)

        names = [section.name for section in sections]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("mutation_build_1", names)
        self.assertIn("mutation_build_2", names)

    def test_sections_that_appear_once_keep_their_plain_name(self) -> None:
        names = [section.name for section in build_arrangement(32)]

        self.assertEqual(names[0], "minimal_intro")
        self.assertNotIn("minimal_intro_1", names)


class PerTrackArrangementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = MusicBrain(seed=8).analyze(EXAMPLE + "5分程度。")

    def test_the_five_minute_arc_rests_the_drums_for_dub_and_outro(self) -> None:
        resting = [
            section.name
            for section in self.spec.arrangement
            if not section.plays("drums")
        ]

        self.assertEqual(resting, ["dub_breakdown", "outro"])

    def test_a_resting_track_writes_no_notes_there(self) -> None:
        drums = compose_drums(self.spec)
        beats_per_bar = 4.0

        for section in self.spec.arrangement:
            if section.plays("drums"):
                continue
            low = section.start_bar * beats_per_bar
            high = (section.start_bar + section.length_bars) * beats_per_bar
            inside = [n for n in drums if low + 0.05 <= n.start_beats < high - 0.05]
            self.assertEqual(inside, [], f"{section.name} should be drumless")

    def test_resting_bars_are_not_scored_as_missing_coverage(self) -> None:
        review = review_midi_tracks(self.spec, compose_tracks(self.spec))

        self.assertEqual(review["coverage"]["empty_bars"], {})
        self.assertEqual(review["coverage"]["score"], 1.0)
        self.assertIn("drums", review["coverage"]["resting_bars"])
        self.assertLess(
            review["coverage"]["scored_track_bars"],
            self.spec.song.total_bars * len(TRACK_NAMES),
        )

    def test_per_track_density_overrides_section_energy(self) -> None:
        section = self.spec.arrangement[0]

        self.assertEqual(section.energy, 0.25)
        self.assertEqual(section.density("bass"), 0.18)
        self.assertEqual(section.density("drums"), 0.45)
        self.assertNotEqual(section.density("bass"), section.density("drums"))

    def test_raising_only_the_bass_density_thickens_only_the_bass(self) -> None:
        thin = self.spec
        thick = dataclasses.replace(
            thin,
            arrangement=tuple(
                dataclasses.replace(section, bass_density=1.0)
                for section in thin.arrangement
            ),
        )

        self.assertGreater(len(compose_bass(thick)), len(compose_bass(thin)))
        self.assertEqual(len(compose_drums(thick)), len(compose_drums(thin)))

    def test_section_mutation_drives_the_composer(self) -> None:
        calm = dataclasses.replace(
            self.spec,
            arrangement=tuple(
                dataclasses.replace(section, mutation=0.0)
                for section in self.spec.arrangement
            ),
        )
        wild = dataclasses.replace(
            self.spec,
            arrangement=tuple(
                dataclasses.replace(section, mutation=1.0)
                for section in self.spec.arrangement
            ),
        )

        def bar_counts(notes, spec):
            counts = [0] * spec.song.total_bars
            for note in notes:
                counts[min(int((note.start_beats + 0.05) // 4.0), spec.song.total_bars - 1)] += 1
            return counts

        calm_counts = bar_counts(compose_bass(calm), calm)
        wild_counts = bar_counts(compose_bass(wild), wild)
        section = self.spec.arrangement[4]
        window = slice(section.start_bar, section.start_bar + section.length_bars)
        self.assertEqual(len(set(calm_counts[window])), 1)
        self.assertGreater(len(set(wild_counts[window])), 1)

    def test_unknown_track_names_are_rejected(self) -> None:
        section = self.spec.arrangement[0]
        with self.assertRaises(ValueError):
            section.density("vocals")
        with self.assertRaises(ValueError):
            section.plays("vocals")
        with self.assertRaises(ValueError):
            SectionSpec(
                name="bad", start_bar=0, length_bars=8, energy=0.5,
                minimal=False, psychedelic=0.5, active_tracks=("theremin",),
            )
        with self.assertRaises(ValueError):
            SectionSpec(
                name="bad", start_bar=0, length_bars=8, energy=0.5,
                minimal=False, psychedelic=0.5, bass_density=1.4,
            )


class ArrangementRoundTripTests(unittest.TestCase):
    def test_an_engine_built_spec_survives_json(self) -> None:
        spec = MusicBrain(seed=8).analyze(EXAMPLE + "5分程度。")

        restored = SongSpec.from_json(spec.to_json())

        self.assertEqual(restored, spec)
        self.assertEqual(restored.to_json(), spec.to_json())
        drums_rest = [s for s in restored.arrangement if not s.plays("drums")]
        self.assertTrue(drums_rest)
        self.assertIsInstance(drums_rest[0].active_tracks, tuple)

    def test_engine_detail_reaches_the_written_project_file(self) -> None:
        spec = MusicBrain(seed=8).analyze(EXAMPLE + "5分程度。")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "song_spec.json"
            spec.write_json(path)
            payload = json.loads(path.read_text(encoding="utf-8"))

        breakdown = next(
            item for item in payload["arrangement"] if item["name"] == "dub_breakdown"
        )
        # drums rest; the vocoder pad EXAMPLE asks for keeps playing
        self.assertEqual(breakdown["active_tracks"], ["bass", "chords", "vocoder"])
        self.assertEqual(breakdown["drum_density"], 0.0)

    def test_describe_arrangement_is_printable(self) -> None:
        rows = describe_arrangement(build_arrangement(136))

        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[0]["start_bar"], 1)
        self.assertEqual(sorted(rows[0]["densities"]), sorted(TRACK_NAMES))
        json.dumps(rows)


if __name__ == "__main__":
    unittest.main()
