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
GOLDEN_MIDI_SHA256 = {
    "bass": "44332f694c87fc4cd2c92b40ae9ef53b5d717ba4980a7425387dd489989e4451",
    # Deliberately re-pinned when the hat grid stopped being a switch.
    # ``hat_density * section density`` was thresholded at 0.3 to choose eighths
    # or quarters, so this song's four sections -- 0.195, 0.343, 0.515, 0.686 --
    # got 4, 7, 7 and 7 hats a bar: the drop and the groove were identical
    # despite twice the drum density. Thinning continuously gives 5, 5, 6 and 6,
    # which is the whole point of the field. Bass and chords are untouched.
    "drums": "c85e93d82c507624fef6d5d0af924929ccb618ff3353e1a65a7587d756bf9c25",
    "chords": "64901570dd48134f36d775a9d92576fd20c1335c26719d167cdca74f9fda3fac",
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
