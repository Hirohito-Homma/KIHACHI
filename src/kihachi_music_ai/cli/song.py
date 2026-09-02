"""The commands that shape a song: compose it, read it, change it, learn from it.

Each one takes a parsed ``Namespace`` and returns an exit status, so a test can
build the namespace directly instead of going through ``main`` and a list of
strings.
"""

from __future__ import annotations

import argparse
import json

from ..adapters.ace_step import load_project_spec
from ..arrangement import describe_arrangement
from ..chunked import build_chunk_plan
from ..edit import apply_edit_to_project, build_spec_edit
from ..lyrics import build_lyrics
from ..models import TRACK_NAMES
from ..brief import read_coverage
from ..pipeline import compose_project, run_vertical_slice
from ..preferences import compile_preferences, harvest, load as load_preferences
from ..web import serve as serve_briefs


def serve(args: argparse.Namespace) -> int:
    serve_briefs(args.host, args.port)
    return 0


def compose(args: argparse.Namespace) -> int:
    manifest = compose_project(
        args.prompt,
        args.output,
        seed=args.seed,
        overwrite=args.overwrite,
        preferences=load_preferences(args.preferences),
    )
    coverage = read_coverage(args.prompt)
    if coverage["unread"]:
        # Said at compose time because that is when it can still be acted on.
        # Measured: a real 85-character brief had six of its ten statements go
        # unread, and nothing anywhere reported it.
        print(
            f"- note: {len(coverage['unread'])} of {len(coverage['clauses'])} "
            "statements in this brief went unread; "
            "run `read-brief` to see which"
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


def local_slice(args: argparse.Namespace) -> int:
    manifest = run_vertical_slice(
        args.prompt,
        args.output,
        seed=args.seed,
        overwrite=args.overwrite,
        preferences=load_preferences(args.preferences),
    )
    coverage = read_coverage(args.prompt)
    if coverage["unread"]:
        print(
            f"- note: {len(coverage['unread'])} of {len(coverage['clauses'])} "
            "statements in this brief went unread; "
            "run `read-brief` to see which"
        )
    compose = manifest.compose
    review = manifest.review
    print(f"Local vertical slice: {compose.output_dir}")
    for path in compose.files:
        print(f"- {path.name}")
    spec = compose.spec
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
    midi_alignment = review.review["midi_alignment"]
    alignment = midi_alignment["alignment"]
    print(f"- midi alignment score: {alignment['score']} ({alignment['grade']})")
    density = midi_alignment["density"]
    print(
        f"- density diagnostic: {len(density['entries'])} section×part rows "
        f"({density['scope']})"
    )
    critic = review.review["critic"]
    print(f"- critic phase: {review.review['review_phase']}")
    print(f"- critic evidence: {critic['evidence_status']}")
    print(f"- findings: {len(review.review['findings'])}")
    print(f"- review: {review.review_file.name}")
    print(f"- revision prompt: {review.revision_prompt_file.name}")
    return 0


def edit(args: argparse.Namespace) -> int:
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
    qualities = ", ".join(interpretation["qualities"])
    if interpretation["direction"] == "refuse":
        # A refusal has no magnitude worth printing: it lands on the low pole
        # whatever the value was, and the changes below say what that means.
        print(f"- reading: {qualities} refused, down to the low pole")
    else:
        print(
            f"- reading: {qualities} "
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


def apply_edit(args: argparse.Namespace) -> int:
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


def plan_chunks(args: argparse.Namespace) -> int:
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


def learn(args: argparse.Namespace) -> int:
    observations = harvest(args.projects)
    if not observations:
        print(f"no applied edits found under {args.projects}")
        print("- run apply-edit first; nothing is learned from unapplied plans")
        return 1
    prefs = compile_preferences(observations)
    if args.out.exists() and not args.overwrite:
        raise FileExistsError(
            f"refusing to overwrite preferences: {args.out} (use --overwrite)"
        )
    args.out.write_text(
        json.dumps(prefs.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Learned from {args.projects}")
    print(f"- observations: {len(observations)}")
    print(f"- priors: {len(prefs.priors)}  fingerprint: {prefs.fingerprint}")
    ranked = sorted(
        (p for p in prefs.priors if p.genre != "*"),
        key=lambda p: -abs(p.offset),
    )
    for prior in ranked[:6]:
        print(
            f"    {prior.genre:22} {prior.path:26} "
            f"n={prior.samples:<3} offset {prior.offset:+.3f}"
        )
    print(f"- written: {args.out}")
    print("- nothing applies it yet; pass --preferences to compose")
    return 0


def lyrics(args: argparse.Namespace) -> int:
    sheet = build_lyrics(load_project_spec(args.project))
    print(f"Lyric sheet: {args.project}")
    print(f"- vocal mode: {sheet.mode}")
    print(f"- hook: {sheet.hook or '(none)'}")
    print(f"- lines: {sheet.line_count}")
    for section in sheet.sections:
        body = " / ".join(section.lines) if section.lines else "(no vocal)"
        print(f"    {section.section_name:18} {section.tag:10} {body}")
    return 0
