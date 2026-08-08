from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.composer import compose_bass, compose_chords, compose_drums
from kihachi_music_ai.midi import PPQ, inspect_midi, write_midi
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

