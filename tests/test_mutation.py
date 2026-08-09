from __future__ import annotations

import dataclasses
import random
import unittest

from kihachi_music_ai.composer import compose_bass, compose_chords, compose_drums
from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.mutation import (
    _displace,
    GRID,
    Step,
    build_pattern,
    mutate_pattern,
    mutation_series,
)
from kihachi_music_ai.theory import chord_root, midi_pitch
from test_music_brain import EXAMPLE


def build_spec() -> SongSpec:
    return MusicBrain(seed=8).analyze(EXAMPLE)


def with_bass(spec: SongSpec, **changes: float) -> SongSpec:
    return dataclasses.replace(spec, bass=dataclasses.replace(spec.bass, **changes))


# Humanize deliberately pushes a downbeat a few milliseconds either side of the
# bar line, so notes are counted against the bar they were written for.
BAR_TOLERANCE = 0.05


def bar_of(note) -> int:
    return int((note.start_beats + BAR_TOLERANCE) // 4.0)


def notes_per_bar(notes, spec: SongSpec) -> list[int]:
    counts = [0] * spec.song.total_bars
    for note in notes:
        counts[min(bar_of(note), spec.song.total_bars - 1)] += 1
    return counts


def notes_in_section(notes, section) -> list:
    last_bar = section.start_bar + section.length_bars
    return [note for note in notes if section.start_bar <= bar_of(note) < last_bar]


def section_bars(spec: SongSpec, name: str) -> range:
    section = next(item for item in spec.arrangement if item.name == name)
    return range(section.start_bar, section.start_bar + section.length_bars)


class PatternBuilderTests(unittest.TestCase):
    def test_density_adds_the_least_load_bearing_slots_last(self) -> None:
        ranked = (0.0, 1.5, 2.75, 0.75)

        sparse = build_pattern(
            ranked, density=0.0, minimum=1, maximum=4, duration=0.2, velocity=100
        )
        dense = build_pattern(
            ranked, density=1.0, minimum=1, maximum=4, duration=0.2, velocity=100
        )

        self.assertEqual([step.position for step in sparse], [0.0])
        self.assertEqual([step.position for step in dense], [0.0, 0.75, 1.5, 2.75])
        # The sparse pattern survives inside the dense one: density adds, never reshuffles.
        self.assertTrue({s.position for s in sparse} <= {s.position for s in dense})

    def test_anchors_are_flagged(self) -> None:
        pattern = build_pattern(
            (0.0, 1.5), density=1.0, minimum=1, maximum=2, duration=0.2,
            velocity=100, anchors=(0.0,),
        )
        self.assertTrue(pattern[0].anchor)
        self.assertFalse(pattern[1].anchor)

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_pattern((0.0,), density=1.4, minimum=1, maximum=2, duration=0.2, velocity=100)
        with self.assertRaises(ValueError):
            Step(position=0.0, duration=0.0, velocity=100)
        with self.assertRaises(ValueError):
            Step(position=-1.0, duration=0.2, velocity=100)


class MutatePatternTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = build_pattern(
            (0.0, 1.5, 2.75, 0.75), density=1.0, minimum=1, maximum=4,
            duration=0.2, velocity=90, anchors=(0.0,),
        )

    def test_zero_amount_is_pure_repetition(self) -> None:
        result = mutate_pattern(self.base, amount=0.0, rng=random.Random(1), syncopation=1.0)
        self.assertEqual(result, self.base)

    def test_anchors_are_never_dropped_or_displaced(self) -> None:
        for seed in range(40):
            result = mutate_pattern(
                self.base,
                amount=1.0,
                rng=random.Random(seed),
                syncopation=1.0,
                space=1.0,
                ghost_probability=1.0,
                octave_jump_probability=1.0,
            )
            anchors = [step for step in result if step.anchor]
            self.assertEqual(len(anchors), 1, f"seed {seed} lost the anchor")
            self.assertEqual(anchors[0].position, 0.0, f"seed {seed} moved the anchor")

    def test_mutation_stays_inside_the_bar_and_within_midi_range(self) -> None:
        for seed in range(40):
            result = mutate_pattern(
                self.base,
                amount=1.0,
                rng=random.Random(seed),
                syncopation=1.0,
                space=1.0,
                ghost_probability=1.0,
                octave_jump_probability=1.0,
            )
            for step in result:
                self.assertGreaterEqual(step.position, 0.0)
                self.assertLess(step.position, 4.0)
                self.assertGreater(step.duration, 0.0)
                self.assertTrue(1 <= step.velocity <= 127)
                self.assertIn(step.octave, (-12, 0, 12))

    def test_pitch_only_ever_moves_by_whole_octaves_so_harmony_is_kept(self) -> None:
        for seed in range(40):
            result = mutate_pattern(
                self.base, amount=1.0, rng=random.Random(seed), octave_jump_probability=1.0
            )
            self.assertTrue(all(step.octave % 12 == 0 for step in result))

    def test_space_is_what_removes_steps(self) -> None:
        crowded = mutate_pattern(
            self.base, amount=1.0, rng=random.Random(3), syncopation=1.0, space=0.0
        )
        spacious = mutate_pattern(
            self.base, amount=1.0, rng=random.Random(3), syncopation=0.0, space=1.0
        )
        self.assertGreaterEqual(len(crowded), len(spacious))
        self.assertGreaterEqual(len(spacious), 1)

    def test_displacement_lands_on_the_sixteenth_grid(self) -> None:
        result = mutate_pattern(
            self.base, amount=1.0, rng=random.Random(11), syncopation=1.0
        )
        for step in result:
            if not step.ghost:
                self.assertAlmostEqual((step.position / GRID) % 1.0, 0.0, places=6)

    def test_same_seed_reproduces_the_same_mutation(self) -> None:
        first = mutate_pattern(
            self.base, amount=0.8, rng=random.Random(7), syncopation=0.9,
            ghost_probability=0.4, octave_jump_probability=0.5,
        )
        second = mutate_pattern(
            self.base, amount=0.8, rng=random.Random(7), syncopation=0.9,
            ghost_probability=0.4, octave_jump_probability=0.5,
        )
        self.assertEqual(first, second)


class MutationSeriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = build_pattern(
            (0.0, 1.5, 2.75, 0.75), density=1.0, minimum=1, maximum=4,
            duration=0.2, velocity=90, anchors=(0.0,),
        )

    def test_the_first_bar_states_the_idea_before_deforming_it(self) -> None:
        series = mutation_series(
            self.base, bars=4, amount=1.0, rng=random.Random(2), syncopation=1.0
        )
        self.assertEqual(len(series), 4)
        self.assertEqual(series[0], self.base)

    def test_each_bar_mutates_the_bar_before_it_rather_than_the_base(self) -> None:
        # Compounding drift is the point: A -> A' -> A'' should keep moving away.
        series = mutation_series(
            self.base, bars=8, amount=1.0, rng=random.Random(5),
            syncopation=1.0, ghost_probability=0.6,
        )
        distinct = {tuple((s.position, s.velocity, s.octave) for s in bar) for bar in series}
        self.assertGreater(len(distinct), 1)

    def test_zero_amount_repeats_the_base_forever(self) -> None:
        series = mutation_series(
            self.base, bars=6, amount=0.0, rng=random.Random(5), syncopation=1.0
        )
        self.assertTrue(all(bar == self.base for bar in series))


class ComposerDrivenBySongSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = build_spec()

    def test_section_energy_drives_written_intensity(self) -> None:
        # Intensity is velocity-weighted, not a raw note count: mutation both
        # adds ghost notes and opens up space, so counts wobble by a note or two
        # between adjacent sections while the energy curve still rises cleanly.
        for compose in (compose_bass, compose_drums, compose_chords):
            notes = compose(self.spec)
            intensity = [
                sum(note.velocity for note in notes_in_section(notes, section))
                for section in self.spec.arrangement
            ]
            counts = [
                len(notes_in_section(notes, section)) for section in self.spec.arrangement
            ]
            self.assertEqual(
                intensity,
                sorted(intensity),
                f"{compose.__name__} intensity does not rise with section energy: {intensity}",
            )
            self.assertGreater(intensity[-1], intensity[0])
            self.assertGreater(counts[-1], counts[0])

    def test_section_energy_drives_velocity(self) -> None:
        # Mean, not peak: a single accent can push one note above a louder
        # section's peak without the section itself being louder.
        notes = compose_bass(self.spec)
        means = []
        for section in self.spec.arrangement:
            velocities = [note.velocity for note in notes_in_section(notes, section)]
            means.append(sum(velocities) / len(velocities))

        self.assertEqual(means, sorted(means))
        self.assertGreater(means[-1], means[0])

    def test_bass_mutation_zero_repeats_every_bar_of_a_section(self) -> None:
        still = with_bass(self.spec, mutation=0.0)

        counts = notes_per_bar(compose_bass(still), still)
        for section in still.arrangement:
            bars = section_bars(still, section.name)
            within = {counts[bar] for bar in bars}
            self.assertEqual(
                len(within), 1, f"{section.name} drifted with mutation 0.0: {within}"
            )

    def test_high_bass_mutation_makes_bars_drift(self) -> None:
        wild = with_bass(self.spec, mutation=1.0)

        counts = notes_per_bar(compose_bass(wild), wild)
        drifting = [
            section.name
            for section in wild.arrangement
            if len({counts[bar] for bar in section_bars(wild, section.name)}) > 1
        ]
        self.assertTrue(drifting, f"no section drifted at mutation 1.0: {counts}")

    def test_mutation_never_changes_the_harmony(self) -> None:
        expected = {
            midi_pitch(chord_root(chord), 2) % 12 for chord in self.spec.harmony.progression
        }
        for mutation in (0.0, 0.5, 1.0):
            notes = compose_bass(with_bass(self.spec, mutation=mutation))
            self.assertTrue(
                {note.pitch % 12 for note in notes} <= expected,
                f"mutation {mutation} moved the bass off the progression roots",
            )

    def test_octave_jumps_only_appear_when_the_spec_asks_for_them(self) -> None:
        flat = with_bass(self.spec, octave_jump_probability=0.0)
        jumpy = with_bass(self.spec, octave_jump_probability=1.0)

        flat_span = {note.pitch for note in compose_bass(flat)}
        jumpy_span = {note.pitch for note in compose_bass(jumpy)}

        self.assertLess(max(flat_span) - min(flat_span), max(jumpy_span) - min(jumpy_span))

    def test_ghost_notes_only_appear_when_the_spec_asks_for_them(self) -> None:
        silent = with_bass(self.spec, ghost_note_probability=0.0, octave_jump_probability=0.0)
        ghosted = with_bass(self.spec, ghost_note_probability=1.0, octave_jump_probability=0.0)

        def quiet(notes) -> int:
            loudest = max(note.velocity for note in notes)
            return len([note for note in notes if note.velocity < loudest * 0.6])

        self.assertGreater(quiet(compose_bass(ghosted)), quiet(compose_bass(silent)))

    def test_composition_is_reproducible_from_the_seed(self) -> None:
        for compose in (compose_bass, compose_drums, compose_chords):
            self.assertEqual(compose(build_spec()), compose(build_spec()))

    def test_every_note_stays_inside_the_song_and_midi_range(self) -> None:
        song_end = self.spec.song.total_bars * 4
        for compose in (compose_bass, compose_drums, compose_chords):
            for note in compose(self.spec):
                self.assertGreaterEqual(note.start_beats, 0.0)
                self.assertLess(note.start_beats, song_end)
                self.assertTrue(0 <= note.pitch <= 127)
                self.assertTrue(1 <= note.velocity <= 127)


if __name__ == "__main__":
    unittest.main()


class DisplacementTests(unittest.TestCase):
    def test_displacement_never_lands_on_an_occupied_slot(self) -> None:
        """Two steps at one position is a doubled note, not a mutation.

        Humanize then separates them by a fraction of a millisecond, so it hides
        in the data as two notes 0.0001 beats apart rather than as an exact
        duplicate. Ghost insertion always checked occupancy; displacement did not.
        """

        base = build_pattern(
            (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75),
            density=1.0,
            minimum=8,
            maximum=8,
            duration=0.2,
            velocity=90,
        )
        for seed in range(60):
            pattern = base
            rng = random.Random(seed)
            for _ in range(12):
                pattern = mutate_pattern(
                    pattern, amount=1.0, rng=rng, syncopation=1.0, minimum_steps=2
                )
                positions = [step.position for step in pattern]
                self.assertEqual(
                    len(positions), len(set(positions)), f"seed {seed}: {positions}"
                )

    def test_a_step_with_nowhere_to_go_stays_put(self) -> None:
        # both neighbouring slots taken, so displacement has no legal move
        packed = (
            Step(position=0.0, duration=0.2, velocity=90),
            Step(position=0.25, duration=0.2, velocity=90),
            Step(position=0.5, duration=0.2, velocity=90),
        )
        moved = _displace(packed[1], random.Random(0), 4.0, (packed[0], packed[2]))

        self.assertEqual(moved.position, 0.25)
