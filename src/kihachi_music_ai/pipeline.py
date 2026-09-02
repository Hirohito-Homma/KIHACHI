from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .composer import compose_tracks
from .lyrics import compile_lyrics
from .midi import write_midi
from .models import CORE_TRACKS, SongSpec
from .music_brain import MusicBrain
from .preferences import Preferences
from .project_artifacts import managed_midi_names
from .prompt_compiler import compile_audio_prompt, render_brief

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

