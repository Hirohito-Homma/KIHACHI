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
from .preference_memory import (
    PreferenceMemory,
    load_preference_memory,
    record_preference,
)
from .ableton_execution import (
    AbletonExecutionError,
    AbletonExecutionManifest,
    ValidatedHandoff,
    execute_ableton_handoff,
    load_validated_handoff,
    prepare_ableton_execution,
)
from .ableton_handoff import (
    AbletonHandoffError,
    AbletonHandoffManifest,
    AdoptedTake,
    build_ableton_handoff,
    resolve_adopted_take,
)
from .revision import (
    Adoption,
    AdoptionManifest,
    RevisionLog,
    adopt_revision,
    compare_rounds,
    describe_comparison,
    load_revision_log,
)
from .reviewer import GenerationReviewManifest, review_project, review_project_midi_only

__all__ = [
    "AbletonExecutionError",
    "AbletonExecutionManifest",
    "AbletonHandoffError",
    "AbletonHandoffManifest",
    "AdoptedTake",
    "Adoption",
    "AdoptionManifest",
    "ArtifactManifest",
    "AudioAnalysisManifest",
    "AudioRevisionLoopManifest",
    "AudioVerticalSliceManifest",
    "MusicBrain",
    "GenerationReviewManifest",
    "PreferenceMemory",
    "RevisionLog",
    "SongSpec",
    "ValidatedHandoff",
    "VerticalSliceManifest",
    "adopt_revision",
    "analyze_project",
    "build_ableton_handoff",
    "compare_rounds",
    "execute_ableton_handoff",
    "load_validated_handoff",
    "prepare_ableton_execution",
    "compose_project",
    "describe_comparison",
    "load_preference_memory",
    "load_revision_log",
    "make_ace_step_repaint_renderer",
    "record_preference",
    "resolve_adopted_take",
    "review_project",
    "review_project_midi_only",
    "run_audio_revision_loop",
    "run_audio_vertical_slice",
    "run_generate_and_revise",
    "run_vertical_slice",
]
__version__ = "0.1.0"
