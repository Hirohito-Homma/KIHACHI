"""VS6 — Ableton Handoff Execution Integration."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kihachi_music_ai.ableton_execution import (
    ABLETON_EXECUTION_NAME,
    ABLETON_JOB_PLAN_NAME,
    AbletonExecutionError,
    CommandResult,
    ableton_execution_path,
    execute_ableton_handoff,
    load_validated_handoff,
    prepare_ableton_execution,
)
from kihachi_music_ai.ableton_handoff import build_ableton_handoff
from kihachi_music_ai.cli import build_parser, main
from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.pipeline import compose_project
from kihachi_music_ai.preference_memory import load_preference_memory
from kihachi_music_ai.project_artifacts import managed_midi_names
from kihachi_music_ai.revision import adopt_revision, load_revision_log, run_revision_loop
from test_music_brain import EXAMPLE
from test_revision import TAKE_SECONDS, write_take


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_fingerprints(project: Path) -> dict[str, str]:
    spec = SongSpec.from_json((project / "song_spec.json").read_text(encoding="utf-8"))
    names = ("song_spec.json", *managed_midi_names(spec))
    fingerprints = {name: _sha256(project / name) for name in names}
    audio = project / "audio" / "ace-step-01.wav"
    if audio.is_file():
        fingerprints[str(audio.relative_to(project))] = _sha256(audio)
    return fingerprints


def _job_plan_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "name": "kihachi",
        "steps": [
            {
                "step_id": "set_tempo",
                "command": "set_tempo",
                "params": {"bpm": 110},
                "status": "pending",
            },
            {
                "step_id": "create_track",
                "command": "create_track",
                "params": {"name": "KIHACHI Drums"},
                "status": "pending",
            },
        ],
    }


class FakeAbletonGPT:
    """Injected AbletonGPT runner: no Live, no real AbletonGPT install."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.import_returncode = 0
        self.run_returncode = 0
        self.import_stdout = "imported KIHACHI plan 'kihachi' with 2 step(s)\n"
        self.import_stderr = ""
        self.run_stdout = "completed=2 failed=0 pending=0\n"
        self.run_stderr = ""
        self.write_job_plan = True
        self.job_plan = _job_plan_document()
        self.fail_if_run = False

    def __call__(self, argv: list[str] | tuple[str, ...]) -> CommandResult:
        argv_list = [str(part) for part in argv]
        self.calls.append(argv_list)
        if "import-kihachi" in argv_list:
            if self.write_job_plan:
                out = Path(argv_list[argv_list.index("--out") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(self.job_plan, indent=2) + "\n", encoding="utf-8"
                )
            return CommandResult(
                tuple(argv_list),
                self.import_returncode,
                self.import_stdout,
                self.import_stderr,
            )
        if "run" in argv_list:
            if self.fail_if_run:
                raise AssertionError("AbletonGPT run must not be invoked")
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


class AbletonExecutionTests(unittest.TestCase):
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

    def _handoff(self, root: Path, round_number: int, *, rounds: int = 1) -> Path:
        project = self._project_with_revisions(root, rounds=rounds)
        adopt_revision(project, round_number, reason=f"adopt round {round_number}")
        build_ableton_handoff(project)
        return project

    def test_prepare_only_succeeds_for_valid_handoff(self) -> None:
        fake = FakeAbletonGPT()
        fake.fail_if_run = True
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            manifest = prepare_ableton_execution(project, runner=fake)
            self.assertTrue(manifest.prepare_only)
            self.assertEqual(fake.commands(), ["import-kihachi"])
            self.assertTrue((project / ABLETON_JOB_PLAN_NAME).is_file())
            self.assertEqual(manifest.receipt["status"], "success")
            self.assertEqual(manifest.receipt["mode"], "prepare_only")
            self.assertEqual(manifest.receipt["execution_state"], "prepared_not_applied")
            self.assertFalse(manifest.receipt["live_applied"])
            self.assertIsNone(manifest.receipt["run"])
            self.assertEqual(manifest.receipt["completed"], 0)
            self.assertEqual(manifest.receipt["failed"], 0)
            self.assertEqual(manifest.receipt["pending"], 2)

    def test_execute_calls_import_then_run(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            manifest = execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.commands(), ["import-kihachi", "run"])
            import_argv = fake.calls[0]
            run_argv = fake.calls[1]
            self.assertEqual(import_argv[1:4], ["-m", "abletongpt.cli.jobs", "import-kihachi"])
            self.assertIn("--arrangement-plan", import_argv)
            self.assertIn("--out", import_argv)
            plan_arg = Path(import_argv[import_argv.index("--arrangement-plan") + 1])
            self.assertEqual(plan_arg, manifest.arrangement_plan_file)
            self.assertEqual(run_argv[1:4], ["-m", "abletongpt.cli.jobs", "run"])
            self.assertEqual(
                Path(run_argv[run_argv.index("--plan") + 1]),
                project / ABLETON_JOB_PLAN_NAME,
            )
            self.assertEqual(manifest.receipt["status"], "success")
            self.assertEqual(manifest.receipt["mode"], "execute")
            self.assertTrue(manifest.receipt["live_applied"])

    def test_no_handoff_refuses_before_subprocess(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            adopt_revision(project, 1, reason="adopt")
            with self.assertRaisesRegex(AbletonExecutionError, "No Ableton handoff"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_malformed_handoff_refuses(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            (project / "ableton_handoff.json").write_text("{not json\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonExecutionError, "not valid JSON"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_unsupported_handoff_version_refuses(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            payload = json.loads((project / "ableton_handoff.json").read_text())
            payload["ableton_handoff_version"] = "9.9"
            (project / "ableton_handoff.json").write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(AbletonExecutionError, "Unsupported"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_missing_arrangement_plan_refuses(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            plan = project.parent / "song-rev01" / "arrangement_plan.json"
            plan.unlink()
            with self.assertRaisesRegex(AbletonExecutionError, "Arrangement plan is missing"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_arrangement_sha_mismatch_refuses_before_subprocess(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            plan = project.parent / "song-rev01" / "arrangement_plan.json"
            plan.write_text(plan.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonExecutionError, "Arrangement plan SHA-256"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_revision_relative_paths_resolve_from_handoff(self) -> None:
        fake = FakeAbletonGPT()
        fake.fail_if_run = True
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            doc = json.loads((project / "ableton_handoff.json").read_text())
            self.assertEqual(doc["path_base"], ".")
            self.assertTrue(doc["arrangement_plan"]["path"].startswith("../song-rev01/"))
            validated = load_validated_handoff(project)
            self.assertEqual(validated.arrangement_plan_file.parent.name, "song-rev01")
            self.assertTrue(validated.arrangement_plan_file.is_file())
            prepare_ableton_execution(project, runner=fake)
            plan_arg = Path(fake.calls[0][fake.calls[0].index("--arrangement-plan") + 1])
            self.assertEqual(plan_arg, validated.arrangement_plan_file)

    def test_tampered_adopted_audio_refuses(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            audio = project.parent / "song-rev01" / "audio" / "ace-step-01.wav"
            audio.write_bytes(audio.read_bytes() + b"\x00tampered")
            with self.assertRaisesRegex(AbletonExecutionError, "Adopted audio SHA-256"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_tampered_managed_midi_refuses(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            midi = project.parent / "song-rev01" / "vocoder.mid"
            midi.write_bytes(midi.read_bytes() + b"\x00")
            with self.assertRaisesRegex(AbletonExecutionError, "Managed MIDI SHA-256"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_import_failure_does_not_run(self) -> None:
        fake = FakeAbletonGPT()
        fake.import_returncode = 2
        fake.import_stderr = "import-kihachi: invalid plan\n"
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            with self.assertRaisesRegex(AbletonExecutionError, "import-kihachi failed"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.commands(), ["import-kihachi"])
            receipt = json.loads((project / ABLETON_EXECUTION_NAME).read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertIsNone(receipt["run"])

    def test_missing_job_plan_does_not_run(self) -> None:
        fake = FakeAbletonGPT()
        fake.write_job_plan = False
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            with self.assertRaisesRegex(AbletonExecutionError, "did not produce a job plan"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.commands(), ["import-kihachi"])
            receipt = json.loads((project / ABLETON_EXECUTION_NAME).read_text())
            self.assertEqual(receipt["status"], "failed")

    def test_empty_job_plan_does_not_run(self) -> None:
        fake = FakeAbletonGPT()
        fake.job_plan = {"schema_version": 1, "name": "empty", "steps": []}
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            with self.assertRaisesRegex(AbletonExecutionError, "no steps"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.commands(), ["import-kihachi"])

    def test_run_failure_receipt_cannot_claim_success(self) -> None:
        fake = FakeAbletonGPT()
        fake.run_returncode = 1
        fake.run_stdout = "completed=1 failed=1 pending=0\n"
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            with self.assertRaisesRegex(AbletonExecutionError, "did not complete every Live step"):
                execute_ableton_handoff(project, runner=fake)
            receipt = json.loads((project / ABLETON_EXECUTION_NAME).read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["execution_state"], "failed")
            self.assertNotEqual(receipt["status"], "success")
            self.assertFalse(receipt["live_applied"])
            self.assertEqual(receipt["failed"], 1)

    def test_pending_steps_are_not_success(self) -> None:
        fake = FakeAbletonGPT()
        fake.run_returncode = 0
        fake.run_stdout = "completed=1 failed=0 pending=1\n"
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            with self.assertRaisesRegex(AbletonExecutionError, "pending=1"):
                execute_ableton_handoff(project, runner=fake)
            receipt = json.loads((project / ABLETON_EXECUTION_NAME).read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertFalse(receipt["live_applied"])
            self.assertEqual(receipt["pending"], 1)
            fake.calls.clear()
            fake.run_stdout = "completed=2 failed=0 pending=0\n"
            execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.commands(), ["import-kihachi", "run"])

    def test_completed_count_must_match_job_plan(self) -> None:
        fake = FakeAbletonGPT()
        fake.run_returncode = 0
        fake.run_stdout = "completed=1 failed=0 pending=0\n"
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            with self.assertRaisesRegex(AbletonExecutionError, "job plan steps=2"):
                execute_ableton_handoff(project, runner=fake)
            receipt = json.loads((project / ABLETON_EXECUTION_NAME).read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertFalse(receipt["live_applied"])

    def test_successful_run_records_handoff_and_plan_identity(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            handoff = project / "ableton_handoff.json"
            plan = project.parent / "song-rev01" / "arrangement_plan.json"
            manifest = execute_ableton_handoff(project, runner=fake)
            receipt = json.loads(manifest.receipt_file.read_text())
            self.assertEqual(receipt["source_handoff"]["sha256"], _sha256(handoff))
            self.assertEqual(receipt["arrangement_plan"]["sha256"], _sha256(plan))
            self.assertEqual(receipt["adopted_round"], 1)
            self.assertEqual(
                receipt["job_plan"]["sha256"],
                _sha256(project / ABLETON_JOB_PLAN_NAME),
            )
            self.assertEqual(receipt["import_kihachi"]["returncode"], 0)
            self.assertEqual(receipt["run"]["returncode"], 0)

    def test_repeated_successful_execution_refuses_without_rerun(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            execute_ableton_handoff(project, runner=fake)
            fake.calls.clear()
            with self.assertRaisesRegex(AbletonExecutionError, "already applied"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_explicit_rerun_permits_execution(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            first = execute_ableton_handoff(project, runner=fake)
            second = execute_ableton_handoff(project, rerun=True, runner=fake)
            self.assertEqual(fake.commands(), ["import-kihachi", "run", "import-kihachi", "run"])
            self.assertEqual(second.receipt["status"], "success")
            self.assertEqual(
                second.receipt["source_handoff"]["sha256"],
                first.receipt["source_handoff"]["sha256"],
            )

    def test_prepare_only_never_invokes_run(self) -> None:
        fake = FakeAbletonGPT()
        fake.fail_if_run = True
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 0)
            execute_ableton_handoff(project, prepare_only=True, runner=fake)
            execute_ableton_handoff(project, prepare_only=True, runner=fake)
            self.assertEqual(fake.commands(), ["import-kihachi", "import-kihachi"])
            self.assertNotIn("run", fake.commands())

    def test_execution_does_not_mutate_provenance_artifacts(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            rev01 = project.parent / "song-rev01"
            before_root = _artifact_fingerprints(project)
            before_rev = _artifact_fingerprints(rev01)
            before_handoff = (project / "ableton_handoff.json").read_bytes()
            before_plan = (rev01 / "arrangement_plan.json").read_bytes()
            before_adoption = json.loads((project / "revision_log.json").read_text())["adopted"]
            before_prefs = len(load_preference_memory(project).entries)
            execute_ableton_handoff(project, runner=fake)
            self.assertEqual(_artifact_fingerprints(project), before_root)
            self.assertEqual(_artifact_fingerprints(rev01), before_rev)
            self.assertEqual((project / "ableton_handoff.json").read_bytes(), before_handoff)
            self.assertEqual((rev01 / "arrangement_plan.json").read_bytes(), before_plan)
            after_adoption = json.loads((project / "revision_log.json").read_text())["adopted"]
            self.assertEqual(before_adoption, after_adoption)
            self.assertEqual(len(load_preference_memory(project).entries), before_prefs)
            self.assertEqual(load_revision_log(project).adopted.round, 1)

    def test_best_ranked_round_cannot_replace_adopted_round(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp), rounds=2)
            log = load_revision_log(project)
            best = log.ranked()[0].index
            chosen = next(round_.index for round_ in log.rounds if round_.index != best)
            adopt_revision(project, chosen, reason="not the ranked winner")
            build_ableton_handoff(project)
            manifest = execute_ableton_handoff(project, runner=fake)
            self.assertEqual(manifest.receipt["adopted_round"], chosen)
            self.assertNotEqual(manifest.receipt["adopted_round"], best)
            handoff = json.loads((project / "ableton_handoff.json").read_text())
            self.assertEqual(handoff["adopted_round"], chosen)

    def test_unready_execution_state_refuses(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            payload = json.loads((project / "ableton_handoff.json").read_text())
            payload["execution_state"] = "planned_not_applied"
            (project / "ableton_handoff.json").write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(AbletonExecutionError, "not ready to apply"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_path_escape_refuses_before_subprocess(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            with tempfile.TemporaryDirectory() as other:
                outside = Path(other) / "arrangement_plan.json"
                outside.write_text("{}\n", encoding="utf-8")
                payload = json.loads((project / "ableton_handoff.json").read_text())
                payload["arrangement_plan"]["path"] = str(outside)
                (project / "ableton_handoff.json").write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(AbletonExecutionError, "escapes"):
                    execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_missing_python_refuses_without_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            missing = Path(temp) / "no-such-abletongpt-python"
            with self.assertRaisesRegex(AbletonExecutionError, "interpreter not found"):
                execute_ableton_handoff(
                    project,
                    abletongpt_python=missing,
                    prepare_only=True,
                )
            receipt_path = ableton_execution_path(project)
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                self.assertNotEqual(receipt.get("status"), "success")

    def test_abletongpt_module_unavailable_is_actionable(self) -> None:
        fake = FakeAbletonGPT()
        fake.import_returncode = 1
        fake.import_stderr = "No module named abletongpt\n"
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            with self.assertRaisesRegex(AbletonExecutionError, "AbletonGPT is not available"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.commands(), ["import-kihachi"])

    def test_prepare_only_does_not_clear_successful_live_apply(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            execute_ableton_handoff(project, runner=fake)
            fake.fail_if_run = True
            prepare_ableton_execution(project, runner=fake)
            fake.fail_if_run = False
            fake.calls.clear()
            with self.assertRaisesRegex(AbletonExecutionError, "already applied"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_malformed_receipt_refuses_before_subprocess(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            (project / ABLETON_EXECUTION_NAME).write_text("{truncated\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonExecutionError, "not valid JSON"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_non_object_receipt_refuses_before_subprocess(self) -> None:
        fake = FakeAbletonGPT()
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            (project / ABLETON_EXECUTION_NAME).write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonExecutionError, "JSON object"):
                execute_ableton_handoff(project, runner=fake)
            self.assertEqual(fake.calls, [])

    def test_cli_parser_accepts_ableton_apply(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "ableton-apply",
                "projects/song",
                "--prepare-only",
                "--rerun",
                "--abletongpt-python",
                "/usr/bin/python3",
            ]
        )
        self.assertEqual(args.command, "ableton-apply")
        self.assertTrue(args.prepare_only)
        self.assertTrue(args.rerun)
        self.assertEqual(args.abletongpt_python, Path("/usr/bin/python3"))

    def test_cli_overwrite_is_rerun_alias(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["ableton-apply", "projects/song", "--overwrite"])
        self.assertTrue(args.rerun)

    def test_cli_prepare_only_integration(self) -> None:
        fake = FakeAbletonGPT()
        fake.fail_if_run = True
        with tempfile.TemporaryDirectory() as temp:
            project = self._handoff(Path(temp), 1)
            buffer = io.StringIO()
            with patch(
                "kihachi_music_ai.ableton_execution.run_command",
                fake,
            ), contextlib.redirect_stdout(buffer):
                status = main(["ableton-apply", str(project), "--prepare-only"])
            self.assertEqual(status, 0)
            text = buffer.getvalue()
            self.assertIn("Prepared Ableton execution", text)
            self.assertIn("Live job: not invoked", text)
            self.assertIn("Adopted round: 1", text)
            self.assertTrue((project / ABLETON_EXECUTION_NAME).is_file())
            self.assertEqual(load_revision_log(project).adopted.round, 1)

    def test_cli_refuses_without_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            adopt_revision(project, 1, reason="adopt")
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                status = main(["ableton-apply", str(project), "--prepare-only"])
            self.assertEqual(status, 2)
            self.assertIn("No Ableton handoff", buffer.getvalue())

    def test_cli_does_not_auto_adopt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            self.assertIsNone(load_revision_log(project).adopted)
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                status = main(["ableton-apply", str(project)])
            self.assertEqual(status, 2)
            self.assertIsNone(load_revision_log(project).adopted)


if __name__ == "__main__":
    unittest.main()
