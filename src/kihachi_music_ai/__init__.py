"""KIHACHI Music AI v0.1 public API."""

from .analyzer import AudioAnalysisManifest, analyze_project
from .models import SongSpec
from .music_brain import MusicBrain
from .pipeline import (
    ArtifactManifest,
    AudioRevisionLoopManifest,
    AudioVerticalSliceManifest,
    VerticalSliceManifest,
    compose_project,
    make_ace_step_repaint_renderer,
    run_audio_revision_loop,
    run_audio_vertical_slice,
    run_generate_and_revise,
    run_vertical_slice,
)
from .revision import RevisionLog, compare_rounds, describe_comparison
from .reviewer import GenerationReviewManifest, review_project, review_project_midi_only

__all__ = [
    "ArtifactManifest",
    "AudioAnalysisManifest",
    "AudioRevisionLoopManifest",
    "AudioVerticalSliceManifest",
    "MusicBrain",
    "GenerationReviewManifest",
    "RevisionLog",
    "SongSpec",
    "VerticalSliceManifest",
    "analyze_project",
    "compare_rounds",
    "compose_project",
    "describe_comparison",
    "make_ace_step_repaint_renderer",
    "review_project",
    "review_project_midi_only",
    "run_audio_revision_loop",
    "run_audio_vertical_slice",
    "run_generate_and_revise",
    "run_vertical_slice",
]
__version__ = "0.1.0"
