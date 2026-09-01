"""Turn a monophonic sample into MIDI notes, and say how much it missed.

The v0.1 boundary listed Audio-to-MIDI as a next step, with a caveat that has
only got sharper: transcribing a *song* render captures what the model made, not
what KIHACHI designed, and the design is already in MIDI. ADR-0010 made that
concrete -- the render stopped being the song.

What is worth transcribing is **material**. A four-bar bass cut out of a render
is a line somebody might want re-voiced, moved into the song's key, or played by
a Live instrument instead of as audio. That needs notes, and notes are exactly
what `pitch.py` can now produce for one voice at a time.

The honesty this inherits is the point. Recall on audible frames measured 61%
across five real bass stems -- 48% to 93% -- so a transcription is a partial
reading of the audio, and its coverage travels with it rather than being implied
by the note count. Nothing here decides whether the result is good.

**Monophonic only.** A chord arrives as one note, usually its loudest partial.
The tracker has no opinion about polyphony and this does not invent one.

Pure and stdlib-only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .material import detect_onsets
from .midi import MidiNote
from .pitch import PitchFrame, track_pitch

TRANSCRIPTION_VERSION = "0.1"

MAX_SEMITONE_STEP = 0.75
"""How far the pitch may move inside one note before it becomes the next note.

Three quarters of a semitone: wide enough to ride vibrato and the tracker's own
few cents of wobble, narrow enough that a real step to a neighbouring note
starts one.
"""

MIN_NOTE_FRAMES = 2
"""A note has to hold for two analysis frames. One is a detection, not a note."""

DEFAULT_VELOCITY = 90

ONSET_SNAP_SEC = 0.13
"""How far a note start may be moved onto a detected onset.

The pitch tracker's hop is 128 ms, so a note start read from it can only land on
a 128 ms grid -- a quarter of a beat at 120 BPM, which is not a transcription
anyone can use. Onsets come from a 10 ms energy envelope and were exact on a
synthetic four-note line where the tracker's own starts were up to 0.23 beats
out. So the pitch says *what*, the onset says *when*, and this is how far the
two are allowed to be apart before the onset is treated as somebody else's.
"""


@dataclass(frozen=True)
class Transcription:
    notes: tuple[MidiNote, ...]
    coverage: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _nearest(values: Sequence[float], target: float) -> float | None:
    return min(values, key=lambda value: abs(value - target)) if values else None


def _segment(frames: Sequence[PitchFrame]) -> list[list[PitchFrame]]:
    """Split the voiced frames into runs that stay on one pitch."""

    runs: list[list[PitchFrame]] = []
    current: list[PitchFrame] = []
    for frame in frames:
        if not frame.voiced:
            if current:
                runs.append(current)
                current = []
            continue
        assert frame.midi is not None
        if current:
            previous = current[-1].midi
            assert previous is not None
            if abs(frame.midi - previous) > MAX_SEMITONE_STEP:
                runs.append(current)
                current = []
        current.append(frame)
    if current:
        runs.append(current)
    return runs


def transcribe(
    samples: Sequence[float],
    sample_rate: float,
    *,
    bpm: float,
    velocity: int = DEFAULT_VELOCITY,
) -> Transcription:
    """Notes in beats, plus what the reading could not account for."""

    frames = track_pitch(samples, sample_rate)
    if not frames:
        return Transcription(
            notes=(),
            coverage={
                "transcription_version": TRANSCRIPTION_VERSION,
                "frames": 0,
                "voiced_frames": 0,
                "voiced_fraction": 0.0,
                "notes": 0,
                "note": "the sample is shorter than one analysis window",
            },
        )

    hop_sec = frames[1].at_sec - frames[0].at_sec if len(frames) > 1 else 0.0
    beats_per_second = bpm / 60.0
    onsets = detect_onsets(samples, sample_rate)
    snapped = 0

    spans: list[tuple[float, float, int]] = []
    for run in _segment(frames):
        if len(run) < MIN_NOTE_FRAMES:
            continue
        pitches = [frame.midi for frame in run if frame.midi is not None]
        # The median, not the mean: one frame landing an octave out during a
        # transient would drag an average off the note the ear hears.
        ordered = sorted(pitches)
        median = ordered[len(ordered) // 2]
        start_sec = run[0].at_sec
        end_sec = run[-1].at_sec + hop_sec
        nearest = _nearest(onsets, start_sec)
        if nearest is not None and abs(nearest - start_sec) <= ONSET_SNAP_SEC:
            if nearest < end_sec:
                start_sec = nearest
                snapped += 1
        spans.append((start_sec, end_sec, int(round(median))))

    # A snapped start can now sit inside the note before it; the earlier note
    # ends where the later one begins rather than overlapping it.
    notes: list[MidiNote] = []
    for index, (start_sec, end_sec, pitch) in enumerate(spans):
        if index + 1 < len(spans):
            end_sec = min(end_sec, spans[index + 1][0])
        duration = (end_sec - start_sec) * beats_per_second
        if duration <= 0:
            continue
        notes.append(
            MidiNote(
                pitch=max(0, min(127, pitch)),
                start_beats=round(start_sec * beats_per_second, 6),
                duration_beats=round(duration, 6),
                velocity=velocity,
            )
        )

    voiced = sum(1 for frame in frames if frame.voiced)
    return Transcription(
        notes=tuple(notes),
        coverage={
            "transcription_version": TRANSCRIPTION_VERSION,
            "scope": "monophonic_only_a_chord_arrives_as_one_note",
            "frames": len(frames),
            "voiced_frames": voiced,
            "voiced_fraction": round(voiced / len(frames), 4),
            "notes": len(notes),
            "starts_snapped_to_onsets": snapped,
            "note": (
                "voiced_fraction counts rests as well as misses: the tracker "
                "found a pitch in this share of the analysis frames. Measured "
                "recall on frames that were actually audible is 61% across five "
                "real bass stems (48-93%), so quiet or inharmonic material comes "
                "back with holes rather than with wrong notes"
            ),
        },
    )


def read_wav_mono(path: Path) -> tuple[list[float], float]:
    """One channel's worth of a 16-bit WAV, averaged down from however many."""

    import wave
    from array import array

    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        rate = float(source.getframerate())
        if source.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit audio: {path}")
        raw = source.readframes(source.getnframes())
    data = array("h")
    data.frombytes(raw)
    if channels == 1:
        return [value / 32768.0 for value in data], rate
    return [
        sum(data[index : index + channels]) / channels / 32768.0
        for index in range(0, len(data) - channels + 1, channels)
    ], rate


