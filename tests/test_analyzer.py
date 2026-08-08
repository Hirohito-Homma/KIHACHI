from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.analyzer import _analyze_sections, analyze_project, analyze_wave
from kihachi_music_ai.cli import main
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.pipeline import compose_project
from test_music_brain import EXAMPLE


CHORD_MIDI_NOTES = {
    "D#m": (51, 54, 58),
    "B": (47, 51, 54),
    "F#": (54, 58, 61),
    "C#": (49, 53, 56),
}


def write_click_track(path: Path, bpm: float, *, duration: float = 12.0, sample_rate: int = 8000) -> None:
    beat_frames = round(sample_rate * 60.0 / bpm)
    click_frames = round(sample_rate * 0.025)
    samples = array("h")
    for frame in range(round(duration * sample_rate)):
        beat_position = frame % beat_frames
        if beat_position < click_frames:
            envelope = math.exp(-beat_position / (sample_rate * 0.006))
            value = 0.8 * envelope * math.sin(2.0 * math.pi * 1000.0 * frame / sample_rate)
        else:
            value = 0.0
        samples.append(round(value * 32767.0))
    if sys.byteorder != "little":
        samples.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.tobytes())


def write_harmonic_section_track(path: Path, *, bpm: float = 110.0, sample_rate: int = 8000) -> None:
    bar_frames = round(sample_rate * 4.0 * 60.0 / bpm)
    beat_frames = round(sample_rate * 60.0 / bpm)
    section_amplitudes = (0.07, 0.12, 0.22, 0.36)
    samples = array("h")
    phase = [0.0, 0.0, 0.0]
    for bar in range(32):
        chord = ("D#m", "B", "F#", "C#")[bar % 4]
        frequencies = [440.0 * (2.0 ** ((note - 69) / 12.0)) for note in CHORD_MIDI_NOTES[chord]]
        amplitude = section_amplitudes[bar // 8]
        for frame_in_bar in range(bar_frames):
            value = 0.0
            for tone, frequency in enumerate(frequencies):
                phase[tone] += 2.0 * math.pi * frequency / sample_rate
                value += math.sin(phase[tone])
            value *= amplitude / len(frequencies)
            beat_position = frame_in_bar % beat_frames
            if beat_position < round(sample_rate * 0.012):
                click_envelope = math.exp(-beat_position / (sample_rate * 0.003))
                value += 0.12 * click_envelope * math.sin(2.0 * math.pi * 1500.0 * frame_in_bar / sample_rate)
            samples.append(round(max(-0.999, min(0.999, value)) * 32767.0))
    if sys.byteorder != "little":
        samples.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.tobytes())


