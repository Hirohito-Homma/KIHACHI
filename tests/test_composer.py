from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.composer import (
    MONOPHONIC_GAP_BEATS,
    compose_bass,
    compose_chords,
    compose_drums,
    compose_tracks,
)
from kihachi_music_ai.midi import PPQ, inspect_midi, read_midi, write_midi
from kihachi_music_ai.music_brain import MusicBrain
from test_music_brain import EXAMPLE


class ComposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = MusicBrain(seed=8).analyze(EXAMPLE)

    def test_three_composers_generate_bounded_notes(self) -> None:
        song_end = self.spec.song.total_bars * 4
        for notes in (compose_bass(self.spec), compose_drums(self.spec), compose_chords(self.spec)):
            self.assertGreater(len(notes), 0)
            self.assertTrue(all(0 <= note.start_beats < song_end for note in notes))
            self.assertTrue(all(0 <= note.pitch <= 127 for note in notes))

    def test_drum_notes_use_general_midi_channel_ten(self) -> None:
        self.assertTrue(all(note.channel == 9 for note in compose_drums(self.spec)))

    def test_midi_writer_emits_valid_format_zero_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bass.mid"
            write_midi(
                path,
                compose_bass(self.spec),
                track_name="KIHACHI Bass",
                bpm=self.spec.song.bpm,
                key=self.spec.song.key,
            )
            info = inspect_midi(path)
            self.assertEqual(info.format_type, 0)
            self.assertEqual(info.track_count, 1)
            self.assertEqual(info.ppq, PPQ)
            self.assertGreater(info.track_bytes, 100)


if __name__ == "__main__":
    unittest.main()



LONG_PROMPT = EXAMPLE + "5分程度。"


class MaterialIntegrityTests(unittest.TestCase):
    """Properties every composed part has to hold whatever the seed does."""

    def _tracks(self, seed: int, prompt: str = LONG_PROMPT):
        return compose_tracks(MusicBrain(seed=seed).analyze(prompt))

    def test_no_part_ever_writes_two_notes_at_one_position(self) -> None:
        """A doubled note is a flam nobody asked for.

        This came from displacement moving a step onto an occupied slot; the
        duplicate then hid behind humanize as two notes 0.0001 beats apart.
        """

        for seed in (3, 8, 21, 42):
            for part, notes in self._tracks(seed).items():
                keys = [(round(note.start_beats, 6), note.pitch) for note in notes]
                self.assertEqual(
                    len(keys), len(set(keys)), f"seed {seed} {part} doubled a note"
                )

    def test_the_bass_plays_one_note_at_a_time(self) -> None:
        for seed in (3, 8, 21, 42):
            notes = sorted(self._tracks(seed)["bass"], key=lambda n: n.start_beats)
            for earlier, later in zip(notes, notes[1:]):
                self.assertLessEqual(
                    earlier.start_beats + earlier.duration_beats,
                    later.start_beats,
                    f"seed {seed}: bass note at {earlier.start_beats} runs into the next",
                )

    def test_every_part_survives_the_trip_through_a_midi_file(self) -> None:
        """The file is what Live reads, so the notes have to come back out.

        When one note of a pitch is still sounding as the next one starts *and*
        ends after it, MIDI cannot say which note-off closes which note-on: a
        reader pairs them first-in-first-out and hands back lengths nobody wrote.
        The bass used to do this on 4-6 notes a song, worst case 0.173 beats (83
        ticks) -- but only on some seeds, so this runs across several. Seed 8
        happens to be clean and on its own would have proved nothing.
        """

        tick = 1.0 / PPQ
        for seed in (3, 8, 21, 42):
            with tempfile.TemporaryDirectory() as temp:
                for part, written in self._tracks(seed).items():
                    path = Path(temp) / f"{part}.mid"
                    write_midi(path, written, bpm=110.0, key="D# minor", track_name=part)
                    back = read_midi(path).notes

                    self.assertEqual(len(back), len(written), f"seed {seed} {part}")
                    # Compared per pitch: notes a hair apart can swap places in a
                    # global sort once their starts quantize, without anything
                    # actually having changed. Comparing positionally across all
                    # pitches reports differences that are not there.
                    for pitch in {note.pitch for note in written}:
                        before = sorted(
                            (n for n in written if n.pitch == pitch),
                            key=lambda n: n.start_beats,
                        )
                        after = sorted(
                            (n for n in back if n.pitch == pitch),
                            key=lambda n: n.start_beats,
                        )
                        self.assertEqual(len(before), len(after), f"{part} pitch {pitch}")
                        for source, restored in zip(before, after):
                            self.assertEqual(source.velocity, restored.velocity)
                            self.assertLess(
                                abs(source.start_beats - restored.start_beats), tick
                            )
                            self.assertLess(
                                abs(source.duration_beats - restored.duration_beats),
                                tick,
                                f"seed {seed} {part}: pitch {pitch} came back a "
                                "different length",
                            )

    def test_trimming_never_leaves_a_zero_length_note(self) -> None:
        for seed in (3, 8, 21, 42):
            for note in self._tracks(seed)["bass"]:
                self.assertGreaterEqual(note.duration_beats, MONOPHONIC_GAP_BEATS)
