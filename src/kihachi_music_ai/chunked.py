"""Chunked rendering: build a long song a few sections at a time.

A measured failure motivates this module. A 136-bar, nine-section arrangement
rendered in one text2music pass followed its own plan for about a third of the
song and then flattened out: planned boundary recall fell from 1.0 (at 32 bars,
four sections) to 0.25, section energy correlation from ~0.75 to 0.34, and the
sections that were supposed to be quiet -- a drumless dub breakdown, a 0.22
outro -- came back at 0.71 and 0.88. Nine sections do not survive one prompt.

So the song is rendered in chunks of whole sections, and **each chunk is
rendered under a prompt that describes only its own sections**. The first pass
lays down a full-length bed; every later chunk is a repaint of its own window
with the previous render as source, which is the path already measured to
preserve out-of-range audio at correlation 0.9999 and to splice without clicks.
The last chunk carries the tail guard, so the song still ends on music.

The planner is pure and read-only. Driving the renders is a separate function
that never writes into the source project.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters.ace_step import (
    AceStepClient,
    AceStepLoraConfig,
    AceStepOptions,
    AceStepRepaintWindow,
    render_with_ace_step,
    resolve_repaint_window,
)
from .models import TRACK_NAMES, SectionSpec, SongSpec
from .tail_guard import DEFAULT_TAIL_GUARD_BARS, seconds_per_bar, validate_guard_bars

CHUNK_PLAN_VERSION = "0.1"
DEFAULT_CHUNK_BARS = 32
# A trailing stub shorter than this is folded into the chunk before it rather
# than rendered on its own; an 8-bar repaint has too little context to work with.
MIN_CHUNK_BARS = 16

DEFAULT_CHUNK_OPTIONS: dict[str, Any] = {
    "task_type": "repaint",
    "audio_cover_strength": 1.0,
    "cover_noise_strength": 0.0,
    "repaint_mode": "balanced",
    "repaint_strength": 0.75,
    "repaint_latent_crossfade_frames": 10,
    "repaint_wav_crossfade_sec": 0.25,
    "chunk_mask_mode": "explicit",
}


@dataclass(frozen=True)
class ChunkRenderManifest:
    project_dir: Path
    plan_file: Path
    log_file: Path
    audio_file: Path
    steps: tuple[dict[str, Any], ...]


def build_chunk_plan(
    spec: SongSpec,
    *,
    target_chunk_bars: int = DEFAULT_CHUNK_BARS,
    tail_guard_bars: float = DEFAULT_TAIL_GUARD_BARS,
) -> dict[str, Any]:
    """Group whole sections into chunks and write a prompt for each."""

    if target_chunk_bars < MIN_CHUNK_BARS:
        raise ValueError(f"target_chunk_bars must be at least {MIN_CHUNK_BARS}")
    guard_bars = validate_guard_bars(tail_guard_bars)
    groups = _group_sections(spec, target_chunk_bars)

    chunks: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        first, last = group[0], group[-1]
        start_bar = first.start_bar + 1
        end_bar = last.start_bar + last.length_bars
        window = resolve_repaint_window(
            spec,
            bar_range=f"{start_bar}:{end_bar}",
            tail_guard_bars=guard_bars,
        )
        chunks.append(
            {
                "index": index + 1,
                "sections": [section.name for section in group],
                "task_type": "text2music" if index == 0 else "repaint",
                "selection": window.to_dict(),
                "revision_prompt": _chunk_prompt(spec, group, window, first_pass=index == 0),
                "ace_step_options": dict(DEFAULT_CHUNK_OPTIONS),
            }
        )
    return {
        "chunk_plan_version": CHUNK_PLAN_VERSION,
        "execution_state": "planned_not_rendered",
        "song_spec_sha256": song_spec_sha256(spec),
        "total_bars": spec.song.total_bars,
        "target_chunk_bars": target_chunk_bars,
        "tail_guard_bars": guard_bars,
        "rationale": (
            "One prompt cannot hold a nine-section arrangement; each chunk is "
            "rendered under a prompt describing only its own sections."
        ),
        "chunks": chunks,
        "safety": {
            "source_audio_mutated": False,
            "render_started": False,
            "writes_only_the_output_project": True,
        },
    }


def load_chunk_plan(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"chunk plan not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("chunk plan root must be an object")
    if payload.get("chunk_plan_version") != CHUNK_PLAN_VERSION:
        raise ValueError(f"unsupported chunk plan version: {payload.get('chunk_plan_version')!r}")
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("chunk plan requires a non-empty chunks list")
    if chunks[0].get("task_type") != "text2music":
        raise ValueError("the first chunk must lay the bed with text2music")
    for chunk in chunks[1:]:
        if chunk.get("task_type") != "repaint":
            raise ValueError("every chunk after the first must be a repaint")
    return payload


def render_chunk_plan(
    project_dir: Path,
    client: AceStepClient,
    plan: Mapping[str, Any],
    *,
    lora: AceStepLoraConfig | None = None,
    base_options: AceStepOptions | None = None,
    poll_interval: float = 3.0,
    wait_timeout: float = 1500.0,
    overwrite: bool = False,
) -> ChunkRenderManifest:
    """Render every chunk in order, each one from the previous render.

    Each step keeps its own audio and result under ``chunks/`` so the chain can
    be audited; the project's ``audio/ace-step-01.wav`` is the final pass.
    """

    project_dir = Path(project_dir)
    spec_path = project_dir / "song_spec.json"
    if not spec_path.is_file():
        raise FileNotFoundError(f"SongSpec not found: {spec_path}")
    spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))
    if plan.get("song_spec_sha256") != song_spec_sha256(spec):
        raise ValueError("chunk plan SongSpec does not match this project")

    log_file = project_dir / "chunk_render_log.json"
    final_audio = project_dir / "audio" / "ace-step-01.wav"
    for path in (log_file, final_audio):
        if path.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite chunk render artifact: {path}")

    chunks_dir = project_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    template = base_options or AceStepOptions()
    steps: list[dict[str, Any]] = []
    source_audio: Path | None = None

    for chunk in plan["chunks"]:
        step_dir = chunks_dir / f"{int(chunk['index']):02d}-{'-'.join(chunk['sections'])[:40]}"
        if step_dir.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite chunk step: {step_dir}")
        step_dir.mkdir(parents=True, exist_ok=True)
        spec.write_json(step_dir / "song_spec.json")

        selection = chunk["selection"]
        settings = chunk["ace_step_options"]
        is_first = chunk["task_type"] == "text2music"
        window: AceStepRepaintWindow | None = None
        options = _chunk_options(template, chunk, plan, settings, is_first)
        if not is_first:
            window = resolve_repaint_window(
                spec,
                bar_range=f"{int(selection['start_bar'])}:{int(selection['end_bar'])}",
                tail_guard_bars=float(plan.get("tail_guard_bars", 0.0)),
            )

        manifest = render_with_ace_step(
            step_dir,
            client,
            options,
            lora=lora,
            source_audio=source_audio,
            repaint_selection=window,
            overwrite=overwrite,
            poll_interval=poll_interval,
            wait_timeout=wait_timeout,
        )
        rendered = manifest.audio_files[0]
        steps.append(
            {
                "index": chunk["index"],
                "sections": chunk["sections"],
                "task_type": chunk["task_type"],
                "bars": [selection["start_bar"], selection["end_bar"]],
                "seconds": [selection["start_sec"], selection["end_sec"]],
                "task_id": manifest.task_id,
                "source_audio_sha256": _file_sha256(source_audio) if source_audio else None,
                "rendered_audio_sha256": _file_sha256(rendered),
                "rendered_audio": str(rendered.relative_to(project_dir)),
            }
        )
        source_audio = rendered

    assert source_audio is not None
    final_audio.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_audio, final_audio)

    document = {
        "chunk_plan_version": CHUNK_PLAN_VERSION,
        "execution_state": "rendered",
        "project": project_dir.name,
        "song_spec_sha256": plan["song_spec_sha256"],
        "chunks_rendered": len(steps),
        "final_audio": str(final_audio.relative_to(project_dir)),
        "final_audio_sha256": _file_sha256(final_audio),
        "steps": steps,
    }
    _atomic_write_text(log_file, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return ChunkRenderManifest(
        project_dir=project_dir,
        plan_file=project_dir / "chunk_plan.json",
        log_file=log_file,
        audio_file=final_audio,
        steps=tuple(steps),
    )


def song_spec_sha256(spec: SongSpec) -> str:
    return hashlib.sha256(spec.to_json().encode("utf-8")).hexdigest()


def _chunk_options(
    template: AceStepOptions,
    chunk: Mapping[str, Any],
    plan: Mapping[str, Any],
    settings: Mapping[str, Any],
    is_first: bool,
) -> AceStepOptions:
    selection = chunk["selection"]
    import dataclasses

    if is_first:
        return dataclasses.replace(
            template,
            task_type="text2music",
            revision=str(chunk["revision_prompt"]),
            tail_guard_bars=0.0,
        )
    return dataclasses.replace(
        template,
        task_type="repaint",
        revision=str(chunk["revision_prompt"]),
        audio_cover_strength=float(settings.get("audio_cover_strength", 1.0)),
        cover_noise_strength=float(settings.get("cover_noise_strength", 0.0)),
        repainting_start=float(selection["start_sec"]),
        repainting_end=float(selection["end_sec"]),
        repaint_mode=str(settings.get("repaint_mode", "balanced")),
        repaint_strength=float(settings.get("repaint_strength", 0.75)),
        repaint_latent_crossfade_frames=int(
            settings.get("repaint_latent_crossfade_frames", 10)
        ),
        repaint_wav_crossfade_sec=float(settings.get("repaint_wav_crossfade_sec", 0.25)),
        chunk_mask_mode=str(settings.get("chunk_mask_mode", "explicit")),
        tail_guard_bars=float(selection.get("tail_guard_sec", 0.0) and plan["tail_guard_bars"]),
    )


def _group_sections(spec: SongSpec, target_chunk_bars: int) -> list[list[SectionSpec]]:
    """Whole sections only: a repaint window that splits a section is not musical."""

    groups: list[list[SectionSpec]] = []
    current: list[SectionSpec] = []
    bars = 0
    for section in spec.arrangement:
        current.append(section)
        bars += section.length_bars
        if bars >= target_chunk_bars:
            groups.append(current)
            current, bars = [], 0
    if current:
        if groups and bars < MIN_CHUNK_BARS:
            groups[-1].extend(current)
        else:
            groups.append(current)
    return groups


def _chunk_prompt(
    spec: SongSpec,
    group: Sequence[SectionSpec],
    window: AceStepRepaintWindow,
    *,
    first_pass: bool,
) -> str:
    bar_seconds = seconds_per_bar(spec)
    sentences: list[str] = []
    scope = ", ".join(section.name.replace("_", " ") for section in group)
    if first_pass:
        sentences.append(
            f"This pass is only about the opening {window.end_bar} bars: {scope}. "
            f"Establish {spec.song.bpm:g} BPM, {spec.song.key}, {spec.song.time_signature} "
            "and the core groove there; the rest of the track is replaced later."
        )
    else:
        sentences.append(
            f"Repaint only bars {window.start_bar}-{window.end_bar} ({scope}). "
            f"Preserve all Audio outside this range exactly and keep "
            f"{spec.song.bpm:g} BPM, {spec.song.key}, {spec.song.time_signature}."
        )
    for section in group:
        sentences.append(_section_sentence(spec, section, bar_seconds))
    sentences.append(
        f"State one clear chord per bar in the repeating progression "
        f"{' - '.join(spec.harmony.progression)}; make {spec.song.key} unambiguous."
    )
    if not first_pass:
        sentences.append("Match the level and tone at both edges so the splice is inaudible.")
    if window.tail_guard_sec:
        sentences.append(
            f"Maintain intentional musical energy through bar {window.end_bar}; "
            "avoid an accidental silent tail."
        )
    return " ".join(sentences)


def _section_sentence(spec: SongSpec, section: SectionSpec, bar_seconds: float) -> str:
    start = section.start_bar + 1
    end = section.start_bar + section.length_bars
    resting = [track for track in TRACK_NAMES if not section.plays(track)]
    parts = [
        f"Bars {start}-{end} are {section.name.replace('_', ' ')} at energy "
        f"{section.energy:.2f}"
    ]
    if resting:
        parts.append("with no " + " or ".join(resting))
    if section.minimal:
        parts.append("kept minimal and sparse")
    if section.fx_amount is not None and section.fx_amount >= 0.85:
        parts.append("drenched in dub delay")
    if section.energy <= 0.3:
        parts.append("clearly quieter than everything around it")
    elif section.energy >= 0.9:
        parts.append("the loudest point of the track")
    return ", ".join(parts) + "."


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{path.name}-", dir=path.parent, delete=False
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
