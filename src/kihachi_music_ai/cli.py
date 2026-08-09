from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .analyzer import analyze_project
from .adapters.ace_step import (
    AceStepClient,
    AceStepConfig,
    AceStepError,
    AceStepLoraConfig,
    AceStepLoraStatus,
    AceStepOptions,
    AceStepRepaintWindow,
    load_project_spec,
    prepare_ace_step_request,
    render_with_ace_step,
    resolve_repaint_window,
)
from .pipeline import compose_project
from .repaint_planner import (
    load_repaint_plan,
    song_spec_sha256,
    stage_repaint_project,
)
from .ableton import parse_automation_binding, plan_project_arrangement
from .arrangement import describe_arrangement
from .defects import scan_material
from .chunked import (
    DEFAULT_CHUNK_BARS,
    build_chunk_plan,
    load_chunk_plan,
    render_chunk_plan,
)
from .edit import apply_edit_to_project, build_spec_edit
from .lyrics import build_lyrics
from .midi_review import review_project_midi
from .models import TRACK_NAMES
from .reviewer import review_project
from .tail_guard import DEFAULT_TAIL_GUARD_BARS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kihachi", description="KIHACHI Music AI v0.1")
    subparsers = parser.add_subparsers(dest="command", required=True)
    compose = subparsers.add_parser("compose", help="generate SongSpec, MIDI, and an audio prompt")
    compose.add_argument("prompt", help="natural-language music brief")
    compose.add_argument("--output", type=Path, help="output directory")
    compose.add_argument("--seed", type=int, default=8, help="deterministic composition seed")
    compose.add_argument("--overwrite", action="store_true", help="replace only the five known artifacts")

    analyze = subparsers.add_parser("analyze", help="analyze generated WAV and compare it with SongSpec")
    analyze.add_argument("project", type=Path, help="directory containing song_spec.json")
    analyze.add_argument("--audio", type=Path, help="WAV path, relative to the project unless absolute")
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
    ableton_plan.add_argument("--overwrite", action="store_true", help="replace arrangement_plan.json")

    midi_review = subparsers.add_parser(
        "midi-review",
        help="compare a project's written MIDI with its SongSpec (no audio needed)",
    )
    midi_review.add_argument("project", type=Path, help="project containing song_spec.json and .mid")

    ace_step = subparsers.add_parser("ace-step", help="prepare or run the ACE-Step 1.5 adapter")
    ace_commands = ace_step.add_subparsers(dest="ace_command", required=True)
    prepare = ace_commands.add_parser("prepare", help="write ace_step_request.json without network access")
    _add_ace_generation_arguments(prepare)

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
    _add_ace_generation_arguments(render)
    _add_ace_connection_arguments(render)
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
    render_chunks.add_argument("--inference-steps", type=int, default=8)
    render_chunks.add_argument("--lora-path")
    render_chunks.add_argument("--lora-scale", type=float, default=1.0)
    render_chunks.add_argument("--lora-adapter-name")
    render_chunks.add_argument("--wait-timeout", type=float, default=1500.0)
    render_chunks.add_argument("--poll-interval", type=float, default=3.0)
    render_chunks.add_argument("--overwrite", action="store_true")
    _add_ace_connection_arguments(render_chunks)

    lora = ace_commands.add_parser("lora", help="manage ACE-Step LoRA state")
    lora_commands = lora.add_subparsers(dest="lora_command", required=True)

    lora_status = lora_commands.add_parser("status", help="show the active ACE-Step LoRA state")
    _add_ace_connection_arguments(lora_status)

    lora_load = lora_commands.add_parser("load", help="load, scale, and enable a server-side LoRA")
    lora_load.add_argument("lora_path", help="server-side LoRA adapter directory or safetensors path")
    lora_load.add_argument("--scale", type=float, default=1.0, help="LoRA strength from 0.0 to 1.0")
    lora_load.add_argument("--adapter-name", help="optional ACE-Step multi-adapter name")
    _add_ace_connection_arguments(lora_load)

    lora_scale = lora_commands.add_parser("scale", help="change the loaded LoRA strength")
    lora_scale.add_argument("scale", type=float, help="LoRA strength from 0.0 to 1.0")
    lora_scale.add_argument("--adapter-name", help="optional ACE-Step multi-adapter name")
    _add_ace_connection_arguments(lora_scale)

    for command, help_text in (
        ("enable", "enable the loaded LoRA"),
        ("disable", "temporarily disable the loaded LoRA"),
        ("unload", "unload LoRA and restore the base model"),
    ):
        lifecycle = lora_commands.add_parser(command, help=help_text)
        _add_ace_connection_arguments(lifecycle)
    return parser


