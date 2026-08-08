"""KIHACHI Music AI v0.1 public API."""

from .analyzer import AudioAnalysisManifest, analyze_project
from .models import SongSpec
from .music_brain import MusicBrain
from .pipeline import ArtifactManifest, compose_project
from .reviewer import GenerationReviewManifest, review_project

__all__ = [
    "ArtifactManifest",
    "AudioAnalysisManifest",
    "MusicBrain",
    "GenerationReviewManifest",
    "SongSpec",
    "analyze_project",
    "compose_project",
    "review_project",
]
__version__ = "0.1.0"
