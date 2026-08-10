"""The drum pattern name has to reach the notes, not just the audio prompt.

Every genre used to play one groove here. These tests are mostly about the two
ways that can silently come back: a name that no table defines, and a genre
whose SongSpec says ``one_drop`` while its MIDI says four-on-the-floor.
"""

from __future__ import annotations

import unittest

from kihachi_music_ai.composer import compose_chords, compose_drums
from kihachi_music_ai.derive import FAMILY_PROFILES, GENRE_PROFILES
from kihachi_music_ai.groove_tables import (
    CHORD_ARTICULATIONS,
    DEFAULT_ARTICULATION,
    DEFAULT_PATTERN,
    DRUM_PATTERNS,
    KICK,
    SNARE,
    DrumPattern,
    chord_articulation,
    drum_pattern,
)
from kihachi_music_ai.music_brain import MusicBrain


def _kicks(spec, bar: int = 0) -> list[float]:
    """Kick positions inside one bar, rounded onto the sixteenth grid.

    Bar 0 is the first bar of the first section, which ``mutation_series``
    always leaves as the untouched base pattern -- so this reads the groove
    itself rather than a mutation of it.
    """

    return sorted(
        round(note.start_beats - bar * 4.0, 2)
        for note in compose_drums(spec)
        if note.pitch == KICK and bar * 4.0 <= note.start_beats < (bar + 1) * 4.0
    )


class VocabularyTests(unittest.TestCase):
    def test_every_pattern_a_genre_can_ask_for_is_defined(self) -> None:
        profiles = list(FAMILY_PROFILES.values()) + list(GENRE_PROFILES.values())
        named = {p.drum_pattern for p in profiles if p.drum_pattern}

        self.assertEqual(named - set(DRUM_PATTERNS), set())

    def test_no_pattern_is_defined_that_nothing_can_ask_for(self) -> None:
        profiles = list(FAMILY_PROFILES.values()) + list(GENRE_PROFILES.values())
        named = {p.drum_pattern for p in profiles if p.drum_pattern}
        # The composer's own fallback is reachable without a profile naming it.
        named.add(DEFAULT_PATTERN)

        self.assertEqual(set(DRUM_PATTERNS) - named, set())

    def test_an_unknown_name_composes_rather_than_raising(self) -> None:
        # Specs are hand-edited and older versions wrote other strings; a song
        # that will not compose is worse than an ordinary kick.
        self.assertEqual(drum_pattern("no_such_pattern"), DRUM_PATTERNS[DEFAULT_PATTERN])

    def test_an_anchor_outside_the_quietest_pattern_is_rejected(self) -> None:
        # Anchors are what the mutation engine will not drop. One that only
        # appears at high density is a groove that holds together when loud.
        with self.assertRaises(ValueError):
            DrumPattern(kick_positions=(0.0, 2.0), kick_steps=(1, 2), kick_anchors=(2.0,))


class GrooveReachesTheNotesTests(unittest.TestCase):
    def _spec(self, prompt: str):
        return MusicBrain(seed=8).analyze(prompt + "。Am。")

    def test_reggae_leaves_the_downbeat_empty(self) -> None:
        spec = self._spec("レゲエ")

        self.assertEqual(spec.drums.pattern, "one_drop")
        kicks = _kicks(spec)
        self.assertNotIn(0.0, kicks)
        self.assertIn(2.0, kicks)

    def test_house_still_puts_a_kick_on_every_beat_it_can(self) -> None:
        spec = self._spec("ディープハウス")

        self.assertEqual(spec.drums.pattern, "four_on_floor")
        self.assertIn(0.0, _kicks(spec))

    def test_two_genres_no_longer_play_the_same_kick(self) -> None:
        reggae = _kicks(self._spec("レゲエ"))
        metal = _kicks(self._spec("Death Metal"))
        ambient = _kicks(self._spec("アンビエント"))

        self.assertNotEqual(reggae, metal)
        self.assertNotEqual(reggae, ambient)
        self.assertNotEqual(metal, ambient)

    def test_the_snare_moves_off_the_clap_where_the_kit_is_acoustic(self) -> None:
        rock = compose_drums(self._spec("ロック"))

        self.assertTrue(any(note.pitch == SNARE for note in rock))

    def test_a_groove_with_no_backbeat_writes_none(self) -> None:
        # Ambient does not get a quiet snare, it gets no snare.
        ambient = compose_drums(self._spec("アンビエント"))

        self.assertEqual([n for n in ambient if n.pitch in (SNARE, 39)], [])
        self.assertTrue(ambient, "ambient wrote no drums at all")

    def test_the_seed_prompt_keeps_the_groove_it_was_rendered_with(self) -> None:
        """``test_golden_midi`` pins the bytes; this says why they may not move."""

        spec = MusicBrain(seed=8).analyze(
            "Mutation Funk、DUB、Tech House。110 BPM、D#m。ファンキーなスラップベース。"
        )

        self.assertEqual(spec.drums.pattern, "syncopated_tech_house")
        self.assertEqual(
            DRUM_PATTERNS["syncopated_tech_house"], DRUM_PATTERNS["four_on_floor"]
        )


