"""Every flag the command line accepts, and nothing about what they do.

Keeping the parser apart from the command bodies is what lets a command be
called with a plain ``argparse.Namespace`` in a test, instead of through
``main`` and a list of strings.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ..adapters.ace_step import DEFAULT_REQUEST_TIMEOUT
from ..chunked import DEFAULT_CHUNK_BARS
from ..revision import DEFAULT_ROUNDS
from ..tail_guard import DEFAULT_TAIL_GUARD_BARS
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
            "get_track_devices; low/high keep a musical 0..1 off the parameter's extremes"
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
    render_chunks.add_argument("--audio-format", choices=("wav",), default="wav")
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
    parser.add_argument("--audio-format", choices=("wav", "flac", "mp3", "opus", "aac", "wav32"), default="wav")
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
        default=0.0,
        help=(
            "extra bars of render buffer past the song grid so ACE-Step writes its "
            "ending outside the scored bars; the delivered WAV is trimmed back to the "
            "grid and the untrimmed render is kept alongside it"
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
