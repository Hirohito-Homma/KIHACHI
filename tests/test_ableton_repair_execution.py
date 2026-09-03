"""VS9 — Human-authorized Ableton repair execution (tempo only)."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import inspect
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kihachi_music_ai.ableton_execution import (
    ABLETON_JOB_PLAN_NAME,
    CommandResult,
    execute_ableton_handoff,
    run_command,
)
from kihachi_music_ai.ableton_handoff import build_ableton_handoff
from kihachi_music_ai.ableton_repair import (
    ABLETON_REPAIR_PLAN_NAME,
    STATE_CANDIDATES_READY,
    build_ableton_repair_plan,
    load_validated_repair_plan,
    source_operation_view,
)
from kihachi_music_ai.ableton_repair_execution import (
    ABLETON_REPAIR_EXECUTION_NAME,
    ABLETON_REPAIR_JOB_PLAN_NAME,
    AbletonRepairExecutionError,
    execute_ableton_repair,
    load_validated_repair_selection,
)
from kihachi_music_ai.ableton_verification import (
    ABLETON_VERIFICATION_NAME,
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


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _artifact_fingerprints(project: Path) -> dict[str, str]:
    spec = SongSpec.from_json((project / "song_spec.json").read_text(encoding="utf-8"))
    names = (
        "song_spec.json",
        "ableton_handoff.json",
        "ableton_execution.json",
        ABLETON_JOB_PLAN_NAME,
        ABLETON_VERIFICATION_NAME,
        ABLETON_REPAIR_PLAN_NAME,
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
    plan_path = (project / handoff["arrangement_plan"]["path"]).resolve()
    if plan_path.is_file():
        fingerprints["arrangement_plan.json"] = _sha256(plan_path)
    return fingerprints


class RepairFakeAbletonGPT:
    """Injected AbletonGPT runner for VS9: import-kihachi + optional run."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.arrangement_requests: list[dict] = []
        self.import_returncode = 0
        self.run_returncode = 0
        self.import_stdout = "imported KIHACHI plan with 1 step(s)\n"
        self.import_stderr = ""
        self.run_stdout = "completed=1 failed=0 pending=0\n"
        self.run_stderr = ""
        self.write_job_plan = True
        self.fail_if_run = False
        self.job_plan_override: dict | None = None

    def __call__(self, argv: list[str] | tuple[str, ...]) -> CommandResult:
        argv_list = [str(part) for part in argv]
        self.calls.append(argv_list)
        if "import-kihachi" in argv_list:
            plan_path = Path(argv_list[argv_list.index("--arrangement-plan") + 1])
            self.arrangement_requests.append(
                json.loads(plan_path.read_text(encoding="utf-8"))
            )
            out = Path(argv_list[argv_list.index("--out") + 1])
            if self.write_job_plan:
                arrangement = self.arrangement_requests[-1]
                operation = arrangement["operations"][0]
                job = self.job_plan_override or {
                    "schema_version": 1,
                    "name": arrangement["song"]["title"],
                    "steps": [
                        {
                            "step_id": "0000_set_tempo",
                            "command": operation["op"],
                            "params": dict(operation.get("params") or {}),
                            "status": "pending",
                        }
                    ],
                }
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
            return CommandResult(
                tuple(argv_list),
                self.import_returncode,
                self.import_stdout,
                self.import_stderr,
            )
        if "run" in argv_list:
            if self.fail_if_run:
                raise AssertionError("AbletonGPT run must not be invoked")
            plan_path = Path(argv_list[argv_list.index("--plan") + 1])
            if plan_path.is_file() and self.run_returncode == 0:
                payload = json.loads(plan_path.read_text(encoding="utf-8"))
                for step in payload.get("steps") or []:
                    if isinstance(step, dict):
                        step["status"] = "succeeded"
                plan_path.write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                )
            return CommandResult(
                tuple(argv_list),
                self.run_returncode,
                self.run_stdout,
                self.run_stderr,
            )
        raise AssertionError(f"unexpected AbletonGPT argv: {argv_list}")

    def commands(self) -> list[str]:
        names: list[str] = []
        for argv in self.calls:
            if "import-kihachi" in argv:
                names.append("import-kihachi")
            elif "run" in argv:
                names.append("run")
        return names


