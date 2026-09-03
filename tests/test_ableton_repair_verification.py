"""VS11 — Explicit post-repair verification closure."""

from __future__ import annotations

import contextlib
import copy
import inspect
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kihachi_music_ai.ableton_execution import CommandResult
from kihachi_music_ai.ableton_repair import ABLETON_REPAIR_PLAN_NAME, AbletonRepairPlanError, load_validated_repair_plan
from kihachi_music_ai.ableton_repair_execution import (
    ABLETON_REPAIR_EXECUTION_NAME,
    AbletonRepairExecutionError,
    execute_ableton_repair,
)
from kihachi_music_ai.ableton_repair_verification import (
    ABLETON_REPAIR_VERIFICATION_NAME,
    STATE_REPAIR_CHECK_FAILED,
    STATE_REPAIR_CHECK_NOT_OBSERVABLE,
    STATE_REPAIR_CHECK_VERIFIED,
    STATE_REPAIR_VERIFICATION_NOT_RUN,
    AbletonRepairVerificationError,
    load_validated_repair_execution_receipt,
    verify_ableton_repair,
)
from kihachi_music_ai.ableton_verification import (
    ABLETON_VERIFICATION_NAME,
    CHECK_FAIL,
    CHECK_NOT_OBSERVABLE,
    CHECK_PASS,
    STATE_FAILED,
    STATE_NOT_RUN,
    STATE_VERIFIED,
    verify_ableton_execution,
)
from kihachi_music_ai.cli import build_parser, main
from kihachi_music_ai.preference_memory import load_preference_memory
from kihachi_music_ai.revision import load_revision_log
from test_ableton_repair_execution import (
    RepairFakeAbletonGPT,
    _AbletonRepairFixtures,
    _artifact_fingerprints,
    _read_json,
    _sha256,
    _write_json,
)
from test_ableton_verification import matching_evidence
import kihachi_music_ai.ableton_repair_verification as repair_verification_module


