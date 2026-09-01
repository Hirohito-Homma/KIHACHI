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
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .material import detect_onsets
from .midi import MidiNote
from .pitch import PitchFrame, track_pitch

TRANSCRIPTION_VERSION = "0.4"

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


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace a text artifact only after its complete contents are on disk."""

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}-",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Binary counterpart used so a MIDI file is never partially replaced."""

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}-",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _restore_artifact(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write_bytes(path, previous)


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


def _sample_records(project_dir: Path) -> list[dict[str, Any]]:
    """Load every unambiguous, schema-checked sample manifest record."""

    import json

    project_dir = Path(project_dir)
    manifest_path = project_dir / "sample_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no samples cut here: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("sample manifest root must be an object")
    from .sampler import SAMPLE_MANIFEST_VERSION

    version = manifest.get("manifest_version")
    if version is not None and version != SAMPLE_MANIFEST_VERSION:
        raise ValueError(f"unsupported sample manifest version: {version!r}")
    records = manifest.get("samples")
    if not isinstance(records, list):
        raise ValueError("sample manifest must contain a samples list")
    seen_names: set[str] = set()
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise ValueError(f"sample manifest entry {index} must be an object")
        item_name = item.get("name")
        if not isinstance(item_name, str):
            raise ValueError(
                f"sample manifest entry {index} has a missing or non-string name"
            )
        if item_name in seen_names:
            raise ValueError(f"duplicate sample name in manifest: {item_name!r}")
        seen_names.add(item_name)
    from .theory import parse_key

    for record in records:
        name = record["name"]
        for field in ("path", "bpm", "key"):
            if field not in record:
                raise ValueError(f"sample {name!r} is missing manifest field: {field}")
        if not isinstance(record["path"], str):
            raise ValueError(f"sample {name!r} has a non-string manifest path")
        try:
            bpm = float(record["bpm"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"sample {name!r} has a non-numeric manifest bpm") from exc
        if not 30.0 <= bpm <= 300.0:
            raise ValueError(f"sample {name!r} manifest bpm must be between 30 and 300")
        if not isinstance(record["key"], str):
            raise ValueError(f"sample {name!r} has a non-string manifest key")
        key = record["key"]
        try:
            normalized_key, _, _, _ = parse_key(key, default="")
        except ValueError as exc:
            raise ValueError(f"sample {name!r} has an invalid manifest key") from exc
        if normalized_key != key:
            raise ValueError(f"sample {name!r} has an invalid manifest key: {key!r}")
        expected_sha256 = record.get("sha256")
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str):
                raise ValueError(f"sample {name!r} has a non-string manifest sha256")
            if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
                raise ValueError(f"sample {name!r} has an invalid manifest sha256")
    return records


def _sample_record(project_dir: Path, name: str) -> dict[str, Any]:
    records = _sample_records(project_dir)
    record = next((item for item in records if item["name"] == name), None)
    if record is None:
        manifest_path = Path(project_dir) / "sample_manifest.json"
        raise ValueError(f"no sample named {name!r} in {manifest_path}")
    return record


