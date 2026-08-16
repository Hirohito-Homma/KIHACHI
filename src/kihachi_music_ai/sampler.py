"""Cut a bar-aligned sample out of the middle of a render.

The measurements this exists to act on: the generator does not follow the design
across time -- progression match 0.0 on a finished mix, tail guard sufficiency
that varies with the seed alone, an alignment score spanning 37.32 to 77.52 on
one design. What it does well is a few bars of texture. So the render stops
being the song and becomes material, and the song is carried by MIDI, which
reads back out of Live at 56 of 56 bars.

**Cut from the middle, not from a short render.** Asking the model for two bars
lands squarely in the failure it is worst at: 32 bars already came back 2.32 s
short of its own grid, and 56 bars missed on four seeds out of five. Rendering
16 or 32 bars and taking bars 8-12 avoids both ends -- the opening ramp and the
tail the guard cannot reach -- and uses only the stretch where the model is
steady.

Two cuts, not one. The window is placed on the bar grid, then each edge is
nudged to the nearest zero crossing so the join cannot click; `defects.py`
scores exactly that discontinuity, and a sample that trips its own scanner is
not worth keeping. The nudge is bounded, so a sample never drifts audibly off
the grid to find silence that is not there.

Nothing is overwritten. The render stays as it is -- it is the evidence for how
the model behaved -- and the sample lands beside it with a record of where it
came from.

Pure and stdlib-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import SongSpec
from .tail_guard import seconds_per_bar

SAMPLE_MANIFEST_VERSION = "sample-manifest-v1"
SAMPLE_DIRECTORY = "samples"
"""`<project>/audio/samples/`, one level below the render, same as stems."""

MAX_ZERO_CROSSING_NUDGE_SEC = 0.01
"""How far an edge may move to find a zero crossing.

10 ms at 110 BPM is 0.5% of a bar -- inaudible as timing, and long enough to
reach a crossing in anything but a sustained DC-ish tone. Beyond this the honest
answer is that there is no crossing nearby, so the edge stays on the grid and
the fade handles it.
"""

EDGE_FADE_SEC = 0.002
"""Applied when an edge could not be snapped. Two milliseconds kills a click."""


@dataclass(frozen=True)
class SampleManifest:
    project_dir: Path
    sample_file: Path
    manifest_file: Path
    record: dict[str, Any]


def _read_frames(path: Path) -> tuple[wave._wave_params, bytes]:
    with wave.open(str(path), "rb") as source:
        return source.getparams(), source.readframes(source.getnframes())


def _sample_at(raw: bytes, frame: int, channels: int) -> int:
    """First channel's value at ``frame``, as a signed 16-bit int."""

    offset = frame * channels * 2
    return int.from_bytes(raw[offset : offset + 2], "little", signed=True)


def _snap_to_zero_crossing(
    raw: bytes, frame: int, channels: int, total_frames: int, limit_frames: int
) -> tuple[int, bool]:
    """Nearest frame sitting closest to zero at a crossing, within ``limit_frames``.

    Searches outward from the target so the edge moves as little as possible.
    Returns the frame and whether a crossing was actually found -- an edge that
    was not snapped still has to be faded, and the caller has to know which.

    A crossing is a *pair* of frames, and the one after the sign change is not
    the quiet one: at 8 kHz a 220 Hz tone steps 17% of peak per frame, so
    landing on it leaves an edge as loud as the click it was meant to avoid.
    Whichever of the two sits closer to zero is the edge.
    """

    if total_frames <= 1:
        return frame, False
    for distance in range(0, limit_frames + 1):
        for candidate in sorted({frame - distance, frame + distance}):
            if candidate < 1 or candidate >= total_frames:
                continue
            previous = _sample_at(raw, candidate - 1, channels)
            current = _sample_at(raw, candidate, channels)
            if previous == 0 or (previous < 0) != (current < 0):
                return (candidate if abs(current) <= abs(previous) else candidate - 1), True
    return frame, False


