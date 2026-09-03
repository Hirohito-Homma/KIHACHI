"""KIHACHI Music AI v0.1 public API."""

from .analyzer import AudioAnalysisManifest, analyze_project
from .models import SongSpec
from .music_brain import MusicBrain
from .pipeline import (
    ArtifactManifest,
    AudioVerticalSliceManifest,
    VerticalSliceManifest,
    compose_project,
    run_audio_vertical_slice,
    run_vertical_slice,
)
from .reviewer import GenerationReviewManifest, review_project, review_project_midi_only

__all__ = [
    "ArtifactManifest",
    "AudioAnalysisManifest",
    "AudioVerticalSliceManifest",
    "MusicBrain",
    "GenerationReviewManifest",
    "SongSpec",
    "VerticalSliceManifest",
    "analyze_project",
    "compose_project",
    "review_project",
    "review_project_midi_only",
    "run_audio_vertical_slice",
    "run_vertical_slice",
]
__version__ = "0.1.0"
