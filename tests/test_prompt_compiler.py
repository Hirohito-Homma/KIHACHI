from __future__ import annotations

import dataclasses
import unittest

from kihachi_music_ai.models import TRACK_NAMES
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.prompt_compiler import band, compile_audio_prompt
from test_music_brain import EXAMPLE

LONG_PROMPT = EXAMPLE + "5分程度。"


def build_spec(prompt: str = EXAMPLE):
    return MusicBrain(seed=8).analyze(prompt)


def with_song(spec, **groups):
    for group, fields in groups.items():
        spec = dataclasses.replace(
            spec, **{group: dataclasses.replace(getattr(spec, group), **fields)}
        )
    return spec


def with_sections(spec, **fields):
    return dataclasses.replace(
        spec,
        arrangement=tuple(
            dataclasses.replace(section, **fields) for section in spec.arrangement
        ),
    )


class BandTests(unittest.TestCase):
    def test_band_spans_the_unit_interval(self) -> None:
        labels = ("low", "mid", "high")

        self.assertEqual(band(0.0, labels), "low")
        self.assertEqual(band(0.5, labels), "mid")
        self.assertEqual(band(1.0, labels), "high")

    def test_band_clamps_out_of_range_values(self) -> None:
        labels = ("low", "high")

        self.assertEqual(band(-5.0, labels), "low")
        self.assertEqual(band(5.0, labels), "high")

    def test_band_requires_labels(self) -> None:
        with self.assertRaises(ValueError):
            band(0.5, ())


class DerivedPromptTests(unittest.TestCase):
    """Every clause has to move when the number behind it moves."""

    def setUp(self) -> None:
        self.spec = build_spec(LONG_PROMPT)
        self.base = compile_audio_prompt(self.spec)

    def assert_responds(self, changed) -> str:
        text = compile_audio_prompt(changed)
        self.assertNotEqual(text, self.base)
        return text

    def test_song_level_numbers_all_reach_the_prompt(self) -> None:
        cases = {
            "style.darkness": with_song(self.spec, style={"darkness": 0.05}),
            "style.psychedelic": with_song(self.spec, style={"psychedelic": 0.05}),
            "groove.syncopation": with_song(self.spec, groove={"syncopation": 0.05}),
            "groove.humanize": with_song(self.spec, groove={"humanize": 0.95}),
            "groove.swing": with_song(self.spec, groove={"swing": 0.5}),
            "bass.role": with_song(self.spec, bass={"role": "supporting"}),
            "bass.syncopation": with_song(self.spec, bass={"syncopation": 0.05}),
            "bass.ghost": with_song(self.spec, bass={"ghost_note_probability": 0.0}),
            "bass.octave": with_song(self.spec, bass={"octave_jump_probability": 0.0}),
            "drums.kick_density": with_song(self.spec, drums={"kick_density": 0.05}),
            "drums.hat_density": with_song(self.spec, drums={"hat_density": 0.05}),
            "drums.dub_space": with_song(self.spec, drums={"dub_space": 0.0}),
            "chords.instrument": with_song(self.spec, chords={"instrument": "rhodes"}),
            "chords.articulation": with_song(self.spec, chords={"articulation": "long_pads"}),
            "chords.dub_delay": with_song(self.spec, chords={"dub_delay": 0.02}),
            "vocal.vocoder": with_song(self.spec, vocal={"vocoder": False}),
        }
        for name, changed in cases.items():
            with self.subTest(field=name):
                self.assert_responds(changed)

    def test_section_fx_amount_reaches_the_prompt(self) -> None:
        dry = self.assert_responds(with_sections(self.spec, fx_amount=0.0))

        self.assertNotIn("dub echoes", dry.split("Arrangement:")[1].split("Overall")[0])
        drenched = compile_audio_prompt(with_sections(self.spec, fx_amount=1.0))
        self.assertIn("drenched in dub fx", drenched)

    def test_section_vocal_probability_reaches_the_prompt(self) -> None:
        silent = self.assert_responds(with_sections(self.spec, vocal_probability=0.0))
        led = compile_audio_prompt(with_sections(self.spec, vocal_probability=1.0))

        self.assertIn("no vocal", silent)
        self.assertIn("vocal-led", led)

    def test_an_instrumental_song_never_mentions_section_vocals(self) -> None:
        instrumental = with_song(self.spec, vocal={"enabled": False})

        text = compile_audio_prompt(with_sections(instrumental, vocal_probability=1.0))

        self.assertIn("instrumental, no vocal", text)
        self.assertNotIn("vocal-led", text)

    def test_resting_tracks_are_stated(self) -> None:
        text = compile_audio_prompt(self.spec)

        self.assertIn("no drums", text)

    def test_the_arc_is_described_by_its_peak_not_its_last_section(self) -> None:
        # The five-minute arc ends on a quiet outro; describing it by the final
        # section would call a song that climbs to 0.95 "one level throughout".
        text = compile_audio_prompt(self.spec)

        peak = max(self.spec.arrangement, key=lambda section: section.energy)
        self.assertIn(f"peak of {peak.energy:.2f}", text)
        self.assertIn(peak.name.replace("_", " "), text)
        self.assertNotIn("hold one level throughout", text)

    def test_a_flat_arrangement_is_described_as_flat(self) -> None:
        flat = with_sections(self.spec, energy=0.5)

        self.assertIn("hold one level throughout", compile_audio_prompt(flat))

    def test_one_chord_per_bar_reads_as_english(self) -> None:
        text = compile_audio_prompt(self.spec)

        self.assertIn("one chord per bar", text)
        self.assertNotIn("bar(s)", text)
        slower = with_song(self.spec, harmony={"harmonic_rhythm_bars": 2})
        self.assertIn("one chord every 2 bars", compile_audio_prompt(slower))

    def test_per_section_detail_is_capped_so_a_long_song_stays_readable(self) -> None:
        loud = with_sections(
            self.spec, fx_amount=1.0, vocal_probability=1.0, psychedelic=1.0, minimal=True
        )

        text = compile_audio_prompt(loud)

        arrangement = text.split("Arrangement:")[1]
        for section in loud.arrangement:
            phrase = arrangement.split(section.name.replace("_", " "))[1].split(";")[0]
            # name + bars + energy + at most three traits
            self.assertLessEqual(phrase.count(","), 2 + 3)

    def test_the_prompt_still_states_the_hard_musical_facts(self) -> None:
        text = compile_audio_prompt(self.spec)

        self.assertIn("110 BPM", text)
        self.assertIn("D# minor", text)
        self.assertIn("D#m - B - F# - C#", text)
        self.assertIn("dark robotic phrases", text)
        for section in self.spec.arrangement:
            self.assertIn(section.name.replace("_", " "), text)

    def test_a_legacy_spec_without_engine_detail_still_compiles(self) -> None:
        bare = dataclasses.replace(
            self.spec,
            arrangement=tuple(
                dataclasses.replace(
                    section,
                    bass_density=None,
                    drum_density=None,
                    chord_density=None,
                    fx_amount=None,
                    vocal_probability=None,
                    mutation=None,
                    active_tracks=None,
                )
                for section in self.spec.arrangement
            ),
        )

        text = compile_audio_prompt(bare)

        self.assertIn("Arrangement:", text)
        self.assertNotIn("no drums", text)
        self.assertNotIn("vocal-led", text)
        for track in TRACK_NAMES:
            self.assertNotIn(f"no {track} or", text)


if __name__ == "__main__":
    unittest.main()
