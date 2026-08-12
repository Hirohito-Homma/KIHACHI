"""Cut the model's silent tail off a delivered render, without touching the source.

The tail guard in :mod:`tail_guard` asks for extra bars so the model's own ending
lands past the song grid, then trims back to the grid. That works only if the
model honours the longer buffer. Measured on 2026-08-13 against
``acestep-v15-turbo``, it does not: a render asked for 74.182 s came back at
69.80 s, ``source_frames == kept_frames`` (nothing to trim back), and the music
stopped at 67.77 s. The guard was structurally unable to fire, and every take
carried a ~2 s silent tail -- past the 0.5 s threshold that makes the defect scan
report ``silent_gap`` as blocking.

So the tail is cut after the fact instead. :func:`plan_tail_trim` measures where
the music actually stops and reports what a cut would keep; :func:`trim_project_tail`
writes the result **beside** the render and never over it, because the untrimmed
take is the evidence for how the model behaved.

The cut makes the audio shorter than the SongSpec's grid, and that is a real
consequence rather than a rounding detail: a 69.818 s song delivered as 67.8 s no
longer lines up with a bar count. The manifest records both durations and the
shortfall in bars so nothing downstream has to infer it.

Pure and stdlib-only; the WAV work is reused from :mod:`tail_guard`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .models import SongSpec
from .tail_guard import (
    DEFAULT_TAIL_FADE_SEC,
    MUSIC_END_THRESHOLD_DBFS,
    measure_music_end,
    seconds_per_bar,
    trim_wav_to_duration,
)

#: Audio kept after the last audible sample, so a decaying reverb is not clipped.
DEFAULT_TAIL_PAD_SEC = 0.25

#: A cut shorter than this is not worth a second file; the defect scan calls a
#: gap blocking at 0.5 s, so anything under that is already acceptable.
MIN_TRIM_SEC = 0.5

DEFAULT_TRIMMED_SUFFIX = ".tail-trimmed"


@dataclass(frozen=True)
class TailTrimPlan:
    """What a tail cut would do, measured but not yet written."""

    audio_file: str
    source_duration_sec: float
    music_end_sec: float
    pad_sec: float
    kept_duration_sec: float
    removed_sec: float
    grid_duration_sec: float
    shortfall_sec: float
    shortfall_bars: float
    threshold_dbfs: float
    worth_trimming: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def diagnose_tail_silence(material_defects: Any) -> dict[str, Any] | None:
    """Report a blocking silence that sits at the very end of the take.

    Repainting cannot fix this one. Measured on 2026-08-13, two repaint rounds
    moved the tail from 4.80 s to 2.02 s and never removed it, because the cause
    is the delivered duration rather than any section's material. So the review
    says so, and the revision loop stops instead of spending another render on it.

    A gap in the *middle* is a different defect and is not reported here: that is
    material a repaint can genuinely rewrite.
    """

    if not isinstance(material_defects, dict):
        return None
    measurements = material_defects.get("measurements")
    if not isinstance(measurements, dict):
        return None
    duration = measurements.get("duration_sec")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return None

    for finding in material_defects.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        if finding.get("code") != "silent_gap" or finding.get("severity") != "blocking":
            continue
        start = measurements.get("longest_silence_at_sec")
        length = measurements.get("longest_silence_sec")
        if not isinstance(start, (int, float)) or not isinstance(length, (int, float)):
            continue
        # The gap has to run to the end of the file to be a tail.
        if float(start) + float(length) < float(duration) - TAIL_REACH_TOLERANCE_SEC:
            continue
        return {
            "silence_sec": round(float(length), 4),
            "silence_at_sec": round(float(start), 4),
            "duration_sec": round(float(duration), 4),
            "repaint_can_fix": False,
            "remedy": "trim-tail",
            "reason": (
                "the blocking silence runs to the end of the take, so it comes from "
                "the delivered duration rather than from a section a repaint could rewrite"
            ),
        }
    return None


#: How far short of the file end a gap may stop and still count as a tail.
TAIL_REACH_TOLERANCE_SEC = 0.05


def _wav_duration_sec(audio_path: Path) -> float:
    import wave

    with wave.open(str(audio_path), "rb") as source:
        if source.getframerate() <= 0:
            raise ValueError("WAV must declare a positive sample rate")
        return round(source.getnframes() / source.getframerate(), 4)


def plan_tail_trim(
    project_dir: Path | str,
    *,
    audio_file: Path | str | None = None,
    pad_sec: float = DEFAULT_TAIL_PAD_SEC,
    threshold_dbfs: float = MUSIC_END_THRESHOLD_DBFS,
) -> TailTrimPlan:
    """Measure the silent tail on a project's render. Reads only; writes nothing."""

    if pad_sec < 0.0:
        raise ValueError("pad_sec must not be negative")

    project_dir = Path(project_dir)
    spec_path = project_dir / "song_spec.json"
    if not spec_path.is_file():
        raise FileNotFoundError(f"SongSpec not found: {spec_path}")
    spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))

    audio_path = _resolve_audio(project_dir, audio_file)
    source_duration = _wav_duration_sec(audio_path)
    music_end = measure_music_end(audio_path, threshold_dbfs=threshold_dbfs)

    if music_end <= 0.0:
        return TailTrimPlan(
            audio_file=_display_path(audio_path, project_dir),
            source_duration_sec=source_duration,
            music_end_sec=0.0,
            pad_sec=pad_sec,
            kept_duration_sec=source_duration,
            removed_sec=0.0,
            grid_duration_sec=round(spec.song.target_duration_sec, 4),
            shortfall_sec=0.0,
            shortfall_bars=0.0,
            threshold_dbfs=threshold_dbfs,
            worth_trimming=False,
            # Refusing here matters: trimming a file that never rose above the
            # threshold would "fix" the defect by deleting the whole render.
            reason="audio never rises above the threshold; there is no music to keep",
        )

    kept = min(source_duration, round(music_end + pad_sec, 4))
    removed = round(source_duration - kept, 4)
    grid = round(spec.song.target_duration_sec, 4)
    shortfall = round(max(0.0, grid - kept), 4)
    bar_sec = seconds_per_bar(spec)
    worth = removed >= MIN_TRIM_SEC
    return TailTrimPlan(
        audio_file=_display_path(audio_path, project_dir),
        source_duration_sec=source_duration,
        music_end_sec=music_end,
        pad_sec=pad_sec,
        kept_duration_sec=kept,
        removed_sec=removed,
        grid_duration_sec=grid,
        shortfall_sec=shortfall,
        shortfall_bars=round(shortfall / bar_sec, 3) if bar_sec > 0 else 0.0,
        threshold_dbfs=threshold_dbfs,
        worth_trimming=worth,
        reason=(
            f"{removed:.2f} s of tail below {threshold_dbfs:g} dBFS"
            if worth
            else f"only {removed:.2f} s to remove, under the {MIN_TRIM_SEC:g} s floor"
        ),
    )


