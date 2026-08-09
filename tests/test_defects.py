from __future__ import annotations

import contextlib
import io
import json
import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.defects import scan_material
from test_analyzer import write_click_track
from test_music_brain import EXAMPLE

RATE = 8000


def write_wav(path: Path, frames, channels=2, sample_width=2, rate=RATE) -> None:
    data = array("h")
    for frame in frames:
        values = frame if isinstance(frame, (tuple, list)) else (frame,) * channels
        for value in values[:channels]:
            data.append(max(-32768, min(32767, int(round(value * 32767)))))
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(channels)
        sink.setsampwidth(sample_width)
        sink.setframerate(rate)
        sink.writeframes(data.tobytes())


def tone(seconds, amplitude=0.4, freq=220.0, rate=RATE):
    return [
        amplitude * math.sin(2.0 * math.pi * freq * i / rate)
        for i in range(int(seconds * rate))
    ]


def codes(report):
    return {finding["code"] for finding in report["findings"]}


def severity_of(report, code):
    return next(f["severity"] for f in report["findings"] if f["code"] == code)


class CleanMaterialTests(unittest.TestCase):
    def test_a_healthy_take_reports_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clean.wav"
            # a tone with light stereo decorrelation and honest dynamics
            # musical dynamics: a quiet bed with periodic transients, so the crest
            # lands in the 14-19 dB range real renders show
            base = tone(3.0, amplitude=0.12)
            frames = []
            for i, value in enumerate(base):
                phase = i % 4000
                if phase < 240:
                    # enveloped transient: real percussion decays, it does not step
                    envelope = math.exp(-phase / 45.0)
                    value += 0.85 * envelope * math.sin(2 * math.pi * 200 * i / RATE)
                frames.append((value, value * 0.8 + 0.06 * math.sin(2 * math.pi * 3 * i / RATE)))
            write_wav(path, frames)

            report = scan_material(path)

            self.assertTrue(report["clean"], report["findings"])
            self.assertEqual(report["blocking"], 0)
            self.assertEqual(report["warnings"], 0)

    def test_the_scan_never_reads_the_song_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.wav"
            write_wav(path, [(v, v) for v in tone(1.0)])

            report = scan_material(path)

        self.assertEqual(
            report["scope"], "absolute_audio_defects_not_song_spec_conformance"
        )
        self.assertIn("measurements", report)


class SilenceTests(unittest.TestCase):
    def _with_gap(self, gap_seconds):
        frames = (
            [(v, v) for v in tone(1.0)]
            + [(0.0, 0.0)] * int(gap_seconds * RATE)
            + [(v, v) for v in tone(1.0)]
        )
        return frames

    def test_a_short_dropout_is_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "gap.wav"
            write_wav(path, self._with_gap(0.8))

            report = scan_material(path)

            self.assertIn("silent_gap", codes(report))
            self.assertEqual(severity_of(report, "silent_gap"), "warning")
            self.assertGreaterEqual(report["measurements"]["longest_silence_sec"], 0.7)

    def test_a_long_dropout_blocks(self) -> None:
        """The bar-32 silence generalised: a hole this big makes a take unusable."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hole.wav"
            write_wav(path, self._with_gap(2.5))

            report = scan_material(path)

            self.assertEqual(severity_of(report, "silent_gap"), "blocking")
            self.assertEqual(report["blocking"], 1)

    def test_the_gap_is_located_not_just_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "gap.wav"
            write_wav(path, self._with_gap(1.0))

            report = scan_material(path)

            self.assertAlmostEqual(
                report["measurements"]["longest_silence_at_sec"], 1.0, delta=0.1
            )


class ClippingTests(unittest.TestCase):
    def test_sustained_full_scale_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clip.wav"
            frames = [(v, v) for v in tone(1.0, amplitude=2.0)]  # driven past full scale
            write_wav(path, frames)

            report = scan_material(path)

            self.assertIn("clipping", codes(report))
            self.assertGreater(report["measurements"]["clipped_runs"], 0)

    def test_a_single_peak_at_full_scale_is_not_clipping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "peak.wav"
            frames = [(v, v) for v in tone(1.0, amplitude=0.4)]
            frames[500] = (1.0, 1.0)
            write_wav(path, frames)

            report = scan_material(path)

            self.assertNotIn("clipping", codes(report))


class DcOffsetTests(unittest.TestCase):
    def test_a_constant_offset_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dc.wav"
            frames = [(v + 0.05, v + 0.05) for v in tone(2.0)]  # 5%, well past the 1% limit
            write_wav(path, frames)

            report = scan_material(path)

            self.assertIn("dc_offset", codes(report))
            self.assertGreater(max(report["measurements"]["dc_offset"]), 0.04)


class StereoTests(unittest.TestCase):
    def test_identical_channels_read_as_mono(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mono.wav"
            write_wav(path, [(v, v) for v in tone(2.0)])

            report = scan_material(path)

            self.assertIn("mono_collapse", codes(report))
            self.assertEqual(severity_of(report, "mono_collapse"), "info")

    def test_inverted_channels_are_a_phase_warning(self) -> None:
        """A mix that cancels in mono loses level wherever mono playback happens."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "phase.wav"
            write_wav(path, [(v, -v) for v in tone(2.0)])

            report = scan_material(path)

            self.assertIn("phase_cancellation", codes(report))
            self.assertLess(report["measurements"]["stereo_correlation"], 0.0)

    def test_a_mono_file_is_not_judged_on_stereo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "single.wav"
            write_wav(path, tone(1.0), channels=1)

            report = scan_material(path)

            self.assertIsNone(report["measurements"]["stereo_correlation"])
            self.assertNotIn("mono_collapse", codes(report))