def _add_ace_connection_arguments(parser: argparse.ArgumentParser) -> None:
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
    parser.add_argument("--request-timeout", type=float, default=30.0, help="HTTP timeout in seconds")


def _add_ace_generation_arguments(parser: argparse.ArgumentParser) -> None:
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


def _ace_options_and_window(
    args: argparse.Namespace,
) -> tuple[AceStepOptions, AceStepRepaintWindow | None]:
    lyrics = _resolve_lyrics(args)
    revision = ""
    if args.revision_file is not None:
        revision = args.revision_file.read_text(encoding="utf-8")
    task_type = args.task_type
    audio_cover_strength = args.audio_cover_strength
    cover_noise_strength = args.cover_noise_strength
    repaint_mode = args.repaint_mode
    repaint_strength = args.repaint_strength
    repaint_latent_crossfade_frames = args.repaint_latent_crossfade_frames
    repaint_wav_crossfade_sec = args.repaint_wav_crossfade_sec
    chunk_mask_mode = args.chunk_mask_mode
    tail_guard_bars = args.tail_guard_bars
    repaint_window = None
    repainting_start = args.repainting_start
    repainting_end = args.repainting_end
    if args.repaint_plan is not None:
        if args.repaint_section is not None or args.repaint_bars is not None:
            raise ValueError("do not combine --repaint-plan with section or bar selectors")
        if args.repainting_end is not None or args.repainting_start != 0.0:
            raise ValueError("do not combine --repaint-plan with repainting seconds")
        if args.revision_file is not None:
            raise ValueError("--repaint-plan already supplies the revision prompt")
        if args.task_type == "cover":
            raise ValueError("--repaint-plan cannot be combined with --task-type cover")
        if args.tail_guard_bars:
            raise ValueError("--repaint-plan already supplies the tail guard")
        plan_path = _resolve_repaint_plan_path(args.project, args.repaint_plan)
        plan = load_repaint_plan(plan_path)
        spec = load_project_spec(args.project)
        if plan.get("song_spec_sha256") != song_spec_sha256(spec):
            raise ValueError("repaint plan SongSpec does not match this project")
        selection = plan["selection"]
        settings = plan["ace_step_options"]
        tail_guard_bars = float(settings.get("tail_guard_bars", 0.0))
        if str(selection.get("selector", "section")) == "bars":
            repaint_window = resolve_repaint_window(
                spec,
                bar_range=f"{int(selection['start_bar'])}:{int(selection['end_bar'])}",
                tail_guard_bars=tail_guard_bars,
            )
        else:
            repaint_window = resolve_repaint_window(
                spec,
                section_name=str(selection["section_name"]),
                tail_guard_bars=tail_guard_bars,
            )
        _verify_planned_window(selection, repaint_window)
        task_type = "repaint"
        revision = str(plan["revision_prompt"])
        audio_cover_strength = float(settings.get("audio_cover_strength", 1.0))
        cover_noise_strength = float(settings.get("cover_noise_strength", 0.0))
        repaint_mode = str(settings.get("repaint_mode", "balanced"))
        repaint_strength = float(settings.get("repaint_strength", 0.65))
        repaint_latent_crossfade_frames = int(
            settings.get("repaint_latent_crossfade_frames", 10)
        )
        repaint_wav_crossfade_sec = float(settings.get("repaint_wav_crossfade_sec", 0.25))
        chunk_mask_mode = str(settings.get("chunk_mask_mode", "explicit"))
        repainting_start = repaint_window.start_sec
        repainting_end = repaint_window.end_sec
    elif args.repaint_section is not None or args.repaint_bars is not None:
        if task_type != "repaint":
            raise ValueError("--repaint-section/--repaint-bars require --task-type repaint")
        if args.repainting_end is not None or args.repainting_start != 0.0:
            raise ValueError(
                "do not combine --repaint-section/--repaint-bars with repainting seconds"
            )
        repaint_window = resolve_repaint_window(
            load_project_spec(args.project),
            section_name=args.repaint_section,
            bar_range=args.repaint_bars,
            tail_guard_bars=tail_guard_bars,
        )
        repainting_start = repaint_window.start_sec
        repainting_end = repaint_window.end_sec
    options = AceStepOptions(
        audio_format=args.audio_format,
        thinking=args.thinking,
        model=args.model,
        inference_steps=args.inference_steps,
        batch_size=args.batch_size,
        lyrics=lyrics,
        revision=revision,
        task_type=task_type,
        audio_cover_strength=audio_cover_strength,
        cover_noise_strength=cover_noise_strength,
        repainting_start=repainting_start,
        repainting_end=repainting_end,
        repaint_mode=repaint_mode,
        repaint_strength=repaint_strength,
        repaint_latent_crossfade_frames=repaint_latent_crossfade_frames,
        repaint_wav_crossfade_sec=repaint_wav_crossfade_sec,
        chunk_mask_mode=chunk_mask_mode,
        tail_guard_bars=tail_guard_bars,
    )
    return options, repaint_window


