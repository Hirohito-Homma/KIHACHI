from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .defects import scan_material
from .loudness import integrated_loudness
from .spectrum import band_energies
from .models import SongSpec
from .theory import NOTE_TO_PC, chord_is_minor, chord_root

ANALYSIS_VERSION = "0.3"
PITCH_CLASS_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)
CHORD_CONFIDENCE_THRESHOLD = 0.15
KEY_CONFIDENCE_THRESHOLD = 0.25


@dataclass(frozen=True)
class AudioAnalysisManifest:
    project_dir: Path
    audio_file: Path
    analysis_file: Path
    analysis: dict[str, Any]
    defects_file: Path | None = None
    defects: dict[str, Any] | None = None


def analyze_project(
    project_dir: Path,
    audio_file: Path | None = None,
    *,
    overwrite: bool = False,
    scan_defects: bool = True,
    measure_loudness: bool = False,
) -> AudioAnalysisManifest:
    """Analyze a project's audio and scan it for defects.

    Two passes over the same file answering different questions. The analysis
    asks whether the audio followed the SongSpec; the scan asks whether the audio
    is usable at all, without reference to any plan. They stay separate
    artifacts because averaging them hides the failure that matters: the baseline
    take scores 88.69 "aligned" while carrying a 2.28 s silent hole, and the
    seed-42 take that scores 35.38 is defect-free.

    The scan lives here rather than in the CLI so that every caller gets it.
    It was wired into the command line first, which meant a programmatic
    ``analyze_project`` silently skipped it -- and a batch rescore of twenty
    stored renders reported no defects at all.
    """

    project_dir = Path(project_dir)
    spec_path = project_dir / "song_spec.json"
    if not spec_path.is_file():
        raise FileNotFoundError(f"SongSpec not found: {spec_path}")
    spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))

    audio_path = Path(audio_file) if audio_file is not None else project_dir / "audio" / "ace-step-01.wav"
    if not audio_path.is_absolute() and audio_file is not None:
        audio_path = project_dir / audio_path
    if not audio_path.is_file():
        raise FileNotFoundError(f"WAV audio not found: {audio_path}")

    analysis_path = project_dir / "audio_analysis.json"
    if analysis_path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite audio analysis: {analysis_path}")
    defects_path = project_dir / "material_defects.json"
    if scan_defects and defects_path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite defect scan: {defects_path}")

    analysis = analyze_wave(audio_path, spec, measure_loudness=measure_loudness)
    try:
        display_path = str(audio_path.relative_to(project_dir))
    except ValueError:
        display_path = str(audio_path)
    analysis["audio_file"] = display_path
    _atomic_write_text(
        analysis_path,
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n",
    )

    defects = None
    if scan_defects:
        defects = scan_material(audio_path)
        _atomic_write_text(
            defects_path,
            json.dumps(defects, ensure_ascii=False, indent=2) + "\n",
        )
    return AudioAnalysisManifest(
        project_dir=project_dir,
        audio_file=audio_path,
        analysis_file=analysis_path,
        analysis=analysis,
        defects_file=defects_path if scan_defects else None,
        defects=defects,
    )


