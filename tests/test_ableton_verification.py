"""VS7 — Ableton Live Postcondition Audit."""

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

from kihachi_music_ai.ableton_execution import (
    ABLETON_EXECUTION_NAME,
    CommandResult,
    execute_ableton_handoff,
    prepare_ableton_execution,
)
from kihachi_music_ai.ableton_handoff import build_ableton_handoff
from kihachi_music_ai.ableton_verification import (
    ABLETON_VERIFICATION_NAME,
    CHECK_FAIL,
    CHECK_NOT_OBSERVABLE,
    CHECK_PASS,
    NOTE_TIME_TOLERANCE_BEATS,
    STATE_FAILED,
    STATE_NOT_RUN,
    STATE_PARTIAL,
    STATE_VERIFIED,
    TEMPO_TOLERANCE_BPM,
    AbletonVerificationError,
    build_expected_live_state,
    collect_via_abletongpt,
    load_verified_execution,
    verify_ableton_execution,
)
from kihachi_music_ai.cli import build_parser, main
from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.preference_memory import load_preference_memory
from kihachi_music_ai.project_artifacts import managed_midi_names
from kihachi_music_ai.revision import adopt_revision, load_revision_log, run_revision_loop
from test_ableton_execution import FakeAbletonGPT
from test_music_brain import EXAMPLE
from test_revision import TAKE_SECONDS, write_take
from kihachi_music_ai.pipeline import compose_project


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


def matching_evidence(expected: dict[str, object]) -> dict[str, object]:
    """AbletonGPT-shaped read-only snapshot that satisfies ``expected``."""

    first = int(expected["first_track_index"])
    total = int(expected["expected_track_count"])
    tracks: list[dict[str, object]] = [
        {"index": index, "name": f"Existing {index}", "clip_slots": 8}
        for index in range(total)
    ]
    for row in expected["tracks"]:
        index = int(row["index"])
        while len(tracks) <= index:
            tracks.append({"index": len(tracks), "name": "?", "clip_slots": 8})
        tracks[index] = {"index": index, "name": row["name"], "clip_slots": 8}

    devices: dict[str, object] = {}
    for device in expected["devices"]:
        index = str(int(device["track_index"]))
        devices[index] = [
            {
                "index": 0,
                "name": "Operator",
                "class_name": "Operator",
                "class_display_name": "Operator",
                "type": 1,
                "is_active": True,
            }
        ]

    session_clips: dict[str, object] = {}
    length_by_track: dict[int, float] = {}
    for clip in expected["clips"]:
        track_index = int(clip["track_index"])
        clip_index = int(clip["clip_index"])
        key = f"{track_index}:{clip_index}"
        notes = list(clip["notes"])
        session_clips[key] = {
            "track_index": track_index,
            "clip_index": clip_index,
            "clip": clip["name"],
            "length_beats": clip["length_beats"],
            "note_count": len(notes),
            "truncated": False,
            "notes": notes,
        }
        length_by_track[track_index] = float(clip["length_beats"])

    arrangement_clips: dict[str, object] = {}
    for target in expected["arrangement"]:
        track_index = int(target["track_index"])
        start = float(target["destination_time_beats"])
        length = length_by_track.get(track_index, 0.0)
        arrangement_clips[str(track_index)] = {
            "track_index": track_index,
            "track": target["name"],
            "clips": [
                {
                    "index": 0,
                    "name": target["name"],
                    "start_time": start,
                    "end_time": start + length,
                    "length_beats": length,
                    "is_midi_clip": True,
                    "is_audio_clip": False,
                    "muted": False,
                }
            ],
            "clip_count": 1,
            "truncated": False,
            "read_only": True,
        }

    return {
        "abletongpt_evidence_version": "0.1",
        "read_only": True,
        "ping": {"connected": True, "app": "Ableton Live"},
        "live_state": {"tempo": expected["tempo"], "tracks": tracks},
        "devices": devices,
        "session_clips": session_clips,
        "arrangement_clips": arrangement_clips,
        "arrangement_observable": True,
    }