class AnalyzerTests(unittest.TestCase):
    def test_late_energy_edit_does_not_change_earlier_boundary_detection(self) -> None:
        spec = MusicBrain(seed=8).analyze(EXAMPLE)
        hop_seconds = 0.02
        duration = spec.song.target_duration_sec
        bar_seconds = 4.0 * 60.0 / spec.song.bpm

        def envelope(final_section_dbfs: float) -> list[float]:
            section_dbfs = (-20.0, -19.4, -18.8, final_section_dbfs)
            values = []
            for index in range(math.ceil(duration / hop_seconds)):
                bar = min(spec.song.total_bars - 1, int(index * hop_seconds / bar_seconds))
                values.append(10.0 ** (section_dbfs[bar // 8] / 20.0))
            return values

        baseline = _analyze_sections(envelope(-18.2), hop_seconds, duration, spec)
        edited = _analyze_sections(envelope(-5.0), hop_seconds, duration, spec)

        self.assertIn(8, baseline["detected_boundaries_after_bar"])
        self.assertIn(8, edited["detected_boundaries_after_bar"])
        self.assertIn(16, baseline["detected_boundaries_after_bar"])
        self.assertIn(16, edited["detected_boundaries_after_bar"])
        self.assertEqual(baseline["planned_boundary_recall_within_one_bar"], 1.0)
        self.assertEqual(edited["planned_boundary_recall_within_one_bar"], 1.0)

    def test_click_track_tempo_and_pcm_metrics_are_measured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / "click.wav"
            write_click_track(audio, 120.0)
            result = analyze_wave(audio)

            self.assertEqual(result["format"]["sample_rate_hz"], 8000)
            self.assertEqual(result["format"]["channels"], 1)
            self.assertEqual(result["format"]["sample_width_bits"], 16)
            self.assertAlmostEqual(result["format"]["duration_sec"], 12.0, places=3)
            self.assertAlmostEqual(result["tempo"]["estimated_bpm"], 120.0, delta=1.0)
            self.assertGreater(result["tempo"]["confidence"], 0.5)
            self.assertEqual(result["level"]["clipped_sample_ratio"], 0.0)
            self.assertLess(result["harmony"]["key"]["confidence"], 0.2)

    def test_key_chords_sections_and_energy_are_measured_on_song_grid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            audio = project / "audio" / "ace-step-01.wav"
            write_harmonic_section_track(audio)

            payload = analyze_project(project).analysis

            self.assertEqual(payload["harmony"]["key"]["estimated_key"], "D# minor")
            self.assertGreaterEqual(payload["harmony"]["key"]["confidence"], 0.25)
            self.assertGreaterEqual(payload["harmony"]["chords"]["progression_match_ratio"], 0.75)
            self.assertEqual(payload["harmony"]["chords"]["confident_bar_coverage"], 1.0)
            self.assertEqual(len(payload["harmony"]["chords"]["bars"]), 32)
            self.assertGreater(payload["sections"]["energy_correlation_to_song_spec"], 0.9)
            boundaries = payload["sections"]["detected_boundaries_after_bar"]
            for expected in (8, 16, 24):
                self.assertTrue(any(abs(observed - expected) <= 1 for observed in boundaries))
            self.assertEqual(payload["sections"]["planned_boundary_recall_within_one_bar"], 1.0)

    def test_project_analysis_is_non_destructive_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            audio = project / "audio" / "ace-step-01.wav"
            write_click_track(audio, 110.0)
            original_hash = hashlib.sha256(audio.read_bytes()).hexdigest()

            manifest = analyze_project(project)
            payload = json.loads(manifest.analysis_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["audio_file"], "audio/ace-step-01.wav")
            self.assertEqual(payload["sha256"], original_hash)
            self.assertAlmostEqual(payload["tempo"]["estimated_bpm"], 110.0, delta=1.5)
            self.assertEqual(payload["song_spec_comparison"]["target_bpm"], 110)
            self.assertEqual(payload["song_spec_comparison"]["target_key"], "D# minor")
            self.assertIn(payload["song_spec_comparison"]["key_status"], {"not_detected", "low_confidence"})
            self.assertEqual(hashlib.sha256(audio.read_bytes()).hexdigest(), original_hash)
            with self.assertRaises(FileExistsError):
                analyze_project(project)

    def test_analyze_cli_reports_result_and_measured_tempo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            compose_project(EXAMPLE, project)
            write_click_track(project / "audio" / "ace-step-01.wav", 110.0)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(["analyze", str(project)])
            self.assertEqual(status, 0)
            self.assertTrue((project / "audio_analysis.json").is_file())
            self.assertIn("estimated BPM", stdout.getvalue())
            self.assertIn("estimated key", stdout.getvalue())
            self.assertIn("planned boundary recall", stdout.getvalue())


    def test_boundary_detection_scales_with_the_planned_section_count(self) -> None:
        # A fixed cap of 7 made full recall unreachable for a nine-section song,
        # which has eight planned boundaries, whatever the audio did.
        from kihachi_music_ai.analyzer import _analyze_sections

        short = MusicBrain(seed=8).analyze(EXAMPLE)
        long_form = MusicBrain(seed=8).analyze(EXAMPLE + "5分程度。")
        self.assertGreater(len(long_form.arrangement) - 1, 7)

        # A square energy curve that steps at every planned boundary.
        def envelope_for(spec):
            hop = 0.02
            bar_seconds = 4 * 60.0 / spec.song.bpm
            frames = int(spec.song.total_bars * bar_seconds / hop)
            values = []
            for frame in range(frames):
                bar = int(frame * hop / bar_seconds)
                section = next(
                    s for s in spec.arrangement
                    if s.start_bar <= bar < s.start_bar + s.length_bars
                )
                values.append(0.05 + 0.9 * section.energy)
            return values, hop, frames * hop

        for spec in (short, long_form):
            envelope, hop, duration = envelope_for(spec)
            report = _analyze_sections(envelope, hop, duration, spec)
            planned = report["planned_boundaries_after_bar"]
            matched = sum(
                item["matched_within_one_bar"] for item in report["planned_boundary_matches"]
            )
            self.assertEqual(
                matched,
                len(planned),
                f"{len(spec.arrangement)} sections: only {matched}/{len(planned)} recalled",
            )
        # And the old fixed cap of 7 is genuinely no longer the binding limit.
        envelope, hop, duration = envelope_for(long_form)
        detected = _analyze_sections(envelope, hop, duration, long_form)
        self.assertGreater(len(detected["detected_boundaries_after_bar"]), 7)


if __name__ == "__main__":
    unittest.main()
