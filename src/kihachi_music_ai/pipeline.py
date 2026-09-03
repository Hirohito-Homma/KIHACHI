from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .adapters.ace_step import (
    AceStepClient,
    AceStepError,
    AceStepOptions,
    AceStepRenderManifest,
    AceStepTaskResult,
    load_project_spec,
    render_with_ace_step,
    resolve_repaint_window,
)
from .repaint_planner import load_repaint_plan
from .revision import DEFAULT_ROUNDS, Renderer, RevisionLog, Round, run_revision_loop
from .analyzer import AudioAnalysisManifest, analyze_project
from .composer import compose_tracks
from .lyrics import compile_lyrics
from .midi import write_midi
from .models import CORE_TRACKS, SongSpec
from .music_brain import MusicBrain
from .preferences import Preferences
from .project_artifacts import managed_midi_names
from .prompt_compiler import compile_audio_prompt, render_brief
from .reviewer import GenerationReviewManifest, review_project, review_project_midi_only
from .tail_guard import DEFAULT_TAIL_GUARD_BARS

ARTIFACT_NAMES = (
    "song_spec.json",
    *(f"{name}.mid" for name in CORE_TRACKS),
    "prompt.txt",
    "prompt.json",
    "lyrics.txt",
)
"""What a legacy/default core-three song writes."""


def artifact_names(spec: SongSpec) -> tuple[str, ...]:
    """The files this particular SongSpec writes, in a stable order."""

    extras = tuple(
        name
        for name in managed_midi_names(spec)
        if name not in ARTIFACT_NAMES
    )
    return ARTIFACT_NAMES + extras


@dataclass(frozen=True)
class ArtifactManifest:
    output_dir: Path
    spec: SongSpec
    files: tuple[Path, ...]


@dataclass(frozen=True)
class VerticalSliceManifest:
    compose: ArtifactManifest
    review: GenerationReviewManifest


@dataclass(frozen=True)
class AudioVerticalSliceManifest:
    compose: ArtifactManifest
    render: AceStepRenderManifest
    analysis: AudioAnalysisManifest
    review: GenerationReviewManifest


@dataclass(frozen=True)
class AudioRevisionLoopManifest:
    initial: AudioVerticalSliceManifest | None
    revision_log: RevisionLog


def slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "kihachi-project"