class AbletonRepairVerificationTests(_AbletonRepairFixtures):
    def _applied_tempo_repair(self, root: Path) -> Path:
        project = self._tempo_ready(root)
        fake = RepairFakeAbletonGPT()
        execute_ableton_repair(
            project,
            check_id="tempo",
            approved_plan_sha256=self._plan_sha(project),
            runner=fake,
            preflight_provider=self._provider(self._preflight_evidence(project)),
        )
        return project

    def _attempted_tempo_repair(self, root: Path) -> Path:
        project = self._tempo_ready(root)
        fake = RepairFakeAbletonGPT()
        fake.run_returncode = 1
        fake.run_stdout = "completed=0 failed=1 pending=0\n"
        with self.assertRaises(AbletonRepairExecutionError):
            execute_ableton_repair(
                project,
                check_id="tempo",
                approved_plan_sha256=self._plan_sha(project),
                runner=fake,
                preflight_provider=self._provider(self._preflight_evidence(project)),
            )
        return project

    def _applied_device_repair(self, root: Path) -> Path:
        project = self._device_ready(root)
        fake = RepairFakeAbletonGPT()
        execute_ableton_repair(
            project,
            check_id=self._device_check_id(project),
            approved_plan_sha256=self._plan_sha(project),
            runner=fake,
        )
        return project

    def _satisfied_device_repair(self, root: Path) -> Path:
        project = self._device_ready(root)
        fake = RepairFakeAbletonGPT()
        fake.device_result = {
            "status": "noop",
            "operation": "set_device_power",
            "target": {"track_index": 0, "device_index": 0},
            "before": {"is_active": True, "power_on": True, "device": "Operator"},
            "after": {"is_active": True, "power_on": True, "device": "Operator"},
            "mutation_performed": False,
        }
        execute_ableton_repair(
            project,
            check_id=self._device_check_id(project),
            approved_plan_sha256=self._plan_sha(project),
            runner=fake,
        )
        return project

    def _matching(self, project: Path) -> dict:
        return matching_evidence(self._expected(project))

    def test_missing_repair_execution_refuses_before_live_read(self) -> None:
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            with self.assertRaisesRegex(
                AbletonRepairVerificationError, "No Ableton repair execution receipt"
            ):
                verify_ableton_repair(project, provider=provider)
            self.assertEqual(calls, [])
            self.assertFalse((project / ABLETON_REPAIR_VERIFICATION_NAME).is_file())

    def test_malformed_receipt_refuses_before_live_read(self) -> None:
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            (project / ABLETON_REPAIR_EXECUTION_NAME).write_text("{not json\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonRepairVerificationError, "not valid JSON"):
                verify_ableton_repair(project, provider=provider)
            (project / ABLETON_REPAIR_EXECUTION_NAME).write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonRepairVerificationError, "JSON object"):
                verify_ableton_repair(project, provider=provider)
            self.assertEqual(calls, [])

    def test_unsupported_receipt_version_refuses(self) -> None:
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            payload = _read_json(project / ABLETON_REPAIR_EXECUTION_NAME)
            payload["ableton_repair_execution_version"] = "99.0"
            _write_json(project / ABLETON_REPAIR_EXECUTION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairVerificationError, "Unsupported"):
                verify_ableton_repair(project, provider=provider)
            self.assertEqual(calls, [])

    def test_prepare_only_receipt_refuses(self) -> None:
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        fake = RepairFakeAbletonGPT()
        fake.fail_if_run = True
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            execute_ableton_repair(
                project, check_id="tempo", prepare_only=True, runner=fake
            )
            with self.assertRaisesRegex(AbletonRepairVerificationError, "prepare-only"):
                verify_ableton_repair(project, provider=provider)
            self.assertEqual(calls, [])
            self.assertFalse((project / ABLETON_REPAIR_VERIFICATION_NAME).is_file())

    def test_prepared_not_applied_state_refuses(self) -> None:
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            payload = _read_json(project / ABLETON_REPAIR_EXECUTION_NAME)
            payload["execution_state"] = "repair_prepared_not_applied"
            _write_json(project / ABLETON_REPAIR_EXECUTION_NAME, payload)
            with self.assertRaisesRegex(
                AbletonRepairVerificationError, "repair_prepared_not_applied"
            ):
                verify_ableton_repair(project, provider=provider)
            self.assertEqual(calls, [])

    def test_repair_plan_sha_mismatch_refuses(self) -> None:
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            plan = _read_json(project / ABLETON_REPAIR_PLAN_NAME)
            plan["created_at"] = "tampered"
            _write_json(project / ABLETON_REPAIR_PLAN_NAME, plan)
            with self.assertRaisesRegex(AbletonRepairVerificationError, "does not match"):
                verify_ableton_repair(project, provider=provider)
            self.assertEqual(calls, [])

    def test_missing_selection_refuses(self) -> None:
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            payload = _read_json(project / ABLETON_REPAIR_EXECUTION_NAME)
            del payload["selection"]
            _write_json(project / ABLETON_REPAIR_EXECUTION_NAME, payload)
            with self.assertRaisesRegex(AbletonRepairVerificationError, "missing selection"):
                verify_ableton_repair(project, provider=provider)
            self.assertEqual(calls, [])

    def test_ambiguous_plan_selection_refuses(self) -> None:
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            plan = _read_json(project / ABLETON_REPAIR_PLAN_NAME)
            tempo = next(
                item for item in plan["candidate_actions"] if item["check_id"] == "tempo"
            )
            plan["candidate_actions"].append(copy.deepcopy(tempo))
            _write_json(project / ABLETON_REPAIR_PLAN_NAME, plan)
            receipt = _read_json(project / ABLETON_REPAIR_EXECUTION_NAME)
            receipt["source"]["repair_plan"]["sha256"] = _sha256(
                project / ABLETON_REPAIR_PLAN_NAME
            )
            _write_json(project / ABLETON_REPAIR_EXECUTION_NAME, receipt)
            with self.assertRaisesRegex(AbletonRepairVerificationError, "more than once"):
                verify_ableton_repair(project, provider=provider)
            self.assertEqual(calls, [])

    def test_source_operation_identity_mismatch_refuses(self) -> None:
        calls: list[object] = []

        def provider(request: dict) -> dict:
            calls.append(request)
            raise AssertionError("Live must not be read")

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            payload = _read_json(project / ABLETON_REPAIR_EXECUTION_NAME)
            payload["selection"]["source_operation_sha256"] = "0" * 64
            _write_json(project / ABLETON_REPAIR_EXECUTION_NAME, payload)
            with self.assertRaisesRegex(
                AbletonRepairVerificationError, "source_operation_sha256"
            ):
                verify_ableton_repair(project, provider=provider)
            self.assertEqual(calls, [])

    def test_applied_satisfied_and_attempted_receipts_are_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            loaded = load_validated_repair_execution_receipt(project)
            self.assertEqual(loaded.execution_state, "repair_applied_unverified")
            self.assertEqual(loaded.check_id, "tempo")

        with tempfile.TemporaryDirectory() as temp:
            project = self._satisfied_device_repair(Path(temp))
            loaded = load_validated_repair_execution_receipt(project)
            self.assertEqual(loaded.execution_state, "repair_satisfied_unverified")
            self.assertTrue(loaded.check_id.startswith("device:"))

        with tempfile.TemporaryDirectory() as temp:
            project = self._attempted_tempo_repair(Path(temp))
            loaded = load_validated_repair_execution_receipt(project)
            self.assertEqual(loaded.execution_state, "repair_attempted_unverified")

        for factory in (
            self._applied_tempo_repair,
            self._satisfied_device_repair,
            self._attempted_tempo_repair,
        ):
            with tempfile.TemporaryDirectory() as temp:
                project = factory(Path(temp))
                evidence = self._matching(project)
                manifest = verify_ableton_repair(
                    project, provider=self._provider(evidence)
                )
                self.assertEqual(
                    manifest.repair_verification_state, STATE_REPAIR_CHECK_VERIFIED
                )
                self.assertEqual(manifest.exit_code, 0)

    def test_calls_existing_vs7_verification_and_avoids_mutation_apis(self) -> None:
        source = inspect.getsource(repair_verification_module)
        self.assertIn("verify_ableton_execution", source)
        self.assertNotIn("ABLETONGPT_EVIDENCE_COLLECTOR", source)
        self.assertNotIn("repair_live_device", source)
        self.assertNotIn("AbletonBridge", source)
        self.assertNotIn("execute_ableton_repair", source)
        self.assertNotIn("build_ableton_repair_plan", source)
        self.assertNotIn("load_validated_repair_plan", source)
        self.assertNotIn("abletongpt.cli.jobs", source)
        calls: list[Path] = []
        real = verify_ableton_execution

        def wrapper(project_dir, **kwargs):
            calls.append(Path(project_dir))
            self.assertTrue(kwargs["provider"] is not None)
            return real(project_dir, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            with patch(
                "kihachi_music_ai.ableton_repair_verification.verify_ableton_execution",
                wrapper,
            ):
                verify_ableton_repair(
                    project, provider=self._provider(self._matching(project))
                )
            self.assertEqual(calls, [project.resolve()])

    def test_abletongpt_python_propagates(self) -> None:
        recorded: dict[str, object] = {}
        real = verify_ableton_execution

        def wrapper(project_dir, **kwargs):
            recorded.update(kwargs)
            return real(project_dir, **kwargs)

        python = Path("/custom/abletongpt-python")
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            with patch(
                "kihachi_music_ai.ableton_repair_verification.verify_ableton_execution",
                wrapper,
            ):
                verify_ableton_repair(
                    project,
                    abletongpt_python=python,
                    provider=self._provider(self._matching(project)),
                )
            self.assertEqual(recorded["abletongpt_python"], python)

    def test_selected_tempo_pass_is_repair_check_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            manifest = verify_ableton_repair(
                project, provider=self._provider(self._matching(project))
            )
            self.assertEqual(manifest.repair_verification_state, STATE_REPAIR_CHECK_VERIFIED)
            self.assertEqual(manifest.exit_code, 0)
            document = _read_json(project / ABLETON_REPAIR_VERIFICATION_NAME)
            self.assertEqual(document["result"]["check_status"], CHECK_PASS)
            self.assertEqual(document["selection"]["check_id"], "tempo")
            self.assertIs(document["claims"]["causality_claimed"], False)
            self.assertTrue(document["claims"]["selected_postcondition_observed"])
            self.assertFalse(document["boundary"]["live_mutation"])
            self.assertFalse(document["boundary"]["repair_attempted"])
            self.assertFalse(document["boundary"]["automatic_retry"])
            self.assertFalse(document["boundary"]["automatic_repair_plan"])
            self.assertFalse(document["boundary"]["automatic_adoption"])

    def test_selected_device_pass_is_repair_check_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_device_repair(Path(temp))
            check_id = self._device_check_id(project)
            manifest = verify_ableton_repair(
                project, provider=self._provider(self._matching(project))
            )
            self.assertEqual(manifest.repair_verification_state, STATE_REPAIR_CHECK_VERIFIED)
            self.assertEqual(manifest.exit_code, 0)
            document = _read_json(project / ABLETON_REPAIR_VERIFICATION_NAME)
            self.assertEqual(document["selection"]["check_id"], check_id)
            self.assertEqual(document["selection"]["repair_kind"], "device")
            self.assertEqual(document["result"]["check_status"], CHECK_PASS)

    def test_selected_check_fail_exits_1_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            evidence = self._matching(project)
            evidence["live_state"]["tempo"] = 90.0
            retry = 0

            def provider(request: dict) -> dict:
                nonlocal retry
                retry += 1
                self.assertTrue(request.get("read_only"))
                return copy.deepcopy(evidence)

            manifest = verify_ableton_repair(project, provider=provider)
            self.assertEqual(manifest.repair_verification_state, STATE_REPAIR_CHECK_FAILED)
            self.assertEqual(manifest.exit_code, 1)
            self.assertEqual(retry, 1)
            document = _read_json(project / ABLETON_REPAIR_VERIFICATION_NAME)
            self.assertEqual(document["result"]["check_status"], CHECK_FAIL)
            self.assertIs(document["claims"]["causality_claimed"], False)
            self.assertFalse(document["claims"]["selected_postcondition_observed"])
            self.assertIn("No automatic retry", document["next_action"])

    def test_selected_check_not_observable_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            evidence = self._matching(project)
            evidence["live_state"]["tempo"] = None
            manifest = verify_ableton_repair(
                project, provider=self._provider(evidence)
            )
            self.assertEqual(
                manifest.repair_verification_state, STATE_REPAIR_CHECK_NOT_OBSERVABLE
            )
            self.assertEqual(manifest.exit_code, 2)
            document = _read_json(project / ABLETON_REPAIR_VERIFICATION_NAME)
            self.assertEqual(document["result"]["check_status"], CHECK_NOT_OBSERVABLE)
            self.assertFalse(document["claims"]["selected_postcondition_observed"])

    def test_missing_selected_check_refuses_closure(self) -> None:
        real = verify_ableton_execution

        def wrapper(project_dir, **kwargs):
            manifest = real(project_dir, **kwargs)
            manifest.document["checks"] = [
                item
                for item in manifest.document["checks"]
                if item.get("id") != "tempo"
            ]
            return manifest

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            with patch(
                "kihachi_music_ai.ableton_repair_verification.verify_ableton_execution",
                wrapper,
            ):
                with self.assertRaisesRegex(
                    AbletonRepairVerificationError, "missing from the fresh"
                ):
                    verify_ableton_repair(
                        project, provider=self._provider(self._matching(project))
                    )
            self.assertFalse((project / ABLETON_REPAIR_VERIFICATION_NAME).is_file())

    def test_duplicate_selected_check_refuses_closure(self) -> None:
        real = verify_ableton_execution

        def wrapper(project_dir, **kwargs):
            manifest = real(project_dir, **kwargs)
            tempo = next(
                item for item in manifest.document["checks"] if item.get("id") == "tempo"
            )
            manifest.document["checks"].append(copy.deepcopy(tempo))
            return manifest

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            with patch(
                "kihachi_music_ai.ableton_repair_verification.verify_ableton_execution",
                wrapper,
            ):
                with self.assertRaisesRegex(
                    AbletonRepairVerificationError, "more than once"
                ):
                    verify_ableton_repair(
                        project, provider=self._provider(self._matching(project))
                    )

    def test_selected_pass_with_other_fail_keeps_full_set_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            first = int(expected["tracks"][0]["index"])
            evidence["live_state"]["tracks"][first]["name"] = "Wrong Name"
            manifest = verify_ableton_repair(project, provider=self._provider(evidence))
            self.assertEqual(manifest.repair_verification_state, STATE_REPAIR_CHECK_VERIFIED)
            self.assertEqual(manifest.exit_code, 0)
            document = _read_json(project / ABLETON_REPAIR_VERIFICATION_NAME)
            self.assertEqual(document["result"]["check_status"], CHECK_PASS)
            self.assertEqual(document["full_verification"]["verification_state"], STATE_FAILED)
            self.assertGreaterEqual(document["full_verification"]["failed"], 1)
            self.assertFalse(document["claims"]["full_live_set_verified"])
            self.assertIs(document["claims"]["causality_claimed"], False)
            verification = _read_json(project / ABLETON_VERIFICATION_NAME)
            self.assertEqual(verification["verification_state"], STATE_FAILED)

    def test_abletongpt_unavailable_is_not_run(self) -> None:
        def runner(argv: list[str] | tuple[str, ...]) -> CommandResult:
            argv_list = [str(part) for part in argv]
            return CommandResult(tuple(argv_list), 1, "", "No module named abletongpt\n")

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            with self.assertRaisesRegex(
                AbletonRepairVerificationError, "AbletonGPT is not available"
            ):
                verify_ableton_repair(project, runner=runner)
            document = _read_json(project / ABLETON_REPAIR_VERIFICATION_NAME)
            self.assertEqual(
                document["repair_verification_state"], STATE_REPAIR_VERIFICATION_NOT_RUN
            )
            self.assertFalse(document["claims"]["selected_postcondition_observed"])
            self.assertIs(document["claims"]["causality_claimed"], False)
            verification = _read_json(project / ABLETON_VERIFICATION_NAME)
            self.assertEqual(verification["verification_state"], STATE_NOT_RUN)

    def test_live_unreachable_is_not_run(self) -> None:
        def runner(argv: list[str] | tuple[str, ...]) -> CommandResult:
            argv_list = [str(part) for part in argv]
            return CommandResult(
                tuple(argv_list),
                1,
                "",
                "Ableton Live に接続できません\n",
            )

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            with self.assertRaisesRegex(
                AbletonRepairVerificationError, "Ableton Live is unreachable"
            ):
                verify_ableton_repair(project, runner=runner)
            document = _read_json(project / ABLETON_REPAIR_VERIFICATION_NAME)
            self.assertEqual(
                document["repair_verification_state"], STATE_REPAIR_VERIFICATION_NOT_RUN
            )
            self.assertIsNone(document["result"])
            self.assertFalse(document["claims"]["selected_postcondition_observed"])

    def test_does_not_mutate_repair_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            before = _artifact_fingerprints(project)
            receipt_before = (project / ABLETON_REPAIR_EXECUTION_NAME).read_bytes()
            plan_before = (project / ABLETON_REPAIR_PLAN_NAME).read_bytes()
            adoption_before = load_revision_log(project).adopted
            memory_before = load_preference_memory(project)
            verify_ableton_repair(
                project, provider=self._provider(self._matching(project))
            )
            after = _artifact_fingerprints(project)
            for name, digest in before.items():
                if name == ABLETON_VERIFICATION_NAME:
                    self.assertNotEqual(after[name], digest)
                    continue
                self.assertEqual(after[name], digest, name)
            self.assertEqual(
                (project / ABLETON_REPAIR_EXECUTION_NAME).read_bytes(), receipt_before
            )
            self.assertEqual((project / ABLETON_REPAIR_PLAN_NAME).read_bytes(), plan_before)
            self.assertEqual(load_revision_log(project).adopted, adoption_before)
            self.assertEqual(load_preference_memory(project).entries, memory_before.entries)
            self.assertTrue((project / ABLETON_REPAIR_VERIFICATION_NAME).is_file())
            with self.assertRaises(AbletonRepairPlanError):
                load_validated_repair_plan(project)

    def test_rerun_replaces_closure_without_mutating_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            receipt_before = (project / ABLETON_REPAIR_EXECUTION_NAME).read_bytes()
            first = verify_ableton_repair(
                project, provider=self._provider(self._matching(project))
            )
            first_sha = _sha256(project / ABLETON_REPAIR_VERIFICATION_NAME)
            evidence = self._matching(project)
            evidence["live_state"]["tempo"] = 90.0
            second = verify_ableton_repair(project, provider=self._provider(evidence))
            self.assertEqual(first.repair_verification_state, STATE_REPAIR_CHECK_VERIFIED)
            self.assertEqual(second.repair_verification_state, STATE_REPAIR_CHECK_FAILED)
            self.assertNotEqual(
                _sha256(project / ABLETON_REPAIR_VERIFICATION_NAME), first_sha
            )
            self.assertEqual(
                (project / ABLETON_REPAIR_EXECUTION_NAME).read_bytes(), receipt_before
            )

    def test_cli_parser_accepts_ableton_repair_verify_without_check_id(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "ableton-repair-verify",
                "projects/song",
                "--abletongpt-python",
                "/usr/bin/python3",
            ]
        )
        self.assertEqual(args.command, "ableton-repair-verify")
        self.assertEqual(args.abletongpt_python, Path("/usr/bin/python3"))
        self.assertFalse(hasattr(args, "check_id"))
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "ableton-repair-verify",
                    "projects/song",
                    "--check-id",
                    "tempo",
                ]
            )

    def test_cli_verified_selected_check_with_failed_full_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            first = int(expected["tracks"][0]["index"])
            evidence["live_state"]["tracks"][first]["name"] = "Wrong Name"
            buffer = io.StringIO()
            with patch(
                "kihachi_music_ai.ableton_verification.collect_live_evidence",
                lambda request, **kwargs: evidence,
            ), contextlib.redirect_stdout(buffer):
                status = main(["ableton-repair-verify", str(project)])
            self.assertEqual(status, 0)
            text = buffer.getvalue()
            self.assertIn("REPAIR CHECK VERIFIED", text)
            self.assertIn("Full Ableton verification: FAILED", text)
            self.assertIn("this closes only the selected repair check", text)
            self.assertIn("causality claimed: no", text)
            self.assertIn("automatic retry: no", text)

    def test_cli_failed_selected_check_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied_tempo_repair(Path(temp))
            evidence = self._matching(project)
            evidence["live_state"]["tempo"] = 72.0
            buffer = io.StringIO()
            with patch(
                "kihachi_music_ai.ableton_verification.collect_live_evidence",
                lambda request, **kwargs: evidence,
            ), contextlib.redirect_stdout(buffer):
                status = main(["ableton-repair-verify", str(project)])
            self.assertEqual(status, 1)
            text = buffer.getvalue()
            self.assertIn("REPAIR CHECK FAILED", text)
            self.assertIn("no automatic retry", text)

    def test_cli_not_run_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._tempo_ready(Path(temp))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status = main(["ableton-repair-verify", str(project)])
            self.assertEqual(status, 2)
            self.assertIn("No Ableton repair execution receipt", err.getvalue())

    def test_repair_apply_does_not_auto_invoke_verify(self) -> None:
        apply_source = inspect.getsource(execute_ableton_repair)
        self.assertNotIn("verify_ableton_repair", apply_source)
        self.assertNotIn("ableton-repair-verify", apply_source)


if __name__ == "__main__":
    unittest.main()
