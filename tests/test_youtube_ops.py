from __future__ import annotations

import json
import tempfile
import unittest
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path

from kihachi_music_ai.cli import main
from kihachi_music_ai.youtube_ops import (
    authorize_package,
    build_release_package,
    current_shift,
    enqueue_brief,
    ensure_ops_workspace,
    load_checklist,
    roster,
    run_shift,
    update_checklist_item,
)


def _write_project(root: Path, name: str, *, with_audio: bool = True, blocking: int = 0) -> Path:
    project = root / name
    project.mkdir(parents=True)
    (project / "song_spec.json").write_text(
        json.dumps(
            {
                "meta": {"title": "Mutation Signal", "mood": "dark"},
                "song": {"bpm": 110, "key": "D#m"},
                "genres": [{"name": "Mutation Funk"}, {"name": "Dub"}],
                "arrangement": {
                    "sections": [
                        {"name": "minimal_intro", "bars": 8},
                        {"name": "psychedelic_drop", "bars": 8},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (project / "lyrics.txt").write_text("Echo the floor\n", encoding="utf-8")
    (project / "prompt.txt").write_text("Create a mutation funk hybrid.\n", encoding="utf-8")
    (project / "generation_review.json").write_text(
        json.dumps({"alignment": {"score": 82.0, "grade": "aligned"}}),
        encoding="utf-8",
    )
    (project / "material_defects.json").write_text(
        json.dumps({"blocking": blocking, "clean": blocking == 0, "findings": []}),
        encoding="utf-8",
    )
    if with_audio:
        audio = project / "audio" / "ace-step-01.wav"
        audio.parent.mkdir(parents=True)
        samples = array("h", [1000, -1000] * 400)
        with wave.open(str(audio), "wb") as sink:
            sink.setnchannels(2)
            sink.setsampwidth(2)
            sink.setframerate(8000)
            sink.writeframes(samples.tobytes())
    return project


class YouTubeOpsTests(unittest.TestCase):
    def test_roster_covers_a_full_utc_day(self) -> None:
        data = roster()
        self.assertEqual(data["shift_hours"], 4)
        self.assertEqual(len(data["shifts"]), 6)
        self.assertEqual(data["shifts"][0]["utc_start_hour"], 0)
        self.assertEqual(data["shifts"][-1]["utc_end_hour"], 24)
        self.assertTrue(data["boundary"]["human_authorize_required"])
        self.assertFalse(data["boundary"]["uploads"])

    def test_current_shift_picks_the_role_for_the_utc_block(self) -> None:
        shift = current_shift(datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc))
        self.assertEqual(shift["index"], 3)
        self.assertEqual(shift["role"]["id"], "gate")
        self.assertEqual(shift["label"], "12:00-16:00 UTC")

    def test_shift_log_and_enqueue_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ops = Path(temp) / "ops"
            ensure_ops_workspace(ops)
            queued = enqueue_brief(ops, brief="Mutation Funk 110 BPM", title="Night Wire")
            self.assertTrue(queued.is_file())
            manifest = run_shift(
                ops,
                role_id="strategy",
                note="seed the queue",
                now=datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(manifest.entry["role"]["id"], "strategy")
            self.assertEqual(manifest.entry["snapshot"]["queue_items"], 1)
            log = json.loads((ops / "ops_log.json").read_text(encoding="utf-8"))
            self.assertEqual(len(log["entries"]), 1)

    def test_package_blocks_authorize_until_audio_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ops = root / "ops"
            project = _write_project(root, "bare", with_audio=False)
            package = build_release_package(project, ops)
            self.assertFalse(package.package["ready_for_authorize"])
            self.assertTrue(any("audio" in item for item in package.package["blockers"]))
            with self.assertRaises(ValueError):
                authorize_package(package.package["slug"], ops, reason="sounds good")

    def test_ready_package_can_be_authorized_without_uploading(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ops = root / "ops"
            project = _write_project(root, "ready")
            package = build_release_package(project, ops)
            self.assertTrue(package.package["ready_for_authorize"])
            auth = authorize_package(
                package.package["slug"],
                ops,
                reason="listened; ready for premiere",
            )
            self.assertFalse(auth.record["upload_performed"])
            self.assertTrue((ops / "authorized" / package.package["slug"] / "authorize.json").is_file())

    def test_checklist_requires_evidence_for_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ops = Path(temp) / "ops"
            ensure_ops_workspace(ops)
            with self.assertRaises(ValueError):
                update_checklist_item("ypp_subscribers", ops, status="done", evidence="")
            data = update_checklist_item(
                "ypp_subscribers",
                ops,
                status="done",
                evidence="1,012 subscribers on 2026-09-04",
            )
            self.assertEqual(data["done_count"], 1)
            self.assertFalse(data["ready"])

    def test_cli_roster_and_shift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ops = Path(temp) / "ops"
            self.assertEqual(main(["youtube-ops", "roster"]), 0)
            self.assertEqual(
                main(["youtube-ops", "shift", "--ops-dir", str(ops), "--role", "analyst"]),
                0,
            )
            self.assertEqual(main(["youtube-ops", "status", "--ops-dir", str(ops)]), 0)
            self.assertEqual(main(["youtube-ops", "checklist", "--ops-dir", str(ops)]), 0)
            checklist = load_checklist(ops)
            self.assertEqual(checklist["total_count"], 7)

    def test_external_audio_is_recorded_by_path_not_copied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = _write_project(root, "source")
            master = root / "drive" / "shadow.wav"
            master.parent.mkdir()
            (source / "audio" / "ace-step-01.wav").rename(master)
            project = _write_project(root, "new-song", with_audio=False)
            package = build_release_package(project, root / "ops", audio=master)
            self.assertTrue(package.package["ready_for_authorize"])
            self.assertEqual(package.package["audio_relative"], str(master.resolve()))
            self.assertAlmostEqual(package.package["audio_seconds"], 0.05)
            self.assertFalse((project / "audio").exists())

    def test_external_audio_must_be_a_readable_wav(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _write_project(root, "p", with_audio=False)
            mp3 = root / "master.mp3"
            mp3.write_bytes(b"ID3")
            with self.assertRaises(ValueError):
                build_release_package(project, root / "ops", audio=mp3)
            broken = root / "master.wav"
            broken.write_text("not audio", encoding="utf-8")
            package = build_release_package(project, root / "ops", audio=broken)
            self.assertFalse(package.package["ready_for_authorize"])
            self.assertTrue(any("could not be read" in b for b in package.package["blockers"]))

    def test_overwrite_drops_store_copy_no_longer_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _write_project(root, "p")
            first = build_release_package(project, root / "ops", copy_for=["stock"])
            self.assertTrue((first.package_dir / "stock_listing.md").is_file())
            second = build_release_package(project, root / "ops", overwrite=True)
            self.assertFalse((second.package_dir / "stock_listing.md").exists())
            self.assertEqual(second.package["store_copy"], {})

    def test_missing_external_audio_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _write_project(root, "p", with_audio=False)
            with self.assertRaises(FileNotFoundError):
                build_release_package(project, root / "ops", audio=root / "nope.wav")

    def test_ollama_copy_goes_into_the_description(self) -> None:
        sent: list[tuple[str, str]] = []

        def fake(prompt: str, *, model: str) -> str:
            sent.append((prompt, model))
            return "夜の街を走る低音。\n\nLow end for night drives."

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _write_project(root, "p")
            package = build_release_package(
                project, root / "ops", describe_with="gemma4", generate=fake
            )
            text = (package.package_dir / "youtube_description.md").read_text(encoding="utf-8")
            self.assertIn("Low end for night drives.", text)
            self.assertIn("Tempo: 110 BPM", text)
            self.assertEqual(package.package["description_source"]["source"], "ollama")
            self.assertEqual(sent[0][1], "gemma4")
            self.assertIn("Mutation Funk", sent[0][0])
            self.assertIn("Do not invent facts", sent[0][0])
            self.assertIn("- mood: dark", sent[0][0])

    def test_store_copy_for_streaming_and_stock(self) -> None:
        prompts: list[str] = []

        def fake(prompt: str, *, model: str) -> str:
            prompts.append(prompt)
            return f"draft {len(prompts)}"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _write_project(root, "p")
            package = build_release_package(
                project,
                root / "ops",
                describe_with="gemma4",
                copy_for=["streaming", "stock"],
                generate=fake,
            )
            self.assertEqual(len(prompts), 3)
            self.assertIn("playlist editors", prompts[1])
            self.assertIn("BGM stock site", prompts[2])
            store = package.package["store_copy"]
            self.assertEqual(store["streaming"]["file"], "streaming_pitch.md")
            stock = (package.package_dir / "stock_listing.md").read_text(encoding="utf-8")
            self.assertIn("- Tempo: 110 BPM", stock)
            self.assertIn("draft 3", stock)

    def test_store_copy_without_model_keeps_facts_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _write_project(root, "p")
            package = build_release_package(project, root / "ops", copy_for=["stock"])
            stock = (package.package_dir / "stock_listing.md").read_text(encoding="utf-8")
            self.assertIn("- Genre: Mutation Funk, Dub", stock)
            self.assertEqual(package.package["store_copy"]["stock"]["source"], "template")
            with self.assertRaises(ValueError):
                build_release_package(project, root / "ops", copy_for=["tiktok"], overwrite=True)

    def test_ollama_down_falls_back_to_template(self) -> None:
        from kihachi_music_ai.adapters.ollama_text import OllamaUnavailable

        def down(prompt: str, *, model: str) -> str:
            raise OllamaUnavailable("connection refused")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = _write_project(root, "p")
            package = build_release_package(
                project, root / "ops", describe_with="gemma4", generate=down
            )
            source = package.package["description_source"]
            self.assertEqual(source["source"], "template")
            self.assertIn("connection refused", source["fallback_reason"])
            text = (package.package_dir / "youtube_description.md").read_text(encoding="utf-8")
            self.assertIn("Original track prepared with KIHACHI Music AI.", text)


if __name__ == "__main__":
    unittest.main()
