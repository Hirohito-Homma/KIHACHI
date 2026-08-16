from __future__ import annotations

import json
import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.sampler import MAX_ZERO_CROSSING_NUDGE_SEC, cut_sample
from kihachi_music_ai.tail_guard import seconds_per_bar
from test_music_brain import EXAMPLE

RATE = 8000


def write_take(
    path: Path, *, seconds: float, frequency: float = 220.0, offset: float = 0.0
) -> None:
    """A continuous tone: every bar boundary lands mid-signal, as in a real take.

    ``offset`` lifts the whole tone off zero. At 0.7 with a 0.2 tone there is no
    crossing anywhere in the file, which is the only way to test the fade path
    without depending on where a crossing happens to fall.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    amplitude = 0.2 if offset else 0.6
    samples = array("h")
    for frame in range(int(seconds * RATE)):
        value = offset + amplitude * math.sin(2.0 * math.pi * frequency * frame / RATE)
        sample = max(-32767, min(32767, int(value * 32767)))
        samples.extend((sample, sample))
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(2)
        sink.setsampwidth(2)
        sink.setframerate(RATE)
        sink.writeframes(samples.tobytes())


def build_project(root: Path, seconds: float = 40.0) -> tuple[Path, SongSpec]:
    project = root / "song"
    compose_project(EXAMPLE, project)
    write_take(project / "audio" / "ace-step-01.wav", seconds=seconds)
    spec = SongSpec.from_json((project / "song_spec.json").read_text(encoding="utf-8"))
    return project, spec


def read(path: Path) -> tuple[array, int]:
    with wave.open(str(path), "rb") as source:
        raw = source.readframes(source.getnframes())
        channels = source.getnchannels()
    samples = array("h")
    samples.frombytes(raw)
    return samples, channels


class WindowTests(unittest.TestCase):
    def test_the_window_is_the_bar_grid_the_song_spec_defines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))

            manifest = cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="mid")

            bar = seconds_per_bar(spec)
            self.assertAlmostEqual(manifest.record["grid_duration_sec"], 4 * bar, places=5)
            # The snap moves each edge, but never audibly.
            self.assertAlmostEqual(
                manifest.record["duration_sec"], 4 * bar, delta=2 * MAX_ZERO_CROSSING_NUDGE_SEC
            )
            self.assertEqual(manifest.record["bars"], {"start": 5, "end": 9, "count": 4})

    def test_a_window_past_the_end_of_the_render_is_refused(self) -> None:
        """Rather than returning a short sample that looks like a whole one."""

        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp), seconds=10.0)

            with self.assertRaises(ValueError) as caught:
                cut_sample(project, spec=spec, start_bar=1, end_bar=32, name="toolong")

            self.assertIn("the render is", str(caught.exception))

    def test_an_inverted_or_zero_width_window_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))

            for start, end in ((9, 9), (13, 9), (0, 4)):
                with self.assertRaises(ValueError):
                    cut_sample(project, spec=spec, start_bar=start, end_bar=end, name="bad")


class EdgeTests(unittest.TestCase):
    """The whole point of cutting on zero crossings: no click at the join.

    The bound is one sample step of the signal, not an absolute fraction of
    peak: a discrete crossing lands within a step of zero and cannot do better.
    That step is what the sample rate buys -- 17% of peak for a 220 Hz tone at
    this test's 8 kHz, and 2.9% at the 48 kHz a real render arrives in, where
    this cut measured 0.04%.
    """

    def step(self, peak: int, frequency: float = 220.0) -> float:
        return peak * math.sin(2.0 * math.pi * frequency / RATE)

    def test_both_edges_land_within_one_sample_step_of_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))

            manifest = cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="mid")

            samples, channels = read(manifest.sample_file)
            peak = max(abs(value) for value in samples)
            self.assertLessEqual(abs(samples[0]), self.step(peak))
            self.assertLessEqual(abs(samples[-channels]), self.step(peak))

    def test_the_loop_point_does_not_jump(self) -> None:
        """End-to-start is the join a looped sample actually plays."""

        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))

            manifest = cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="mid")

            samples, channels = read(manifest.sample_file)
            peak = max(abs(value) for value in samples)
            jump = abs(samples[0] - samples[-channels])
            self.assertLessEqual(jump, 2 * self.step(peak))
            # Far below the discontinuity the defect scanner calls a click.
            self.assertLess(jump / peak, 0.05)

    def test_an_edge_with_no_crossing_nearby_is_faded_and_says_so(self) -> None:
        """A tone that never reaches zero has no crossing to snap to."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, spec = build_project(root)
            write_take(project / "audio" / "ace-step-01.wav", seconds=40.0, offset=0.7)

            manifest = cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="slow")

            edges = manifest.record["edges"]
            self.assertTrue(edges["faded_edges"])
            for edge in edges["faded_edges"]:
                self.assertFalse(edges[f"{edge}_snapped_to_zero_crossing"])
            samples, channels = read(manifest.sample_file)
            peak = max(abs(value) for value in samples)
            self.assertLess(abs(samples[0]) / peak, 0.01)