def transcribe_sample_file(
    project_dir: Path, *, name: str, overwrite: bool = False
) -> tuple[Path, Transcription]:
    """Transcribe one sample from a project's manifest and write the MIDI beside it.

    The bpm comes from the manifest rather than from the audio: the sample was
    cut on that grid, and re-deriving it from four bars would be guessing at
    something already recorded.
    """

    import json

    from .midi import write_midi

    project_dir = Path(project_dir)
    manifest_path = project_dir / "sample_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no samples cut here: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = next((item for item in manifest["samples"] if item["name"] == name), None)
    if record is None:
        raise KeyError(f"no sample named {name!r} in {manifest_path}")

    audio_path = project_dir / record["path"]
    expected_sha256 = record.get("sha256")
    source_sha256 = _sha256(audio_path)
    if expected_sha256 is not None and source_sha256 != str(expected_sha256):
        raise ValueError(f"sample sha256 does not match manifest: {audio_path}")
    samples, rate = read_wav_mono(audio_path)
    transcription = transcribe(samples, rate, bpm=float(record["bpm"]))
    destination = audio_path.with_suffix(".mid")
    coverage_path = audio_path.with_suffix(".transcription.json")
    if (destination.exists() or coverage_path.exists()) and not overwrite:
        raise FileExistsError(f"refusing to overwrite transcription: {destination}")
    write_midi(
        destination,
        transcription.notes,
        track_name=f"{name} (transcribed)",
        bpm=float(record["bpm"]),
        key=str(record["key"]),
    )
    coverage_path.write_text(
        json.dumps(
            {
                "transcription_version": TRANSCRIPTION_VERSION,
                "sample": name,
                "source_audio": record["path"],
                "source_sha256": source_sha256,
                "midi_file": str(destination.relative_to(project_dir)),
                "coverage": transcription.coverage,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination, transcription
