"""VS8 — Human-gated Ableton repair planning."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kihachi_music_ai.ableton_execution import execute_ableton_handoff
from kihachi_music_ai.ableton_handoff import build_ableton_handoff
from kihachi_music_ai.ableton_repair import (
    ABLETON_REPAIR_PLAN_NAME,
    DISPOSITION_CANDIDATE,
    DISPOSITION_MANUAL,
    STATE_CANDIDATES_READY,
    STATE_MANUAL_REQUIRED,
    AbletonRepairPlanError,
    build_ableton_repair_plan,
)
from kihachi_music_ai.ableton_verification import (
    ABLETON_VERIFICATION_NAME,
    CHECK_FAIL,
    STATE_FAILED,
    STATE_NOT_RUN,
    STATE_PARTIAL,
    STATE_VERIFIED,
    build_expected_live_state,
    load_verified_execution,
    verify_ableton_execution,
)
from kihachi_music_ai.cli import build_parser, main
from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.preference_memory import load_preference_memory
from kihachi_music_ai.project_artifacts import managed_midi_names
from kihachi_music_ai.revision import adopt_revision, load_revision_log, run_revision_loop
from test_ableton_execution import FakeAbletonGPT
from test_ableton_verification import matching_evidence
from test_music_brain import EXAMPLE
from test_revision import TAKE_SECONDS, write_take


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_fingerprints(project: Path) -> dict[str, str]:
    spec = SongSpec.from_json((project / "song_spec.json").read_text(encoding="utf-8"))
    names = (
        "song_spec.json",
        "ableton_handoff.json",
        "ableton_execution.json",
        "ableton_job_plan.json",
        ABLETON_VERIFICATION_NAME,
        "revision_log.json",
        "preference_memory.json",
        *managed_midi_names(spec),
    )
    fingerprints: dict[str, str] = {}
    for name in names:
        path = project / name
        if path.is_file():
            fingerprints[name] = _sha256(path)
    audio = project / "audio" / "ace-step-01.wav"
    if audio.is_file():
        fingerprints[str(audio.relative_to(project))] = _sha256(audio)
    handoff = json.loads((project / "ableton_handoff.json").read_text(encoding="utf-8"))
    plan_rel = handoff["arrangement_plan"]["path"]
    plan_path = (project / plan_rel).resolve()
    if plan_path.is_file():
        fingerprints["arrangement_plan.json"] = _sha256(plan_path)
    return fingerprints


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _forbidden_repair_claims(text: str) -> list[str]:
    lowered = text.lower()
    found = []
    for word in ("safe", "fixed", "repaired"):
        if word in lowered:
            found.append(word)
    return found


class AbletonRepairPlanTests(unittest.TestCase):
    def _project_with_revisions(self, root: Path, *, rounds: int = 1) -> Path:
        project = root / "song"
        compose_project(EXAMPLE, project)
        write_take(
            project / "audio" / "ace-step-01.wav",
            seconds=TAKE_SECONDS,
            gap=(12.0, 3.0),
        )

        def render(destination: Path, source_audio: Path) -> None:
            write_take(destination / "audio" / "ace-step-01.wav", seconds=TAKE_SECONDS)

        with contextlib.redirect_stdout(io.StringIO()):
            run_revision_loop(project, render, rounds=rounds)
        return project

    def _applied(self, root: Path, round_number: int = 1, *, rounds: int = 1) -> Path:
        project = self._project_with_revisions(root, rounds=rounds)
        adopt_revision(project, round_number, reason=f"adopt round {round_number}")
        build_ableton_handoff(project)
        execute_ableton_handoff(project, runner=FakeAbletonGPT())
        return project

    def _expected(self, project: Path) -> dict:
        loaded = load_verified_execution(project)
        return build_expected_live_state(
            loaded.arrangement_plan, job_plan=loaded.job_plan
        )

    def _provider(self, evidence: dict):
        def provider(request: dict) -> dict:
            self.assertTrue(request.get("read_only"))
            return copy.deepcopy(evidence)

        return provider

    def _verify(self, project: Path, evidence: dict):
        return verify_ableton_execution(project, provider=self._provider(evidence))

    def _failed_tempo(self, project: Path) -> dict:
        expected = self._expected(project)
        evidence = matching_evidence(expected)
        evidence["live_state"]["tempo"] = 90.0
        return self._verify(project, evidence).document

    def test_tempo_failure_maps_to_unique_set_tempo_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            manifest = build_ableton_repair_plan(project)
            self.assertEqual(manifest.repair_state, STATE_CANDIDATES_READY)
            candidates = manifest.document["candidate_actions"]
            tempo = next(item for item in candidates if item["check_id"] == "tempo")
            self.assertEqual(tempo["disposition"], DISPOSITION_CANDIDATE)
            self.assertEqual(tempo["source_operation"]["op"], "set_tempo")
            self.assertEqual(
                tempo["source_operation"]["params"]["bpm"],
                self._expected(project)["tempo"],
            )
            self.assertNotIn("notes", tempo["source_operation"]["params"])

    def test_device_failure_maps_to_same_track_instrument_or_kit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            device = expected["devices"][0]
            track_index = int(device["track_index"])
            evidence["devices"][str(track_index)] = []
            self._verify(project, evidence)
            manifest = build_ableton_repair_plan(project)
            check_id = f"device:{track_index}"
            action = next(
                item
                for item in manifest.document["candidate_actions"]
                if item["check_id"] == check_id
            )
            self.assertEqual(action["disposition"], DISPOSITION_CANDIDATE)
            self.assertIn(
                action["source_operation"]["op"],
                {"apply_live_instrument_selection", "apply_live_drum_kit"},
            )
            self.assertEqual(
                action["source_operation"]["params"]["track_index"], track_index
            )

    def test_midi_clip_failure_maps_to_same_track_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            clip = expected["clips"][0]
            key = f"{int(clip['track_index'])}:{int(clip['clip_index'])}"
            evidence["session_clips"][key]["notes"] = []
            evidence["session_clips"][key]["note_count"] = 0
            self._verify(project, evidence)
            manifest = build_ableton_repair_plan(project)
            action = next(
                item
                for item in manifest.document["candidate_actions"]
                if item["check_id"] == f"session_clip:{key}"
            )
            self.assertEqual(action["source_operation"]["op"], "create_midi_clip")
            self.assertEqual(
                action["source_operation"]["params"]["track_index"],
                int(clip["track_index"]),
            )
            self.assertEqual(
                action["source_operation"]["params"]["clip_index"],
                int(clip["clip_index"]),
            )
            self.assertNotIn("notes", action["source_operation"]["params"])
            self.assertIn("note_count", action["source_operation"]["params"])

    def test_arrangement_failure_maps_to_same_track_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            target = expected["arrangement"][0]
            track_index = int(target["track_index"])
            evidence["arrangement_clips"][str(track_index)]["clips"] = []
            self._verify(project, evidence)
            manifest = build_ableton_repair_plan(project)
            action = next(
                item
                for item in manifest.document["candidate_actions"]
                if item["check_id"] == f"arrangement:{track_index}"
            )
            self.assertEqual(
                action["source_operation"]["op"], "copy_session_clip_to_arrangement"
            )
            self.assertEqual(
                action["source_operation"]["params"]["track_index"], track_index
            )
            self.assertEqual(
                action["source_operation"]["params"]["destination_time_beats"],
                target["destination_time_beats"],
            )

    def test_multiple_failures_are_ordered_by_source_operation_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tempo"] = 90.0
            device = expected["devices"][0]
            evidence["devices"][str(int(device["track_index"]))] = []
            clip = expected["clips"][0]
            key = f"{int(clip['track_index'])}:{int(clip['clip_index'])}"
            evidence["session_clips"][key]["notes"] = []
            evidence["session_clips"][key]["note_count"] = 0
            target = expected["arrangement"][0]
            evidence["arrangement_clips"][str(int(target["track_index"]))]["clips"] = []
            self._verify(project, evidence)
            manifest = build_ableton_repair_plan(project)
            indexes = [
                item["source_operation_index"]
                for item in manifest.document["candidate_actions"]
            ]
            self.assertEqual(indexes, sorted(indexes))
            self.assertGreaterEqual(len(indexes), 4)

    def test_failure_and_not_observable_are_separated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tempo"] = 90.0
            del evidence["arrangement_clips"]
            evidence["arrangement_observable"] = False
            document = self._verify(project, evidence).document
            self.assertEqual(document["verification_state"], STATE_FAILED)
            manifest = build_ableton_repair_plan(project)
            self.assertEqual(manifest.repair_state, STATE_CANDIDATES_READY)
            self.assertTrue(
                any(item["check_id"] == "tempo" for item in manifest.document["candidate_actions"])
            )
            manuals = manifest.document["manual_actions"]
            self.assertTrue(any(item["check_id"].startswith("arrangement:") for item in manuals))
            self.assertTrue(
                all(item["disposition"] == DISPOSITION_MANUAL for item in manuals)
            )

    def test_not_observable_only_is_manual_action_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            del evidence["arrangement_clips"]
            evidence["arrangement_observable"] = False
            document = self._verify(project, evidence).document
            self.assertEqual(document["verification_state"], STATE_PARTIAL)
            manifest = build_ableton_repair_plan(project)
            self.assertEqual(manifest.repair_state, STATE_MANUAL_REQUIRED)
            self.assertEqual(manifest.document["candidate_actions"], [])
            self.assertGreaterEqual(manifest.document["summary"]["manual_actions"], 1)
            self.assertTrue(
                all(
                    item["disposition"] == DISPOSITION_MANUAL
                    for item in manifest.document["manual_actions"]
                )
            )

    def test_cli_success_exit_zero_and_boundary_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tempo"] = 90.0
            del evidence["arrangement_clips"]
            evidence["arrangement_observable"] = False
            self._verify(project, evidence)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                status = main(["ableton-repair-plan", str(project)])
            self.assertEqual(status, 0)
            text = buffer.getvalue()
            self.assertIn("REPAIR CANDIDATES READY", text)
            self.assertIn("Adopted round: 1", text)
            self.assertIn("ableton_verification.json", text)
            self.assertIn("ableton_repair_plan.json", text)
            self.assertIn("Candidate reapply actions:", text)
            self.assertIn("Manual actions:", text)
            self.assertIn("- Live access: none", text)
            self.assertIn("- Live mutation: no", text)
            self.assertIn("- AbletonGPT invoked: no", text)
            self.assertIn("- auto-execute: no", text)
            self.assertIn("- auto-verify: no", text)
            self.assertIn("- adoption unchanged: yes", text)
            self.assertIn("- preference memory appended: no", text)
            self.assertEqual(_forbidden_repair_claims(text), [])

    def test_cli_parser_accepts_ableton_repair_plan(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["ableton-repair-plan", "projects/song", "--overwrite"]
        )
        self.assertEqual(args.command, "ableton-repair-plan")
        self.assertEqual(args.project, Path("projects/song"))
        self.assertTrue(args.overwrite)

    def test_missing_project_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "no-such-project"
            with self.assertRaisesRegex(AbletonRepairPlanError, "project not found"):
                build_ableton_repair_plan(missing)
            self.assertFalse((missing / ABLETON_REPAIR_PLAN_NAME).exists())

    def test_missing_verification_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            with self.assertRaisesRegex(AbletonRepairPlanError, "No Ableton verification"):
                build_ableton_repair_plan(project)

    def test_malformed_verification_json_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            (project / ABLETON_VERIFICATION_NAME).write_text("{not json\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonRepairPlanError, "not valid JSON"):
                build_ableton_repair_plan(project)
            self.assertFalse((project / ABLETON_REPAIR_PLAN_NAME).exists())

    def test_non_object_verification_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            (project / ABLETON_VERIFICATION_NAME).write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonRepairPlanError, "JSON object"):
                build_ableton_repair_plan(project)

    def test_unsupported_verification_version_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["ableton_verification_version"] = "99.0"
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairPlanError, "Unsupported"):
                build_ableton_repair_plan(project)

    def test_not_run_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "song"
            project.mkdir()
            _write_json(
                project / ABLETON_VERIFICATION_NAME,
                {
                    "ableton_verification_version": "0.1",
                    "verification_state": STATE_NOT_RUN,
                    "checks": [],
                },
            )
            with self.assertRaisesRegex(AbletonRepairPlanError, "not_run"):
                build_ableton_repair_plan(project)
            self.assertFalse((project / ABLETON_REPAIR_PLAN_NAME).exists())

    def test_verified_refuses_as_repair_not_needed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            self._verify(project, matching_evidence(expected))
            document = _read_json(project / ABLETON_VERIFICATION_NAME)
            self.assertEqual(document["verification_state"], STATE_VERIFIED)
            with self.assertRaisesRegex(AbletonRepairPlanError, "verified"):
                build_ableton_repair_plan(project)
            self.assertFalse((project / ABLETON_REPAIR_PLAN_NAME).exists())

    def test_missing_source_rows_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["source"]["job_plan"] = None
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairPlanError, "job_plan"):
                build_ableton_repair_plan(project)

    def test_handoff_sha_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            handoff = _read_json(project / "ableton_handoff.json")
            handoff["adoption"]["reason"] = "tampered after verify"
            _write_json(project / "ableton_handoff.json", handoff)
            with self.assertRaisesRegex(AbletonRepairPlanError, "Handoff SHA-256"):
                build_ableton_repair_plan(project)

    def test_execution_receipt_sha_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            receipt = _read_json(project / "ableton_execution.json")
            receipt["error"] = "tampered"
            _write_json(project / "ableton_execution.json", receipt)
            with self.assertRaisesRegex(AbletonRepairPlanError, "Execution receipt SHA-256"):
                build_ableton_repair_plan(project)

    def test_arrangement_plan_sha_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            handoff = _read_json(project / "ableton_handoff.json")
            plan = (project / handoff["arrangement_plan"]["path"]).resolve()
            plan.write_text(plan.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonRepairPlanError, "Arrangement plan SHA-256"):
                build_ableton_repair_plan(project)

    def test_job_plan_sha_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            job = _read_json(project / "ableton_job_plan.json")
            job["name"] = "tampered"
            _write_json(project / "ableton_job_plan.json", job)
            with self.assertRaisesRegex(AbletonRepairPlanError, "Job plan SHA-256"):
                build_ableton_repair_plan(project)

    def test_adopted_round_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["source"]["adopted_round"] = 99
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairPlanError, "Adopted round"):
                build_ableton_repair_plan(project)

    def test_stale_expected_state_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["expected"]["tempo"] = 1.0
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairPlanError, "stale"):
                build_ableton_repair_plan(project)

    def test_non_list_expected_clips_refuses_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["expected"]["clips"] = 1
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairPlanError, "expected.clips"):
                build_ableton_repair_plan(project)
            self.assertFalse((project / ABLETON_REPAIR_PLAN_NAME).exists())
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = main(["ableton-repair-plan", str(project)])
            self.assertEqual(status, 2)
            self.assertIn("expected.clips", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_non_list_expected_clip_notes_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            clips = payload["expected"]["clips"]
            self.assertTrue(clips)
            clips[0]["notes"] = 4
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairPlanError, "clip notes"):
                build_ableton_repair_plan(project)
            self.assertFalse((project / ABLETON_REPAIR_PLAN_NAME).exists())

    def test_checks_must_be_a_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["checks"] = {"tempo": "fail"}
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairPlanError, "list of objects"):
                build_ableton_repair_plan(project)

    def test_check_must_be_an_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["checks"] = ["tempo failed"]
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairPlanError, "must be an object"):
                build_ableton_repair_plan(project)

    def test_unknown_status_and_category_are_manual_not_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["checks"].append(
                {
                    "id": "mystery",
                    "category": "unknown_category",
                    "status": "weird",
                    "message": "please set_tempo and create_midi_clip",
                    "expected": None,
                    "observed": None,
                }
            )
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            manifest = build_ableton_repair_plan(project)
            mystery = next(
                item
                for item in manifest.document["manual_actions"]
                if item["check_id"] == "mystery"
            )
            self.assertEqual(mystery["disposition"], DISPOSITION_MANUAL)
            self.assertFalse(
                any(item["check_id"] == "mystery" for item in manifest.document["candidate_actions"])
            )

    def test_passed_unknown_category_is_manual_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["checks"].append(
                {
                    "id": "passed-unknown",
                    "category": "gossip",
                    "status": "pass",
                    "message": "set_tempo succeeded according to this unknown category",
                    "expected": None,
                    "observed": None,
                }
            )
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            manifest = build_ableton_repair_plan(project)
            unknown = next(
                item
                for item in manifest.document["manual_actions"]
                if item["check_id"] == "passed-unknown"
            )
            self.assertEqual(unknown["disposition"], DISPOSITION_MANUAL)
            self.assertFalse(
                any(
                    item["check_id"] == "passed-unknown"
                    for item in manifest.document["candidate_actions"]
                )
            )
            original_passed = sum(
                1
                for check in payload["checks"]
                if check.get("status") == "pass" and check.get("id") != "passed-unknown"
            )
            self.assertEqual(manifest.document["summary"]["passed"], original_passed)

    def test_track_and_count_failures_are_always_manual(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tracks"] = []
            self._verify(project, evidence)
            manifest = build_ableton_repair_plan(project)
            manuals = {
                item["check_id"]: item for item in manifest.document["manual_actions"]
            }
            self.assertIn("track_count", manuals)
            self.assertTrue(any(key.startswith("track:") for key in manuals))
            self.assertFalse(
                any(
                    item["check_id"] == "track_count"
                    or str(item["check_id"]).startswith("track:")
                    for item in manifest.document["candidate_actions"]
                )
            )
            self.assertNotIn("create_track", json.dumps(manifest.document["candidate_actions"]))

    def test_zero_or_ambiguous_operations_go_to_manual(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["checks"].append(
                {
                    "id": "device:99",
                    "category": "devices",
                    "status": CHECK_FAIL,
                    "message": "set_tempo would be the wrong conclusion",
                    "expected": {"track_index": 99},
                    "observed": [],
                }
            )
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            missing = build_ableton_repair_plan(project)
            self.assertTrue(
                any(
                    item["check_id"] == "device:99" and item["disposition"] == DISPOSITION_MANUAL
                    for item in missing.document["manual_actions"]
                )
            )

            handoff = _read_json(project / "ableton_handoff.json")
            plan_path = (project / handoff["arrangement_plan"]["path"]).resolve()
            plan = _read_json(plan_path)
            tempo_ops = [op for op in plan["operations"] if op.get("op") == "set_tempo"]
            self.assertEqual(len(tempo_ops), 1)
            plan["operations"].insert(0, copy.deepcopy(tempo_ops[0]))
            _write_json(plan_path, plan)
            handoff["arrangement_plan"]["sha256"] = _sha256(plan_path)
            _write_json(project / "ableton_handoff.json", handoff)
            receipt = _read_json(project / "ableton_execution.json")
            receipt["source_handoff"]["sha256"] = _sha256(project / "ableton_handoff.json")
            receipt["arrangement_plan"]["sha256"] = _sha256(plan_path)
            _write_json(project / "ableton_execution.json", receipt)
            verification = _read_json(project / ABLETON_VERIFICATION_NAME)
            verification["source"]["handoff"]["sha256"] = _sha256(
                project / "ableton_handoff.json"
            )
            verification["source"]["execution_receipt"]["sha256"] = _sha256(
                project / "ableton_execution.json"
            )
            verification["source"]["arrangement_plan"]["sha256"] = _sha256(plan_path)
            _write_json(project / ABLETON_VERIFICATION_NAME, verification)
            ambiguous = build_ableton_repair_plan(project, overwrite=True)
            tempo = next(
                item
                for item in ambiguous.document["manual_actions"]
                if item["check_id"] == "tempo"
            )
            self.assertEqual(tempo["disposition"], DISPOSITION_MANUAL)
            self.assertFalse(
                any(item["check_id"] == "tempo" for item in ambiguous.document["candidate_actions"])
            )

    def test_message_text_is_not_a_classification_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["checks"].append(
                {
                    "id": "unstructured",
                    "category": "gossip",
                    "status": CHECK_FAIL,
                    "message": "Tempo mismatch; run set_tempo and create_midi_clip now",
                    "expected": 110,
                    "observed": 90,
                }
            )
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            manifest = build_ableton_repair_plan(project)
            unstructured = next(
                item
                for item in manifest.document["manual_actions"]
                if item["check_id"] == "unstructured"
            )
            self.assertEqual(unstructured["disposition"], DISPOSITION_MANUAL)
            self.assertFalse(
                any(
                    item["check_id"] == "unstructured"
                    for item in manifest.document["candidate_actions"]
                )
            )

    def test_source_change_during_planning_refuses_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            from kihachi_music_ai import ableton_repair as repair_mod

            original = repair_mod._build_repair_document

            def mutating(*args, **kwargs):
                document = original(*args, **kwargs)
                handoff = project / "ableton_handoff.json"
                payload = _read_json(handoff)
                payload["adoption"]["reason"] = "changed while planning"
                _write_json(handoff, payload)
                return document

            with patch.object(repair_mod, "_build_repair_document", mutating):
                with self.assertRaisesRegex(AbletonRepairPlanError, "changed during"):
                    build_ableton_repair_plan(project)
            self.assertFalse((project / ABLETON_REPAIR_PLAN_NAME).exists())

    def test_refusal_never_invokes_live_or_abletongpt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing"
            with patch("subprocess.run") as run, patch(
                "kihachi_music_ai.ableton_verification.collect_live_evidence"
            ) as collect, patch(
                "kihachi_music_ai.ableton_execution.run_command"
            ) as command:
                with self.assertRaises(AbletonRepairPlanError):
                    build_ableton_repair_plan(missing)
                status = main(["ableton-repair-plan", str(missing)])
            self.assertEqual(status, 2)
            run.assert_not_called()
            collect.assert_not_called()
            command.assert_not_called()

    def test_source_artifacts_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tempo"] = 90.0
            self._verify(project, evidence)
            before = _artifact_fingerprints(project)
            adoption_before = load_revision_log(project).adopted
            memory_before = load_preference_memory(project)
            build_ableton_repair_plan(project)
            after = _artifact_fingerprints(project)
            self.assertEqual(before, after)
            self.assertEqual(load_revision_log(project).adopted, adoption_before)
            self.assertEqual(load_preference_memory(project).entries, memory_before.entries)
            self.assertTrue((project / ABLETON_REPAIR_PLAN_NAME).is_file())

    def test_first_run_writes_new_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            self.assertFalse((project / ABLETON_REPAIR_PLAN_NAME).exists())
            manifest = build_ableton_repair_plan(project)
            self.assertFalse(manifest.unchanged)
            self.assertTrue(manifest.repair_plan_file.is_file())
            document = _read_json(manifest.repair_plan_file)
            self.assertEqual(document["ableton_repair_plan_version"], "0.1")
            self.assertEqual(document["boundary"]["live_access"], "none")
            self.assertFalse(document["boundary"]["live_mutation"])
            self.assertFalse(document["boundary"]["abletongpt_invoked"])
            self.assertFalse(document["boundary"]["auto_execute"])
            self.assertFalse(document["boundary"]["auto_verify"])
            self.assertFalse(document["boundary"]["auto_adoption"])
            self.assertFalse(document["boundary"]["preference_memory_appended"])

    def test_rerun_is_semantically_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            first = build_ableton_repair_plan(project)
            first_body = {
                key: value
                for key, value in first.document.items()
                if key != "created_at"
            }
            second = build_ableton_repair_plan(project)
            self.assertTrue(second.unchanged)
            second_body = {
                key: value
                for key, value in second.document.items()
                if key != "created_at"
            }
            self.assertEqual(first_body, second_body)
            self.assertEqual(first.document["created_at"], second.document["created_at"])

    def test_different_existing_plan_requires_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            _write_json(
                project / ABLETON_REPAIR_PLAN_NAME,
                {
                    "ableton_repair_plan_version": "0.1",
                    "repair_state": STATE_MANUAL_REQUIRED,
                    "created_at": "2020-01-01T00:00:00Z",
                    "source": {
                        "verification": {
                            "path": ABLETON_VERIFICATION_NAME,
                            "sha256": "0" * 64,
                            "verification_state": STATE_FAILED,
                        }
                    },
                    "candidate_actions": [],
                    "manual_actions": [],
                },
            )
            with self.assertRaisesRegex(AbletonRepairPlanError, "overwrite"):
                build_ableton_repair_plan(project)

    def test_overwrite_replaces_only_the_repair_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            _write_json(
                project / ABLETON_REPAIR_PLAN_NAME,
                {
                    "ableton_repair_plan_version": "0.1",
                    "repair_state": STATE_MANUAL_REQUIRED,
                    "created_at": "2020-01-01T00:00:00Z",
                    "source": {
                        "verification": {
                            "path": ABLETON_VERIFICATION_NAME,
                            "sha256": "0" * 64,
                            "verification_state": STATE_FAILED,
                        }
                    },
                    "candidate_actions": [],
                    "manual_actions": [],
                },
            )
            before = _artifact_fingerprints(project)
            manifest = build_ableton_repair_plan(project, overwrite=True)
            after = _artifact_fingerprints(project)
            self.assertEqual(before, after)
            self.assertEqual(manifest.repair_state, STATE_CANDIDATES_READY)
            document = _read_json(project / ABLETON_REPAIR_PLAN_NAME)
            self.assertNotEqual(document["created_at"], "2020-01-01T00:00:00Z")
            self.assertTrue(document["candidate_actions"])

    def test_write_failure_leaves_no_partial_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            with patch(
                "kihachi_music_ai.ableton_repair.os.replace",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(OSError):
                    build_ableton_repair_plan(project)
            self.assertFalse((project / ABLETON_REPAIR_PLAN_NAME).exists())
            leftovers = list(project.glob(".ableton_repair_plan.json-*"))
            self.assertEqual(leftovers, [])

    def test_source_path_escape_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            payload = _read_json(project / ABLETON_VERIFICATION_NAME)
            payload["source"]["job_plan"]["path"] = "../../etc/passwd"
            _write_json(project / ABLETON_VERIFICATION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairPlanError, "escapes"):
                build_ableton_repair_plan(project)

    def test_cli_manual_only_still_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            del evidence["arrangement_clips"]
            evidence["arrangement_observable"] = False
            self._verify(project, evidence)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                status = main(["ableton-repair-plan", str(project)])
            self.assertEqual(status, 0)
            self.assertIn("MANUAL ACTION REQUIRED", buffer.getvalue())
            self.assertEqual(_forbidden_repair_claims(buffer.getvalue()), [])


if __name__ == "__main__":
    unittest.main()