class AbletonRepairExecutionTests(unittest.TestCase):
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

    def _tempo_ready(self, root: Path) -> Path:
        project = self._applied(root)
        self._failed_tempo(project)
        build_ableton_repair_plan(project)
        return project

    def _preflight_evidence(self, project: Path) -> dict:
        expected = self._expected(project)
        evidence = matching_evidence(expected)
        verification = _read_json(project / ABLETON_VERIFICATION_NAME)
        tempo = next(item for item in verification["checks"] if item["id"] == "tempo")
        evidence["live_state"]["tempo"] = tempo["observed"]
        return evidence

    def _plan_sha(self, project: Path) -> str:
        return _sha256(project / ABLETON_REPAIR_PLAN_NAME)

    def test_valid_tempo_candidate_resolves_to_full_arrangement_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            selected = load_validated_repair_selection(project, check_id="tempo")
            plan = json.loads(selected.arrangement_plan_file.read_text(encoding="utf-8"))
            full = plan["operations"][selected.source_operation_index]
            self.assertEqual(full["op"], "set_tempo")
            self.assertEqual(selected.source_operation, full)
            self.assertEqual(selected.source_operation["params"]["bpm"], 110)
            self.assertIn("why", selected.source_operation)

    def test_executes_full_operation_not_repair_plan_view(self) -> None:
        fake = RepairFakeAbletonGPT()
        fake.fail_if_run = True
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            plan = load_validated_repair_plan(project)
            candidate = next(
                item
                for item in plan.repair_plan["candidate_actions"]
                if item["check_id"] == "tempo"
            )
            execute_ableton_repair(
                project, check_id="tempo", prepare_only=True, runner=fake
            )
            requested = fake.arrangement_requests[0]["operations"][0]
            full = plan.arrangement_plan["operations"][candidate["source_operation_index"]]
            self.assertEqual(requested, full)
            self.assertEqual(source_operation_view(full), candidate["source_operation"])
            self.assertNotEqual(requested, candidate["source_operation"])
            self.assertIn("why", requested)
            self.assertNotIn("why", candidate["source_operation"])

    def test_missing_project_refuses(self) -> None:
        fake = RepairFakeAbletonGPT()
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        missing = Path("/tmp/kihachi-vs9-missing-project-does-not-exist")
        with self.assertRaisesRegex(AbletonRepairExecutionError, "project not found"):
            execute_ableton_repair(
                missing,
                check_id="tempo",
                runner=fake,
                preflight_provider=provider,
            )
        self.assertEqual(fake.calls, [])
        self.assertEqual(calls, [])

    def test_missing_repair_plan_refuses(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            self._failed_tempo(project)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "No Ableton repair plan"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)
            self.assertEqual(fake.calls, [])

    def test_malformed_and_non_object_repair_plan_refuse(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            (project / ABLETON_REPAIR_PLAN_NAME).write_text("{not json\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonRepairExecutionError, "not valid JSON"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)
            (project / ABLETON_REPAIR_PLAN_NAME).write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonRepairExecutionError, "JSON object"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)
            self.assertEqual(fake.calls, [])

    def test_unsupported_repair_plan_version_refuses(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            payload = _read_json(project / ABLETON_REPAIR_PLAN_NAME)
            payload["ableton_repair_plan_version"] = "99.0"
            _write_json(project / ABLETON_REPAIR_PLAN_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "Unsupported"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)
            self.assertEqual(fake.calls, [])

    def test_invalid_repair_state_refuses(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            payload = _read_json(project / ABLETON_REPAIR_PLAN_NAME)
            payload["repair_state"] = "not_a_repair_state"
            _write_json(project / ABLETON_REPAIR_PLAN_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "repair_state"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)
            self.assertEqual(fake.calls, [])

    def test_source_sha_mismatches_refuse(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            handoff = _read_json(project / "ableton_handoff.json")
            handoff["adoption"]["reason"] = "tampered after repair plan"
            _write_json(project / "ableton_handoff.json", handoff)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "Handoff SHA-256"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)
            self.assertEqual(fake.calls, [])

        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            receipt = _read_json(project / "ableton_execution.json")
            receipt["error"] = "tampered"
            _write_json(project / "ableton_execution.json", receipt)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "Execution receipt SHA-256"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)

        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            handoff = _read_json(project / "ableton_handoff.json")
            plan = (project / handoff["arrangement_plan"]["path"]).resolve()
            plan.write_text(plan.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonRepairExecutionError, "Arrangement plan SHA-256"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)

        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            job = _read_json(project / ABLETON_JOB_PLAN_NAME)
            job["tampered"] = True
            _write_json(project / ABLETON_JOB_PLAN_NAME, job)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "Job plan SHA-256"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)

        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            verification = _read_json(project / ABLETON_VERIFICATION_NAME)
            verification["summary"]["failed"] = 99
            _write_json(project / ABLETON_VERIFICATION_NAME, verification)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "stale"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)
            self.assertEqual(fake.calls, [])

    def test_adopted_round_mismatch_refuses(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            verification = _read_json(project / ABLETON_VERIFICATION_NAME)
            verification["source"]["adopted_round"] = 9
            _write_json(project / ABLETON_VERIFICATION_NAME, verification)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "Adopted round"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)
            self.assertEqual(fake.calls, [])

    def test_stale_expected_state_refuses(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            verification = _read_json(project / ABLETON_VERIFICATION_NAME)
            verification["expected"]["tempo"] = 40.0
            _write_json(project / ABLETON_VERIFICATION_NAME, verification)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "stale"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)
            self.assertEqual(fake.calls, [])

    def test_missing_and_duplicate_check_id_refuse(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            with self.assertRaisesRegex(AbletonRepairExecutionError, "not a unique"):
                execute_ableton_repair(project, check_id="no-such-check", runner=fake)
            selected = load_validated_repair_selection(project, check_id="tempo")
            mutated = copy.deepcopy(selected.repair_plan)
            mutated["candidate_actions"].append(mutated["candidate_actions"][0])
            with patch(
                "kihachi_music_ai.ableton_repair_execution.load_validated_repair_plan",
                return_value=type(load_validated_repair_plan(project))(
                    **{
                        **load_validated_repair_plan(project).__dict__,
                        "repair_plan": mutated,
                    }
                ),
            ):
                with self.assertRaisesRegex(AbletonRepairExecutionError, "more than once"):
                    load_validated_repair_selection(project, check_id="tempo")
            self.assertEqual(fake.calls, [])

    def test_manual_item_selection_refuses(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tempo"] = 90.0
            del evidence["arrangement_clips"]
            evidence["arrangement_observable"] = False
            self._verify(project, evidence)
            build_ableton_repair_plan(project)
            plan = load_validated_repair_plan(project)
            self.assertEqual(plan.repair_plan["repair_state"], STATE_CANDIDATES_READY)
            manual_id = plan.repair_plan["manual_actions"][0]["check_id"]
            with self.assertRaisesRegex(AbletonRepairExecutionError, "manual_inspection"):
                execute_ableton_repair(project, check_id=manual_id, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_device_clip_and_arrangement_candidates_refuse(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            device = expected["devices"][0]
            evidence["devices"][str(int(device["track_index"]))] = []
            clip = expected["clips"][0]
            key = f"{int(clip['track_index'])}:{int(clip['clip_index'])}"
            evidence["session_clips"][key]["notes"] = []
            evidence["session_clips"][key]["note_count"] = 0
            target = expected["arrangement"][0]
            evidence["arrangement_clips"][str(int(target["track_index"]))]["clips"] = []
            self._verify(project, evidence)
            build_ableton_repair_plan(project)
            for check_id in (
                f"device:{int(device['track_index'])}",
                f"session_clip:{key}",
                f"arrangement:{int(target['track_index'])}",
            ):
                with self.assertRaisesRegex(
                    AbletonRepairExecutionError, "unsupported_for_execution"
                ):
                    execute_ableton_repair(project, check_id=check_id, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_source_operation_index_bool_negative_and_oob_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            loaded = load_validated_repair_plan(project)
            for bad_index in (True, -1, 10_000):
                mutated = copy.deepcopy(loaded.repair_plan)
                mutated["candidate_actions"][0]["source_operation_index"] = bad_index
                with patch(
                    "kihachi_music_ai.ableton_repair_execution.load_validated_repair_plan",
                    return_value=type(loaded)(**{**loaded.__dict__, "repair_plan": mutated}),
                ):
                    with self.assertRaises(AbletonRepairExecutionError):
                        load_validated_repair_selection(project, check_id="tempo")

    def test_operation_view_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            loaded = load_validated_repair_plan(project)
            mutated = copy.deepcopy(loaded.repair_plan)
            mutated["candidate_actions"][0]["source_operation"]["params"]["bpm"] = 40
            with patch(
                "kihachi_music_ai.ableton_repair_execution.load_validated_repair_plan",
                return_value=type(loaded)(**{**loaded.__dict__, "repair_plan": mutated}),
            ):
                with self.assertRaisesRegex(AbletonRepairExecutionError, "does not match"):
                    load_validated_repair_selection(project, check_id="tempo")

    def test_expected_bpm_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            loaded = load_validated_repair_plan(project)
            verification = copy.deepcopy(loaded.verification)
            for check in verification["checks"]:
                if check["id"] == "tempo":
                    check["expected"] = 40.0
            with patch(
                "kihachi_music_ai.ableton_repair_execution.load_validated_repair_plan",
                return_value=type(loaded)(**{**loaded.__dict__, "verification": verification}),
            ):
                with self.assertRaisesRegex(AbletonRepairExecutionError, "does not match set_tempo"):
                    load_validated_repair_selection(project, check_id="tempo")

    def test_refusal_does_not_call_abletongpt_or_live(self) -> None:
        fake = RepairFakeAbletonGPT()
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            with self.assertRaises(AbletonRepairExecutionError):
                execute_ableton_repair(
                    project,
                    check_id="device:0",
                    approved_plan_sha256=self._plan_sha(project),
                    runner=fake,
                    preflight_provider=provider,
                )
            self.assertEqual(fake.calls, [])
            self.assertEqual(calls, [])

    def test_execute_without_approval_refuses(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            with self.assertRaisesRegex(AbletonRepairExecutionError, "approve-plan-sha"):
                execute_ableton_repair(project, check_id="tempo", runner=fake)
            self.assertEqual(fake.calls, [])

    def test_approval_must_be_64_lowercase_hex(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            sha = self._plan_sha(project)
            for bad in ("abcd", " " + sha, sha.upper(), sha + "0", sha[:63]):
                with self.assertRaisesRegex(AbletonRepairExecutionError, "64-character"):
                    execute_ableton_repair(
                        project,
                        check_id="tempo",
                        approved_plan_sha256=bad,
                        runner=fake,
                    )
            self.assertEqual(fake.calls, [])

    def test_wrong_sha_refuses_exact_current_sha_accepted(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            sha = self._plan_sha(project)
            wrong = "a" * 64
            self.assertNotEqual(wrong, sha)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "does not match"):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    approved_plan_sha256=wrong,
                    runner=fake,
                )
            evidence = self._preflight_evidence(project)
            manifest = execute_ableton_repair(
                project,
                check_id="tempo",
                approved_plan_sha256=sha,
                runner=fake,
                preflight_provider=self._provider(evidence),
            )
            self.assertEqual(
                manifest.receipt["authorization"]["approved_plan_sha256"], sha
            )
            self.assertEqual(manifest.receipt["authorization"]["selected_check_id"], "tempo")
            self.assertEqual(
                manifest.receipt["authorization"]["method"], "explicit_cli_plan_sha256"
            )

    def test_prepare_only_imports_without_run_or_live(self) -> None:
        fake = RepairFakeAbletonGPT()
        fake.fail_if_run = True
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("prepare-only must not read Live")

        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            original_job = (project / ABLETON_JOB_PLAN_NAME).read_bytes()
            manifest = execute_ableton_repair(
                project,
                check_id="tempo",
                prepare_only=True,
                runner=fake,
                preflight_provider=provider,
            )
            self.assertEqual(fake.commands(), ["import-kihachi"])
            self.assertEqual(calls, [])
            request = fake.arrangement_requests[0]
            self.assertEqual(len(request["operations"]), 1)
            self.assertEqual(request["operations"][0]["op"], "set_tempo")
            self.assertEqual(request["operations"][0]["params"]["bpm"], 110)
            job = _read_json(project / ABLETON_REPAIR_JOB_PLAN_NAME)
            self.assertEqual(len(job["steps"]), 1)
            self.assertEqual(job["steps"][0]["command"], "set_tempo")
            self.assertEqual((project / ABLETON_JOB_PLAN_NAME).read_bytes(), original_job)
            self.assertEqual(manifest.receipt["execution_state"], "repair_prepared_not_applied")
            self.assertFalse(manifest.receipt["boundary"]["live_mutation_attempted"])
            self.assertIsNone(manifest.receipt["preflight"])
            self.assertIsNone(manifest.receipt["run"])

    def test_preflight_accepts_matching_tempo_and_tracks(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            evidence = self._preflight_evidence(project)
            manifest = execute_ableton_repair(
                project,
                check_id="tempo",
                approved_plan_sha256=self._plan_sha(project),
                runner=fake,
                preflight_provider=self._provider(evidence),
            )
            self.assertEqual(fake.commands(), ["import-kihachi", "run"])
            self.assertEqual(manifest.receipt["preflight"]["observed_tempo"], 90.0)
            self.assertTrue(manifest.receipt["preflight"]["track_identity_match"])

    def test_preflight_refuses_changed_or_already_expected_tempo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            sha = self._plan_sha(project)
            changed = self._preflight_evidence(project)
            changed["live_state"]["tempo"] = 95.0
            fake = RepairFakeAbletonGPT()
            with self.assertRaisesRegex(AbletonRepairExecutionError, "changed since verification"):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    approved_plan_sha256=sha,
                    runner=fake,
                    preflight_provider=self._provider(changed),
                )
            self.assertEqual(fake.commands(), ["import-kihachi"])

        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            already = self._preflight_evidence(project)
            already["live_state"]["tempo"] = 110.0
            fake = RepairFakeAbletonGPT()
            with self.assertRaisesRegex(AbletonRepairExecutionError, "already matches"):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    approved_plan_sha256=self._plan_sha(project),
                    runner=fake,
                    preflight_provider=self._provider(already),
                )
            self.assertEqual(fake.commands(), ["import-kihachi"])

    def test_preflight_refuses_track_count_and_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            sha = self._plan_sha(project)
            evidence = self._preflight_evidence(project)
            evidence["live_state"]["tracks"] = evidence["live_state"]["tracks"][:-1]
            fake = RepairFakeAbletonGPT()
            with self.assertRaisesRegex(AbletonRepairExecutionError, "track count"):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    approved_plan_sha256=sha,
                    runner=fake,
                    preflight_provider=self._provider(evidence),
                )
            self.assertEqual(fake.commands(), ["import-kihachi"])

        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            evidence = self._preflight_evidence(project)
            expected = self._expected(project)
            index = int(expected["tracks"][0]["index"])
            for track in evidence["live_state"]["tracks"]:
                if track["index"] == index:
                    track["name"] = "Renamed Track"
            fake = RepairFakeAbletonGPT()
            with self.assertRaisesRegex(AbletonRepairExecutionError, "track identity"):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    approved_plan_sha256=self._plan_sha(project),
                    runner=fake,
                    preflight_provider=self._provider(evidence),
                )
            self.assertEqual(fake.commands(), ["import-kihachi"])

    def test_preflight_refuses_malformed_evidence_and_skips_run(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            sha = self._plan_sha(project)
            with self.assertRaises(AbletonRepairExecutionError):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    approved_plan_sha256=sha,
                    runner=fake,
                    preflight_provider=lambda request: "not-an-object",  # type: ignore[return-value]
                )
            self.assertEqual(fake.commands(), ["import-kihachi"])
            fake.calls.clear()
            with self.assertRaises(AbletonRepairExecutionError):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    approved_plan_sha256=sha,
                    runner=fake,
                    preflight_provider=lambda request: {"read_only": False},
                )
            self.assertNotIn("run", fake.commands())

    def test_kihachi_does_not_import_live_bridge(self) -> None:
        from kihachi_music_ai import ableton_repair_execution as module

        source = inspect.getsource(module)
        self.assertNotIn("AbletonBridge", source)
        self.assertNotIn("abletongpt.bridge", source)
        self.assertIn("collect_live_evidence", source)
        self.assertIn("shell=False", inspect.getsource(run_command))

    def test_execute_calls_import_then_run_with_argv_list(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            before = _artifact_fingerprints(project)
            evidence = self._preflight_evidence(project)
            manifest = execute_ableton_repair(
                project,
                check_id="tempo",
                approved_plan_sha256=self._plan_sha(project),
                runner=fake,
                preflight_provider=self._provider(evidence),
            )
            self.assertEqual(fake.commands(), ["import-kihachi", "run"])
            import_argv = fake.calls[0]
            run_argv = fake.calls[1]
            self.assertIsInstance(import_argv, list)
            self.assertEqual(import_argv[1:4], ["-m", "abletongpt.cli.jobs", "import-kihachi"])
            self.assertIn("--arrangement-plan", import_argv)
            self.assertEqual(run_argv[1:4], ["-m", "abletongpt.cli.jobs", "run"])
            self.assertFalse(manifest.receipt["boundary"]["live_repair_verified"])
            self.assertFalse(manifest.receipt["boundary"]["auto_verify"])
            self.assertEqual(manifest.receipt["execution_state"], "repair_applied_unverified")
            self.assertEqual(manifest.receipt["completed"], 1)
            self.assertEqual(manifest.receipt["failed"], 0)
            self.assertEqual(manifest.receipt["pending"], 0)
            self.assertEqual(_artifact_fingerprints(project), before)

    def test_run_failure_records_attempted_unverified_without_retry(self) -> None:
        fake = RepairFakeAbletonGPT()
        fake.run_returncode = 1
        fake.run_stdout = "completed=0 failed=1 pending=0\n"
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            evidence = self._preflight_evidence(project)
            with self.assertRaisesRegex(AbletonRepairExecutionError, "repair job failed"):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    approved_plan_sha256=self._plan_sha(project),
                    runner=fake,
                    preflight_provider=self._provider(evidence),
                )
            self.assertEqual(fake.commands(), ["import-kihachi", "run"])
            receipt = _read_json(project / ABLETON_REPAIR_EXECUTION_NAME)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["execution_state"], "repair_attempted_unverified")
            self.assertTrue(receipt["boundary"]["live_mutation_attempted"])
            self.assertFalse(receipt["boundary"]["live_repair_verified"])
            self.assertIn("ableton-verify", receipt["next_action"])

    def test_output_capture_is_bounded(self) -> None:
        fake = RepairFakeAbletonGPT()
        fake.run_stdout = "x" * 9000
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            evidence = self._preflight_evidence(project)
            manifest = execute_ableton_repair(
                project,
                check_id="tempo",
                approved_plan_sha256=self._plan_sha(project),
                runner=fake,
                preflight_provider=self._provider(evidence),
            )
            captured = manifest.receipt["run"]["stdout"]
            self.assertLess(len(captured), 9000)
            self.assertIn("characters omitted", captured)

    def test_unresolved_counts_exclude_selected_candidate(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tempo"] = 90.0
            device = expected["devices"][0]
            evidence["devices"][str(int(device["track_index"]))] = []
            self._verify(project, evidence)
            build_ableton_repair_plan(project)
            preflight = self._preflight_evidence(project)
            manifest = execute_ableton_repair(
                project,
                check_id="tempo",
                approved_plan_sha256=self._plan_sha(project),
                runner=fake,
                preflight_provider=self._provider(preflight),
            )
            self.assertGreaterEqual(manifest.receipt["unresolved"]["candidate_actions"], 1)
            self.assertEqual(manifest.receipt["unresolved"]["manual_actions"], 0)

    def test_receipt_atomic_write_leaves_no_partial_json(self) -> None:
        fake = RepairFakeAbletonGPT()
        fake.fail_if_run = True
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            with patch(
                "kihachi_music_ai.ableton_repair_execution.os.replace",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(OSError):
                    execute_ableton_repair(
                        project, check_id="tempo", prepare_only=True, runner=fake
                    )
            self.assertFalse((project / ABLETON_REPAIR_EXECUTION_NAME).exists())
            leftovers = list(project.glob(".ableton_repair_execution.json-*"))
            self.assertEqual(leftovers, [])

    def test_successful_execute_refuses_rerun_without_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            sha = self._plan_sha(project)
            evidence = self._preflight_evidence(project)
            first = RepairFakeAbletonGPT()
            execute_ableton_repair(
                project,
                check_id="tempo",
                approved_plan_sha256=sha,
                runner=first,
                preflight_provider=self._provider(evidence),
            )
            second = RepairFakeAbletonGPT()
            with self.assertRaisesRegex(AbletonRepairExecutionError, "already executed"):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    approved_plan_sha256=sha,
                    runner=second,
                    preflight_provider=self._provider(evidence),
                )
            self.assertEqual(second.calls, [])
            third = RepairFakeAbletonGPT()
            with self.assertRaisesRegex(AbletonRepairExecutionError, "approve-plan-sha"):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    rerun=True,
                    runner=third,
                    preflight_provider=self._provider(evidence),
                )
            self.assertEqual(third.calls, [])
            fourth = RepairFakeAbletonGPT()
            execute_ableton_repair(
                project,
                check_id="tempo",
                approved_plan_sha256=sha,
                rerun=True,
                runner=fourth,
                preflight_provider=self._provider(evidence),
            )
            self.assertEqual(fourth.commands(), ["import-kihachi", "run"])

    def test_prepare_only_does_not_destroy_success_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            sha = self._plan_sha(project)
            evidence = self._preflight_evidence(project)
            execute_ableton_repair(
                project,
                check_id="tempo",
                approved_plan_sha256=sha,
                runner=RepairFakeAbletonGPT(),
                preflight_provider=self._provider(evidence),
            )
            before = _read_json(project / ABLETON_REPAIR_EXECUTION_NAME)
            fake = RepairFakeAbletonGPT()
            fake.fail_if_run = True
            execute_ableton_repair(
                project, check_id="tempo", prepare_only=True, runner=fake
            )
            after = _read_json(project / ABLETON_REPAIR_EXECUTION_NAME)
            self.assertEqual(after["mode"], "execute")
            self.assertEqual(after["execution_state"], "repair_applied_unverified")
            self.assertEqual(after["executed_at"], before["executed_at"])

    def test_cli_parser_accepts_all_options(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "ableton-repair-apply",
                "projects/song",
                "--check-id",
                "tempo",
                "--prepare-only",
                "--abletongpt-python",
                "/usr/bin/python3",
            ]
        )
        self.assertEqual(args.command, "ableton-repair-apply")
        self.assertEqual(args.check_id, "tempo")
        self.assertTrue(args.prepare_only)
        self.assertEqual(args.abletongpt_python, Path("/usr/bin/python3"))
        execute_args = parser.parse_args(
            [
                "ableton-repair-apply",
                "projects/song",
                "--check-id",
                "tempo",
                "--approve-plan-sha",
                "a" * 64,
                "--rerun",
            ]
        )
        self.assertEqual(execute_args.approve_plan_sha, "a" * 64)
        self.assertTrue(execute_args.rerun)

    def test_cli_usage_and_refusal_exit_two(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit) as caught:
            parser.parse_args(["ableton-repair-apply", "projects/song"])
        self.assertEqual(caught.exception.code, 2)
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing"
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                status = main(
                    ["ableton-repair-apply", str(missing), "--check-id", "tempo"]
                )
            self.assertEqual(status, 2)

    def test_cli_prepare_and_execute_success_and_run_failure_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            sha = self._plan_sha(project)
            fake = RepairFakeAbletonGPT()
            fake.fail_if_run = True
            buffer = io.StringIO()
            with patch(
                "kihachi_music_ai.ableton_repair_execution.run_command", fake
            ), contextlib.redirect_stdout(buffer):
                status = main(
                    [
                        "ableton-repair-apply",
                        str(project),
                        "--check-id",
                        "tempo",
                        "--prepare-only",
                    ]
                )
            self.assertEqual(status, 0)
            text = buffer.getvalue()
            self.assertIn("Prepared Ableton repair execution (no Live job)", text)
            self.assertIn(f"Repair plan SHA-256: {sha}", text)
            heading = text.splitlines()[0].lower()
            self.assertNotIn("repaired", heading)
            self.assertNotIn("fixed", heading)
            self.assertNotIn("verified", heading)

            evidence = self._preflight_evidence(project)
            fake = RepairFakeAbletonGPT()
            buffer = io.StringIO()
            with patch(
                "kihachi_music_ai.ableton_repair_execution.run_command", fake
            ), patch(
                "kihachi_music_ai.ableton_repair_execution.collect_live_evidence",
                lambda request, **kwargs: evidence,
            ), contextlib.redirect_stdout(buffer):
                status = main(
                    [
                        "ableton-repair-apply",
                        str(project),
                        "--check-id",
                        "tempo",
                        "--approve-plan-sha",
                        sha,
                    ]
                )
            self.assertEqual(status, 0)
            text = buffer.getvalue()
            self.assertIn("Applied authorized Ableton repair candidate through AbletonGPT", text)
            self.assertIn("Live repair verified: no", text)
            heading = text.splitlines()[0].lower()
            self.assertNotIn("repaired", heading)
            self.assertNotIn("fixed", heading)
            self.assertNotIn("verified", heading)

        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            sha = self._plan_sha(project)
            evidence = self._preflight_evidence(project)
            fake = RepairFakeAbletonGPT()
            fake.run_returncode = 1
            fake.run_stdout = "completed=0 failed=1 pending=0\n"
            err = io.StringIO()
            with patch(
                "kihachi_music_ai.ableton_repair_execution.run_command", fake
            ), patch(
                "kihachi_music_ai.ableton_repair_execution.collect_live_evidence",
                lambda request, **kwargs: evidence,
            ), contextlib.redirect_stderr(err):
                status = main(
                    [
                        "ableton-repair-apply",
                        str(project),
                        "--check-id",
                        "tempo",
                        "--approve-plan-sha",
                        sha,
                    ]
                )
            self.assertEqual(status, 1)
            self.assertIn("ableton-verify", err.getvalue())

    def test_prepare_only_and_conflicting_flags_refuse(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            with self.assertRaisesRegex(AbletonRepairExecutionError, "cannot be combined"):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    prepare_only=True,
                    rerun=True,
                    runner=fake,
                )
            with self.assertRaisesRegex(AbletonRepairExecutionError, "cannot be combined"):
                execute_ableton_repair(
                    project,
                    check_id="tempo",
                    prepare_only=True,
                    approved_plan_sha256=self._plan_sha(project),
                    runner=fake,
                )
            self.assertEqual(fake.calls, [])

    def test_source_artifacts_and_adoption_unchanged_after_execute(self) -> None:
        fake = RepairFakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            before = _artifact_fingerprints(project)
            adoption_before = load_revision_log(project).adopted
            memory_before = load_preference_memory(project)
            execute_ableton_repair(
                project,
                check_id="tempo",
                approved_plan_sha256=self._plan_sha(project),
                runner=fake,
                preflight_provider=self._provider(self._preflight_evidence(project)),
            )
            self.assertEqual(_artifact_fingerprints(project), before)
            self.assertEqual(load_revision_log(project).adopted, adoption_before)
            self.assertEqual(load_preference_memory(project).entries, memory_before.entries)
            self.assertTrue((project / ABLETON_REPAIR_EXECUTION_NAME).is_file())
            self.assertTrue((project / ABLETON_REPAIR_JOB_PLAN_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
