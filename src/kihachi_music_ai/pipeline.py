from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .composer import compose_tracks
from .lyrics import compile_lyrics
from .midi import write_midi
from .models import SongSpec
from .music_brain import MusicBrain
from .prompt_compiler import compile_audio_prompt

ARTIFACT_NAMES = (
    "song_spec.json",
    "bass.mid",
    "drums.mid",
    "chords.mid",
    "prompt.txt",
    "lyrics.txt",
)
"""What a core-three song writes. Kept as a constant because the overwrite guard
names these five files; a song with extra parts adds to it, never removes."""


def artifact_names(spec: SongSpec) -> tuple[str, ...]:
    """The files this particular SongSpec writes, in a stable order."""

    extra = tuple(f"{name}.mid" for name in spec.parts() if f"{name}.mid" not in ARTIFACT_NAMES)
    return ARTIFACT_NAMES + extra


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
) -> ArtifactManifest:
    spec = MusicBrain(seed=seed).analyze(prompt)
    destination = Path(output_dir) if output_dir is not None else Path("projects") / slugify_title(spec.song.title)
    existing = [destination / name for name in ARTIFACT_NAMES if (destination / name).exists()]
    if existing and not overwrite:
        names = ", ".join(path.name for path in existing)
        raise FileExistsError(f"refusing to overwrite existing artifacts: {names}")

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
        (stage / "lyrics.txt").write_text(compile_lyrics(spec), encoding="utf-8")

        destination.mkdir(parents=True, exist_ok=True)
        for name in artifact_names(spec):
            os.replace(stage / name, destination / name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    files = tuple(destination / name for name in artifact_names(spec))
    return ArtifactManifest(destination, spec, files)