class ArticulationReachesTheNotesTests(unittest.TestCase):
    def _spec(self, prompt: str):
        return MusicBrain(seed=8).analyze(prompt + "。Am。")

    def _chord_lengths(self, prompt: str) -> list[float]:
        spec = self._spec(prompt)
        return [round(n.duration_beats, 3) for n in compose_chords(spec)]

    def test_every_articulation_a_genre_can_ask_for_is_defined(self) -> None:
        named = {p.articulation for p in FAMILY_PROFILES.values() if p.articulation}

        self.assertEqual(named - set(CHORD_ARTICULATIONS), set())

    def test_a_pad_is_no_longer_written_as_a_stab(self) -> None:
        # Ambient asks for sustained_pads. A fifth of a beat is not a pad.
        pad = self._chord_lengths("アンビエント")
        stab = self._chord_lengths("ディープハウス")

        self.assertGreater(min(pad), max(stab))

    def test_reggae_puts_its_chords_only_on_the_offbeats(self) -> None:
        spec = self._spec("レゲエ")

        self.assertEqual(spec.chords.articulation, "offbeat_skank")
        first_bar = {
            round(n.start_beats * 2) / 2
            for n in compose_chords(spec)
            if n.start_beats < 4.0
        }
        self.assertTrue(first_bar, "the skank wrote nothing")
        self.assertTrue(
            all(abs(position % 1.0 - 0.5) < 1e-6 for position in first_bar),
            f"a skank landed on a downbeat: {sorted(first_bar)}",
        )

    def test_an_unknown_articulation_composes_rather_than_raising(self) -> None:
        self.assertEqual(
            chord_articulation("no_such_articulation"),
            CHORD_ARTICULATIONS[DEFAULT_ARTICULATION],
        )

    def test_the_seed_prompts_articulation_is_the_pinned_one(self) -> None:
        spec = MusicBrain(seed=8).analyze(
            "Mutation Funk、DUB、Tech House。110 BPM、D#m。ファンキーなスラップベース。"
        )

        self.assertEqual(spec.chords.articulation, DEFAULT_ARTICULATION)


class MaterialTests(unittest.TestCase):
    """Whatever the groove, the properties every part is held to still hold."""

    def test_no_pattern_writes_two_notes_at_one_position(self) -> None:
        for name in DRUM_PATTERNS:
            spec = MusicBrain(seed=8).analyze("テクノ。Am。")
            spec = _with_pattern(spec, name)
            with self.subTest(pattern=name):
                notes = compose_drums(spec)
                self.assertTrue(notes, f"{name} wrote nothing")
                keys = [(round(n.start_beats, 6), n.pitch) for n in notes]
                self.assertEqual(len(keys), len(set(keys)), f"{name} doubled a note")

    def test_no_articulation_leaves_two_notes_of_one_pitch_overlapping(self) -> None:
        """The sustains introduced this and it is invisible until Live opens it.

        MIDI cannot say which note-off closes which note-on when two notes of
        the same pitch overlap. ``sustained_chords`` held a chord for two beats
        every two beats, so every voice ran into its own repeat: 36 overlaps in
        a 32-bar song, all of them read back at the wrong length.
        """

        base = MusicBrain(seed=8).analyze("テクノ。Am。")
        for name in CHORD_ARTICULATIONS:
            spec = _with_articulation(base, name)
            with self.subTest(articulation=name):
                by_pitch: dict[int, list] = {}
                for note in compose_chords(spec):
                    by_pitch.setdefault(note.pitch, []).append(note)
                for pitch, group in by_pitch.items():
                    group.sort(key=lambda note: note.start_beats)
                    for earlier, later in zip(group, group[1:]):
                        self.assertLessEqual(
                            earlier.start_beats + earlier.duration_beats,
                            later.start_beats,
                            f"{name}: pitch {pitch} runs into its own repeat",
                        )

    def test_no_pattern_writes_outside_the_song(self) -> None:
        for name in DRUM_PATTERNS:
            spec = _with_pattern(MusicBrain(seed=8).analyze("テクノ。Am。"), name)
            end = spec.song.total_bars * 4
            with self.subTest(pattern=name):
                for note in compose_drums(spec):
                    self.assertGreaterEqual(note.start_beats, 0.0)
                    self.assertLess(note.start_beats, end)
                    self.assertTrue(0 <= note.pitch <= 127)
                    self.assertEqual(note.channel, 9)


def _with_pattern(spec, name: str):
    from dataclasses import replace

    return replace(spec, drums=replace(spec.drums, pattern=name))


def _with_articulation(spec, name: str):
    from dataclasses import replace

    return replace(spec, chords=replace(spec.chords, articulation=name))


if __name__ == "__main__":
    unittest.main()