def _print_midi_alignment(review: dict[str, object], project_dir: Path) -> None:
    alignment = review["alignment"]
    harmony = review["harmony"]
    key = review["key"]
    print(f"MIDI vs SongSpec: {project_dir}")
    print(f"- midi alignment score: {alignment['score']} ({alignment['grade']})")
    print(
        f"- harmony: bass-root match {harmony['bass_root_match_ratio']}, "
        f"chord-tone match {harmony['chord_tone_match_ratio']} "
        f"(progression {' - '.join(harmony['progression'])})"
    )
    print(
        f"- key: {key['out_of_key_notes']}/{key['pitched_notes']} pitched notes outside "
        f"{key['key']}"
    )
    print(f"- written energy correlation: {review['sections']['energy_correlation']}")
    empty = review["coverage"]["empty_bars"]
    print(f"- coverage: {review['coverage']['score']}" + (f" (silent bars {empty})" if empty else ""))


def _resolve_lyrics(args: argparse.Namespace) -> str:
    """Lyrics for the render: the flag wins, else the project's own sheet.

    Composing writes ``lyrics.txt``, so a render should sing it without being
    told to. Before this the file was written and then never sent, and every
    render went out with an empty lyrics field.
    """

    if args.no_lyrics:
        if args.lyrics_file is not None:
            raise ValueError("do not combine --no-lyrics with --lyrics-file")
        return ""
    if args.lyrics_file is not None:
        return args.lyrics_file.read_text(encoding="utf-8")
    project_sheet = Path(args.project) / "lyrics.txt"
    return project_sheet.read_text(encoding="utf-8") if project_sheet.is_file() else ""


def _resolve_repaint_plan_path(project: Path, requested: Path) -> Path:
    if requested.is_absolute() or requested.is_file():
        return requested
    return Path(project) / requested


def _verify_planned_window(
    selection: dict[str, object],
    resolved: AceStepRepaintWindow,
) -> None:
    expected = resolved.to_dict()
    for name in ("start_bar", "end_bar", "start_sec", "end_sec", "section_name"):
        if selection.get(name) != expected.get(name):
            raise ValueError(f"repaint plan {name} does not match its SongSpec section")


def _print_repaint_window(window: AceStepRepaintWindow | None) -> None:
    if window is None:
        return
    section = f"; section {window.section_name}" if window.section_name is not None else ""
    print(
        f"- repaint bars {window.start_bar}:{window.end_bar} "
        f"-> {window.start_sec:.3f}-{window.end_sec:.3f} sec{section}"
    )


def _ace_client(args: argparse.Namespace) -> AceStepClient:
    return AceStepClient(
        AceStepConfig(
            base_url=args.base_url,
            api_key=os.environ.get(args.api_key_env),
            request_timeout=args.request_timeout,
        )
    )


