from __future__ import annotations

import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.adapters.ace_step import (
    AceStepGenerationRequest,
    AceStepOptions,
    resolve_repaint_window,
)
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.repaint_planner import build_repaint_plan, load_repaint_plan
from kihachi_music_ai.tail_guard import (
    DEFAULT_TAIL_GUARD_BARS,
    guard_seconds,
    guarded_duration,
    measure_music_end,
    seconds_per_bar,
    trim_wav_to_duration,
    validate_guard_bars,
)
from test_music_brain import EXAMPLE
from test_reviewer import write_analysis


def build_spec():
    return MusicBrain(seed=8).analyze(EXAMPLE)


def write_tone_wav(
    path: Path,
    *,
    duration: float,
    music_end: float,
    sample_rate: int = 8000,
    channels: int = 2,
    sample_width: int = 2,
) -> None:
    """A tone that stops at ``music_end``, then a low noise floor to ``duration``.

    This is the shape ACE-Step delivers when it composes its ending before the
    requested buffer runs out.
    """

    frames = array("h")
    for frame in range(round(duration * sample_rate)):
        seconds = frame / sample_rate
        if seconds < music_end:
            value = 0.6 * math.sin(2.0 * math.pi * 220.0 * seconds)
        else:
            value = 0.0002 * math.sin(2.0 * math.pi * 3000.0 * seconds)
        for _ in range(channels):
            frames.append(int(value * 32767))
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(channels)
        sink.setsampwidth(sample_width)
        sink.setframerate(sample_rate)
        sink.writeframes(frames.tobytes())


class TailGuardMathTests(unittest.TestCase):
    def test_guard_duration_adds_whole_bars_to_the_song_grid(self) -> None:
        spec = build_spec()

        self.assertAlmostEqual(seconds_per_bar(spec), 4 * 60.0 / 110.0, places=9)
        self.assertEqual(guard_seconds(spec, 2.0), 4.364)
        self.assertEqual(guarded_duration(spec, 0.0), spec.song.target_duration_sec)
        self.assertEqual(guarded_duration(spec, 2.0), 74.182)

    def test_guard_bars_are_range_checked(self) -> None:
        self.assertEqual(validate_guard_bars(0), 0.0)
        for invalid in (-1.0, 9.0, float("nan"), True):
            with self.assertRaises(ValueError):
                validate_guard_bars(invalid)

    def test_guard_extends_only_a_window_that_reaches_the_final_bar(self) -> None:
        spec = build_spec()

        final = resolve_repaint_window(spec, section_name="psychedelic_drop", tail_guard_bars=2.0)
        self.assertEqual(final.start_sec, 52.364)
        self.assertEqual(final.end_sec, 74.182)
        self.assertEqual(final.tail_guard_sec, 4.364)
        self.assertEqual(final.to_dict()["tail_guard_sec"], 4.364)

        interior = resolve_repaint_window(spec, bar_range="17:24", tail_guard_bars=2.0)
        self.assertEqual(interior.end_sec, 52.364)
        self.assertEqual(interior.tail_guard_sec, 0.0)
        self.assertNotIn("tail_guard_sec", interior.to_dict())

        unguarded = resolve_repaint_window(spec, section_name="psychedelic_drop")
        self.assertEqual(unguarded.end_sec, 69.818)

    def test_bar_range_records_its_enclosing_section(self) -> None:
        spec = build_spec()

        inside = resolve_repaint_window(spec, bar_range="29:32")
        self.assertEqual(inside.section_name, "psychedelic_drop")

        straddling = resolve_repaint_window(spec, bar_range="20:28")
        self.assertIsNone(straddling.section_name)
        self.assertNotIn("section_name", straddling.to_dict())

    def test_request_duration_carries_the_guard(self) -> None:
        spec = build_spec()

        plain = AceStepGenerationRequest.from_song_spec(spec, AceStepOptions())
        guarded = AceStepGenerationRequest.from_song_spec(
            spec, AceStepOptions(tail_guard_bars=2.0)
        )

        self.assertEqual(plain.audio_duration, 69.818)
        self.assertEqual(guarded.audio_duration, 74.182)

    def test_guard_requires_wav_because_only_wav_is_trimmed(self) -> None:
        with self.assertRaises(ValueError):
            AceStepOptions(audio_format="mp3", tail_guard_bars=2.0)


