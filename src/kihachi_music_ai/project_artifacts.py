"""Resolve the MIDI artifacts owned by a project's SongSpec."""

from __future__ import annotations

from pathlib import Path

from .models import SongSpec


def managed_midi_names(spec: SongSpec) -> tuple[str, ...]:
    """Return the MIDI filenames declared by ``spec``, in stable part order."""

    return tuple(f"{part}.mid" for part in spec.parts())


def managed_midi_paths(project_dir: Path, spec: SongSpec) -> tuple[Path, ...]:
    """Return the project paths for every MIDI artifact declared by ``spec``."""

    project_dir = Path(project_dir)
    return tuple(project_dir / name for name in managed_midi_names(spec))


def require_managed_midi(
    project_dir: Path,
    spec: SongSpec,
    *,
    context: str,
) -> tuple[Path, ...]:
    """Resolve declared MIDI and fail clearly when the project is incomplete."""

    paths = managed_midi_paths(project_dir, spec)
    missing = tuple(path for path in paths if not path.is_file())
    if missing:
        names = ", ".join(path.name for path in missing)
        raise FileNotFoundError(f"{context} missing managed MIDI artifact(s): {names}")
    return paths