def analyze_wave(
    audio_path: Path,
    spec: SongSpec | None = None,
    *,
    measure_loudness: bool = False,
) -> dict[str, Any]:
    audio_path = Path(audio_path)
    digest = hashlib.sha256()
    with audio_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)

    with wave.open(str(audio_path), "rb") as source:
        channels = source.getnchannels()
        sample_rate = source.getframerate()
        sample_width = source.getsampwidth()
        frame_count = source.getnframes()
        compression = source.getcomptype()
        if compression != "NONE":
            raise ValueError(f"compressed WAV is not supported: {compression}")
        if channels <= 0 or sample_rate <= 0 or frame_count <= 0:
            raise ValueError("WAV must contain non-empty PCM audio")
        if sample_width not in {1, 2, 3, 4}:
            raise ValueError(f"unsupported PCM sample width: {sample_width} bytes")

        hop_frames = max(1, round(sample_rate * 0.02))
        tonal_factor = max(1, round(sample_rate / 4000.0))
        tonal_sample_rate = sample_rate / tonal_factor
        tonal_samples: list[float] = []
        tonal_sum = 0.0
        tonal_count = 0
        channel_square_sums = [0.0] * channels
        square_sum = 0.0
        signed_sum = 0.0
        peak = 0.0
        clipped_samples = 0
        sample_count = 0
        envelope: list[float] = []
        window_square_sum = 0.0
        window_frames = 0

        while data := source.readframes(8192):
            samples = _decode_pcm(data, sample_width)
            usable = len(samples) - (len(samples) % channels)
            for offset in range(0, usable, channels):
                mono_sum = 0.0
                for channel in range(channels):
                    sample = samples[offset + channel]
                    magnitude = abs(sample)
                    peak = max(peak, magnitude)
                    if magnitude >= 0.999:
                        clipped_samples += 1
                    square = sample * sample
                    square_sum += square
                    signed_sum += sample
                    channel_square_sums[channel] += square
                    sample_count += 1
                    mono_sum += sample
                mono = mono_sum / channels
                tonal_sum += mono
                tonal_count += 1
                if tonal_count == tonal_factor:
                    tonal_samples.append(tonal_sum / tonal_count)
                    tonal_sum = 0.0
                    tonal_count = 0
                window_square_sum += mono * mono
                window_frames += 1
                if window_frames == hop_frames:
                    envelope.append(math.sqrt(window_square_sum / window_frames))
                    window_square_sum = 0.0
                    window_frames = 0
        if window_frames:
            envelope.append(math.sqrt(window_square_sum / window_frames))
        if tonal_count:
            tonal_samples.append(tonal_sum / tonal_count)

    duration = frame_count / sample_rate
    rms = math.sqrt(square_sum / sample_count)
    peak_dbfs = _dbfs(peak)
    rms_dbfs = _dbfs(rms)
    silence_threshold = 10.0 ** (-50.0 / 20.0)
    silent_windows = sum(value < silence_threshold for value in envelope)
    silence_ratio = silent_windows / len(envelope) if envelope else 1.0
    tempo_bpm, tempo_confidence = _estimate_tempo(envelope, hop_frames / sample_rate)
    channel_rms = [math.sqrt(value / frame_count) for value in channel_square_sums]
    harmony = _analyze_harmony(tonal_samples, tonal_sample_rate, spec)
    sections = _analyze_sections(envelope, hop_frames / sample_rate, duration, spec)

    quality_flags: list[str] = []
    if clipped_samples:
        quality_flags.append("clipping_detected")
    if rms_dbfs < -35.0:
        quality_flags.append("very_low_level")
    if silence_ratio > 0.4:
        quality_flags.append("high_silence_ratio")

    result: dict[str, Any] = {
        "analysis_version": ANALYSIS_VERSION,
        "sha256": digest.hexdigest(),
        "format": {
            "container": "wav",
            "codec": f"pcm_s{sample_width * 8}le" if sample_width > 1 else "pcm_u8",
            "sample_rate_hz": sample_rate,
            "channels": channels,
            "sample_width_bits": sample_width * 8,
            "frame_count": frame_count,
            "duration_sec": round(duration, 6),
        },
        "level": {
            "peak_dbfs": round(peak_dbfs, 3),
            "rms_dbfs": round(rms_dbfs, 3),
            "crest_factor_db": round(peak_dbfs - rms_dbfs, 3),
            "dc_offset": round(signed_sum / sample_count, 8),
            "clipped_sample_ratio": round(clipped_samples / sample_count, 8),
            "silence_ratio_below_minus_50_dbfs": round(silence_ratio, 6),
            "channel_rms_dbfs": [round(_dbfs(value), 3) for value in channel_rms],
        },
        "tempo": {
            "estimated_bpm": round(tempo_bpm, 3) if tempo_bpm is not None else None,
            "confidence": round(tempo_confidence, 4),
            "method": "20ms-rms-positive-flux-autocorrelation",
        },
        "harmony": harmony,
        "sections": sections,
        "spectrum": band_energies(audio_path),
        # Off by default: BS.1770 filters every sample, which is 11 s for a
        # seventy-second take and 49 s for a five-minute one, and `analyze` is
        # called on a loop by `revise`. The corpus also gives no reason to pay
        # it routinely -- 21 renders sit inside a 5 LU band, so loudness is not
        # where this generator goes wrong.
        "loudness": integrated_loudness(audio_path) if measure_loudness else None,
        "quality_flags": quality_flags,
    }

    if spec is not None:
        duration_delta = duration - spec.song.target_duration_sec
        tempo_delta = tempo_bpm - spec.song.bpm if tempo_bpm is not None else None
        if abs(duration_delta) > 0.25:
            quality_flags.append("duration_mismatch")
        if tempo_delta is not None and tempo_confidence >= 0.1 and abs(tempo_delta) > 2.0:
            quality_flags.append("tempo_mismatch")
        observed_key = harmony["key"]["estimated_key"]
        key_confidence = harmony["key"]["confidence"]
        if observed_key is None:
            key_status = "not_detected"
        elif key_confidence < KEY_CONFIDENCE_THRESHOLD:
            key_status = "low_confidence"
        elif _key_matches_song_spec(observed_key, spec):
            key_status = "match"
        else:
            key_status = "mismatch"
        result["song_spec_comparison"] = {
            "target_duration_sec": spec.song.target_duration_sec,
            "duration_delta_sec": round(duration_delta, 6),
            "target_bpm": spec.song.bpm,
            "tempo_delta_bpm": round(tempo_delta, 3) if tempo_delta is not None else None,
            "target_key": spec.song.key,
            "observed_key": observed_key,
            "key_confidence": key_confidence,
            "key_status": key_status,
            "progression_match_ratio": harmony["chords"]["progression_match_ratio"],
            "section_energy_correlation": sections["energy_correlation_to_song_spec"],
            "section_boundary_recall": sections["planned_boundary_recall_within_one_bar"],
        }
    return result