def _fade(samples: bytearray, channels: int, frames: int, at_start: bool) -> None:
    """Linear fade over ``frames`` at one end, in place."""

    total = len(samples) // (channels * 2)
    frames = min(frames, total)
    for index in range(frames):
        gain = index / frames if at_start else 1.0 - index / frames
        frame = index if at_start else total - frames + index
        for channel in range(channels):
            offset = (frame * channels + channel) * 2
            value = int.from_bytes(samples[offset : offset + 2], "little", signed=True)
            scaled = int(value * gain)
            samples[offset : offset + 2] = scaled.to_bytes(2, "little", signed=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cut_sample(
    project_dir: Path,
    *,
    spec: SongSpec,
    start_bar: int,
    end_bar: int,
    name: str,
    audio_file: Path | None = None,
    overwrite: bool = False,
) -> SampleManifest:
    """Write bars ``start_bar``..``end_bar`` of the render as a sample.

    Bars are 1-based and ``end_bar`` is exclusive, so ``8:12`` is four bars
    starting at bar 8 -- the same convention the repaint window uses.
    """

    project_dir = Path(project_dir)
    if start_bar < 1:
        raise ValueError("start_bar is 1-based; the first bar is 1")
    if end_bar <= start_bar:
        raise ValueError("end_bar must be greater than start_bar")
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"invalid sample name: {name!r}")

    source = Path(audio_file) if audio_file else project_dir / "audio" / "ace-step-01.wav"
    if not source.is_file():
        raise FileNotFoundError(f"no render to cut from: {source}")

    bar_seconds = seconds_per_bar(spec)
    start_sec = (start_bar - 1) * bar_seconds
    end_sec = (end_bar - 1) * bar_seconds

    params, raw = _read_frames(source)
    channels, sample_rate = params.nchannels, params.framerate
    if params.sampwidth != 2:
        raise ValueError(f"expected 16-bit audio: {source}")
    total_frames = len(raw) // (channels * 2)
    duration_sec = total_frames / sample_rate if sample_rate else 0.0
    if end_sec > duration_sec:
        raise ValueError(
            f"bars {start_bar}:{end_bar} end at {end_sec:.3f} s but the render is "
            f"{duration_sec:.3f} s long"
        )

    limit = int(MAX_ZERO_CROSSING_NUDGE_SEC * sample_rate)
    grid_start = int(round(start_sec * sample_rate))
    grid_end = int(round(end_sec * sample_rate))
    start_frame, start_snapped = _snap_to_zero_crossing(
        raw, grid_start, channels, total_frames, limit
    )
    end_frame, end_snapped = _snap_to_zero_crossing(
        raw, grid_end, channels, total_frames, limit
    )
    if end_snapped and end_frame + 1 <= total_frames:
        # The end is exclusive, so the frame the snap chose would be dropped and
        # the last kept sample would be its neighbour. Measured on the first
        # cut: a sample that started at -9 ended at 158, 0.6% of peak, for want
        # of this. Include the chosen frame instead.
        end_frame += 1
    if end_frame <= start_frame:
        raise ValueError("sample window collapsed after snapping to zero crossings")

    cut = bytearray(raw[start_frame * channels * 2 : end_frame * channels * 2])
    fade_frames = int(EDGE_FADE_SEC * sample_rate)
    if not start_snapped:
        _fade(cut, channels, fade_frames, at_start=True)
    if not end_snapped:
        _fade(cut, channels, fade_frames, at_start=False)

    destination = project_dir / "audio" / SAMPLE_DIRECTORY / f"{name}.wav"
    if destination.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite sample: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as sink:
        sink.setnchannels(channels)
        sink.setsampwidth(2)
        sink.setframerate(sample_rate)
        sink.writeframes(bytes(cut))

    record = {
        "name": name,
        "path": f"audio/{SAMPLE_DIRECTORY}/{name}.wav",
        "sha256": _file_sha256(destination),
        "bars": {"start": start_bar, "end": end_bar, "count": end_bar - start_bar},
        "bpm": spec.song.bpm,
        "key": spec.song.key,
        "time_signature": spec.song.time_signature,
        "duration_sec": round((end_frame - start_frame) / sample_rate, 6),
        "grid_duration_sec": round(end_sec - start_sec, 6),
        "edges": {
            "start_snapped_to_zero_crossing": start_snapped,
            "end_snapped_to_zero_crossing": end_snapped,
            "start_offset_sec": round((start_frame - grid_start) / sample_rate, 6),
            "end_offset_sec": round((end_frame - grid_end) / sample_rate, 6),
            "faded_edges": [
                edge
                for edge, snapped in (("start", start_snapped), ("end", end_snapped))
                if not snapped
            ],
        },
        "source": _provenance(project_dir, source),
        "known_defects_inside": _known_defects_inside(project_dir, start_sec, end_sec),
        "known_defects_scope": (
            "only what the render's scan located, which is one position per code "
            "-- the worst one. An empty list is not a clean sample: a window can "
            "hold a second, smaller discontinuity the render-level scan never "
            "placed. Scan the sample itself"
        ),
        "scope": (
            "material cut from a render, not the render's design: the key and bpm "
            "here are what was asked for, not what was measured in the audio"
        ),
    }
    manifest_file = _append_to_manifest(project_dir, record, overwrite=overwrite)
    return SampleManifest(
        project_dir=project_dir,
        sample_file=destination,
        manifest_file=manifest_file,
        record=record,
    )