class AbletonVerificationTests(unittest.TestCase):
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

    def _expected(self, project: Path) -> dict[str, object]:
        loaded = load_verified_execution(project)
        return build_expected_live_state(
            loaded.arrangement_plan, job_plan=loaded.job_plan
        )

    def _provider(self, evidence: dict[str, object]):
        def provider(request: dict[str, object]) -> dict[str, object]:
            self.assertTrue(request.get("read_only"))
            return copy.deepcopy(evidence)

        return provider

    def test_matching_live_evidence_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            manifest = verify_ableton_execution(
                project, provider=self._provider(matching_evidence(expected))
            )
            self.assertEqual(manifest.verification_state, STATE_VERIFIED)
            self.assertEqual(manifest.exit_code, 0)
            document = json.loads((project / ABLETON_VERIFICATION_NAME).read_text())
            self.assertEqual(document["verification_state"], STATE_VERIFIED)
            self.assertEqual(document["source"]["adopted_round"], 1)
            self.assertEqual(
                document["source"]["execution_receipt"]["sha256"],
                _sha256(project / ABLETON_EXECUTION_NAME),
            )
            self.assertFalse(document["boundary"]["kihachi_direct_live_access"])
            self.assertEqual(document["boundary"]["live_access"], "AbletonGPT")
            self.assertFalse(document["boundary"]["repair"])
            self.assertEqual(document["summary"]["failed"], 0)
            self.assertEqual(document["summary"]["not_observable"], 0)

    def test_prepare_only_receipt_refuses_before_live_read(self) -> None:
        calls: list[object] = []

        def provider(request: dict[str, object]) -> dict[str, object]:
            calls.append(request)
            raise AssertionError("Live evidence must not be requested")

        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            adopt_revision(project, 1, reason="adopt")
            build_ableton_handoff(project)
            prepare_ableton_execution(project, runner=FakeAbletonGPT())
            with self.assertRaisesRegex(AbletonVerificationError, "prepare-only"):
                verify_ableton_execution(project, provider=provider)
            self.assertEqual(calls, [])
            self.assertFalse((project / ABLETON_VERIFICATION_NAME).is_file())

    def test_failed_execution_receipt_refuses(self) -> None:
        fake = FakeAbletonGPT()
        fake.run_returncode = 1
        fake.run_stdout = "completed=1 failed=1 pending=0\n"
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            adopt_revision(project, 1, reason="adopt")
            build_ableton_handoff(project)
            with self.assertRaisesRegex(Exception, "records a failure"):
                execute_ableton_handoff(project, runner=fake)
            with self.assertRaisesRegex(AbletonVerificationError, "successful runner"):
                verify_ableton_execution(project, provider=self._provider({}))

    def test_missing_execution_receipt_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            adopt_revision(project, 1, reason="adopt")
            build_ableton_handoff(project)
            with self.assertRaisesRegex(AbletonVerificationError, "No Ableton execution receipt"):
                verify_ableton_execution(project, provider=self._provider({}))

    def test_malformed_receipt_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            (project / ABLETON_EXECUTION_NAME).write_text("{not json\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonVerificationError, "not valid JSON"):
                verify_ableton_execution(project, provider=self._provider({}))

    def test_stale_handoff_sha_refuses_before_live_read(self) -> None:
        calls: list[object] = []

        def provider(request: dict[str, object]) -> dict[str, object]:
            calls.append(request)
            raise AssertionError("Live read must not run")

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            handoff = json.loads((project / "ableton_handoff.json").read_text())
            handoff["adoption"]["reason"] = "tampered after apply"
            (project / "ableton_handoff.json").write_text(
                json.dumps(handoff, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(AbletonVerificationError, "Handoff SHA-256"):
                verify_ableton_execution(project, provider=provider)
            self.assertEqual(calls, [])

    def test_stale_arrangement_sha_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            plan = project.parent / "song-rev01" / "arrangement_plan.json"
            plan.write_text(plan.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                AbletonVerificationError, "Arrangement plan SHA-256"
            ):
                verify_ableton_execution(project, provider=self._provider({}))

    def test_stale_job_plan_sha_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            job = project / "ableton_job_plan.json"
            payload = json.loads(job.read_text(encoding="utf-8"))
            payload["name"] = "tampered"
            job.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AbletonVerificationError, "Job plan SHA-256"):
                verify_ableton_execution(project, provider=self._provider({}))

    def test_correct_bpm_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tempo"] = float(expected["tempo"]) + (
                TEMPO_TOLERANCE_BPM / 2
            )
            manifest = verify_ableton_execution(
                project, provider=self._provider(evidence)
            )
            tempo = next(check for check in manifest.checks if check["id"] == "tempo")
            self.assertEqual(tempo["status"], CHECK_PASS)

    def test_wrong_bpm_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tempo"] = 90.0
            manifest = verify_ableton_execution(
                project, provider=self._provider(evidence)
            )
            self.assertEqual(manifest.verification_state, STATE_FAILED)
            self.assertEqual(manifest.exit_code, 1)
            tempo = next(check for check in manifest.checks if check["id"] == "tempo")
            self.assertEqual(tempo["status"], CHECK_FAIL)
            self.assertIn("90", tempo["message"])

    def test_expected_track_at_expected_index_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            manifest = verify_ableton_execution(
                project, provider=self._provider(matching_evidence(expected))
            )
            track_checks = [check for check in manifest.checks if check["category"] == "tracks"]
            named = [check for check in track_checks if check["id"].startswith("track:")]
            self.assertTrue(named)
            self.assertTrue(all(check["status"] == CHECK_PASS for check in named))

    def test_same_name_at_wrong_index_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            tracks = list(evidence["live_state"]["tracks"])
            first = expected["tracks"][0]
            second = expected["tracks"][1]
            tracks[int(first["index"])] = {
                "index": int(first["index"]),
                "name": second["name"],
                "clip_slots": 8,
            }
            tracks[int(second["index"])] = {
                "index": int(second["index"]),
                "name": first["name"],
                "clip_slots": 8,
            }
            evidence["live_state"]["tracks"] = tracks
            manifest = verify_ableton_execution(
                project, provider=self._provider(evidence)
            )
            self.assertEqual(manifest.verification_state, STATE_FAILED)
            failed = [
                check
                for check in manifest.checks
                if check["id"] == f"track:{first['index']}"
            ][0]
            self.assertEqual(failed["status"], CHECK_FAIL)
            self.assertIn("authoritative", failed["message"])

    def test_missing_track_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tracks"] = evidence["live_state"]["tracks"][:1]
            manifest = verify_ableton_execution(
                project, provider=self._provider(evidence)
            )
            self.assertEqual(manifest.verification_state, STATE_FAILED)
            self.assertTrue(
                any(
                    check["status"] == CHECK_FAIL and check["category"] == "tracks"
                    for check in manifest.checks
                )
            )

    def test_expected_device_present_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            self.assertTrue(expected["devices"])
            manifest = verify_ableton_execution(
                project, provider=self._provider(matching_evidence(expected))
            )
            device_checks = [
                check for check in manifest.checks if check["category"] == "devices"
            ]
            self.assertTrue(device_checks)
            self.assertTrue(all(check["status"] == CHECK_PASS for check in device_checks))

    def test_expected_device_missing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            target = str(int(expected["devices"][0]["track_index"]))
            evidence["devices"][target] = []
            manifest = verify_ableton_execution(
                project, provider=self._provider(evidence)
            )
            self.assertEqual(manifest.verification_state, STATE_FAILED)
            failed = next(
                check
                for check in manifest.checks
                if check["id"] == f"device:{target}"
            )
            self.assertEqual(failed["status"], CHECK_FAIL)

    def test_midi_clip_match_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            self.assertTrue(expected["clips"])
            manifest = verify_ableton_execution(
                project, provider=self._provider(matching_evidence(expected))
            )
            clip_checks = [
                check for check in manifest.checks if check["category"] == "clips"
            ]
            self.assertTrue(clip_checks)
            self.assertTrue(all(check["status"] == CHECK_PASS for check in clip_checks))

    def test_midi_clip_note_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            first_clip = expected["clips"][0]
            key = f"{first_clip['track_index']}:{first_clip['clip_index']}"
            notes = copy.deepcopy(evidence["session_clips"][key]["notes"])
            notes[0]["pitch"] = (int(notes[0]["pitch"]) + 1) % 127
            evidence["session_clips"][key]["notes"] = notes
            manifest = verify_ableton_execution(
                project, provider=self._provider(evidence)
            )
            self.assertEqual(manifest.verification_state, STATE_FAILED)
            failed = next(
                check for check in manifest.checks if check["id"] == f"session_clip:{key}"
            )
            self.assertEqual(failed["status"], CHECK_FAIL)
            self.assertIn("mismatch", failed["message"])

    def test_float_timing_uses_explicit_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            first_clip = expected["clips"][0]
            key = f"{first_clip['track_index']}:{first_clip['clip_index']}"

            within = matching_evidence(expected)
            within["session_clips"][key]["notes"] = [
                {**note, "start_time": float(note["start_time"]) + NOTE_TIME_TOLERANCE_BEATS / 2}
                for note in within["session_clips"][key]["notes"]
            ]
            passed = verify_ableton_execution(project, provider=self._provider(within))
            clip_check = next(check for check in passed.checks if check["id"] == f"session_clip:{key}")
            self.assertEqual(clip_check["status"], CHECK_PASS)

            outside = matching_evidence(expected)
            outside["session_clips"][key]["notes"] = [
                {**note, "start_time": float(note["start_time"]) + NOTE_TIME_TOLERANCE_BEATS * 10}
                for note in outside["session_clips"][key]["notes"]
            ]
            failed = verify_ableton_execution(project, provider=self._provider(outside))
            clip_check = next(check for check in failed.checks if check["id"] == f"session_clip:{key}")
            self.assertEqual(clip_check["status"], CHECK_FAIL)
            self.assertIn(f"{NOTE_TIME_TOLERANCE_BEATS:g}", clip_check["message"])

    def test_unobservable_arrangement_is_partial_never_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            self.assertTrue(expected["arrangement"])
            evidence = matching_evidence(expected)
            del evidence["arrangement_clips"]
            evidence["arrangement_observable"] = False
            manifest = verify_ableton_execution(
                project, provider=self._provider(evidence)
            )
            self.assertEqual(manifest.verification_state, STATE_PARTIAL)
            self.assertEqual(manifest.exit_code, 2)
            self.assertNotEqual(manifest.verification_state, STATE_VERIFIED)
            arrangement = [
                check for check in manifest.checks if check["category"] == "arrangement"
            ]
            self.assertTrue(arrangement)
            self.assertTrue(
                all(check["status"] == CHECK_NOT_OBSERVABLE for check in arrangement)
            )

    def test_invalid_evidence_json_is_actionable(self) -> None:
        def runner(argv: list[str] | tuple[str, ...]) -> CommandResult:
            return CommandResult(tuple(str(part) for part in argv), 0, "{not json\n", "")

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            with self.assertRaisesRegex(AbletonVerificationError, "not valid JSON"):
                verify_ableton_execution(project, runner=runner)
            document = json.loads((project / ABLETON_VERIFICATION_NAME).read_text())
            self.assertEqual(document["verification_state"], STATE_NOT_RUN)

    def test_live_unavailable_does_not_fabricate_verification(self) -> None:
        def runner(argv: list[str] | tuple[str, ...]) -> CommandResult:
            return CommandResult(
                tuple(str(part) for part in argv),
                1,
                "",
                "Ableton Liveに接続できません。Liveを起動し、AbletonGPTをControl Surfaceに選択してください。\n",
            )

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            with self.assertRaisesRegex(AbletonVerificationError, "unreachable"):
                verify_ableton_execution(project, runner=runner)
            document = json.loads((project / ABLETON_VERIFICATION_NAME).read_text())
            self.assertEqual(document["verification_state"], STATE_NOT_RUN)
            self.assertNotEqual(document["verification_state"], STATE_VERIFIED)
            self.assertIsNone(document.get("observed"))

    def test_abletongpt_unavailable_is_not_run(self) -> None:
        def runner(argv: list[str] | tuple[str, ...]) -> CommandResult:
            return CommandResult(
                tuple(str(part) for part in argv),
                1,
                "",
                "No module named abletongpt\n",
            )

        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            with self.assertRaisesRegex(AbletonVerificationError, "AbletonGPT is not available"):
                verify_ableton_execution(project, runner=runner)

    def test_unsupported_evidence_schema_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            with self.assertRaisesRegex(AbletonVerificationError, "Unsupported AbletonGPT evidence schema"):
                verify_ableton_execution(
                    project,
                    provider=self._provider(
                        {
                            "abletongpt_evidence_version": "9.9",
                            "read_only": True,
                            "live_state": {"tempo": 110, "tracks": []},
                        }
                    ),
                )

    def test_verification_does_not_mutate_vs1_to_vs6_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            rev01 = project.parent / "song-rev01"
            before_root = _artifact_fingerprints(project)
            before_rev = _artifact_fingerprints(rev01)
            before_handoff = (project / "ableton_handoff.json").read_bytes()
            before_execution = (project / ABLETON_EXECUTION_NAME).read_bytes()
            before_plan = (rev01 / "arrangement_plan.json").read_bytes()
            before_job = (project / "ableton_job_plan.json").read_bytes()
            expected = self._expected(project)
            verify_ableton_execution(
                project, provider=self._provider(matching_evidence(expected))
            )
            self.assertEqual(_artifact_fingerprints(project), before_root)
            self.assertEqual(_artifact_fingerprints(rev01), before_rev)
            self.assertEqual((project / "ableton_handoff.json").read_bytes(), before_handoff)
            self.assertEqual((project / ABLETON_EXECUTION_NAME).read_bytes(), before_execution)
            self.assertEqual((rev01 / "arrangement_plan.json").read_bytes(), before_plan)
            self.assertEqual((project / "ableton_job_plan.json").read_bytes(), before_job)

    def test_verification_does_not_alter_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            before = json.loads((project / "revision_log.json").read_text())["adopted"]
            expected = self._expected(project)
            verify_ableton_execution(
                project, provider=self._provider(matching_evidence(expected))
            )
            after = json.loads((project / "revision_log.json").read_text())["adopted"]
            self.assertEqual(before, after)
            self.assertEqual(load_revision_log(project).adopted.round, 1)

    def test_verification_does_not_append_preference_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            before = len(load_preference_memory(project).entries)
            expected = self._expected(project)
            verify_ableton_execution(
                project, provider=self._provider(matching_evidence(expected))
            )
            self.assertEqual(len(load_preference_memory(project).entries), before)

    def test_ranking_cannot_change_verification_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp), rounds=2)
            log = load_revision_log(project)
            best = log.ranked()[0].index
            chosen = next(round_.index for round_ in log.rounds if round_.index != best)
            adopt_revision(project, chosen, reason="not the ranked winner")
            build_ableton_handoff(project)
            execute_ableton_handoff(project, runner=FakeAbletonGPT())
            expected = self._expected(project)
            manifest = verify_ableton_execution(
                project, provider=self._provider(matching_evidence(expected))
            )
            self.assertEqual(manifest.document["source"]["adopted_round"], chosen)
            self.assertNotEqual(manifest.document["source"]["adopted_round"], best)
            handoff = json.loads((project / "ableton_handoff.json").read_text())
            self.assertEqual(handoff["adopted_round"], chosen)

    def test_repeated_read_only_verification_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            first = verify_ableton_execution(
                project, provider=self._provider(matching_evidence(expected))
            )
            second = verify_ableton_execution(
                project, provider=self._provider(matching_evidence(expected))
            )
            self.assertEqual(first.verification_state, STATE_VERIFIED)
            self.assertEqual(second.verification_state, STATE_VERIFIED)
            document = json.loads((project / ABLETON_VERIFICATION_NAME).read_text())
            self.assertEqual(
                document["source"]["execution_receipt"]["sha256"],
                first.document["source"]["execution_receipt"]["sha256"],
            )

    def test_cli_parser_accepts_ableton_verify(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "ableton-verify",
                "projects/song",
                "--abletongpt-python",
                "/usr/bin/python3",
            ]
        )
        self.assertEqual(args.command, "ableton-verify")
        self.assertEqual(args.abletongpt_python, Path("/usr/bin/python3"))

    def test_cli_verified_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            buffer = io.StringIO()
            with patch(
                "kihachi_music_ai.ableton_verification.collect_live_evidence",
                lambda request, **kwargs: matching_evidence(expected),
            ), contextlib.redirect_stdout(buffer):
                status = main(["ableton-verify", str(project)])
            self.assertEqual(status, 0)
            text = buffer.getvalue()
            self.assertIn("VERIFIED", text)
            self.assertIn("Adopted round: 1", text)
            self.assertIn("ableton_verification.json", text)

    def test_cli_mismatch_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            evidence["live_state"]["tempo"] = 72.0
            buffer = io.StringIO()
            with patch(
                "kihachi_music_ai.ableton_verification.collect_live_evidence",
                lambda request, **kwargs: evidence,
            ), contextlib.redirect_stdout(buffer):
                status = main(["ableton-verify", str(project)])
            self.assertEqual(status, 1)
            self.assertIn("FAILED", buffer.getvalue())

    def test_cli_partial_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._applied(Path(temp))
            expected = self._expected(project)
            evidence = matching_evidence(expected)
            del evidence["arrangement_clips"]
            evidence["arrangement_observable"] = False
            buffer = io.StringIO()
            with patch(
                "kihachi_music_ai.ableton_verification.collect_live_evidence",
                lambda request, **kwargs: evidence,
            ), contextlib.redirect_stdout(buffer):
                status = main(["ableton-verify", str(project)])
            self.assertEqual(status, 2)
            self.assertIn("PARTIALLY VERIFIED", buffer.getvalue())
            self.assertIn("NOT OBSERVABLE", buffer.getvalue())

    def test_cli_unavailable_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self._project_with_revisions(Path(temp))
            adopt_revision(project, 1, reason="adopt")
            build_ableton_handoff(project)
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                status = main(["ableton-verify", str(project)])
            self.assertEqual(status, 2)
            self.assertIn("No Ableton execution receipt", buffer.getvalue())

    def test_collect_via_abletongpt_invokes_python_c_not_shell(self) -> None:
        captured: list[list[str]] = []

        def runner(argv: list[str] | tuple[str, ...]) -> CommandResult:
            argv_list = [str(part) for part in argv]
            captured.append(argv_list)
            return CommandResult(
                tuple(argv_list),
                0,
                json.dumps(
                    {
                        "abletongpt_evidence_version": "0.1",
                        "read_only": True,
                        "live_state": {"tempo": 110, "tracks": []},
                    }
                ),
                "",
            )

        payload = collect_via_abletongpt(
            {"read_only": True, "device_indices": [], "session_clips": [], "arrangement_indices": []},
            runner=runner,
        )
        self.assertEqual(payload["read_only"], True)
        self.assertEqual(len(captured), 1)
        argv = captured[0]
        self.assertEqual(argv[1], "-c")
        self.assertNotIn("shell", " ".join(argv).lower())
        self.assertIn("get_state", argv[2])
        self.assertIn("get_track_devices", argv[2])
        self.assertIn("get_midi_clip_notes", argv[2])
        self.assertIn("get_arrangement_clips", argv[2])


if __name__ == "__main__":
    unittest.main()