def compose_project(
    prompt: str,
    output_dir: Path | None = None,
    *,
    seed: int = 8,
    overwrite: bool = False,
    preferences: Preferences | None = None,
) -> ArtifactManifest:
    spec = MusicBrain(seed=seed, preferences=preferences).analyze(prompt)
    destination = Path(output_dir) if output_dir is not None else Path("projects") / slugify_title(spec.song.title)
    names = artifact_names(spec)
    existing = [destination / name for name in names if (destination / name).exists()]
    if existing and not overwrite:
        names = ", ".join(path.name for path in existing)
        raise FileExistsError(f"refusing to overwrite existing artifacts: {names}")

    previous_midi: tuple[str, ...] = ()
    previous_spec_path = destination / "song_spec.json"
    if overwrite and previous_spec_path.is_file():
        try:
            previous_spec = SongSpec.from_json(previous_spec_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            # Without valid project metadata, no existing MIDI can safely be
            # classified as managed rather than imported or user-authored.
            pass
        else:
            previous_midi = managed_midi_names(previous_spec)

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        spec.write_json(stage / "song_spec.json")
        tracks = compose_tracks(spec)
        for name, notes in tracks.items():
            write_midi(
                stage / f"{name}.mid",
                notes,
                track_name=f"KIHACHI {name.title()}",
                bpm=spec.song.bpm,
                key=spec.song.key,
            )
        (stage / "prompt.txt").write_text(compile_audio_prompt(spec), encoding="utf-8")
        # The same prompt, structured, for any renderer -- including none yet.
        (stage / "prompt.json").write_text(
            json.dumps(render_brief(spec), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (stage / "lyrics.txt").write_text(compile_lyrics(spec), encoding="utf-8")

        destination.mkdir(parents=True, exist_ok=True)
        for name in names:
            os.replace(stage / name, destination / name)
        current_midi = set(managed_midi_names(spec))
        for stale_name in previous_midi:
            stale_path = destination / stale_name
            if stale_name not in current_midi and stale_path.is_file():
                stale_path.unlink()
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    files = tuple(destination / name for name in names)
    return ArtifactManifest(destination, spec, files)


def run_vertical_slice(
    prompt: str,
    output_dir: Path | None = None,
    *,
    seed: int = 8,
    overwrite: bool = False,
    preferences: Preferences | None = None,
) -> VerticalSliceManifest:
    """Compose from a brief and run the local MIDI review + critic path."""

    compose = compose_project(
        prompt,
        output_dir,
        seed=seed,
        overwrite=overwrite,
        preferences=preferences,
    )
    review = review_project_midi_only(compose.output_dir, overwrite=overwrite)
    return VerticalSliceManifest(compose=compose, review=review)


def _project_lyrics(project_dir: Path, *, use_lyrics: bool) -> str:
    """Lyrics for ACE-Step: the project's own sheet when present."""

    if not use_lyrics:
        return ""
    lyrics_path = project_dir / "lyrics.txt"
    return lyrics_path.read_text(encoding="utf-8") if lyrics_path.is_file() else ""


def _require_generated_audio(audio_files: tuple[Path, ...]) -> Path:
    """Fail at the audio artifact boundary before analysis or review."""

    if not audio_files:
        raise AceStepError("ACE-Step render completed without audio files")
    canonical = audio_files[0]
    if not canonical.is_file():
        raise AceStepError(f"generated audio artifact is missing: {canonical}")
    if canonical.stat().st_size <= 0:
        raise AceStepError(f"generated audio artifact is empty: {canonical}")
    return canonical


def run_audio_vertical_slice(
    prompt: str,
    output_dir: Path | None = None,
    *,
    client: AceStepClient,
    seed: int = 8,
    overwrite: bool = False,
    preferences: Preferences | None = None,
    no_lyrics: bool = False,
    tail_guard_bars: float | None = None,
    poll_interval: float = 2.0,
    wait_timeout: float = 600.0,
    on_poll: Callable[[AceStepTaskResult, float], None] | None = None,
) -> AudioVerticalSliceManifest:
    """Compose, render through ACE-Step, analyze, and run the audio-aware review path."""

    guard = DEFAULT_TAIL_GUARD_BARS if tail_guard_bars is None else tail_guard_bars
    compose = compose_project(
        prompt,
        output_dir,
        seed=seed,
        overwrite=overwrite,
        preferences=preferences,
    )
    project_dir = compose.output_dir
    options = AceStepOptions(
        audio_format="wav",
        task_type="text2music",
        tail_guard_bars=guard,
        lyrics=_project_lyrics(project_dir, use_lyrics=not no_lyrics),
    )
    render = render_with_ace_step(
        project_dir,
        client,
        options,
        overwrite=overwrite,
        poll_interval=poll_interval,
        wait_timeout=wait_timeout,
        on_poll=on_poll,
    )
    _require_generated_audio(render.audio_files)
    analysis = analyze_project(project_dir, overwrite=overwrite)
    review = review_project(project_dir, overwrite=overwrite)
    return AudioVerticalSliceManifest(
        compose=compose,
        render=render,
        analysis=analysis,
        review=review,
    )


def make_ace_step_repaint_renderer(
    client: AceStepClient,
    *,
    poll_interval: float = 2.0,
    wait_timeout: float = 600.0,
) -> Renderer:
    """Build a :class:`~kihachi_music_ai.revision.Renderer` from a staged repaint plan.

    The staged project's ``repaint_plan.json`` is the machine-readable authority for
    task type, selection window, and ACE-Step options. ``source_audio`` is the take
    being repainted relative to, validated by staging rather than copied into the
    revision project.
    """

    def render(project_dir: Path, source_audio: Path) -> None:
        spec = load_project_spec(project_dir)
        plan = load_repaint_plan(project_dir / "repaint_plan.json")
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
            project_dir,
            client,
            AceStepOptions(
                audio_format="wav",
                revision=str(plan["revision_prompt"]),
                task_type="repaint",
                audio_cover_strength=float(settings.get("audio_cover_strength", 1.0)),
                cover_noise_strength=float(settings.get("cover_noise_strength", 0.0)),
                repainting_start=window.start_sec,
                repainting_end=window.end_sec,
                repaint_mode=str(settings.get("repaint_mode", "balanced")),
                repaint_strength=float(settings.get("repaint_strength", 0.65)),
                repaint_latent_crossfade_frames=int(
                    settings.get("repaint_latent_crossfade_frames", 10)
                ),
                repaint_wav_crossfade_sec=float(settings.get("repaint_wav_crossfade_sec", 0.25)),
                chunk_mask_mode=str(settings.get("chunk_mask_mode", "explicit")),
                tail_guard_bars=guard,
            ),
            source_audio=source_audio,
            repaint_selection=window,
            overwrite=True,
            poll_interval=poll_interval,
            wait_timeout=wait_timeout,
        )

    return render


def run_audio_revision_loop(
    project_dir: Path,
    client: AceStepClient,
    *,
    rounds: int = DEFAULT_ROUNDS,
    resume: bool = False,
    log_file: Path | None = None,
    markdown_log_file: Path | None = None,
    poll_interval: float = 2.0,
    wait_timeout: float = 600.0,
    on_round: Callable[[Round], None] | None = None,
) -> RevisionLog:
    """Run the existing revision loop with a real ACE-Step repaint renderer."""

    return run_revision_loop(
        project_dir,
        make_ace_step_repaint_renderer(
            client,
            poll_interval=poll_interval,
            wait_timeout=wait_timeout,
        ),
        rounds=rounds,
        resume=resume,
        log_file=log_file,
        markdown_log_file=markdown_log_file,
        on_round=on_round,
    )


def run_generate_and_revise(
    prompt: str,
    output_dir: Path | None = None,
    *,
    client: AceStepClient,
    seed: int = 8,
    overwrite: bool = False,
    preferences: Preferences | None = None,
    no_lyrics: bool = False,
    tail_guard_bars: float | None = None,
    rounds: int = DEFAULT_ROUNDS,
    resume: bool = False,
    log_file: Path | None = None,
    markdown_log_file: Path | None = None,
    poll_interval: float = 2.0,
    wait_timeout: float = 600.0,
    on_round: Callable[[Round], None] | None = None,
) -> AudioRevisionLoopManifest:
    """Compose and render once, then run the audio revision loop on that project."""

    initial = run_audio_vertical_slice(
        prompt,
        output_dir,
        client=client,
        seed=seed,
        overwrite=overwrite,
        preferences=preferences,
        no_lyrics=no_lyrics,
        tail_guard_bars=tail_guard_bars,
        poll_interval=poll_interval,
        wait_timeout=wait_timeout,
    )
    revision_log = run_audio_revision_loop(
        initial.compose.output_dir,
        client,
        rounds=rounds,
        resume=resume,
        log_file=log_file,
        markdown_log_file=markdown_log_file,
        poll_interval=poll_interval,
        wait_timeout=wait_timeout,
        on_round=on_round,
    )
    return AudioRevisionLoopManifest(initial=initial, revision_log=revision_log)