class DiscontinuityTests(unittest.TestCase):
    def test_a_splice_click_is_located(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "click.wav"
            # a constant level then a hard step: the surrounding slew is ~0, so the
            # step stands far above it and reads as a click
            frames = [(0.3, 0.3)] * RATE + [(-0.6, -0.6)] * RATE
            write_wav(path, frames)

            report = scan_material(path)

            self.assertIn("discontinuity", codes(report))
            self.assertAlmostEqual(
                report["measurements"]["max_sample_jump_at_sec"], 1.0, delta=0.05
            )


class DynamicsTests(unittest.TestCase):
    def test_a_square_wave_reads_as_crushed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "square.wav"
            frames = [
                (0.7 if (i // 40) % 2 else -0.7, 0.68 if (i // 40) % 2 else -0.68)
                for i in range(2 * RATE)
            ]
            write_wav(path, frames)

            report = scan_material(path)

            self.assertIn("crushed_dynamics", codes(report))
            self.assertLess(report["measurements"]["crest_db"], 8.0)


class RobustnessTests(unittest.TestCase):
    def test_findings_carry_the_number_they_were_based_on(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "gap.wav"
            write_wav(path, [(v, v) for v in tone(0.5)] + [(0.0, 0.0)] * RATE)

            report = scan_material(path)

            for finding in report["findings"]:
                self.assertIn("value", finding)
                self.assertIn("threshold", finding)
                self.assertIn(finding["severity"], {"blocking", "warning", "info"})

    def test_an_empty_or_compressed_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "empty.wav"
            with wave.open(str(path), "wb") as sink:
                sink.setnchannels(2)
                sink.setsampwidth(2)
                sink.setframerate(RATE)
                sink.writeframes(b"")
            with self.assertRaises(ValueError):
                scan_material(path)


class AnalyzeWiringTests(unittest.TestCase):
    """`analyze` writes the defect scan as its own artifact.

    Deliberately a separate file from audio_analysis.json: conformance and
    defects answer different questions, and the take that scored 56.32 for
    alignment while carrying a 2.28 s hole is why they must not be averaged.
    """

    def _project(self, root: Path):
        from kihachi_music_ai.pipeline import compose_project

        project = root / "project"
        compose_project(EXAMPLE, project)
        audio = project / "audio" / "ace-step-01.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        write_click_track(audio, 110.0)
        return project

    def test_analyze_writes_a_separate_defect_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = main(["analyze", str(project)])

            self.assertEqual(status, 0)
            payload = json.loads(
                (project / "material_defects.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                payload["scope"], "absolute_audio_defects_not_song_spec_conformance"
            )
            self.assertIn("material defects", out.getvalue())
            analysis = json.loads(
                (project / "audio_analysis.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("findings", analysis)

    def test_a_hole_in_the_take_is_reported_by_the_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            audio = project / "audio" / "ace-step-01.wav"
            write_wav(
                audio,
                [(v, v) for v in tone(4.0)]
                + [(0.0, 0.0)] * int(2.5 * RATE)
                + [(v, v) for v in tone(4.0)],
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["analyze", str(project)])

            self.assertIn("silent_gap(blocking)", out.getvalue())

    def test_an_existing_scan_is_refused_rather_than_replaced(self) -> None:
        """Same guard as every other artifact: say no, do not quietly proceed."""

        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            scan = project / "material_defects.json"
            scan.write_text('{"authored": true}\n', encoding="utf-8")

            error = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error):
                status = main(["analyze", str(project)])

            self.assertEqual(status, 2)
            self.assertIn("material_defects.json", error.getvalue())
            self.assertEqual(json.loads(scan.read_text(encoding="utf-8")), {"authored": True})

    def test_overwrite_replaces_both_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["analyze", str(project)]), 0)
                self.assertEqual(main(["analyze", str(project), "--overwrite"]), 0)

            payload = json.loads(
                (project / "material_defects.json").read_text(encoding="utf-8")
            )
            self.assertIn("measurements", payload)

    def test_the_library_scans_without_going_through_the_command_line(self) -> None:
        """The whole point of moving it: callers other than the CLI get it too.

        A batch rescore of twenty stored renders reported no defects at all
        because analyze_project skipped the scan that only the CLI performed.
        """

        from kihachi_music_ai.analyzer import analyze_project

        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))

            manifest = analyze_project(project)

            self.assertIsNotNone(manifest.defects)
            self.assertEqual(manifest.defects_file, project / "material_defects.json")
            self.assertTrue(manifest.defects_file.is_file())

    def test_the_scan_can_be_turned_off_for_callers_that_do_not_want_it(self) -> None:
        from kihachi_music_ai.analyzer import analyze_project

        with tempfile.TemporaryDirectory() as temp:
            project = self._project(Path(temp))

            manifest = analyze_project(project, scan_defects=False)

            self.assertIsNone(manifest.defects)
            self.assertIsNone(manifest.defects_file)
            self.assertFalse((project / "material_defects.json").exists())


class ReviewWiringTests(unittest.TestCase):
    def test_review_raises_a_defect_finding_when_the_scan_is_there(self) -> None:
        from kihachi_music_ai.reviewer import _defect_findings

        findings = _defect_findings(
            {
                "clean": False,
                "findings": [
                    {
                        "code": "silent_gap",
                        "severity": "blocking",
                        "detail": "2.28 s of silence at 66.1 s",
                    },
                    {"code": "mono_collapse", "severity": "info", "detail": "identical channels"},
                ],
            }
        )

        # info-level observations are not defects to act on
        self.assertEqual([f["code"] for f in findings], ["material_silent_gap"])
        self.assertEqual(findings[0]["severity"], "high")
        self.assertIn("tail-guard", findings[0]["recommendation"])

    def test_the_review_cli_prints_the_defect_next_to_the_score(self) -> None:
        """The score alone hid a 2.28 s hole behind an 88.69 'aligned'."""

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as temp:
            project = self._reviewed_project(Path(temp))
            with contextlib.redirect_stdout(out):
                main(["review", str(project), "--overwrite"])

        printed = out.getvalue()
        self.assertIn("material blocking", printed)
        self.assertIn("starting at 4.00 s", printed)

    def _reviewed_project(self, root: Path):
        from kihachi_music_ai.pipeline import compose_project

        project = root / "project"
        compose_project(EXAMPLE, project)
        audio = project / "audio" / "ace-step-01.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        write_wav(
            audio,
            [(v, v) for v in tone(4.0)]
            + [(0.0, 0.0)] * int(2.5 * RATE)
            + [(v, v) for v in tone(4.0)],
        )
        with contextlib.redirect_stdout(io.StringIO()):
            main(["analyze", str(project)])
        return project

    def test_a_project_without_a_scan_reviews_unchanged(self) -> None:
        from kihachi_music_ai.reviewer import _defect_findings, _material_defects

        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(_material_defects(Path(temp)))
        self.assertEqual(_defect_findings(None), [])


if __name__ == "__main__":
    unittest.main()