class ProvenanceTests(unittest.TestCase):
    def test_the_sample_carries_where_it_came_from(self) -> None:
        """A sample outlives its project, so the trail travels with it."""

        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))

            record = cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="mid").record

            self.assertEqual(record["source"]["project"], project.name)
            self.assertEqual(len(record["source"]["audio_sha256"]), 64)
            self.assertEqual(len(record["sha256"]), 64)
            self.assertEqual(record["bpm"], spec.song.bpm)
            self.assertEqual(record["key"], spec.song.key)

    def test_the_key_is_labelled_as_asked_for_not_as_measured(self) -> None:
        """The generator does not follow the key; the label must not imply it did."""

        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))

            record = cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="mid").record

            self.assertIn("not what was measured", record["scope"])

    def test_the_render_is_never_touched(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))
            render = project / "audio" / "ace-step-01.wav"
            before = render.read_bytes()

            cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="mid")

            self.assertEqual(render.read_bytes(), before)


class KnownDefectTests(unittest.TestCase):
    """Cutting from the middle avoids the model's bad ends, not the material's."""

    def scan(self, project: Path, *, at_sec: float) -> None:
        (project / "material_defects.json").write_text(
            json.dumps(
                {
                    "findings": [
                        {"code": "discontinuity", "severity": "warning", "detail": "a click"}
                    ],
                    "measurements": {"max_sample_jump_at_sec": at_sec},
                }
            ),
            encoding="utf-8",
        )

    def test_a_window_over_a_located_defect_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))
            bar = seconds_per_bar(spec)
            self.scan(project, at_sec=5 * bar)  # inside bars 5:9

            record = cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="over").record

            inside = record["known_defects_inside"]
            self.assertEqual(len(inside), 1)
            self.assertEqual(inside[0]["code"], "discontinuity")
            self.assertAlmostEqual(inside[0]["at_sec_in_sample"], bar, places=2)

    def test_a_window_clear_of_it_carries_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))
            bar = seconds_per_bar(spec)
            self.scan(project, at_sec=2 * bar)  # inside bars 3:4, not 5:9

            record = cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="clear").record

            self.assertEqual(record["known_defects_inside"], [])

    def test_an_empty_list_does_not_claim_the_sample_is_clean(self) -> None:
        """The render scan locates one position per code: the worst one."""

        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))

            record = cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="mid").record

            self.assertEqual(record["known_defects_inside"], [])
            self.assertIn("not a clean sample", record["known_defects_scope"])


class ManifestTests(unittest.TestCase):
    def test_a_second_sample_is_added_rather_than_replacing_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))

            cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="a")
            manifest = cut_sample(project, spec=spec, start_bar=9, end_bar=13, name="b")

            written = json.loads(manifest.manifest_file.read_text(encoding="utf-8"))
            self.assertEqual([item["name"] for item in written["samples"]], ["a", "b"])

    def test_reusing_a_name_needs_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, spec = build_project(Path(temp))
            cut_sample(project, spec=spec, start_bar=5, end_bar=9, name="a")

            with self.assertRaises(FileExistsError):
                cut_sample(project, spec=spec, start_bar=9, end_bar=13, name="a")

            manifest = cut_sample(
                project, spec=spec, start_bar=9, end_bar=13, name="a", overwrite=True
            )
            written = json.loads(manifest.manifest_file.read_text(encoding="utf-8"))
            self.assertEqual(len(written["samples"]), 1)
            self.assertEqual(written["samples"][0]["bars"]["start"], 9)


class CommandTests(unittest.TestCase):
    def test_the_command_cuts_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = build_project(Path(temp))

            exit_code = main(
                ["cut-sample", str(project), "--bars", "5:9", "--name", "mid"]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue((project / "audio" / "samples" / "mid.wav").is_file())
            self.assertTrue((project / "sample_manifest.json").is_file())

    def test_a_malformed_window_is_reported_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = build_project(Path(temp))

            for bad in ("5", "5:", "a:9", "5-9"):
                exit_code = main(
                    ["cut-sample", str(project), "--bars", bad, "--name", "mid"]
                )
                self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