def trim_project_tail(
    project_dir: Path | str,
    *,
    audio_file: Path | str | None = None,
    pad_sec: float = DEFAULT_TAIL_PAD_SEC,
    threshold_dbfs: float = MUSIC_END_THRESHOLD_DBFS,
    fade_out_sec: float = DEFAULT_TAIL_FADE_SEC,
    suffix: str = DEFAULT_TRIMMED_SUFFIX,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write the trimmed take beside the render and return the audit manifest.

    The source render is never modified or replaced. The trimmed file is a new
    sibling, so both the delivered take and the cut one stay on disk.
    """

    project_dir = Path(project_dir)
    plan = plan_tail_trim(
        project_dir,
        audio_file=audio_file,
        pad_sec=pad_sec,
        threshold_dbfs=threshold_dbfs,
    )
    if not plan.worth_trimming:
        raise ValueError(f"refusing to trim: {plan.reason}")

    source_path = _resolve_audio(project_dir, audio_file)
    destination_path = source_path.with_name(f"{source_path.stem}{suffix}{source_path.suffix}")
    if destination_path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite trimmed audio: {destination_path}")

    trim = trim_wav_to_duration(
        source_path,
        destination_path,
        duration_sec=plan.kept_duration_sec,
        fade_out_sec=fade_out_sec,
    )

    manifest: dict[str, Any] = {
        "tail_trim_version": "0.1",
        "scope": "removes_a_silent_tail_only_never_replaces_the_render",
        "source_audio": _display_path(source_path, project_dir),
        "trimmed_audio": _display_path(destination_path, project_dir),
        "plan": plan.to_dict(),
        "trim": trim.to_dict(),
    }
    manifest_path = project_dir / "tail_trim.json"
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite tail trim manifest: {manifest_path}")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest["manifest_file"] = str(manifest_path)
    return manifest


def _resolve_audio(project_dir: Path, audio_file: Path | str | None) -> Path:
    if audio_file is None:
        audio_path = project_dir / "audio" / "ace-step-01.wav"
    else:
        audio_path = Path(audio_file)
        if not audio_path.is_absolute():
            audio_path = project_dir / audio_path
    if not audio_path.is_file():
        raise FileNotFoundError(f"WAV audio not found: {audio_path}")
    return audio_path


def _display_path(audio_path: Path, project_dir: Path) -> str:
    try:
        return str(audio_path.relative_to(project_dir))
    except ValueError:
        return str(audio_path)
