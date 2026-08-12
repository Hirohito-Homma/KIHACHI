from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.decision import decision_audio_status, record_decision


def make_candidate(root: Path, name: str, *, clean: bool = True) -> Path:
    project = root / name
    audio = project / "audio" / "ace-step-01.wav"
    audio.parent.mkdir(parents=True)
    samples = array("h", [1000, -1000] * 800)
    with wave.open(str(audio), "wb") as sink:
        sink.setnchannels(2)
        sink.setsampwidth(2)
        sink.setframerate(8000)
        sink.writeframes(samples.tobytes())
    defects = [] if clean else [
        {
            "code": "discontinuity",
            "severity": "warning",
            "detail": "test click",
        }
    ]
    scan = {
        "defect_scan_version": "0.2",
        "measurements": {"duration_sec": 0.1},
        "findings": defects,
        "blocking": 0,
        "warnings": len(defects),
        "clean": clean,
    }
    (project / "audio_analysis.json").write_text(
        json.dumps({"audio_file": "audio/ace-step-01.wav"}), encoding="utf-8"
    )
    (project / "material_defects.json").write_text(
        json.dumps(scan), encoding="utf-8"
    )
    (project / "generation_review.json").write_text(
        json.dumps(
            {
                "alignment": {"score": 80.0 if clean else 81.0, "grade": "aligned"},
                "material_defects": scan,
            }
        ),
        encoding="utf-8",
    )
    return project


class DecisionTests(unittest.TestCase):
    def test_retaining_base_records_hashes_and_never_changes_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_candidate(root, "base", clean=False)
            alternative = make_candidate(root, "candidate")
            base_audio = base / "audio" / "ace-step-01.wav"
            alternative_audio = alternative / "audio" / "ace-step-01.wav"
            before = (base_audio.read_bytes(), alternative_audio.read_bytes())

            manifest = record_decision(
                base,
                selected_project=base,
                candidate_projects=[alternative],
                reason="Base維持。グルーヴが自然だった",
            )

            self.assertEqual(manifest.entry["action"], "retain_base")
            self.assertEqual(manifest.entry["selected"]["project"], ".")
            self.assertEqual(manifest.entry["reason"], "Base維持。グルーヴが自然だった")
            self.assertEqual(
                manifest.entry["selected"]["audio_sha256"],
                hashlib.sha256(base_audio.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                [item["material_status"] for item in manifest.entry["candidates"]],
                ["warning", "clean"],
            )
            self.assertEqual(
                manifest.entry["effects"],
                {
                    "audio_copied": False,
                    "audio_overwritten": False,
                    "audio_deleted": False,
                    "selection_record_only": True,
                },
            )
            self.assertEqual(before, (base_audio.read_bytes(), alternative_audio.read_bytes()))

    def test_a_changed_choice_appends_without_erasing_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_candidate(root, "base")
            alternative = make_candidate(root, "candidate")
            record_decision(
                base,
                selected_project=base,
                candidate_projects=[alternative],
                reason="first choice",
            )

            manifest = record_decision(
                base,
                selected_project=alternative,
                candidate_projects=[alternative],
                reason="changed after another listen",
            )

            self.assertEqual(len(manifest.decision["entries"]), 2)
            self.assertEqual(manifest.decision["entries"][0]["action"], "retain_base")
            self.assertEqual(manifest.entry["action"], "select_candidate")
            self.assertEqual(manifest.decision["current_decision"], 1)

    def test_a_decision_becomes_stale_if_the_selected_audio_bytes_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_candidate(root, "base")
            manifest = record_decision(base, selected_project=base, reason="retain")

            before = decision_audio_status(base, manifest.entry)
            (base / "audio" / "ace-step-01.wav").write_bytes(b"changed")
            after = decision_audio_status(base, manifest.entry)

            self.assertEqual(before["status"], "current")
            self.assertEqual(after["status"], "changed")
            self.assertEqual(before["expected_sha256"], after["expected_sha256"])
            self.assertNotEqual(after["actual_sha256"], after["expected_sha256"])

    def test_selected_project_must_be_one_of_the_compared_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_candidate(root, "base")
            unlisted = make_candidate(root, "unlisted")

            with self.assertRaises(ValueError):
                record_decision(
                    base,
                    selected_project=unlisted,
                    reason="not compared",
                )

            self.assertFalse((base / "decision_log.json").exists())

    def test_an_unrelated_decision_log_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_candidate(root, "base")
            destination = base / "decision_log.json"
            destination.write_text('{"mine": true}\n', encoding="utf-8")

            with self.assertRaises(FileExistsError):
                record_decision(base, selected_project=base, reason="retain")

            self.assertEqual(destination.read_text(encoding="utf-8"), '{"mine": true}\n')

    def test_cli_records_the_explicit_listening_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_candidate(root, "base")
            alternative = make_candidate(root, "candidate")

            status = main(
                [
                    "decide",
                    str(base),
                    "--also",
                    str(alternative),
                    "--selected",
                    str(base),
                    "--reason",
                    "Base維持",
                ]
            )

            self.assertEqual(status, 0)
            log = json.loads((base / "decision_log.json").read_text(encoding="utf-8"))
            self.assertEqual(log["entries"][0]["reason"], "Base維持")


if __name__ == "__main__":
    unittest.main()