def _known_defects_inside(
    project_dir: Path, start_sec: float, end_sec: float
) -> list[dict[str, Any]]:
    """Defects the scan already located, that this window happens to contain.

    Cutting from the middle avoids the ends the model handles badly. It does not
    avoid what the material has in the middle: bars 27:31 of the first render
    tried here came back carrying the whole render's worst discontinuity,
    0.5884, because the window landed on top of it. The scan knows where these
    are; the window should be chosen knowing it too.
    """

    defects_path = project_dir / "material_defects.json"
    if not defects_path.is_file():
        return []
    payload = json.loads(defects_path.read_text(encoding="utf-8"))
    measured = payload.get("measurements", {})
    located = {
        "silent_gap": "longest_silence_at_sec",
        "discontinuity": "max_sample_jump_at_sec",
    }
    found: list[dict[str, Any]] = []
    for finding in payload.get("findings", []):
        key = located.get(finding.get("code"))
        at = measured.get(key) if key else None
        if at is None or not start_sec <= float(at) < end_sec:
            continue
        found.append(
            {
                "code": finding["code"],
                "severity": finding["severity"],
                "at_sec_in_render": round(float(at), 3),
                "at_sec_in_sample": round(float(at) - start_sec, 3),
            }
        )
    return found


def _provenance(project_dir: Path, source: Path) -> dict[str, Any]:
    """Which render, from which request, at which seed.

    A sample outlives the project it came from -- that is the point of cutting
    one -- so the trail has to travel with it rather than sit in a sibling file.
    """

    trail: dict[str, Any] = {
        "project": project_dir.name,
        "audio_file": str(source.relative_to(project_dir))
        if source.is_relative_to(project_dir)
        else str(source),
        "audio_sha256": _file_sha256(source),
    }
    brief_path = project_dir / "prompt.json"
    if brief_path.is_file():
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        trail["seed"] = brief.get("seed")
        trail["song_spec_sha256"] = brief.get("song_spec_sha256")
    result_path = project_dir / "ace_step_result.json"
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        trail["task_id"] = result.get("task_id")
        trail["model"] = result.get("model")
    return trail


def _append_to_manifest(
    project_dir: Path, record: dict[str, Any], *, overwrite: bool
) -> Path:
    """Add one sample to the project's manifest, replacing a same-named entry.

    Appending rather than rewriting: a project accumulates samples over several
    passes, and losing the earlier ones to the latest cut would throw away the
    provenance this file exists to keep.
    """

    destination = project_dir / "sample_manifest.json"
    if destination.is_file():
        manifest = json.loads(destination.read_text(encoding="utf-8"))
        version = manifest.get("manifest_version")
        if version != SAMPLE_MANIFEST_VERSION:
            raise ValueError(f"unsupported sample manifest version: {version!r}")
    else:
        manifest = {
            "manifest_version": SAMPLE_MANIFEST_VERSION,
            "scope": "cut_from_renders_never_replaces_them",
            "samples": [],
        }
    samples = [item for item in manifest["samples"] if item["name"] != record["name"]]
    if len(samples) != len(manifest["samples"]) and not overwrite:
        raise FileExistsError(f"sample {record['name']!r} is already in the manifest")
    samples.append(record)
    manifest["samples"] = sorted(samples, key=lambda item: item["name"])
    _atomic_write_text(
        destination, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    return destination


def _atomic_write_text(path: Path, content: str) -> None:
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as sink:
            sink.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