class TrimTests(unittest.TestCase):
    def test_trim_keeps_the_grid_and_never_touches_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rendered = root / "render.untrimmed.wav"
            trimmed = root / "render.wav"
            write_tone_wav(rendered, duration=10.0, music_end=10.0)
            before = rendered.read_bytes()

            manifest = trim_wav_to_duration(rendered, trimmed, duration_sec=8.0)

            self.assertEqual(rendered.read_bytes(), before)
            self.assertEqual(manifest.source_frames, 80000)
            self.assertEqual(manifest.kept_frames, 64000)
            self.assertEqual(manifest.kept_duration_sec, 8.0)
            self.assertNotEqual(manifest.source_sha256, manifest.trimmed_sha256)
            with wave.open(str(trimmed), "rb") as handle:
                self.assertEqual(handle.getnframes(), 64000)
                self.assertEqual(handle.getnchannels(), 2)
                self.assertEqual(handle.getframerate(), 8000)

    def test_trim_fades_the_last_samples_so_a_mid_signal_cut_cannot_click(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rendered = root / "render.untrimmed.wav"
            trimmed = root / "render.wav"
            write_tone_wav(rendered, duration=6.0, music_end=6.0)

            manifest = trim_wav_to_duration(
                rendered, trimmed, duration_sec=4.0, fade_out_sec=0.01
            )

            self.assertEqual(manifest.fade_out_sec, 0.01)
            with wave.open(str(trimmed), "rb") as handle:
                handle.setpos(handle.getnframes() - 1)
                last = array("h")
                last.frombytes(handle.readframes(1))
            self.assertEqual(last[0], 0)

    def test_trim_refuses_to_overwrite_its_own_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            rendered = Path(temp) / "render.wav"
            write_tone_wav(rendered, duration=4.0, music_end=4.0)
            with self.assertRaises(ValueError):
                trim_wav_to_duration(rendered, rendered, duration_sec=2.0)

    def test_music_end_finds_the_silent_tail_the_guard_exists_to_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unguarded = root / "unguarded.wav"
            guarded = root / "guarded.wav"
            trimmed = root / "guarded-trimmed.wav"
            # Asked for 10 s, the model stopped composing at 8 s: bar-32 silence.
            write_tone_wav(unguarded, duration=10.0, music_end=8.0)
            # With 2 s of guard the ending lands past the grid instead.
            write_tone_wav(guarded, duration=12.0, music_end=10.5)

            trim_wav_to_duration(guarded, trimmed, duration_sec=10.0)

            self.assertAlmostEqual(measure_music_end(unguarded), 8.0, delta=0.05)
            self.assertAlmostEqual(measure_music_end(trimmed), 10.0, delta=0.05)


class BarLevelCandidateTests(unittest.TestCase):
    def _plan(self, **kwargs):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir(parents=True)
            spec = build_spec()
            (project / "song_spec.json").write_text(spec.to_json(), encoding="utf-8")
            write_analysis(
                project,
                tempo_delta=-0.3,
                key_status="low_confidence",
                chord_match=0.0,
                chord_coverage=0.375,
                boundary_recall=1.0,
                energy_correlation=0.4696,
            )
            import json

            analysis = json.loads(
                (project / "audio_analysis.json").read_text(encoding="utf-8")
            )
            return build_repaint_plan(spec, analysis, [], **kwargs)

    def test_plan_offers_a_bar_level_candidate_for_the_collapsed_tail(self) -> None:
        plan = self._plan()

        candidates = plan["bar_level_candidates"]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["selector"], "bars")
        # Only bar 32 collapsed, widened to four bars so the model keeps context.
        self.assertEqual(candidate["collapsed_bars"], [32])
        self.assertEqual((candidate["start_bar"], candidate["end_bar"]), (29, 32))
        self.assertEqual(candidate["section_name"], "psychedelic_drop")
        self.assertEqual(candidate["start_sec"], 61.091)
        self.assertEqual(candidate["end_sec"], 74.182)

    def test_section_stays_the_default_selection_and_the_guard_is_recorded(self) -> None:
        plan = self._plan()

        self.assertEqual(plan["selection"]["selector"], "section")
        self.assertEqual(plan["selection"]["start_bar"], 25)
        self.assertEqual(plan["selection"]["end_sec"], 74.182)
        self.assertEqual(
            plan["ace_step_options"]["tail_guard_bars"], DEFAULT_TAIL_GUARD_BARS
        )

    def test_prefer_bar_level_narrows_the_applied_selection_and_prompt(self) -> None:
        plan = self._plan(prefer_bar_level=True)

        self.assertEqual(plan["selection"]["selector"], "bars")
        self.assertEqual((plan["selection"]["start_bar"], plan["selection"]["end_bar"]), (29, 32))
        self.assertIn("bars 29-32 of psychedelic_drop", plan["revision_prompt"])
        self.assertIn("avoid an accidental silent tail", plan["revision_prompt"])

    def test_a_bar_level_plan_round_trips_through_the_plan_loader(self) -> None:
        import json

        plan = self._plan(prefer_bar_level=True)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "repaint_plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")

            loaded = load_repaint_plan(path)

        # A bar-level selection is accepted even though it is not a whole section.
        self.assertEqual(loaded["selection"]["selector"], "bars")
        self.assertEqual(loaded["selection"]["section_name"], "psychedelic_drop")

    def test_localized_collapse_is_recommended_only_when_the_section_is_on_target(
        self,
    ) -> None:
        # The fixture's drop sits 0.102 under target, so the whole section is wrong.
        self.assertEqual(self._plan()["recommended_selector"], "section")

    def test_guard_can_be_disabled(self) -> None:
        plan = self._plan(tail_guard_bars=0.0)

        self.assertEqual(plan["selection"]["end_sec"], 69.818)
        self.assertEqual(plan["ace_step_options"]["tail_guard_bars"], 0.0)

    def test_discontinuity_uses_a_window_that_contains_the_measured_click(self) -> None:
        defects = {
            "measurements": {"max_sample_jump_at_sec": 16.921},
            "findings": [
                {
                    "code": "discontinuity",
                    "severity": "warning",
                    "value": 0.8219,
                    "threshold": 0.5,
                }
            ],
        }

        plan = self._plan(material_defects=defects)

        selection = plan["selection"]
        self.assertEqual(selection["selector"], "bars")
        self.assertEqual((selection["start_bar"], selection["end_bar"]), (7, 10))
        self.assertEqual((selection["start_sec"], selection["end_sec"]), (13.091, 21.818))
        self.assertEqual(plan["recommended_selector"], "bars")
        self.assertEqual(plan["bar_level_candidates"][0]["defect_bar"], 8)
        self.assertIn("16.921 s falls in bar 8", plan["selection_reason"])
        self.assertIn("measured discontinuity at 16.921 seconds", plan["revision_prompt"])
        self.assertEqual(plan["ace_step_options"]["repaint_wav_crossfade_sec"], 0.5)


if __name__ == "__main__":
    unittest.main()
