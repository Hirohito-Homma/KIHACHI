"""Every flag the command line accepts, and nothing about what they do.

Keeping the parser apart from the command bodies is what lets a command be
called with a plain ``argparse.Namespace`` in a test, instead of through
``main`` and a list of strings.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ..adapters.ace_step import AUDIO_FORMATS, DEFAULT_REQUEST_TIMEOUT
from ..adapters.intent_llm import DEFAULT_MODEL as INTENT_DEFAULT_MODEL
from ..chunked import DEFAULT_CHUNK_BARS
from ..revision import DEFAULT_ROUNDS
from ..select import SHORTLIST_NAME
from ..stems import DEFAULT_MODEL as DEFAULT_STEM_MODEL
from ..tail_guard import DEFAULT_TAIL_GUARD_BARS, MUSIC_END_THRESHOLD_DBFS
from ..tail_trim import DEFAULT_TAIL_PAD_SEC
from ..web import DEFAULT_HOST as WEB_DEFAULT_HOST, DEFAULT_PORT as WEB_DEFAULT_PORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kihachi", description="KIHACHI Music AI v0.1")
    subparsers = parser.add_subparsers(dest="command", required=True)
    compose = subparsers.add_parser("compose", help="generate SongSpec, MIDI, and an audio prompt")
    compose.add_argument("prompt", help="natural-language music brief")
    compose.add_argument("--output", type=Path, help="output directory")
    compose.add_argument("--seed", type=int, default=8, help="deterministic composition seed")
    compose.add_argument("--overwrite", action="store_true", help="replace only the five known artifacts")
    compose.add_argument(
        "--preferences",
        type=Path,
        help=(
            "apply learned priors from a `learn` output. Off by default: the same "
            "prompt and seed must keep producing the same song unless asked otherwise"
        ),
    )

    local_slice = subparsers.add_parser(
        "local-slice",
        help=(
            "compose from a brief and run the local MIDI review + critic path "
            "(no ACE-Step, GPU, Ableton, or LLM required)"
        ),
    )
    local_slice.add_argument("prompt", help="natural-language music brief")
    local_slice.add_argument("--output", type=Path, help="output directory")
    local_slice.add_argument("--seed", type=int, default=8, help="deterministic composition seed")
    local_slice.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing compose and review artifacts",
    )
    local_slice.add_argument(
        "--preferences",
        type=Path,
        help=(
            "apply learned priors from a `learn` output. Off by default: the same "
            "prompt and seed must keep producing the same song unless asked otherwise"
        ),
    )

    audio_slice = subparsers.add_parser(
        "audio-slice",
        help=(
            "compose from a brief, render through ACE-Step, analyze, and run the "
            "audio-aware review + critic path (requires an ACE-Step endpoint)"
        ),
    )
    audio_slice.add_argument("prompt", help="natural-language music brief")
    audio_slice.add_argument("--output", type=Path, help="output directory")
    audio_slice.add_argument("--seed", type=int, default=8, help="deterministic composition seed")
    audio_slice.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing compose, render, analysis, and review artifacts",
    )
    audio_slice.add_argument(
        "--preferences",
        type=Path,
        help=(
            "apply learned priors from a `learn` output. Off by default: the same "
            "prompt and seed must keep producing the same song unless asked otherwise"
        ),
    )
    audio_slice.add_argument(
        "--no-lyrics",
        action="store_true",
        help="render instrumental, ignoring the project's lyrics.txt",
    )
    audio_slice.add_argument(
        "--tail-guard-bars",
        type=float,
        default=None,
        help=(
            "extra bars of render buffer past the song grid; defaults to "
            f"{DEFAULT_TAIL_GUARD_BARS} for text2music. Pass 0 to disable"
        ),
    )
    add_ace_connection_arguments(audio_slice)
    audio_slice.add_argument("--wait-timeout", type=float, default=600.0)
    audio_slice.add_argument("--poll-interval", type=float, default=2.0)

    generate_and_revise = subparsers.add_parser(
        "generate-and-revise",
        help=(
            "compose, render through ACE-Step, analyze, review, then run the "
            "audio revision loop on the same project"
        ),
    )
    generate_and_revise.add_argument("prompt", help="natural-language music brief")
    generate_and_revise.add_argument("--output", type=Path, help="output directory")
    generate_and_revise.add_argument("--seed", type=int, default=8, help="deterministic composition seed")
    generate_and_revise.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing compose, render, analysis, and review artifacts",
    )
    generate_and_revise.add_argument(
        "--preferences",
        type=Path,
        help=(
            "apply learned priors from a `learn` output. Off by default: the same "
            "prompt and seed must keep producing the same song unless asked otherwise"
        ),
    )
    generate_and_revise.add_argument(
        "--no-lyrics",
        action="store_true",
        help="render instrumental, ignoring the project's lyrics.txt",
    )
    generate_and_revise.add_argument(
        "--tail-guard-bars",
        type=float,
        default=None,
        help=(
            "extra bars of render buffer past the song grid; defaults to "
            f"{DEFAULT_TAIL_GUARD_BARS} for text2music. Pass 0 to disable"
        ),
    )
    generate_and_revise.add_argument(
        "--rounds", type=int, default=DEFAULT_ROUNDS, help="maximum repaint rounds"
    )
    generate_and_revise.add_argument(
        "--resume",
        action="store_true",
        help=(
            "continue a revision run that stopped part-way: a -revNN project that "
            "already has audio is measured rather than rendered again"
        ),
    )
    add_ace_connection_arguments(generate_and_revise)
    generate_and_revise.add_argument("--wait-timeout", type=float, default=1800.0)
    generate_and_revise.add_argument("--poll-interval", type=float, default=5.0)
    generate_and_revise.add_argument(
        "--revision-log-markdown",
        type=Path,
        help="write a markdown summary of the revision log to this path",
    )

    serve = subparsers.add_parser(
        "serve", help="open the brief screen in a local browser tab (writes nothing)"
    )
    serve.add_argument("--host", default=WEB_DEFAULT_HOST, help="interface to bind")
    serve.add_argument("--port", type=int, default=WEB_DEFAULT_PORT, help="port to bind")

    analyze = subparsers.add_parser("analyze", help="analyze generated WAV and compare it with SongSpec")
    analyze.add_argument("project", type=Path, help="directory containing song_spec.json")
    analyze.add_argument("--audio", type=Path, help="WAV path, relative to the project unless absolute")
    analyze.add_argument(
        "--loudness",
        action="store_true",
        help=(
            "also measure integrated loudness (ITU-R BS.1770-4). Off by default: "
            "it filters every sample, which is ~11 s for a 70 s take and ~49 s "
            "for a five-minute one"
        ),
    )
    analyze.add_argument("--overwrite", action="store_true", help="replace only audio_analysis.json")

    review = subparsers.add_parser("review", help="turn audio analysis into a non-destructive revision plan")
    review.add_argument("project", type=Path, help="project containing SongSpec and audio_analysis.json")
    review.add_argument("--against", type=Path, help="optional baseline project for alignment comparison")
    review.add_argument(
        "--overwrite",
        action="store_true",
        help="replace only generation_review.json, repaint_plan.json, and revision_prompt.txt",
    )
    review.add_argument(
        "--preserve-revision-prompt",
        action="store_true",
        help="write the review without replacing an authored revision_prompt.txt",
    )
    review.add_argument(
        "--tail-guard-bars",
        type=float,
        default=DEFAULT_TAIL_GUARD_BARS,
        help=(
            "bars of render headroom the planned repaint should request past the song "
            "grid so ACE-Step writes its ending outside the scored bars (0 disables)"
        ),
    )
    review.add_argument(
        "--prefer-bar-level",
        action="store_true",
        help="plan the narrow bar window over the whole section when the defect is localized",
    )

    edit = subparsers.add_parser(
        "edit",
        help="plan a difference instruction as a reviewable Spec Diff (writes no MIDI)",
    )
    edit.add_argument("project", type=Path, help="project containing song_spec.json")
    edit.add_argument("instruction", help='e.g. "Dropのベースだけもっと変態的に"')
    edit.add_argument(
        "--overwrite", action="store_true", help="replace only spec_edit.json"
    )

    apply_edit = subparsers.add_parser(
        "apply-edit",
        help="write a new project with a planned Spec Diff applied",
    )
    apply_edit.add_argument("source_project", type=Path)
    apply_edit.add_argument("output_project", type=Path)
    apply_edit.add_argument(
        "--spec-edit",
        type=Path,
        default=Path("spec_edit.json"),
        help="planned edit, relative to the source project unless absolute",
    )

    plan_chunks = subparsers.add_parser(
        "plan-chunks",
        help="split a long song into section-aligned render chunks (writes no audio)",
    )
    plan_chunks.add_argument("project", type=Path, help="project containing song_spec.json")
    plan_chunks.add_argument(
        "--chunk-bars",
        type=int,
        default=DEFAULT_CHUNK_BARS,
        help="target bars per chunk; whole sections are never split",
    )
    plan_chunks.add_argument(
        "--tail-guard-bars", type=float, default=DEFAULT_TAIL_GUARD_BARS
    )
    plan_chunks.add_argument("--overwrite", action="store_true", help="replace chunk_plan.json")

    learn_command = subparsers.add_parser(
        "learn",
        help="compile the edits already applied under a projects directory into "
             "a preferences file (reads only; never changes a project)",
    )
    learn_command.add_argument(
        "projects", type=Path, help="directory holding the project folders"
    )
    learn_command.add_argument(
        "--out", type=Path, default=Path("preferences.json"),
        help="where to write the compiled priors",
    )
    learn_command.add_argument("--overwrite", action="store_true")

    lyrics_command = subparsers.add_parser(
        "lyrics",
        help="show the lyric sheet a project's SongSpec writes (read-only)",
    )
    lyrics_command.add_argument("project", type=Path, help="project containing song_spec.json")

    ableton_plan = subparsers.add_parser(
        "ableton-plan",
        help="emit the operation list that lays this song out in Live (talks to nothing)",
    )
    ableton_plan.add_argument("project", type=Path, help="project containing song_spec.json and .mid")
    ableton_plan.add_argument(
        "--first-track-index",
        type=int,
        default=0,
        help="Live index the first created track lands on; check it with get_live_state",
    )
    ableton_plan.add_argument(
        "--session-slot",
        type=int,
        default=0,
        help="empty Session slot the clips are built in before being copied",
    )
    ableton_plan.add_argument(
        "--automate",
        action="append",
        default=[],
        metavar="BINDING",
        help=(
            "bind a per-section SongSpec field to a Live device parameter as "
            "part:field:device_index:parameter_index[:low:high], e.g. "
            "chords:fx_amount:1:52:0.18:0.52 (repeatable). The indices come from "
            "get_track_devices, and so do low/high: they are in that parameter's "
            "own units (its min/max), not a normalised 0..1, and they keep the "
            "musical curve off the parameter's extremes"
        ),
    )
    ableton_plan.add_argument(
        "--split-drums",
        action="store_true",
        help=(
            "lay the one composed drum part out as three Live tracks -- kick, "
            "drums and percussion -- as the 12-track layout asks for. The MIDI "
            "file stays one channel-10 track either way"
        ),
    )
    ableton_plan.add_argument(
        "--reference-audio",
        type=Path,
        nargs="?",
        const=Path("audio/ace-step-01.wav"),
        help=(
            "bring a rendered take in as an audio track to write against; with no "
            "value, the project's own audio/ace-step-01.wav"
        ),
    )
    ableton_plan.add_argument("--vocal-audio", type=Path, help="import a recorded vocal take")
    ableton_plan.add_argument(
        "--send",
        action="append",
        default=[],
        metavar="BINDING",
        help=(
            "route a part to a return as part:send_index[:low:high], e.g. "
            "chords:1:0.1:0.6 (repeatable). Send 0 is return A, 1 is return B; "
            "get_mix_snapshot reports their names. SongSpec fx_amount becomes "
            "one Send Envelope step per section"
        ),
    )
    ableton_plan.add_argument(
        "--fx-track",
        action="store_true",
        help="add an empty audio track for FX; the devices on it are AbletonGPT's job",
    )
    ableton_plan.add_argument("--overwrite", action="store_true", help="replace arrangement_plan.json")

    revise = subparsers.add_parser(
        "revise",
        help="measure, repaint, re-render and measure again, keeping every take",
    )
    revise.add_argument("project", type=Path, help="a project whose audio has been rendered")
    revise.add_argument(
        "--rounds", type=int, default=DEFAULT_ROUNDS, help="maximum repaint rounds"
    )
    revise.add_argument(
        "--resume",
        action="store_true",
        help=(
            "continue a run that stopped part-way: a -revNN project that already "
            "has audio is measured rather than rendered again"
        ),
    )
    add_ace_connection_arguments(revise)
    revise.add_argument("--wait-timeout", type=float, default=1800.0)
    revise.add_argument("--poll-interval", type=float, default=5.0)
    revise.add_argument(
        "--dry-run",
        action="store_true",
        help="measure and report what the first round would repaint, rendering nothing",
    )
    revise.add_argument(
        "--revision-log-markdown",
        type=Path,
        help="write a markdown summary of the revision log to this path",
    )

    revisions = subparsers.add_parser(
        "revisions",
        help="inspect revision candidates and adoption state (adopts nothing)",
    )
    revisions.add_argument("project", type=Path, help="a project with revision_log.json")

    adopt = subparsers.add_parser(
        "adopt",
        help="explicitly adopt one existing revision take (human selection only)",
    )
    adopt.add_argument("project", type=Path, help="source project that owns revision_log.json")
    adopt.add_argument(
        "--round",
        type=int,
        required=True,
        dest="round_number",
        help="revision round index to adopt (from revision_log.json)",
    )
    adopt.add_argument(
        "--reason",
        help="optional human reason; stored as preference evidence only",
    )
    adopt.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="optional descriptive tag (repeatable); evidence only, never scoring",
    )

    ableton_handoff = subparsers.add_parser(
        "ableton-handoff",
        help=(
            "build a durable Ableton handoff from the explicitly human-adopted take "
            "(adopts nothing; talks to Live about nothing)"
        ),
    )
    ableton_handoff.add_argument(
        "project",
        type=Path,
        help="source project that owns revision_log.json with an adopted round",
    )
    ableton_handoff.add_argument(
        "--first-track-index",
        type=int,
        default=0,
        help="Live index the first created track lands on; check it with get_live_state",
    )
    ableton_handoff.add_argument(
        "--session-slot",
        type=int,
        default=0,
        help="empty Session slot the clips are built in before being copied",
    )
    ableton_handoff.add_argument(
        "--automate",
        action="append",
        default=[],
        metavar="BINDING",
        help=(
            "bind a per-section SongSpec field to a Live device parameter as "
            "part:field:device_index:parameter_index[:low:high] (repeatable)"
        ),
    )
    ableton_handoff.add_argument(
        "--split-drums",
        action="store_true",
        help="lay the composed drum part out as kick / drums / percussion tracks",
    )
    ableton_handoff.add_argument(
        "--send",
        action="append",
        default=[],
        metavar="BINDING",
        help="route a part to a return as part:send_index[:low:high] (repeatable)",
    )
    ableton_handoff.add_argument(
        "--overwrite",
        action="store_true",
        help="replace ableton_handoff.json / arrangement_plan.json when provenance differs",
    )

    ableton_apply = subparsers.add_parser(
        "ableton-apply",
        help=(
            "apply an existing Ableton handoff through AbletonGPT "
            "(adopts nothing; KIHACHI does not talk to Live)"
        ),
    )
    ableton_apply.add_argument(
        "project",
        type=Path,
        help="source project that owns ableton_handoff.json",
    )
    ableton_apply.add_argument(
        "--prepare-only",
        action="store_true",
        help="validate and run AbletonGPT import-kihachi only; do not run the Live job",
    )
    ableton_apply.add_argument(
        "--rerun",
        "--overwrite",
        action="store_true",
        dest="rerun",
        help=(
            "explicitly permit another Live execution of the same successful handoff "
            "(without this, a successful identical apply is refused)"
        ),
    )
    ableton_apply.add_argument(
        "--abletongpt-python",
        type=Path,
        help=(
            "Python interpreter that has AbletonGPT installed "
            "(default: the interpreter running KIHACHI)"
        ),
    )

    ableton_verify = subparsers.add_parser(
        "ableton-verify",
        help=(
            "read-only Live postcondition audit through AbletonGPT "
            "(compares the applied plan with observed Live state; repairs nothing)"
        ),
    )
    ableton_verify.add_argument(
        "project",
        type=Path,
        help="source project that owns ableton_handoff.json and ableton_execution.json",
    )
    ableton_verify.add_argument(
        "--abletongpt-python",
        type=Path,
        help=(
            "Python interpreter that has AbletonGPT installed "
            "(default: the interpreter running KIHACHI)"
        ),
    )

    ableton_repair_plan = subparsers.add_parser(
        "ableton-repair-plan",
        help=(
            "turn a Live verification failure into a human-gated repair plan "
            "(does not talk to Live, does not invoke AbletonGPT, repairs nothing)"
        ),
    )
    ableton_repair_plan.add_argument(
        "project",
        type=Path,
        help="source project that owns ableton_verification.json",
    )
    ableton_repair_plan.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "replace ableton_repair_plan.json when the existing plan differs "
            "(source artifacts stay unchanged)"
        ),
    )

    ableton_repair_apply = subparsers.add_parser(
        "ableton-repair-apply",
        help=(
            "apply one human-authorized tempo or guarded device repair "
            "through AbletonGPT (does not auto-verify; Live repair remains "
            "unverified)"
        ),
    )
    ableton_repair_apply.add_argument(
        "project",
        type=Path,
        help="source project that owns ableton_repair_plan.json",
    )
    ableton_repair_apply.add_argument(
        "--check-id",
        required=True,
        help="candidate check to execute (tempo, or a supported device:N power repair)",
    )
    ableton_repair_apply.add_argument(
        "--prepare-only",
        action="store_true",
        help=(
            "validate provenance and derive the authorized repair; do not "
            "read Live or mutate a Set"
        ),
    )
    ableton_repair_apply.add_argument(
        "--approve-plan-sha",
        dest="approve_plan_sha",
        help=(
            "full 64-character lowercase SHA-256 of the current "
            "ableton_repair_plan.json; required to run the Live job"
        ),
    )
    ableton_repair_apply.add_argument(
        "--rerun",
        action="store_true",
        help=(
            "explicitly permit another execute of the same successful repair "
            "plan and check (still requires --approve-plan-sha and Live preflight)"
        ),
    )
    ableton_repair_apply.add_argument(
        "--abletongpt-python",
        type=Path,
        help=(
            "Python interpreter that has AbletonGPT installed "
            "(default: the interpreter running KIHACHI)"
        ),
    )

    ableton_repair_verify = subparsers.add_parser(
        "ableton-repair-verify",
        help=(
            "explicit read-only Live observation that closes only the selected "
            "repair check (does not mutate Live, retry, replan, or adopt)"
        ),
    )
    ableton_repair_verify.add_argument(
        "project",
        type=Path,
        help="source project that owns ableton_repair_execution.json",
    )
    ableton_repair_verify.add_argument(
        "--abletongpt-python",
        type=Path,
        help=(
            "Python interpreter that has AbletonGPT installed "
            "(default: the interpreter running KIHACHI)"
        ),
    )

    report = subparsers.add_parser(
        "report",
        help="write a page for comparing takes by ear (renders nothing, adopts nothing)",
    )
    report.add_argument("project", type=Path, help="a reviewed project")
    report.add_argument(
        "--also",
        type=Path,
        action="append",
        default=[],
        help="another reviewed project to compare against (repeatable)",
    )
    report.add_argument(
        "--from-revision-log",
        action="store_true",
        help="include every take the project's revision_log.json recorded",
    )
    report.add_argument("--output", type=Path, help="page path; defaults to candidates.html")
    report.add_argument("--overwrite", action="store_true")

    cut_sample = subparsers.add_parser(
        "cut-sample",
        help="cut bar-aligned material out of a render (never replaces it)",
    )
    cut_sample.add_argument("project", type=Path, help="project holding the render")
    cut_sample.add_argument(
        "--bars",
        required=True,
        metavar="START:END",
        help=(
            "1-based bar window, end exclusive, e.g. 9:13 for four bars from bar 9. "
            "Take the middle: the opening ramp and the tail the guard cannot reach "
            "are where the model is least steady"
        ),
    )
    cut_sample.add_argument("--name", required=True, help="sample file name, without suffix")
    cut_sample.add_argument(
        "--audio",
        type=Path,
        help="render to cut from, relative to the project; defaults to audio/ace-step-01.wav",
    )
    cut_sample.add_argument("--overwrite", action="store_true")

    intent = subparsers.add_parser(
        "intent",
        help="translate a brief into the brain's vocabulary with a model (ADR-0011)",
    )
    intent_commands = intent.add_subparsers(dest="intent_command", required=True)

    intent_prepare = intent_commands.add_parser(
        "prepare",
        help="write exactly what would be sent, without a network call or a key",
    )
    intent_prepare.add_argument("prompt", help="the brief to translate")
    intent_prepare.add_argument(
        "--model", default=INTENT_DEFAULT_MODEL, help="model to address the request to"
    )

    intent_read = intent_commands.add_parser(
        "read",
        help="ask the model and write intent_reading.json (needs ANTHROPIC_API_KEY)",
    )
    intent_read.add_argument("prompt", help="the brief to translate")
    intent_read.add_argument(
        "--output", type=Path, help="project directory to write the reading into"
    )
    intent_read.add_argument("--model", default=INTENT_DEFAULT_MODEL)
    intent_read.add_argument("--overwrite", action="store_true")

    read_brief = subparsers.add_parser(
        "read-brief",
        help="show which statements in a brief this vocabulary acts on",
    )
    read_brief.add_argument("prompt", help="the brief, as you would pass it to compose")

    compare_readings = subparsers.add_parser(
        "compare-readings",
        help=(
            "compare a stored intent_reading.json with what the rules read in "
            "the same brief (calls nothing, decides nothing)"
        ),
    )
    compare_readings.add_argument(
        "reading",
        type=Path,
        help=(
            "an intent_reading.json, or the project directory holding one. The "
            "brief travels inside it, so no key and no second call are needed"
        ),
    )

    transcribe_sample = subparsers.add_parser(
        "transcribe-sample",
        help="read a monophonic sample into MIDI notes (writes a .mid beside it)",
    )
    transcribe_sample.add_argument(
        "project", type=Path, help="project holding sample_manifest.json"
    )
    transcribe_sample.add_argument("--name", required=True, help="sample to transcribe")
    transcribe_sample.add_argument("--overwrite", action="store_true")

    audit_transcription = subparsers.add_parser(
        "audit-transcription",
        help="verify a transcription's WAV, MIDI, hashes, and note count (reads only)",
    )
    audit_transcription.add_argument(
        "project", type=Path, help="project holding sample_manifest.json"
    )
    audit_selection = audit_transcription.add_mutually_exclusive_group(required=True)
    audit_selection.add_argument("--name", help="one transcribed sample to verify")
    audit_selection.add_argument(
        "--all",
        action="store_true",
        help="verify every existing transcription; report untranscribed samples as skipped",
    )

    review_samples = subparsers.add_parser(
        "review-samples",
        help="rank a project's cut samples as material (reads only)",
    )
    review_samples.add_argument(
        "project", type=Path, help="project holding sample_manifest.json"
    )
    review_samples.add_argument(
        "--also",
        type=Path,
        action="append",
        default=[],
        help="another project whose samples join the ranking (repeatable)",
    )

    shortlist = subparsers.add_parser(
        "shortlist",
        help="rank takes on what measurably separates them (adopts nothing)",
    )
    shortlist.add_argument("project", type=Path, help="a reviewed project")
    shortlist.add_argument(
        "--also",
        type=Path,
        action="append",
        default=[],
        help="another reviewed candidate to rank alongside it (repeatable)",
    )
    shortlist.add_argument(
        "--from-revision-log",
        action="store_true",
        help="rank every take the project's revision_log.json recorded",
    )
    shortlist.add_argument(
        "--save",
        action="store_true",
        help=f"write the ranking to {SHORTLIST_NAME}",
    )
    shortlist.add_argument("--overwrite", action="store_true")

    decide = subparsers.add_parser(
        "decide",
        help="record a human listening decision without moving or replacing audio",
    )
    decide.add_argument("project", type=Path, help="base project that owns the decision log")
    decide.add_argument(
        "--selected",
        type=Path,
        required=True,
        help="chosen project; must be the base project or one supplied with --also",
    )
    decide.add_argument(
        "--also",
        type=Path,
        action="append",
        default=[],
        help="another reviewed candidate considered in the listening decision (repeatable)",
    )
    decide.add_argument(
        "--reason",
        required=True,
        help="human reason for the choice; stored verbatim in decision_log.json",
    )

    instrumental = subparsers.add_parser(
        "instrumental-plan",
        help="report which sections should carry no vocal, and the repaints that do it",
    )
    instrumental.add_argument("project", type=Path, help="project containing song_spec.json")
    instrumental.add_argument(
        "--base-url",
        default="http://127.0.0.1:8001",
        help="base URL to put in the printed repaint commands (default: %(default)s)",
    )
    instrumental.add_argument(
        "--save",
        action="store_true",
        help="also write instrumental_plan.json into the project",
    )
    instrumental.add_argument("--overwrite", action="store_true")

    trim_tail = subparsers.add_parser(
        "trim-tail",
        help="cut a render's silent tail into a new file, leaving the render itself alone",
    )
    trim_tail.add_argument("project", type=Path, help="project whose audio has been rendered")
    trim_tail.add_argument(
        "--audio-file",
        type=Path,
        help="render to trim; defaults to audio/ace-step-01.wav",
    )
    trim_tail.add_argument(
        "--pad",
        type=float,
        default=DEFAULT_TAIL_PAD_SEC,
        help="seconds kept after the last audible sample so a decay is not clipped "
        "(default: %(default)s)",
    )
    trim_tail.add_argument(
        "--threshold-dbfs",
        type=float,
        default=MUSIC_END_THRESHOLD_DBFS,
        help="level below which the tail counts as silent (default: %(default)s)",
    )
    trim_tail.add_argument(
        "--dry-run",
        action="store_true",
        help="measure and report the cut, writing nothing",
    )
    trim_tail.add_argument("--overwrite", action="store_true")

    stems = subparsers.add_parser(
        "stems",
        help="separate a render into stems -- print the command, then take the result in",
    )
    stem_commands = stems.add_subparsers(dest="stems_command", required=True)
    stems_prepare = stem_commands.add_parser(
        "prepare",
        help="print the separation command to run; separates nothing itself",
    )
    stems_import = stem_commands.add_parser(
        "import",
        help="verify stems produced elsewhere and record stem_manifest.json",
    )
    for command in (stems_prepare, stems_import):
        command.add_argument("project", type=Path, help="project whose audio has been rendered")
        command.add_argument(
            "--audio-file",
            type=Path,
            help="render to separate; defaults to audio/ace-step-01.wav",
        )
        command.add_argument(
            "--model",
            default=DEFAULT_STEM_MODEL,
            help="separator model name (default: %(default)s)",
        )
    stems_import.add_argument("--overwrite", action="store_true")

    midi_review = subparsers.add_parser(
        "midi-review",
        help="compare a project's written MIDI with its SongSpec (no audio needed)",
    )
    midi_review.add_argument("project", type=Path, help="project containing song_spec.json and .mid")

    ace_step = subparsers.add_parser("ace-step", help="prepare or run the ACE-Step 1.5 adapter")
    ace_commands = ace_step.add_subparsers(dest="ace_command", required=True)
    prepare = ace_commands.add_parser("prepare", help="write ace_step_request.json without network access")
    add_ace_generation_arguments(prepare)
    prepare.add_argument(
        "--from-brief",
        type=Path,
        metavar="PROMPT_JSON",
        help=(
            "build the request from a prompt.json instead of the project's "
            "song_spec.json, taking its prompt exactly as written"
        ),
    )

    stage_repaint = ace_commands.add_parser(
        "stage-repaint",
        help="create a clean output project from a reviewed repaint plan",
    )
    stage_repaint.add_argument("source_project", type=Path)
    stage_repaint.add_argument("output_project", type=Path)
    stage_repaint.add_argument(
        "--repaint-plan",
        type=Path,
        default=Path("repaint_plan.json"),
        help="plan path, relative to source_project unless absolute",
    )

    render = ace_commands.add_parser("render", help="submit, wait, and download audio from ACE-Step")
    add_ace_generation_arguments(render)
    render.add_argument(
        "--from-brief",
        type=Path,
        metavar="PROMPT_JSON",
        help=(
            "render a prompt.json as written, instead of recompiling the "
            "prompt from the project's song_spec.json"
        ),
    )
    add_ace_connection_arguments(render)
    render.add_argument(
        "--source-audio",
        type=Path,
        help="local source Audio uploaded for cover or repaint",
    )
    render.add_argument(
        "--reference-audio",
        type=Path,
        help="optional local style-reference Audio uploaded for task-type cover",
    )
    render.add_argument(
        "--lora-path",
        help="server-side LoRA adapter directory or safetensors path",
    )
    render.add_argument("--lora-scale", type=float, default=1.0, help="LoRA strength from 0.0 to 1.0")
    render.add_argument("--lora-adapter-name", help="optional ACE-Step multi-adapter name")
    render.add_argument("--wait-timeout", type=float, default=600.0, help="generation timeout in seconds")
    render.add_argument("--poll-interval", type=float, default=2.0, help="status polling interval")

    render_chunks = ace_commands.add_parser(
        "render-chunks",
        help="render a chunk plan in order, each chunk repainted from the one before",
    )
    render_chunks.add_argument("project", type=Path)
    render_chunks.add_argument(
        "--chunk-plan",
        type=Path,
        default=Path("chunk_plan.json"),
        help="plan path, relative to the project unless absolute",
    )
    render_chunks.add_argument(
        "--audio-format",
        choices=tuple(sorted(AUDIO_FORMATS)),
        default="wav",
    )
    render_chunks.add_argument(
        "--resume",
        action="store_true",
        help=(
            "reuse chunks that already finished under chunks/ instead of "
            "rendering them again"
        ),
    )
    render_chunks.add_argument("--inference-steps", type=int, default=8)
    render_chunks.add_argument("--lora-path")
    render_chunks.add_argument("--lora-scale", type=float, default=1.0)
    render_chunks.add_argument("--lora-adapter-name")
    render_chunks.add_argument("--wait-timeout", type=float, default=1500.0)
    render_chunks.add_argument("--poll-interval", type=float, default=3.0)
    render_chunks.add_argument("--overwrite", action="store_true")
    add_ace_connection_arguments(render_chunks)

    lora = ace_commands.add_parser("lora", help="manage ACE-Step LoRA state")
    lora_commands = lora.add_subparsers(dest="lora_command", required=True)

    lora_status = lora_commands.add_parser("status", help="show the active ACE-Step LoRA state")
    add_ace_connection_arguments(lora_status)

    lora_load = lora_commands.add_parser("load", help="load, scale, and enable a server-side LoRA")
    lora_load.add_argument("lora_path", help="server-side LoRA adapter directory or safetensors path")
    lora_load.add_argument("--scale", type=float, default=1.0, help="LoRA strength from 0.0 to 1.0")
    lora_load.add_argument("--adapter-name", help="optional ACE-Step multi-adapter name")
    add_ace_connection_arguments(lora_load)

    lora_scale = lora_commands.add_parser("scale", help="change the loaded LoRA strength")
    lora_scale.add_argument("scale", type=float, help="LoRA strength from 0.0 to 1.0")
    lora_scale.add_argument("--adapter-name", help="optional ACE-Step multi-adapter name")
    add_ace_connection_arguments(lora_scale)

    for command, help_text in (
        ("enable", "enable the loaded LoRA"),
        ("disable", "temporarily disable the loaded LoRA"),
        ("unload", "unload LoRA and restore the base model"),
    ):
        lifecycle = lora_commands.add_parser(command, help=help_text)
        add_ace_connection_arguments(lifecycle)
    return parser


def add_ace_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ACESTEP_BASE_URL", "http://127.0.0.1:8001"),
        help="ACE-Step REST base URL",
    )
    parser.add_argument(
        "--api-key-env",
        default="ACESTEP_API_KEY",
        help="environment variable containing the API key",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help=(
            "seconds for a single HTTP call. A CPU-inference server blocks its "
            "worker during generation, so polls wait behind it"
        ),
    )


def add_ace_generation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("project", type=Path, help="directory containing song_spec.json")
    parser.add_argument("--audio-format", choices=tuple(sorted(AUDIO_FORMATS)), default="wav")
    parser.add_argument("--thinking", action="store_true", help="allow ACE-Step 5Hz LM audio-code planning")
    parser.add_argument("--model", help="optional model returned by the server's /v1/models endpoint")
    parser.add_argument("--inference-steps", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument(
        "--task-type",
        choices=("text2music", "cover", "repaint"),
        default="text2music",
        help="text generation, structure-preserving cover, or range repaint",
    )
    parser.add_argument(
        "--audio-cover-strength",
        type=float,
        default=1.0,
        help="ACE-Step cover/repaint conditioning strength from 0.0 to 1.0",
    )
    parser.add_argument(
        "--cover-noise-strength",
        type=float,
        default=0.0,
        help="source preservation from 0.0 (new) to 1.0 (closest to source)",
    )
    parser.add_argument("--repainting-start", type=float, default=0.0, help="repaint start in seconds")
    parser.add_argument("--repainting-end", type=float, help="repaint end in seconds")
    parser.add_argument(
        "--repaint-section",
        help="SongSpec section name to repaint, for example psychedelic_drop",
    )
    parser.add_argument(
        "--repaint-bars",
        metavar="START:END",
        help="one-based inclusive SongSpec bar range, for example 25:32",
    )
    parser.add_argument(
        "--repaint-plan",
        type=Path,
        help="Reviewer repaint_plan.json; supplies section, settings, and revision prompt",
    )
    parser.add_argument(
        "--repaint-mode",
        choices=("conservative", "balanced", "aggressive"),
        default="balanced",
        help="source-preservation strategy inside the repaint range",
    )
    parser.add_argument(
        "--repaint-strength",
        type=float,
        default=0.5,
        help="balanced repaint intensity from 0.0 to 1.0",
    )
    parser.add_argument(
        "--repaint-latent-crossfade-frames",
        type=int,
        default=10,
        help="latent boundary blend frames at 25 Hz",
    )
    parser.add_argument(
        "--repaint-wav-crossfade-sec",
        type=float,
        default=0.0,
        help="waveform splice crossfade in seconds",
    )
    parser.add_argument(
        "--chunk-mask-mode",
        choices=("explicit", "auto"),
        default="explicit",
        help="explicit uses the requested repaint range; auto delegates mask selection",
    )
    parser.add_argument(
        "--tail-guard-bars",
        type=float,
        default=None,
        help=(
            "extra bars of render buffer past the song grid so ACE-Step writes its "
            "ending outside the scored bars; the delivered WAV is trimmed back to the "
            "grid and the untrimmed render is kept alongside it. Defaults to "
            f"{DEFAULT_TAIL_GUARD_BARS} for text2music and 0 for cover and repaint, "
            "which render against a source whose length is already fixed. Pass 0 to "
            "render text2music without a guard"
        ),
    )
    parser.add_argument(
        "--lyrics-file",
        type=Path,
        help="UTF-8 lyrics file; defaults to the project's own lyrics.txt when present",
    )
    parser.add_argument(
        "--no-lyrics",
        action="store_true",
        help="render instrumental, ignoring the project's lyrics.txt",
    )
    parser.add_argument(
        "--revision-file",
        type=Path,
        help="optional UTF-8 revision prompt; prioritized without changing SongSpec fields",
    )
    parser.add_argument("--overwrite", action="store_true")
