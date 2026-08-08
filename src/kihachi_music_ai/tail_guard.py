"""Render tail guard: keep the model's own ending out of the scored song grid.

ACE-Step composes a *complete* song inside whatever buffer it is given, so a
render asked for exactly the song duration writes an ending before the buffer
runs out and leaves a near-silent outro inside the final bar. Measured on this
project's seed-8 renders, every full-length take stopped at 67.504 s of a
69.800 s buffer -- the last 2.296 s (roughly one bar at 110 BPM) were the
model's post-ending noise floor, which is why bar 32 scored ``normalized_energy
0.0`` in both the LoRA baseline and the first automatic repaint.

The guard asks for extra bars of duration so the composed ending lands *past*
the song grid, then trims the delivered audio back to the grid. The final bar
then carries intended musical energy instead of an accidental silent tail.

Pure and stdlib-only: bar math is deterministic, and the WAV helpers read and
write explicit paths without ever mutating their source.
"""

from __future__ import annotations

import hashlib
import math
import sys
import tempfile
import wave
from array import array
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .models import SongSpec

DEFAULT_TAIL_GUARD_BARS = 2.0
MAX_TAIL_GUARD_BARS = 8.0
DEFAULT_TAIL_FADE_SEC = 0.01
MUSIC_END_THRESHOLD_DBFS = -40.0


@dataclass(frozen=True)
class TrimManifest:
    """Audit record for one trim-back-to-grid operation."""

    source_frames: int
    kept_frames: int
    sample_rate: int
    channels: int
    sample_width: int
    requested_duration_sec: float
    kept_duration_sec: float
    fade_out_sec: float
    source_sha256: str
    trimmed_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def seconds_per_bar(spec: SongSpec) -> float:
    """Bar length in seconds from the SongSpec tempo and time signature."""

    try:
        numerator_text, denominator_text = spec.song.time_signature.split("/", 1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            f"invalid SongSpec time signature: {spec.song.time_signature!r}"
        ) from exc
    if numerator <= 0 or denominator <= 0:
        raise ValueError(f"invalid SongSpec time signature: {spec.song.time_signature!r}")
    quarter_note_beats_per_bar = numerator * (4.0 / denominator)
    return quarter_note_beats_per_bar * (60.0 / spec.song.bpm)


def validate_guard_bars(guard_bars: float) -> float:
    if not isinstance(guard_bars, (int, float)) or isinstance(guard_bars, bool):
        raise ValueError("tail_guard_bars must be a number")
    guard = float(guard_bars)
    if math.isnan(guard) or guard < 0.0:
        raise ValueError("tail_guard_bars must not be negative")
    if guard > MAX_TAIL_GUARD_BARS:
        raise ValueError(f"tail_guard_bars must not exceed {MAX_TAIL_GUARD_BARS:g}")
    return guard


def guard_seconds(spec: SongSpec, guard_bars: float) -> float:
    """Extra seconds of render buffer requested beyond the song grid."""

    return round(validate_guard_bars(guard_bars) * seconds_per_bar(spec), 3)


def guarded_duration(spec: SongSpec, guard_bars: float) -> float:
    """Duration to request from ACE-Step so the ending falls outside the grid."""

    return round(spec.song.target_duration_sec + guard_seconds(spec, guard_bars), 3)


def measure_music_end(
    audio_path: Path,
    *,
    threshold_dbfs: float = MUSIC_END_THRESHOLD_DBFS,
) -> float:
    """Seconds at which audible content stops, for auditing a silent tail.

    Returns the timestamp of the last sample above ``threshold_dbfs``; 0.0 when
    the file never rises above it.
    """

    threshold = 10.0 ** (threshold_dbfs / 20.0)
    with wave.open(str(Path(audio_path)), "rb") as source:
        channels = source.getnchannels()
        sample_rate = source.getframerate()
        sample_width = source.getsampwidth()
        frame_count = source.getnframes()
        if source.getcomptype() != "NONE":
            raise ValueError("compressed WAV is not supported")
        if channels <= 0 or sample_rate <= 0 or frame_count <= 0:
            raise ValueError("WAV must contain non-empty PCM audio")
        last_loud_frame = -1
        frame_index = 0
        while data := source.readframes(8192):
            samples = _decode_pcm(data, sample_width)
            for offset in range(0, len(samples) - channels + 1, channels):
                if any(abs(samples[offset + c]) > threshold for c in range(channels)):
                    last_loud_frame = frame_index
                frame_index += 1
    return round((last_loud_frame + 1) / sample_rate, 4) if last_loud_frame >= 0 else 0.0


