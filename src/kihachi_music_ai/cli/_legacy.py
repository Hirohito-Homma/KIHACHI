"""The commands not yet pulled out into modules of their own.

Everything here is on its way to a named module the way ``song``, ``parser``,
and ``connection`` already are: analyze, review, revise, report, midi-review,
ableton-plan, and the whole ``ace-step`` family. Until then this module still
owns ``main`` and the dispatch, and the commands that have moved are called
from it rather than duplicated in it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import song
from .connection import ace_client, print_lora_status
from .parser import build_parser
from ..analyzer import analyze_project
from ..adapters.ace_step import (
    AceStepError,
    AceStepLoraConfig,
    AceStepOptions,
    AceStepRepaintWindow,
    load_project_spec,
    prepare_ace_step_request,
    render_with_ace_step,
    resolve_repaint_window,
)
from ..repaint_planner import (
    load_repaint_plan,
    song_spec_sha256,
    stage_repaint_project,
)
from ..ableton import (
    parse_automation_binding,
    parse_send_binding,
    plan_project_arrangement,
)
from ..chunked import load_chunk_plan, render_chunk_plan
from ..decision import (
    current_decision,
    decision_audio_status,
    load_decision_log,
    record_decision,
)
from ..midi_review import review_project_midi
from ..models import SongSpec
from ..prompt_compiler import brief_matches_spec, compile_audio_prompt, load_render_brief
from ..report import build_report, load_candidate, rank as rank_candidates
from ..revision import describe as describe_revisions, run_revision_loop
from ..adapters.intent_llm import (
    build_request as build_intent_request,
    read_brief as read_brief_with_model,
    write_reading,
)
from ..brief import describe as describe_brief, read_coverage
from ..material import describe as describe_material, review_sample
from ..transcribe import transcribe_sample_file
from ..sampler import cut_sample
from ..select import (
    build_shortlist,
    describe as describe_shortlist,
    write_shortlist,
)
from ..reviewer import review_project
from ..instrumental import plan_instrumental_sections, write_instrumental_plan
from ..stems import import_stems, plan_separation
from ..tail_guard import DEFAULT_TAIL_GUARD_BARS
from ..tail_trim import TailTrimPlan, plan_tail_trim, trim_project_tail


def _candidate_projects(args: argparse.Namespace) -> tuple[list[Path], str | None]:
    """The takes a command was pointed at, deduplicated and in order.

    Shared by `report` and `shortlist` so the two never disagree about which
    takes are in a comparison -- the page and the ranking are read together.
    """

    also_projects = list(args.also)
    projects = [args.project, *also_projects]
    stopped = None
    if getattr(args, "from_revision_log", False):
        log_file = args.project / "revision_log.json"
        if not log_file.is_file():
            raise FileNotFoundError(f"no revision log: {log_file}")
        log = json.loads(log_file.read_text(encoding="utf-8"))
        stopped = log.get("stopped_because")
        logged_projects: list[Path] = []
        for row in log["rounds"]:
            recorded = Path(row["project"])
            if not recorded.is_absolute():
                recorded = log_file.parent / recorded
            logged_projects.append(recorded)
        projects = [*logged_projects, *also_projects]
    seen: list[Path] = []
    for path in projects:
        if path not in seen:
            seen.append(path)
    return seen, stopped


def _print_tail_trim_plan(plan: TailTrimPlan) -> None:
    """Report the cut and, crucially, what it costs against the song grid."""

    print(
        f"- music ends at {plan.music_end_sec:.2f} s of {plan.source_duration_sec:.2f} s; "
        f"keeping {plan.kept_duration_sec:.2f} s (+{plan.pad_sec:g} s pad)"
    )
    print(f"- removes {plan.removed_sec:.2f} s below {plan.threshold_dbfs:g} dBFS")
    if plan.shortfall_sec > 0:
        # Worth saying out loud: the cut file no longer fills the SongSpec's grid.
        print(
            f"- now {plan.shortfall_sec:.2f} s ({plan.shortfall_bars:g} bars) short of the "
            f"{plan.grid_duration_sec:.2f} s song grid"
        )


def _audio_tracks(args: argparse.Namespace) -> list[dict[str, object]]:
    """The audio rows of the layout: a reference take, a vocal, an FX track."""

    rows: list[dict[str, object]] = []
    if args.vocal_audio is not None:
        rows.append({"role": "vocal", "name": "KIHACHI Vocal", "file": args.vocal_audio})
    if args.fx_track:
        rows.append(
            {
                "role": "fx",
                "name": "KIHACHI FX",
                "why": "somewhere to put returns and prints; devices are added in Live",
            }
        )
    if args.reference_audio is not None:
        path = args.reference_audio
        if not path.is_absolute():
            path = args.project / path
        rows.append(
            {
                "role": "reference",
                "name": "ACE-Step Ref",
                "file": path,
                "why": "the rendered take, to write and mix against",
            }
        )
    return rows


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
    # None means the flag was not passed, which is not the same as an explicit 0:
    # text2music wants a guard by default, and only an explicit 0 turns it off.
    # cover and repaint render against a source whose length is already fixed, so
    # lengthening the buffer for them would move the window they were given.
    guard_requested = args.tail_guard_bars is not None
    if guard_requested:
        tail_guard_bars = args.tail_guard_bars
    elif task_type == "text2music":
        tail_guard_bars = DEFAULT_TAIL_GUARD_BARS
    else:
        tail_guard_bars = 0.0
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
        if guard_requested and args.tail_guard_bars:
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
    groove = review.get("groove")
    if groove is not None and groove["written_offbeat_delay_ms"] is not None:
        print(
            f"- groove: offbeats {groove['written_offbeat_delay_ms']} ms late "
            f"(swing {groove['requested_swing']} asks {groove['expected_offbeat_delay_ms']}, "
            f"off by {groove['offbeat_error_ms']:+.3f}); "
            f"humanize jitter {groove['straight_jitter_ms']} ms"
        )
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


def _warn_if_brief_is_stale(brief_path: Path, project_dir: Path) -> None:
    """Say so when the brief no longer describes the spec sitting next to it.

    Not an error: editing the prompt by hand is the reason ``--from-brief``
    exists, and a hand-edited brief will never match. But a brief left over
    from before a ``kihachi edit`` looks identical to an intentional one, and
    the difference is a whole render.
    """

    spec_path = project_dir / "song_spec.json"
    if not spec_path.is_file():
        return
    try:
        brief = load_render_brief(brief_path)
        spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not brief_matches_spec(brief, spec):
        print(
            "- note: this brief was not compiled from the song_spec.json in "
            "this project (song_spec_sha256 differs). Rendering it as written."
        )
        return
    # The digest ties the brief to the spec it was *compiled from*, so editing
    # the prompt leaves it matching. That is the edit people actually make, so
    # it needs its own check rather than being read as "unchanged".
    edited = [
        name
        for name, differs in (
            ("prompt", str(brief["prompt"]).strip() != compile_audio_prompt(spec).strip()),
            (
                "duration",
                abs(float(brief["song"]["duration_sec"]) - spec.song.target_duration_sec)
                > 1e-6,
            ),
            ("seed", int(brief["seed"]) != spec.seed),
        )
        if differs
    ]
    if edited:
        print(f"- note: brief differs from the spec's own ({', '.join(edited)}); using the brief.")


def _print_repaint_window(window: AceStepRepaintWindow | None) -> None:
    if window is None:
        return
    section = f"; section {window.section_name}" if window.section_name is not None else ""
    print(
        f"- repaint bars {window.start_bar}:{window.end_bar} "
        f"-> {window.start_sec:.3f}-{window.end_sec:.3f} sec{section}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return song.serve(args)
        if args.command == "compose":
            return song.compose(args)

        if args.command == "analyze":
            manifest = analyze_project(
                args.project,
                args.audio,
                overwrite=args.overwrite,
                measure_loudness=args.loudness,
            )
            tempo = manifest.analysis["tempo"]
            level = manifest.analysis["level"]
            harmony = manifest.analysis["harmony"]
            sections = manifest.analysis["sections"]
            key = harmony["key"]
            comparison = manifest.analysis["song_spec_comparison"]
            defects = manifest.defects
            defects_file = manifest.defects_file
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
            loudness = manifest.analysis.get("loudness")
            if loudness is not None:
                print(
                    f"- integrated loudness: {loudness['integrated_lufs']} LUFS "
                    f"(range {loudness['loudness_range_lu']} LU, "
                    f"{loudness['gated_blocks']}/{loudness['total_blocks']} blocks kept)"
                )
            spectrum = manifest.analysis.get("spectrum")
            if spectrum is not None:
                shares = " ".join(
                    f"{name} {spectrum['bands'][name]['share']:.0%}"
                    for name in ("sub", "bass", "low_mid", "mid", "high_mid", "high")
                )
                print(f"- spectral balance: {shares}")
                print(
                    f"- low/high ratio: {spectrum['low_to_high_ratio']} "
                    f"(corpus median 21.9), centroid {spectrum['centroid_hz']:.0f} Hz"
                )
            print(f"- peak: {level['peak_dbfs']} dBFS")
            print(f"- RMS: {level['rms_dbfs']} dBFS")
            return 0

        if args.command == "edit":
            return song.edit(args)

        if args.command == "apply-edit":
            return song.apply_edit(args)

        if args.command == "plan-chunks":
            return song.plan_chunks(args)

        if args.command == "learn":
            return song.learn(args)

        if args.command == "lyrics":
            return song.lyrics(args)

        if args.command == "revise":
            if args.dry_run:
                if args.revision_log_markdown is not None:
                    raise ValueError(
                        "--revision-log-markdown cannot be used with --dry-run"
                    )
                manifest = review_project(args.project, overwrite=True)
                plan = manifest.review
                print(f"Would revise: {args.project}")
                print(
                    f"- alignment {plan['alignment']['score']} ({plan['alignment']['grade']})"
                )
                selection = json.loads(
                    manifest.repaint_plan_file.read_text(encoding="utf-8")
                )["selection"]
                print(f"- first round would repaint: {selection}")
                print("- nothing rendered (--dry-run)")
                return 0

            client = ace_client(args)

            def render(project: Path, source_audio: Path) -> None:
                spec = load_project_spec(project)
                plan = load_repaint_plan(project / "repaint_plan.json")
                selection = plan["selection"]
                settings = plan["ace_step_options"]
                guard = float(settings.get("tail_guard_bars", 0.0))
                if selection.get("section_name"):
                    window = resolve_repaint_window(
                        spec,
                        section_name=str(selection["section_name"]),
                        tail_guard_bars=guard,
                    )
                else:
                    window = resolve_repaint_window(
                        spec,
                        bar_range=f"{int(selection['start_bar'])}:{int(selection['end_bar'])}",
                        tail_guard_bars=guard,
                    )
                render_with_ace_step(
                    project,
                    client,
                    AceStepOptions(
                        audio_format="wav",
                        revision=str(plan["revision_prompt"]),
                        task_type="repaint",
                        audio_cover_strength=float(
                            settings.get("audio_cover_strength", 1.0)
                        ),
                        cover_noise_strength=float(
                            settings.get("cover_noise_strength", 0.0)
                        ),
                        repainting_start=window.start_sec,
                        repainting_end=window.end_sec,
                        repaint_mode=str(settings.get("repaint_mode", "balanced")),
                        repaint_strength=float(settings.get("repaint_strength", 0.65)),
                        repaint_latent_crossfade_frames=int(
                            settings.get("repaint_latent_crossfade_frames", 10)
                        ),
                        repaint_wav_crossfade_sec=float(
                            settings.get("repaint_wav_crossfade_sec", 0.25)
                        ),
                        chunk_mask_mode=str(settings.get("chunk_mask_mode", "explicit")),
                        tail_guard_bars=guard,
                    ),
                    source_audio=source_audio,
                    repaint_selection=window,
                    poll_interval=args.poll_interval,
                    wait_timeout=args.wait_timeout,
                )

            def announce(round_) -> None:
                defects = ", ".join(round_.defect_codes) or "clean"
                print(
                    f"  [{round_.index}] {round_.alignment:6.2f} {round_.grade:<14} "
                    f"{defects:<26} {round_.project_dir.name}"
                )

            print(f"Revising: {args.project} (up to {args.rounds} rounds)")
            log_file = args.project / "revision_log.json"
            # The loop writes this after every round, so a run that dies on the
            # third render still leaves the two takes it measured.
            log = run_revision_loop(
                args.project,
                render,
                rounds=args.rounds,
                on_round=announce,
                resume=args.resume,
                log_file=log_file,
                markdown_log_file=args.revision_log_markdown,
            )
            for line in describe_revisions(log):
                print(line)
            print(f"- log: {log_file}")
            if args.revision_log_markdown is not None:
                print(f"- markdown log: {args.revision_log_markdown}")
            return 0

        if args.command == "report":
            seen, stopped = _candidate_projects(args)
            candidates = [load_candidate(path) for path in seen]
            decision = current_decision(load_decision_log(args.project))
            if decision is not None:
                decision = {
                    **decision,
                    "audio_status": decision_audio_status(args.project, decision),
                }
            destination = args.output or (args.project / "candidates.html")
            if destination.exists() and not args.overwrite:
                raise FileExistsError(f"refusing to overwrite report: {destination}")
            destination.write_text(
                build_report(
                    candidates,
                    base_dir=destination.parent,
                    title=f"KIHACHI candidates: {args.project.name}",
                    stopped_because=stopped,
                    decision=decision,
                ),
                encoding="utf-8",
            )
            print(f"Candidate report: {destination}")
            for position, item in enumerate(rank_candidates(candidates), start=1):
                if not item.scanned:
                    defects = "not scanned"
                else:
                    defects = ", ".join(d["code"] for d in item.defects) or "clean"
                print(
                    f"  #{position} {item.alignment:6.2f} {item.grade:<14} "
                    f"{defects:<26} {item.name}"
                )
            if decision is None:
                print("- nothing adopted; open the page and listen")
            else:
                print(
                    f"- current listening decision: {decision['selected']['name']} "
                    f"({decision['reason']}); audio {decision['audio_status']['status']}"
                )
            return 0

        if args.command == "cut-sample":
            try:
                start_text, end_text = args.bars.split(":", 1)
                start_bar, end_bar = int(start_text), int(end_text)
            except ValueError:
                raise ValueError(
                    f"--bars must be START:END with whole bars, got {args.bars!r}"
                ) from None
            spec_path = args.project / "song_spec.json"
            spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))
            manifest = cut_sample(
                args.project,
                spec=spec,
                start_bar=start_bar,
                end_bar=end_bar,
                name=args.name,
                audio_file=(args.project / args.audio) if args.audio else None,
                overwrite=args.overwrite,
            )
            record = manifest.record
            print(f"Cut KIHACHI sample: {manifest.sample_file}")
            print(
                f"- bars {record['bars']['start']}:{record['bars']['end']} "
                f"({record['bars']['count']} bars, {record['duration_sec']:.3f} s "
                f"at {record['bpm']} BPM)"
            )
            edges = record["edges"]
            for edge in ("start", "end"):
                offset = edges[f"{edge}_offset_sec"]
                if edges[f"{edge}_snapped_to_zero_crossing"]:
                    print(f"- {edge}: snapped to a zero crossing ({offset * 1000:+.2f} ms)")
                else:
                    print(f"- {edge}: no zero crossing nearby; faded instead")
            for defect in record["known_defects_inside"]:
                print(
                    f"- carries a known {defect['severity']} {defect['code']} at "
                    f"{defect['at_sec_in_sample']:.3f} s into the sample "
                    f"({defect['at_sec_in_render']:.3f} s in the render); "
                    "the cut did not make it, but the window kept it"
                )
            print(f"- key as designed: {record['key']} (not measured in the audio)")
            print(f"- source render left as it was: {record['source']['audio_file']}")
            print(f"- manifest: {manifest.manifest_file}")
            return 0

        if args.command == "intent":
            if args.intent_command == "prepare":
                request = build_intent_request(args.prompt, model=args.model)
                # Printed rather than sent, like `ace-step prepare`: the whole
                # request is checkable before anything leaves the machine.
                print(json.dumps(request, indent=2, ensure_ascii=False))
                print(
                    "- nothing sent. The API key is read from the environment "
                    "only and never appears here",
                    file=sys.stderr,
                )
                return 0
            reading = read_brief_with_model(args.prompt, model=args.model)
            print(f"Read the brief with {reading['model']}:")
            for trait in reading["traits"]:
                sign = "+" if trait["polarity"] > 0 else "-"
                print(
                    f"  {sign}{trait['name']:<14} strength {trait['strength']:g}"
                    f"   from {trait['evidence']!r}"
                )
            for phrase in reading["unmapped"]:
                print(f"  (no trait says this) {phrase!r}")
            if not reading["unmapped"]:
                print("- the vocabulary covered every musical statement in this brief")
            if args.output is not None:
                written = write_reading(args.output, reading, overwrite=args.overwrite)
                print(f"- reading: {written}")
            return 0

        if args.command == "read-brief":
            for line in describe_brief(read_coverage(args.prompt)):
                print(line)
            return 0

        if args.command == "transcribe-sample":
            written, transcription = transcribe_sample_file(
                args.project, name=args.name, overwrite=args.overwrite
            )
            coverage = transcription.coverage
            print(f"Transcribed KIHACHI sample: {written}")
            print(
                f"- {coverage['notes']} notes from {coverage['voiced_frames']} voiced "
                f"frames of {coverage['frames']} ({coverage['voiced_fraction']:.0%})"
            )
            print(
                f"- {coverage['starts_snapped_to_onsets']} note starts snapped to a "
                "detected onset; the rest sit on the tracker's 128 ms hop"
            )
            print("- monophonic only: a chord arrives as one note")
            if not coverage["notes"]:
                # Measured: the full-mix cut of this very project reads 1% voiced
                # and yields nothing, while its separated bass gives five notes.
                print(
                    "- nothing came back. A full mix reads as unvoiced to a "
                    "monophonic tracker; separate it first (`stems`) and "
                    "transcribe one stem"
                )
            return 0

        if args.command == "review-samples":
            reviews = []
            for project in [args.project, *args.also]:
                manifest_path = project / "sample_manifest.json"
                if not manifest_path.is_file():
                    raise FileNotFoundError(f"no samples cut here: {manifest_path}")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for record in manifest["samples"]:
                    reviews.append(
                        review_sample(
                            project / record["path"],
                            bpm=float(record["bpm"]),
                            source_audio=record.get("source", {}).get("audio_file"),
                            label=f"{project.name}/{record['name']}",
                        )
                    )
            for line in describe_material(reviews):
                print(line)
            return 0

        if args.command == "shortlist":
            seen, _ = _candidate_projects(args)
            if args.save:
                manifest = write_shortlist(
                    args.project, seen[1:], overwrite=args.overwrite
                )
                shortlist = manifest.shortlist
            else:
                manifest = None
                shortlist = build_shortlist(args.project, seen[1:])
            for line in describe_shortlist(shortlist):
                print(line)
            if manifest is not None:
                print(f"- shortlist: {manifest.shortlist_file}")
            return 0

        if args.command == "decide":
            manifest = record_decision(
                args.project,
                selected_project=args.selected,
                candidate_projects=args.also,
                reason=args.reason,
            )
            selected = manifest.entry["selected"]
            print(f"Recorded KIHACHI listening decision: {manifest.decision_file}")
            print(f"- action: {manifest.entry['action']}")
            print(f"- selected: {selected['name']} ({selected['audio_sha256']})")
            print(f"- reason: {manifest.entry['reason']}")
            print("- audio copied/overwritten/deleted: no/no/no")
            return 0

        if args.command == "instrumental-plan":
            plan = plan_instrumental_sections(args.project)
            print(f"Instrumental sections for {args.project}:")
            print(f"- {plan.reason}")
            for section in plan.sections:
                probability = (
                    "unset" if section.vocal_probability is None
                    else f"{section.vocal_probability:.2f}"
                )
                print(
                    f"    bars {section.start_bar}:{section.end_bar}  {section.name}  "
                    f"(energy {section.energy:.2f}, vocal_probability {probability})"
                )
            if plan.sections:
                # Printed rather than run: a repaint is minutes of GPU and
                # overwrites the take, so the caller decides.
                print("- run these in order, each against the previous take:")
                for command in plan.commands(base_url=args.base_url):
                    print(f"    {command}")
            if args.save:
                destination = write_instrumental_plan(args.project, overwrite=args.overwrite)
                print(f"- plan: {destination}")
            return 0

        if args.command == "trim-tail":
            if args.dry_run:
                plan = plan_tail_trim(
                    args.project,
                    audio_file=args.audio_file,
                    pad_sec=args.pad,
                    threshold_dbfs=args.threshold_dbfs,
                )
                print(f"Would trim tail: {args.project}")
                _print_tail_trim_plan(plan)
                print("- nothing written (--dry-run)")
                return 0
            manifest = trim_project_tail(
                args.project,
                audio_file=args.audio_file,
                pad_sec=args.pad,
                threshold_dbfs=args.threshold_dbfs,
                overwrite=args.overwrite,
            )
            print(f"Trimmed KIHACHI tail: {manifest['manifest_file']}")
            print(f"- source kept as-is: {manifest['source_audio']}")
            print(f"- trimmed take: {manifest['trimmed_audio']}")
            _print_tail_trim_plan(TailTrimPlan(**manifest["plan"]))
            return 0

        if args.command == "stems":
            if args.stems_command == "prepare":
                plan = plan_separation(
                    args.project, audio_file=args.audio_file, model=args.model
                )
                print(f"Separation plan for {args.project}:")
                print(f"- source: {plan.source_audio}")
                print(f"- model: {plan.model}")
                # Printed, not run: separation wants a GPU and minutes, and the
                # separator is deliberately outside this package (ADR-0008).
                print("- run this yourself, wherever the separator lives:")
                print(f"    {' '.join(plan.command)}")
                print("- then take the result in:")
                print(f"    python3 -m kihachi_music_ai stems import {args.project}")
                print("- expected afterwards:")
                for path in plan.expected_stems:
                    print(f"    {path}")
                print("- nothing written")
                return 0
            manifest = import_stems(
                args.project,
                audio_file=args.audio_file,
                model=args.model,
                overwrite=args.overwrite,
            )
            print(f"Imported KIHACHI stems: {args.project}")
            print(f"- source audio: {manifest['source_audio']['path']}")
            print(f"- model: {manifest['model']}")
            for entry in manifest["stems"]:
                print(f"    {entry['stem']:<7} {entry['duration_sec']:.3f} s  {entry['path']}")
            print(f"- manifest: {args.project / 'stem_manifest.json'}")
            print("- measure one with: analyze --audio audio/stems/other.wav")
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
                split_drums=args.split_drums,
                audio_tracks=_audio_tracks(args),
                sends=[parse_send_binding(text) for text in args.send],
                overwrite=args.overwrite,
            )
            plan = manifest.plan
            heading = plan["song"]
            print(f"Planned KIHACHI arrangement for Live: {manifest.project_dir}")
            print(
                f"- {heading['title']}: {heading['total_bars']} bars / "
                f"{heading['total_beats']:g} beats "
                f"at {heading['bpm']:g} BPM, {heading['key']}"
            )
            for track in plan["tracks"]:
                if "notes" in track:
                    detail = f"{track['notes']} notes"
                elif track.get("file"):
                    detail = f"audio: {Path(track['file']).name}"
                else:
                    detail = "audio, empty"
                print(f"    track {track['live_track_index']}: {track['name']} ({detail})")
            # The resting tracks are the point of the whole MIDI path: the audio
            # model kept playing drums through the breakdown, the MIDI does not.
            for section in plan["structure"]:
                resting = ", ".join(section["resting_tracks"]) or "-"
                print(
                    f"    bar {section['start_bar']:>3}  {section['name']:<20} "
                    f"energy {section['energy']:.2f}  resting: {resting}"
                )
            for operation in plan["operations"]:
                if operation["op"] == "apply_live_instrument_selection":
                    params = operation["params"]
                    print(
                        f"- instrument: track {params['track_index']} role {params['role']} "
                        f"({params['genre']}, {params['mood']}; AbletonGPT selects the device)"
                    )
                if operation["op"] == "apply_live_drum_kit":
                    params = operation["params"]
                    print(
                        f"- drum kit: track {params['track_index']} role {params['role']} "
                        f"({params['genre']}, {params['mood']}; AbletonGPT resolves the kit)"
                    )
                if operation["op"] == "set_clip_send_envelope":
                    params = operation["params"]
                    values = [step["value"] for step in params["steps"]]
                    print(
                        f"- send envelope: track {params['track_index']} → return "
                        f"{chr(ord('A') + params['send_index'])}, "
                        f"{len(values)} steps ({min(values):.3f}-{max(values):.3f})"
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
            for finding in manifest.review["findings"]:
                if finding["code"] in {"dull_high_end", "bass_masking"}:
                    print(f"- {finding['code']}: {finding['evidence']}")
            defects = manifest.review.get("material_defects")
            if defects is not None:
                if defects["clean"]:
                    print("- material defects: none")
                else:
                    for defect in defects["findings"]:
                        if defect["severity"] in {"blocking", "warning"}:
                            print(f"- material {defect['severity']}: {defect['detail']}")
            tail_silence = manifest.review.get("tail_silence")
            if tail_silence is not None:
                # Naming the remedy here is the point: the repaint candidate printed
                # below cannot remove a tail, and following it costs a render.
                print(
                    f"- silent tail: {tail_silence['silence_sec']:.2f} s runs to the end; "
                    f"a repaint cannot remove it -- run `trim-tail`"
                )
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
            brief_path = args.from_brief
            if brief_path is not None and not brief_path.is_absolute():
                brief_path = args.project / brief_path
            request_path, _request = prepare_ace_step_request(
                args.project,
                options,
                overwrite=args.overwrite,
                brief=brief_path,
            )
            print(f"Prepared ACE-Step request: {request_path}")
            if brief_path is not None:
                print(f"- from render brief: {brief_path}")
                _warn_if_brief_is_stale(brief_path, args.project)
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
                ace_client(args),
                plan,
                lora=lora,
                base_options=AceStepOptions(
                    audio_format=args.audio_format,
                    inference_steps=args.inference_steps,
                ),
                poll_interval=args.poll_interval,
                wait_timeout=args.wait_timeout,
                overwrite=args.overwrite,
                resume=args.resume,
                plan_file=plan_path,
            )
            print(f"Rendered KIHACHI chunk plan: {manifest.project_dir}")
            for step in manifest.steps:
                origin = (
                    "reused" if step.get("reused_from_previous_run") else f"task {step['task_id']}"
                )
                print(
                    f"    [{step['index']}] {step['task_type']:<11} "
                    f"bars {step['bars'][0]}-{step['bars'][1]} "
                    f"({', '.join(step['sections'])}) {origin}"
                )
            print(f"- final audio: {manifest.audio_file}")
            print(f"- chain log: {manifest.log_file}")
            return 0

        if args.ace_command == "lora":
            client = ace_client(args)
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
            print_lora_status(status)
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
        brief_path = args.from_brief
        if brief_path is not None and not brief_path.is_absolute():
            brief_path = args.project / brief_path
        if brief_path is not None:
            _warn_if_brief_is_stale(brief_path, args.project)
        render = render_with_ace_step(
            args.project,
            ace_client(args),
            options,
            brief=brief_path,
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
    # RuntimeError covers the intent reader's two refusals to proceed: a missing
    # API key and a missing optional SDK. Both are things the caller fixes, not
    # bugs, so they read as one-line errors rather than tracebacks.
    except (
        AceStepError,
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