def _analyze_harmony(
    samples: list[float],
    sample_rate: float,
    spec: SongSpec | None,
) -> dict[str, Any]:
    method = "4kHz-box-downsample-goertzel-48pitch-chroma-tonic-anchor"
    if not samples or sample_rate <= 0:
        return {
            "method": method,
            "analysis_sample_rate_hz": round(sample_rate, 3),
            "key": {
                "estimated_key": None,
                "confidence": 0.0,
                "confidence_threshold": KEY_CONFIDENCE_THRESHOLD,
                "margin": 0.0,
                "pitch_class_profile": {},
            },
            "chords": {
                "grid": "unavailable",
                "bars": [],
                "confidence_threshold": CHORD_CONFIDENCE_THRESHOLD,
                "confident_bar_coverage": 0.0,
                "progression_match_ratio": None,
            },
        }

    segment_seconds = _bar_duration_seconds(spec) if spec is not None else 2.0
    segment_frames = max(64, round(segment_seconds * sample_rate))
    available_segments = math.ceil(len(samples) / segment_frames)
    segment_count = min(spec.song.total_bars, available_segments) if spec is not None else available_segments
    chromas: list[list[float]] = []
    segment_rms: list[float] = []
    chord_bars: list[dict[str, Any]] = []
    chord_confidences: list[float] = []
    matches = 0
    compared = 0
    reliable_count = 0

    for index in range(segment_count):
        start = index * segment_frames
        end = min(len(samples), start + segment_frames)
        segment = samples[start:end]
        if len(segment) < 64:
            continue
        chroma = _chroma_for_segment(segment, sample_rate)
        chromas.append(chroma)
        rms = math.sqrt(sum(value * value for value in segment) / len(segment))
        segment_rms.append(rms)
        chord, confidence = _estimate_chord(chroma)
        chord_confidences.append(confidence)
        if confidence >= CHORD_CONFIDENCE_THRESHOLD:
            reliable_count += 1
        entry: dict[str, Any] = {
            "bar": index + 1,
            "start_sec": round(start / sample_rate, 4),
            "end_sec": round(end / sample_rate, 4),
            "estimated_chord": chord,
            "confidence": round(confidence, 4),
            "reliable": confidence >= CHORD_CONFIDENCE_THRESHOLD,
        }
        if spec is not None:
            progression_index = (index // spec.harmony.harmonic_rhythm_bars) % len(spec.harmony.progression)
            expected = spec.harmony.progression[progression_index]
            match = _chords_equivalent(chord, expected) if confidence >= CHORD_CONFIDENCE_THRESHOLD else None
            entry["expected_chord"] = expected
            entry["match"] = match
            if match is not None:
                matches += int(match)
                compared += 1
        chord_bars.append(entry)

    global_chroma = [0.0] * 12
    for chroma, rms in zip(chromas, segment_rms):
        weight = max(rms, 1e-6)
        for pitch_class, value in enumerate(chroma):
            global_chroma[pitch_class] += value * weight
    global_chroma = _normalize_chroma(global_chroma)
    tonic_anchor = next(
        (
            entry["estimated_chord"]
            for entry in chord_bars
            if entry["estimated_chord"] is not None and entry["confidence"] >= CHORD_CONFIDENCE_THRESHOLD
        ),
        None,
    )
    tonal_confidence = sum(chord_confidences) / len(chord_confidences) if chord_confidences else 0.0
    estimated_key, profile_confidence, key_margin = _estimate_key(global_chroma, tonic_anchor)
    key_confidence = profile_confidence * tonal_confidence
    progression_match = matches / compared if compared else None
    return {
        "method": method,
        "analysis_sample_rate_hz": round(sample_rate, 3),
        "key": {
            "estimated_key": estimated_key,
            "confidence": round(key_confidence, 4),
            "confidence_threshold": KEY_CONFIDENCE_THRESHOLD,
            "margin": round(key_margin, 4),
            "tonic_anchor_chord": tonic_anchor,
            "tonal_confidence": round(tonal_confidence, 4),
            "pitch_class_profile": {
                name: round(global_chroma[index], 6)
                for index, name in enumerate(PITCH_CLASS_NAMES)
            },
        },
        "chords": {
            "grid": "song_spec_bars" if spec is not None else "fixed_2_seconds",
            "bars": chord_bars,
            "confidence_threshold": CHORD_CONFIDENCE_THRESHOLD,
            "confident_bar_coverage": round(reliable_count / len(chord_bars), 4) if chord_bars else 0.0,
            "progression_match_ratio": round(progression_match, 4) if progression_match is not None else None,
        },
    }


def _chroma_for_segment(samples: list[float], sample_rate: float) -> list[float]:
    if len(samples) < 2:
        return [0.0] * 12
    mean = sum(samples) / len(samples)
    scale = 2.0 * math.pi / (len(samples) - 1)
    windowed = [
        (value - mean) * (0.5 - 0.5 * math.cos(scale * index))
        for index, value in enumerate(samples)
    ]
    chroma = [0.0] * 12
    for midi_note in range(36, 84):
        frequency = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
        if frequency >= sample_rate * 0.48:
            continue
        power = _goertzel_power(windowed, frequency, sample_rate)
        amplitude = math.sqrt(max(power, 0.0)) / len(windowed)
        chroma[midi_note % 12] += math.log1p(amplitude * 1000.0)
    return _normalize_chroma(chroma)


def _goertzel_power(samples: list[float], frequency: float, sample_rate: float) -> float:
    coefficient = 2.0 * math.cos(2.0 * math.pi * frequency / sample_rate)
    previous = 0.0
    previous_two = 0.0
    for sample in samples:
        current = sample + coefficient * previous - previous_two
        previous_two = previous
        previous = current
    return previous * previous + previous_two * previous_two - coefficient * previous * previous_two


def _normalize_chroma(values: list[float]) -> list[float]:
    if not values:
        return [0.0] * 12
    floor = min(values)
    adjusted = [max(0.0, value - floor) for value in values]
    total = sum(adjusted)
    if total <= 1e-12:
        return [0.0] * 12
    return [value / total for value in adjusted]


def _estimate_key(chroma: list[float], tonic_anchor: str | None = None) -> tuple[str | None, float, float]:
    if sum(chroma) <= 1e-12:
        return None, 0.0, 0.0
    candidates: list[tuple[float, str]] = []
    for root, name in enumerate(PITCH_CLASS_NAMES):
        for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            rotated = [profile[(pitch_class - root) % 12] for pitch_class in range(12)]
            score = _correlation(chroma, rotated)
            tonic_chord = name if mode == "major" else f"{name}m"
            if tonic_anchor == tonic_chord:
                score += 0.15
            candidates.append((score, f"{name} {mode}"))
    candidates.sort(reverse=True)
    best_score, best_key = candidates[0]
    second_score = candidates[1][0]
    return best_key, max(0.0, min(1.0, best_score)), max(0.0, best_score - second_score)


def _estimate_chord(chroma: list[float]) -> tuple[str | None, float]:
    if sum(chroma) <= 1e-12:
        return None, 0.0
    candidates: list[tuple[float, str]] = []
    for root, name in enumerate(PITCH_CLASS_NAMES):
        for suffix, intervals in (("", (0, 4, 7)), ("m", (0, 3, 7))):
            tones = {(root + interval) % 12 for interval in intervals}
            tone_mean = sum(chroma[pitch_class] for pitch_class in tones) / len(tones)
            off_mean = sum(chroma[pitch_class] for pitch_class in range(12) if pitch_class not in tones) / 9.0
            candidates.append((tone_mean - 0.5 * off_mean, f"{name}{suffix}"))
    candidates.sort(reverse=True)
    best_score, best_chord = candidates[0]
    margin = best_score - candidates[1][0]
    confidence = max(0.0, min(1.0, margin * 12.0))
    return best_chord, confidence


def _key_matches_song_spec(observed_key: str, spec: SongSpec) -> bool:
    try:
        tonic, mode = observed_key.rsplit(" ", 1)
        return NOTE_TO_PC[tonic] == spec.song.tonic_pitch_class and mode == spec.song.mode
    except (KeyError, ValueError):
        return False


def _chords_equivalent(observed: str | None, expected: str) -> bool:
    if observed is None:
        return False
    observed_root = chord_root(observed)
    expected_root = chord_root(expected)
    # Compared as triads, because the estimator above only ever emits major or
    # minor: a bar written as ``Am7`` and heard as ``Am`` is the estimator
    # reaching its resolution, not the render departing from the plan. A power
    # chord has no third to compare and reads as major here for the same
    # reason.
    #
    # Not ``startswith("m")``: that reads ``maj7`` as a minor chord, and the
    # progression shapes write major sevenths now.
    observed_minor = chord_is_minor(observed)
    expected_minor = chord_is_minor(expected)
    return NOTE_TO_PC[observed_root] == NOTE_TO_PC[expected_root] and observed_minor == expected_minor


def _analyze_sections(
    envelope: list[float],
    hop_seconds: float,
    duration: float,
    spec: SongSpec | None,
) -> dict[str, Any]:
    grid_seconds = _bar_duration_seconds(spec) if spec is not None else 2.0
    available_bars = math.ceil(duration / grid_seconds)
    bar_count = min(spec.song.total_bars, available_bars) if spec is not None else available_bars
    bar_dbfs: list[float] = []
    for bar in range(bar_count):
        start_index = max(0, math.floor(bar * grid_seconds / hop_seconds))
        end_index = min(len(envelope), math.ceil((bar + 1) * grid_seconds / hop_seconds))
        values = envelope[start_index:end_index]
        rms = math.sqrt(sum(value * value for value in values) / len(values)) if values else 0.0
        bar_dbfs.append(_dbfs(rms))

    if bar_dbfs:
        low = _percentile(bar_dbfs, 0.1)
        high = _percentile(bar_dbfs, 0.9)
        span = max(1e-9, high - low)
        normalized = [max(0.0, min(1.0, (value - low) / span)) for value in bar_dbfs]
    else:
        normalized = []

    bars: list[dict[str, Any]] = []
    target_energy: list[float] = []
    for index, (dbfs, energy) in enumerate(zip(bar_dbfs, normalized)):
        entry: dict[str, Any] = {
            "bar": index + 1,
            "start_sec": round(index * grid_seconds, 4),
            "end_sec": round(min(duration, (index + 1) * grid_seconds), 4),
            "rms_dbfs": round(dbfs, 3),
            "normalized_energy": round(energy, 4),
        }
        if spec is not None:
            section = next(item for item in spec.arrangement if item.start_bar <= index < item.start_bar + item.length_bars)
            entry["planned_section"] = section.name
            entry["target_energy"] = section.energy
            target_energy.append(section.energy)
        bars.append(entry)

    # Detect boundaries from local dB changes. Using the globally normalized
    # energy curve here made an edit near the end of a song change detection
    # thresholds for otherwise byte-equivalent earlier sections.
    boundary_strength: dict[int, float] = {}
    for boundary in range(2, max(2, len(bar_dbfs) - 1)):
        left = bar_dbfs[max(0, boundary - 2) : boundary]
        right = bar_dbfs[boundary : min(len(bar_dbfs), boundary + 2)]
        if left and right:
            boundary_strength[boundary] = abs(sum(left) / len(left) - sum(right) / len(right))
    candidates = sorted(boundary_strength, key=boundary_strength.get, reverse=True)
    # The cap keeps noise out, but a fixed 7 was written when a song was four
    # sections. A nine-section arrangement has eight planned boundaries, so a
    # fixed cap made full recall unreachable no matter what the audio did.
    boundary_limit = max(7, (len(spec.arrangement) - 1) + 3) if spec is not None else 7
    selected: list[int] = []
    for boundary in candidates:
        strength = boundary_strength[boundary]
        if strength < 0.5:
            continue
        if all(abs(boundary - existing) >= 4 for existing in selected):
            selected.append(boundary)
        if len(selected) >= boundary_limit:
            break
    selected.sort()
    edges = [0, *selected, len(normalized)] if normalized else [0]
    observed_sections: list[dict[str, Any]] = []
    for index in range(len(edges) - 1):
        start = edges[index]
        end = edges[index + 1]
        values = normalized[start:end]
        observed_sections.append(
            {
                "index": index + 1,
                "start_bar": start + 1,
                "end_bar": end,
                "mean_normalized_energy": round(sum(values) / len(values), 4) if values else 0.0,
                "boundary_strength": round(boundary_strength.get(start, 0.0), 4),
            }
        )

    planned_sections: list[dict[str, Any]] = []
    planned_boundaries: list[int] = []
    planned_boundary_matches: list[dict[str, Any]] = []
    if spec is not None:
        for section in spec.arrangement:
            values = normalized[section.start_bar : section.start_bar + section.length_bars]
            planned_sections.append(
                {
                    "name": section.name,
                    "start_bar": section.start_bar + 1,
                    "length_bars": section.length_bars,
                    "target_energy": section.energy,
                    "observed_mean_energy": round(sum(values) / len(values), 4) if values else None,
                }
            )
        planned_boundaries = [
            section.start_bar
            for section in spec.arrangement[1:]
        ]
        for boundary in planned_boundaries:
            nearest = min(selected, key=lambda observed: abs(observed - boundary)) if selected else None
            distance = abs(nearest - boundary) if nearest is not None else None
            planned_boundary_matches.append(
                {
                    "planned_after_bar": boundary,
                    "nearest_detected_after_bar": nearest,
                    "distance_bars": distance,
                    "matched_within_one_bar": distance is not None and distance <= 1,
                }
            )
    correlation = _correlation(normalized, target_energy) if normalized and len(normalized) == len(target_energy) else None
    boundary_recall = (
        sum(item["matched_within_one_bar"] for item in planned_boundary_matches) / len(planned_boundary_matches)
        if planned_boundary_matches
        else None
    )
    return {
        "method": "20ms-rms-song-grid-energy-and-local-db-change",
        "grid": "song_spec_bars" if spec is not None else "fixed_2_seconds",
        "bar_duration_sec": round(grid_seconds, 6),
        "bars": bars,
        "detected_boundaries_after_bar": selected,
        "boundary_strength_unit": "dB",
        "boundary_detection_threshold_db": 0.5,
        "observed_sections": observed_sections,
        "planned_sections": planned_sections,
        "planned_boundaries_after_bar": planned_boundaries,
        "planned_boundary_matches": planned_boundary_matches,
        "planned_boundary_recall_within_one_bar": round(boundary_recall, 4) if boundary_recall is not None else None,
        "energy_correlation_to_song_spec": round(correlation, 4) if correlation is not None else None,
    }


def _bar_duration_seconds(spec: SongSpec | None) -> float:
    if spec is None:
        return 2.0
    numerator_text, denominator_text = spec.song.time_signature.split("/", 1)
    numerator = int(numerator_text)
    denominator = int(denominator_text)
    return numerator * (4.0 / denominator) * 60.0 / spec.song.bpm


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _correlation(left: list[float] | tuple[float, ...], right: list[float] | tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_energy = sum((value - left_mean) ** 2 for value in left)
    right_energy = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_energy * right_energy)
    return numerator / denominator if denominator > 1e-12 else 0.0


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


def _estimate_tempo(envelope: list[float], hop_seconds: float) -> tuple[float | None, float]:
    if len(envelope) < 8 or hop_seconds <= 0:
        return None, 0.0
    compressed = [math.log1p(20.0 * value) for value in envelope]
    onset = [0.0]
    onset.extend(max(0.0, compressed[index] - compressed[index - 1]) for index in range(1, len(compressed)))
    onset_energy = sum(value * value for value in onset)
    if onset_energy <= 1e-12:
        return None, 0.0

    minimum_lag = max(1, round(60.0 / (180.0 * hop_seconds)))
    maximum_lag = min(len(onset) // 2, round(60.0 / (60.0 * hop_seconds)))
    scores: dict[int, float] = {}
    for lag in range(minimum_lag, maximum_lag + 1):
        numerator = 0.0
        left_energy = 0.0
        right_energy = 0.0
        for index in range(lag, len(onset)):
            left = onset[index]
            right = onset[index - lag]
            numerator += left * right
            left_energy += left * left
            right_energy += right * right
        denominator = math.sqrt(left_energy * right_energy)
        scores[lag] = numerator / denominator if denominator > 1e-12 else 0.0
    if not scores:
        return None, 0.0

    best_lag = max(scores, key=scores.get)
    refined_lag = float(best_lag)
    if best_lag - 1 in scores and best_lag + 1 in scores:
        left = scores[best_lag - 1]
        center = scores[best_lag]
        right = scores[best_lag + 1]
        denominator = left - 2.0 * center + right
        if abs(denominator) > 1e-12:
            refined_lag += 0.5 * (left - right) / denominator
    bpm = 60.0 / (refined_lag * hop_seconds)
    return bpm, max(0.0, min(1.0, scores[best_lag]))


def _dbfs(amplitude: float) -> float:
    return 20.0 * math.log10(max(abs(amplitude), 1e-12))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