def trim_wav_to_duration(
    source_path: Path,
    destination_path: Path,
    *,
    duration_sec: float,
    fade_out_sec: float = DEFAULT_TAIL_FADE_SEC,
) -> TrimManifest:
    """Write ``destination_path`` holding the first ``duration_sec`` of source.

    The source file is never modified. A short linear fade is applied to the very
    end of the kept audio so trimming mid-signal cannot produce a click; the fade
    is orders of magnitude shorter than a bar, so it does not move bar energy.
    """

    source_path = Path(source_path)
    destination_path = Path(destination_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("trim destination must differ from the source Audio")
    if duration_sec <= 0.0:
        raise ValueError("trim duration must be positive")
    if fade_out_sec < 0.0:
        raise ValueError("fade_out_sec must not be negative")

    with wave.open(str(source_path), "rb") as source:
        channels = source.getnchannels()
        sample_rate = source.getframerate()
        sample_width = source.getsampwidth()
        frame_count = source.getnframes()
        if source.getcomptype() != "NONE":
            raise ValueError("compressed WAV is not supported")
        if channels <= 0 or sample_rate <= 0 or frame_count <= 0:
            raise ValueError("WAV must contain non-empty PCM audio")
        if sample_width not in {1, 2, 3, 4}:
            raise ValueError(f"unsupported PCM sample width: {sample_width} bytes")
        keep_frames = min(frame_count, int(round(duration_sec * sample_rate)))
        if keep_frames <= 0:
            raise ValueError("trim duration is shorter than one frame")
        payload = bytearray(source.readframes(keep_frames))

    fade_frames = min(keep_frames, int(round(fade_out_sec * sample_rate)))
    if fade_frames > 0:
        _apply_fade_out(payload, fade_frames, channels, sample_width)

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".wav",
            prefix=f".{destination_path.name}-",
            dir=destination_path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        with wave.open(str(temporary), "wb") as sink:
            sink.setnchannels(channels)
            sink.setsampwidth(sample_width)
            sink.setframerate(sample_rate)
            sink.writeframes(bytes(payload))
        temporary.replace(destination_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    return TrimManifest(
        source_frames=frame_count,
        kept_frames=keep_frames,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        requested_duration_sec=round(duration_sec, 4),
        kept_duration_sec=round(keep_frames / sample_rate, 4),
        fade_out_sec=round(fade_frames / sample_rate, 4),
        source_sha256=file_sha256(source_path),
        trimmed_sha256=file_sha256(destination_path),
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _apply_fade_out(
    payload: bytearray,
    fade_frames: int,
    channels: int,
    sample_width: int,
) -> None:
    frame_bytes = channels * sample_width
    first_faded_frame = len(payload) // frame_bytes - fade_frames
    for step in range(fade_frames):
        gain = 1.0 - (step + 1) / fade_frames
        base = (first_faded_frame + step) * frame_bytes
        for channel in range(channels):
            offset = base + channel * sample_width
            value = _decode_sample(payload, offset, sample_width)
            payload[offset : offset + sample_width] = _encode_sample(
                value * gain, sample_width
            )


def _decode_sample(payload: bytearray, offset: int, sample_width: int) -> float:
    if sample_width == 1:
        return (payload[offset] - 128) / 128.0
    chunk = bytes(payload[offset : offset + sample_width])
    if sample_width == 2:
        return int.from_bytes(chunk, "little", signed=True) / 32768.0
    if sample_width == 3:
        return int.from_bytes(chunk, "little", signed=True) / 8388608.0
    return int.from_bytes(chunk, "little", signed=True) / 2147483648.0


def _encode_sample(value: float, sample_width: int) -> bytes:
    if sample_width == 1:
        scaled = int(round(value * 128.0)) + 128
        return bytes((max(0, min(255, scaled)),))
    limit = 1 << (8 * sample_width - 1)
    scaled = int(round(value * limit))
    scaled = max(-limit, min(limit - 1, scaled))
    return scaled.to_bytes(sample_width, "little", signed=True)


def _decode_pcm(data: bytes, sample_width: int) -> list[float]:
    if sample_width == 1:
        return [(value - 128) / 128.0 for value in data]
    if sample_width == 2:
        values = array("h")
        values.frombytes(data)
        if sys.byteorder != "little":
            values.byteswap()
        return [value / 32768.0 for value in values]
    if sample_width == 3:
        return [
            int.from_bytes(data[index : index + 3], "little", signed=True) / 8388608.0
            for index in range(0, len(data) - 2, 3)
        ]
    values = array("i")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    return [value / 2147483648.0 for value in values]