def _print_lora_status(status: AceStepLoraStatus) -> None:
    print("ACE-Step LoRA status:")
    print(f"- loaded: {status.lora_loaded}")
    print(f"- enabled: {status.use_lora}")
    print(f"- scale: {status.lora_scale}")
    if status.adapter_type is not None:
        print(f"- type: {status.adapter_type}")
    if status.active_adapter is not None:
        print(f"- active adapter: {status.active_adapter}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "compose":
            manifest = compose_project(
                args.prompt,
                args.output,
                seed=args.seed,
                overwrite=args.overwrite,
            )
            print(f"Generated KIHACHI project: {manifest.output_dir}")
            for path in manifest.files:
                print(f"- {path.name}")
            spec = manifest.spec
            print(
                f"- arrangement: {len(spec.arrangement)} sections over "
                f"{spec.song.total_bars} bars ({spec.song.target_duration_sec:.1f}s)"
            )
            for row in describe_arrangement(spec.arrangement):
                resting = [
                    track for track in TRACK_NAMES if track not in row["active_tracks"]
                ]
                print(
                    f"    bar {row['start_bar']:>4} +{row['length_bars']:<3} "
                    f"{row['name']:<18} energy {row['energy']:.2f}"
                    + (f"  (resting: {', '.join(resting)})" if resting else "")
                )
            return 0

        if args.command == "analyze":
            manifest = analyze_project(
                args.project,
                args.audio,
                overwrite=args.overwrite,
            )
            tempo = manifest.analysis["tempo"]
            level = manifest.analysis["level"]
            harmony = manifest.analysis["harmony"]
            sections = manifest.analysis["sections"]
            key = harmony["key"]
            comparison = manifest.analysis["song_spec_comparison"]
            # A second, deliberately separate view of the same audio: conformance
            # ("did it follow the plan") and defects ("is it usable") answer
            # different questions, and mixing them is what made a take with a
            # 2.28 s silent hole score a respectable 56.32.
            defects_file = manifest.project_dir / "material_defects.json"
            defects = None
            if defects_file.exists() and not args.overwrite:
                print(f"- keeping existing defect scan: {defects_file}")
            else:
                defects = scan_material(manifest.audio_file)
                defects_file.write_text(
                    json.dumps(defects, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            print(f"Analyzed KIHACHI audio: {manifest.audio_file}")
            print(f"- result: {manifest.analysis_file}")
            print(f"- estimated BPM: {tempo['estimated_bpm']} (confidence {tempo['confidence']})")
            print(
                f"- estimated key: {key['estimated_key']} (confidence {key['confidence']}; "
                f"SongSpec {comparison['key_status']})"
            )
            print(
                f"- chord progression match: {harmony['chords']['progression_match_ratio']} "
                f"(confident-bar coverage {harmony['chords']['confident_bar_coverage']})"
            )
            print(f"- section boundaries after bars: {sections['detected_boundaries_after_bar']}")
            print(f"- planned boundary recall: {sections['planned_boundary_recall_within_one_bar']}")
            print(f"- section energy correlation: {sections['energy_correlation_to_song_spec']}")
            if defects is not None:
                summary = (
                    "clean"
                    if defects["clean"]
                    else ", ".join(
                        f"{item['code']}({item['severity']})" for item in defects["findings"]
                    )
                )
                print(f"- material defects: {summary}")
                for item in defects["findings"]:
                    if item["severity"] in {"blocking", "warning"}:
                        print(f"    {item['severity']}: {item['detail']}")
                print(f"- defect scan: {defects_file}")
            print(f"- peak: {level['peak_dbfs']} dBFS")
            print(f"- RMS: {level['rms_dbfs']} dBFS")
            return 0

        if args.command == "edit":
            spec = load_project_spec(args.project)
            spec_edit = build_spec_edit(spec, args.instruction)
            destination = args.project / "spec_edit.json"
            if destination.exists() and not args.overwrite:
                raise FileExistsError(f"refusing to overwrite spec edit: {destination}")
            destination.write_text(
                json.dumps(spec_edit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"Planned KIHACHI edit: {args.project}")
            print(f"- instruction: {spec_edit['instruction']}")
            interpretation = spec_edit["interpretation"]
            print(
                f"- reading: {', '.join(interpretation['qualities'])} "
                f"{interpretation['direction']} by {interpretation['magnitude']}"
            )
            target = spec_edit["target"]
            sections = target["sections"]
            print(
                f"- target: {', '.join(target['tracks'])} in "
                + (", ".join(sections) if isinstance(sections, list) else "every section")
            )
            for change in spec_edit["changes"]:
                where = change["section"] or "(song-wide)"
                print(f"    {where}: {change['path']} {change['from']} -> {change['to']}")
            for warning in spec_edit["scope_warnings"]:
                print(f"- warning: {warning}")
            print(f"- plan: {destination} (nothing regenerated yet)")
            return 0

        if args.command == "apply-edit":
            manifest = apply_edit_to_project(
                args.source_project, args.output_project, edit_path=args.spec_edit
            )
            report = manifest.report
            print(f"Applied KIHACHI edit: {manifest.output_project}")
            print(f"- instruction: {report['instruction']}")
            for path in manifest.files:
                print(f"- {path.name}")
            print(f"- sections regenerated: {report['changed_sections'] or 'none'}")
            print(f"- sections byte-identical: {len(report['unchanged_sections'])}")
            for track, info in sorted(report["tracks"].items()):
                moved = info["changed_sections"]
                print(
                    f"    {track}: {info['notes_before']} -> {info['notes_after']} notes"
                    + (f", changed in {', '.join(moved)}" if moved else ", unchanged")
                )
            print(f"- audio prompt changed: {report.get('audio_prompt_changed')}")
            if report["no_effect"]:
                print(
                    "- warning: this edit changed neither the MIDI nor the audio prompt; "
                    "try a larger magnitude, or a parameter the composer reads"
                )
            return 0

        if args.command == "plan-chunks":
            spec = load_project_spec(args.project)
            plan = build_chunk_plan(
                spec,
                target_chunk_bars=args.chunk_bars,
                tail_guard_bars=args.tail_guard_bars,
            )
            destination = args.project / "chunk_plan.json"
            if destination.exists() and not args.overwrite:
                raise FileExistsError(f"refusing to overwrite chunk plan: {destination}")
            destination.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"Planned KIHACHI chunked render: {args.project}")
            print(
                f"- {len(plan['chunks'])} chunks over {plan['total_bars']} bars "
                f"(target {plan['target_chunk_bars']} bars each)"
            )
            for chunk in plan["chunks"]:
                selection = chunk["selection"]
                guard = selection.get("tail_guard_sec")
                print(
                    f"    [{chunk['index']}] {chunk['task_type']:<11} "
                    f"bars {selection['start_bar']}-{selection['end_bar']}: "
                    f"{', '.join(chunk['sections'])}" + ("  (+tail guard)" if guard else "")
                )
            print(f"- plan: {destination} (nothing rendered yet)")
            return 0

        if args.command == "lyrics":
            sheet = build_lyrics(load_project_spec(args.project))
            print(f"Lyric sheet: {args.project}")
            print(f"- vocal mode: {sheet.mode}")
            print(f"- hook: {sheet.hook or '(none)'}")
            print(f"- lines: {sheet.line_count}")
            for section in sheet.sections:
                body = " / ".join(section.lines) if section.lines else "(no vocal)"
                print(f"    {section.section_name:18} {section.tag:10} {body}")
            return 0

        if args.command == "midi-review":
            manifest = review_project_midi(args.project)
            _print_midi_alignment(manifest.review, manifest.project_dir)
            return 0

        if args.command == "ableton-plan":
            manifest = plan_project_arrangement(
                args.project,
                first_track_index=args.first_track_index,
                session_slot=args.session_slot,
                automation=[parse_automation_binding(text) for text in args.automate],
                overwrite=args.overwrite,
            )
            plan = manifest.plan
            song = plan["song"]
            print(f"Planned KIHACHI arrangement for Live: {manifest.project_dir}")
            print(
                f"- {song['title']}: {song['total_bars']} bars / {song['total_beats']:g} beats "
                f"at {song['bpm']:g} BPM, {song['key']}"
            )
            for track in plan["tracks"]:
                print(
                    f"    track {track['live_track_index']}: {track['name']} "
                    f"({track['notes']} notes)"
                )
            # The resting tracks are the point of the whole MIDI path: the audio
            # model kept playing drums through the breakdown, the MIDI does not.
            for section in plan["structure"]:
                resting = ", ".join(section["resting_tracks"]) or "-"
                print(
                    f"    bar {section['start_bar']:>3}  {section['name']:<20} "
                    f"energy {section['energy']:.2f}  resting: {resting}"
                )
            automated = [op for op in plan["operations"] if op["op"] == "set_clip_parameter_envelope"]
            for operation in automated:
                params = operation["params"]
                print(
                    f"- automation: track {params['track_index']} device "
                    f"{params['device_index']} parameter {params['parameter_index']}, "
                    f"{len(params['steps'])} steps"
                )
            for warning in plan["warnings"]:
                print(f"- warning: {warning}")
            print(f"- {len(plan['operations'])} operations, {plan['execution_state']}")
            print(f"- plan: {manifest.plan_file}")
            return 0

        if args.command == "review":
            manifest = review_project(
                args.project,
                against=args.against,
                overwrite=args.overwrite,
                preserve_revision_prompt=args.preserve_revision_prompt,
                tail_guard_bars=args.tail_guard_bars,
                prefer_bar_level=args.prefer_bar_level,
            )
            alignment = manifest.review["alignment"]
            print(f"Reviewed KIHACHI generation: {manifest.project_dir}")
            print(f"- audio alignment score: {alignment['score']} ({alignment['grade']})")
            midi_alignment = manifest.review.get("midi_alignment")
            if midi_alignment is not None:
                midi_score = midi_alignment["alignment"]
                print(
                    f"- midi alignment score: {midi_score['score']} ({midi_score['grade']}); "
                    f"harmony written {midi_alignment['harmony']['bass_root_match_ratio']}/"
                    f"{midi_alignment['harmony']['chord_tone_match_ratio']}"
                )
            # Printed right under the score on purpose: a take can align well and
            # still be unusable, and the score alone hides that.
            defects = manifest.review.get("material_defects")
            if defects is not None:
                if defects["clean"]:
                    print("- material defects: none")
                else:
                    for defect in defects["findings"]:
                        if defect["severity"] in {"blocking", "warning"}:
                            print(f"- material {defect['severity']}: {defect['detail']}")
            if "comparison" in manifest.review:
                comparison = manifest.review["comparison"]
                print(
                    f"- versus {comparison['baseline_project']}: {comparison['score_delta']:+.2f} points; "
                    f"preferred alignment {comparison['preferred_song_spec_alignment']}"
                )
            print(f"- review: {manifest.review_file}")
            prompt_state = " (preserved)" if args.preserve_revision_prompt else ""
            print(f"- revision prompt: {manifest.revision_prompt_file}{prompt_state}")
            candidate = manifest.review["repaint_candidate"]
            selection = candidate["selection"]
            print(
                f"- repaint candidate: {selection.get('section_name', 'multiple sections')} "
                f"(bars {selection['start_bar']}:{selection['end_bar']}, "
                f"selector {selection['selector']})"
            )
            guard_bars = candidate["ace_step_options"].get("tail_guard_bars", 0.0)
            if guard_bars:
                print(
                    f"- tail guard: {guard_bars:g} bars past the song grid "
                    f"(repaint end {selection['end_sec']}s, trimmed back after render)"
                )
            for narrow in candidate.get("bar_level_candidates", ()):
                print(
                    f"- bar-level candidate: bars {narrow['start_bar']}:{narrow['end_bar']} "
                    f"({narrow['reason']})"
                )
            print(f"- recommended selector: {candidate.get('recommended_selector')}")
            print(f"- repaint plan: {manifest.repaint_plan_file}")
            return 0

        if args.ace_command == "stage-repaint":
            manifest = stage_repaint_project(
                args.source_project,
                args.output_project,
                plan_path=args.repaint_plan,
            )
            print(f"Staged KIHACHI repaint project: {manifest.output_project}")
            print(f"- source Audio verified, not copied: {manifest.source_audio.name}")
            for path in manifest.files:
                print(f"- {path.name}")
            return 0

        if args.ace_command == "prepare":
            options, repaint_window = _ace_options_and_window(args)
            request_path, _request = prepare_ace_step_request(
                args.project,
                options,
                overwrite=args.overwrite,
            )
            print(f"Prepared ACE-Step request: {request_path}")
            _print_repaint_window(repaint_window)
            return 0

        if args.ace_command == "render-chunks":
            plan_path = args.chunk_plan
            if not plan_path.is_absolute():
                plan_path = args.project / plan_path
            plan = load_chunk_plan(plan_path)
            lora = None
            if args.lora_path is not None:
                lora = AceStepLoraConfig(
                    lora_path=args.lora_path,
                    scale=args.lora_scale,
                    adapter_name=args.lora_adapter_name,
                )
            manifest = render_chunk_plan(
                args.project,
                _ace_client(args),
                plan,
                lora=lora,
                base_options=AceStepOptions(
                    audio_format=args.audio_format,
                    inference_steps=args.inference_steps,
                ),
                poll_interval=args.poll_interval,
                wait_timeout=args.wait_timeout,
                overwrite=args.overwrite,
            )
            print(f"Rendered KIHACHI chunk plan: {manifest.project_dir}")
            for step in manifest.steps:
                print(
                    f"    [{step['index']}] {step['task_type']:<11} "
                    f"bars {step['bars'][0]}-{step['bars'][1]} "
                    f"({', '.join(step['sections'])}) task {step['task_id']}"
                )
            print(f"- final audio: {manifest.audio_file}")
            print(f"- chain log: {manifest.log_file}")
            return 0

        if args.ace_command == "lora":
            client = _ace_client(args)
            if args.lora_command == "load":
                status = client.configure_lora(
                    AceStepLoraConfig(
                        lora_path=args.lora_path,
                        scale=args.scale,
                        adapter_name=args.adapter_name,
                    )
                )
            elif args.lora_command == "scale":
                client.set_lora_scale(args.scale, adapter_name=args.adapter_name)
                status = client.get_lora_status()
            elif args.lora_command == "enable":
                client.toggle_lora(True)
                status = client.get_lora_status()
            elif args.lora_command == "disable":
                client.toggle_lora(False)
                status = client.get_lora_status()
            elif args.lora_command == "unload":
                client.unload_lora()
                status = client.get_lora_status()
            else:
                status = client.get_lora_status()
            _print_lora_status(status)
            return 0

        options, repaint_window = _ace_options_and_window(args)
        if options.task_type in {"cover", "repaint"} and args.source_audio is None:
            raise ValueError(f"--task-type {options.task_type} requires --source-audio")
        if options.task_type == "text2music" and (
            args.source_audio is not None or args.reference_audio is not None
        ):
            raise ValueError("--source-audio requires cover/repaint; --reference-audio requires cover")
        if options.task_type == "repaint" and args.reference_audio is not None:
            raise ValueError("--reference-audio is supported only with --task-type cover")
        if args.lora_path is None and args.lora_adapter_name is not None:
            raise ValueError("--lora-adapter-name requires --lora-path")
        lora = None
        if args.lora_path is not None:
            lora = AceStepLoraConfig(
                lora_path=args.lora_path,
                scale=args.lora_scale,
                adapter_name=args.lora_adapter_name,
            )
        render = render_with_ace_step(
            args.project,
            _ace_client(args),
            options,
            lora=lora,
            source_audio=args.source_audio,
            reference_audio=args.reference_audio,
            repaint_selection=repaint_window,
            overwrite=args.overwrite,
            poll_interval=args.poll_interval,
            wait_timeout=args.wait_timeout,
        )
        print(f"ACE-Step task completed: {render.task_id}")
        _print_repaint_window(repaint_window)
        if render.lora_status is not None:
            print(f"- LoRA active at scale {render.lora_status.lora_scale}")
        for path in render.audio_files:
            print(f"- {path}")
        return 0
    except (AceStepError, FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