def transcribe_sample_file(
    project_dir: Path, *, name: str, overwrite: bool = False
) -> tuple[Path, Transcription]:
    """Transcribe one sample from a project's manifest and write the MIDI beside it.

    The bpm comes from the manifest rather than from the audio: the sample was
    cut on that grid, and re-deriving it from four bars would be guessing at
    something already recorded.
    """

    import json

    from .midi import build_midi_bytes

    project_dir = Path(project_dir)
    record = _sample_record(project_dir, name)
    bpm = float(record["bpm"])
    key = record["key"]
    expected_sha256 = record.get("sha256")

    project_root = project_dir.resolve()
    audio_path = (project_root / record["path"]).resolve()
    if project_root not in audio_path.parents:
        raise ValueError(f"sample path escapes project: {record['path']}")
    source_sha256 = _sha256(audio_path)
    if expected_sha256 is not None and source_sha256 != expected_sha256:
        raise ValueError(f"sample sha256 does not match manifest: {audio_path}")
    samples, rate = read_wav_mono(audio_path)
    transcription = transcribe(samples, rate, bpm=bpm)
    destination = audio_path.with_suffix(".mid")
    coverage_path = audio_path.with_suffix(".transcription.json")
    if (destination.exists() or coverage_path.exists()) and not overwrite:
        raise FileExistsError(f"refusing to overwrite transcription: {destination}")
    midi_payload = build_midi_bytes(
        transcription.notes,
        track_name=f"{name} (transcribed)",
        bpm=bpm,
        key=key,
    )
    coverage_payload = (
        json.dumps(
            {
                "transcription_version": TRANSCRIPTION_VERSION,
                "sample": name,
                "source_audio": record["path"],
                "source_sha256": source_sha256,
                "sample_rate": rate,
                "duration_sec": round(len(samples) / rate, 6),
                "bpm": bpm,
                "key": key,
                "midi_file": str(destination.relative_to(project_root)),
                "midi_sha256": hashlib.sha256(midi_payload).hexdigest(),
                "coverage": transcription.coverage,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    previous_midi = destination.read_bytes() if destination.is_file() else None
    previous_coverage = coverage_path.read_bytes() if coverage_path.is_file() else None
    try:
        _atomic_write_bytes(destination, midi_payload)
        _atomic_write_text(coverage_path, coverage_payload)
    except BaseException:
        _restore_artifact(coverage_path, previous_coverage)
        _restore_artifact(destination, previous_midi)
        raise
    return destination, transcription


def audit_transcription(project_dir: Path, *, name: str) -> dict[str, Any]:
    """Verify a stored transcription without rewriting or judging it."""

    import json
    import wave

    from .midi import read_midi

    project_dir = Path(project_dir)
    project_root = project_dir.resolve()
    record = _sample_record(project_dir, name)
    audio_path = (project_root / record["path"]).resolve()
    if project_root not in audio_path.parents:
        raise ValueError(f"sample path escapes project: {record['path']}")
    coverage_path = audio_path.with_suffix(".transcription.json")
    if not coverage_path.is_file():
        raise FileNotFoundError(f"no transcription audit here: {coverage_path}")
    document = json.loads(coverage_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("transcription audit root must be an object")
    version = document.get("transcription_version")
    if version != TRANSCRIPTION_VERSION:
        raise ValueError(f"unsupported transcription version: {version!r}")
    if document.get("sample") != name:
        raise ValueError("transcription audit sample does not match the manifest")
    if document.get("source_audio") != record["path"]:
        raise ValueError("transcription audit source does not match the manifest")

    source_sha256 = document.get("source_sha256")
    if not isinstance(source_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", source_sha256
    ) is None:
        raise ValueError("transcription audit has an invalid source sha256")
    actual_source_sha256 = _sha256(audio_path)
    manifest_sha256 = record.get("sha256")
    if manifest_sha256 is not None and actual_source_sha256 != manifest_sha256:
        raise ValueError(f"sample sha256 does not match manifest: {audio_path}")
    if actual_source_sha256 != source_sha256:
        raise ValueError(f"source sha256 does not match transcription: {audio_path}")

    with wave.open(str(audio_path), "rb") as source:
        actual_sample_rate = float(source.getframerate())
        actual_duration_sec = (
            source.getnframes() / actual_sample_rate if actual_sample_rate else 0.0
        )
    sample_rate = document.get("sample_rate")
    if (
        not isinstance(sample_rate, (int, float))
        or isinstance(sample_rate, bool)
        or float(sample_rate) != actual_sample_rate
    ):
        raise ValueError("transcription sample rate does not match the source WAV")
    duration_sec = document.get("duration_sec")
    if (
        not isinstance(duration_sec, (int, float))
        or isinstance(duration_sec, bool)
        or float(duration_sec) != round(actual_duration_sec, 6)
    ):
        raise ValueError("transcription duration does not match the source WAV")
    bpm = document.get("bpm")
    manifest_bpm = float(record["bpm"])
    if (
        not isinstance(bpm, (int, float))
        or isinstance(bpm, bool)
        or float(bpm) != manifest_bpm
    ):
        raise ValueError("transcription BPM does not match the manifest")
    if document.get("key") != record["key"]:
        raise ValueError("transcription key does not match the manifest")

    midi_file = document.get("midi_file")
    if not isinstance(midi_file, str):
        raise ValueError("transcription audit has a non-string midi_file")
    midi_path = (project_root / midi_file).resolve()
    if project_root not in midi_path.parents:
        raise ValueError(f"transcription MIDI path escapes project: {midi_file}")
    expected_midi_path = audio_path.with_suffix(".mid").resolve()
    if midi_path != expected_midi_path:
        raise ValueError("transcription MIDI does not sit beside its source sample")
    midi_sha256 = document.get("midi_sha256")
    if not isinstance(midi_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", midi_sha256
    ) is None:
        raise ValueError("transcription audit has an invalid MIDI sha256")
    if _sha256(midi_path) != midi_sha256:
        raise ValueError(f"MIDI sha256 does not match transcription: {midi_path}")

    coverage = document.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("transcription audit coverage must be an object")
    expected_notes = coverage.get("notes")
    if (
        not isinstance(expected_notes, int)
        or isinstance(expected_notes, bool)
        or expected_notes < 0
    ):
        raise ValueError("transcription audit has an invalid note count")
    midi = read_midi(midi_path)
    actual_notes = len(midi.notes)
    if actual_notes != expected_notes:
        raise ValueError(
            f"MIDI note count does not match transcription: {actual_notes} != {expected_notes}"
        )
    if midi.bpm is None or abs(midi.bpm - manifest_bpm) > 0.001:
        raise ValueError("MIDI tempo does not match the manifest")
    if coverage.get("transcription_version") != TRANSCRIPTION_VERSION:
        raise ValueError("coverage transcription version does not match its document")
    voiced_fraction = coverage.get("voiced_fraction")
    if (
        not isinstance(voiced_fraction, (int, float))
        or isinstance(voiced_fraction, bool)
        or not 0.0 <= float(voiced_fraction) <= 1.0
    ):
        raise ValueError("transcription audit has an invalid voiced fraction")
    return {
        "status": "verified",
        "sample": name,
        "source_audio": record["path"],
        "source_sha256": source_sha256,
        "midi_file": midi_file,
        "midi_sha256": midi_sha256,
        "notes": actual_notes,
        "sample_rate": actual_sample_rate,
        "duration_sec": round(actual_duration_sec, 6),
        "bpm": manifest_bpm,
        "key": record["key"],
        "voiced_fraction": float(voiced_fraction),
        "coverage_file": str(coverage_path.relative_to(project_root)),
    }


def audit_project_transcriptions(project_dir: Path) -> dict[str, Any]:
    """Audit every transcription that already exists; create nothing."""

    project_dir = Path(project_dir)
    project_root = project_dir.resolve()
    records = _sample_records(project_dir)
    names: list[str] = []
    for record in records:
        audio_path = (project_root / record["path"]).resolve()
        if project_root not in audio_path.parents:
            raise ValueError(f"sample path escapes project: {record['path']}")
        if audio_path.with_suffix(".transcription.json").is_file():
            names.append(record["name"])
    if not names:
        raise FileNotFoundError(f"no transcriptions to audit in: {project_dir}")
    audits: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for name in names:
        try:
            audits.append(audit_transcription(project_dir, name=name))
        except Exception as exc:
            failures.append({"sample": name, "error": str(exc)})
    return {
        "status": "failed" if failures else "verified",
        "project": project_dir.name,
        "samples": len(records),
        "verified": len(audits),
        "failed": len(failures),
        "skipped_untranscribed": len(records) - len(names),
        "audits": audits,
        "failures": failures,
    }
