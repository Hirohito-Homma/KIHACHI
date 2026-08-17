"""The notes themselves, pinned to bytes.

``tests/test_arrangement.py`` pins the SongSpec's SHA-256 and ``test_schema``
pins its serialization, but until now nothing compared a single ``.mid``. That
gap matters most exactly when the brain is being rewritten: a change can leave
every SongSpec number identical and still move the notes, because the composer
reads those numbers through thresholds and per-section RNG streams.

**The digests below are not the shipped files.** Regenerating from
``example_output/mutation-signal-lora/song_spec.json`` does not reproduce the
``.mid`` files sitting next to it -- they were written before some later change
to the composer, and no test noticed because none compared them. They are left
alone rather than refreshed: the audio and the analysis JSON in that directory
were rendered from those exact files, so replacing them would break a matched
set to fix a file nobody reads. What is pinned here is the composer's *current*
output for that spec, captured before the intent layer went in, which is the
thing a brain rewrite could silently move.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from kihachi_music_ai.composer import compose_tracks
from kihachi_music_ai.midi import build_midi_bytes
from kihachi_music_ai.models import SongSpec

GOLDEN_SPEC = (
    Path(__file__).resolve().parents[1]
    / "example_output"
    / "mutation-signal-lora"
    / "song_spec.json"
)

#: sha256 of the rendered file, per track. Captured at d180e79 (main, before
#: `intent.py`). A change here means the notes moved; that may be intended, but
#: it is never incidental.
#
# All three were deliberately re-pinned when swing became a warp of the whole
# beat rather than a delay on the odd 8ths (see ``composer.swung_position``).
# This song swings at 0.54, and its 16ths were previously left straight: the
# old rule classified them by ``int(round(start * 2))``, under which 0.25 and
# 0.75 are indistinguishable from beats. They now move with the beat they are
# in -- by 3.82 ms at this swing and 110 BPM -- while the beats and the 8ths
# land exactly where they did. A straight song (swing 0.5) is untouched, since
# the warp is the identity there; every song that swings at all changes here.
GOLDEN_MIDI_SHA256 = {
    "bass": "b410b53e7a48620d2eefc46521ef3c49343a1f5ca0a19948523db1f1ba3d3a07",
    # The hat grid was re-pinned once before, when it stopped being a switch.
    # ``hat_density * section density`` was thresholded at 0.3 to choose eighths
    # or quarters, so this song's four sections -- 0.195, 0.343, 0.515, 0.686 --
    # got 4, 7, 7 and 7 hats a bar: the drop and the groove were identical
    # despite twice the drum density. Thinning continuously gives 5, 5, 6 and 6,
    # which is the whole point of the field.
    "drums": "cd7c46e20d5f371fe21ca67b833b750ab04da430fb0f9294a3ef20758ca2c103",
    "chords": "ea73aec4152dd77b92ce0ed40b3e32864cd81e498ffa2584ed25e25bf559f4d0",
}


class GoldenMidiTests(unittest.TestCase):
    def test_the_composer_still_writes_the_same_notes(self) -> None:
        spec = SongSpec.from_json(GOLDEN_SPEC.read_text(encoding="utf-8"))

        for name, notes in compose_tracks(spec).items():
            with self.subTest(track=name):
                rendered = build_midi_bytes(
                    notes,
                    track_name=f"KIHACHI {name.title()}",
                    bpm=spec.song.bpm,
                    key=spec.song.key,
                )
                self.assertEqual(
                    hashlib.sha256(rendered).hexdigest(), GOLDEN_MIDI_SHA256[name]
                )


if __name__ == "__main__":
    unittest.main()
