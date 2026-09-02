from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .adapters.ace_step import resolve_repaint_window
from .models import SongSpec
from .project_artifacts import managed_midi_names, require_managed_midi
from .tail_guard import DEFAULT_TAIL_GUARD_BARS, validate_guard_bars

REPAINT_PLAN_VERSION = "0.1"
REPAINT_STAGE_VERSION = "0.1"
# A final bar this far under its section target is a terminal collapse rather
# than ordinary arrangement dynamics.
TERMINAL_COLLAPSE_THRESHOLD = 0.25
# When the section as a whole is already within this of its target energy, the
# remaining defect is localized and a narrower bar window is the better edit.
LOCALIZED_ENERGY_TOLERANCE = 0.05
# Repaint at least this many bars so the model has musical context to work with.
MIN_BAR_CANDIDATE_BARS = 4
# A material discontinuity is already a measured audio defect, not a weak
# conformance hint.  Give it enough context on both sides and use a slightly
# wider waveform blend than the generic musical repaint plan.
DISCONTINUITY_WAV_CROSSFADE_SEC = 0.5
STAGED_DESIGN_ARTIFACTS = ("song_spec.json", "prompt.txt")
# Copied when present. Projects composed before the lyrics module have no sheet,
# and projects composed before ``prompt.json`` have no structured brief; staging
# either of those must still work.
OPTIONAL_DESIGN_ARTIFACTS = ("lyrics.txt", "prompt.json")


@dataclass(frozen=True)
class RepaintStageManifest:
    source_project: Path
    output_project: Path
    source_audio: Path
    files: tuple[Path, ...]
    stage_file: Path


def stage_repaint_project(
    source_project: Path,
    output_project: Path,
    *,
    plan_path: Path | None = None,
) -> RepaintStageManifest:
    source_project = Path(source_project)
    output_project = Path(output_project)
    if source_project.resolve() == output_project.resolve():
        raise ValueError("repaint output project must differ from the source project")
    if output_project.exists():
        raise FileExistsError(f"refusing to replace repaint output project: {output_project}")

    requested_plan = Path(plan_path) if plan_path is not None else Path("repaint_plan.json")
    if not requested_plan.is_absolute():
        requested_plan = source_project / requested_plan
    plan = load_repaint_plan(requested_plan)
    spec_path = source_project / "song_spec.json"
    if not spec_path.is_file():
        raise FileNotFoundError(f"SongSpec not found: {spec_path}")
    spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))
    if plan.get("song_spec_sha256") != song_spec_sha256(spec):
        raise ValueError("repaint plan SongSpec does not match the source project")

    require_managed_midi(source_project, spec, context="repaint source project")
    required_design_artifacts = (
        "song_spec.json",
        *managed_midi_names(spec),
        "prompt.txt",
    )
    for name in STAGED_DESIGN_ARTIFACTS:
        path = source_project / name
        if not path.is_file():
            raise FileNotFoundError(f"repaint source artifact not found: {path}")
    source_audio_record = plan.get("source_audio")
    if not isinstance(source_audio_record, Mapping):
        raise ValueError("repaint plan requires source_audio provenance")
    relative_audio = source_audio_record.get("relative_path")
    if not isinstance(relative_audio, str) or not relative_audio.strip():
        raise ValueError("repaint staging requires a source-project-relative Audio path")
    source_audio = source_project / relative_audio
    if not source_audio.is_file():
        raise FileNotFoundError(f"repaint source Audio not found: {source_audio}")
    expected_audio_sha = source_audio_record.get("sha256")
    actual_audio_sha = _file_sha256(source_audio)
    if expected_audio_sha != actual_audio_sha:
        raise ValueError("repaint source Audio SHA-256 does not match the reviewed analysis")

    output_project.parent.mkdir(parents=True, exist_ok=True)
    stage: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{output_project.name}-", dir=output_project.parent)
    )
    optional_present = tuple(
        name for name in OPTIONAL_DESIGN_ARTIFACTS if (source_project / name).is_file()
    )
    stage_names = (
        *required_design_artifacts,
        *optional_present,
        "repaint_plan.json",
        "applied_repaint_plan.json",
        "revision_prompt.txt",
        "repaint_stage.json",
    )
    try:
        for name in (*required_design_artifacts, *optional_present):
            shutil.copyfile(source_project / name, stage / name)
        shutil.copyfile(requested_plan, stage / "repaint_plan.json")
        shutil.copyfile(requested_plan, stage / "applied_repaint_plan.json")
        (stage / "revision_prompt.txt").write_text(
            str(plan["revision_prompt"]).rstrip() + "\n",
            encoding="utf-8",
        )
        stage_document = {
            "stage_version": REPAINT_STAGE_VERSION,
            "execution_state_at_creation": "staged_not_rendered",
            "source_project": source_project.name,
            "source_song_spec_sha256": plan["song_spec_sha256"],
            "source_audio": {
                "name": source_audio.name,
                "sha256": actual_audio_sha,
                "verified": True,
                "copied_to_output": False,
            },
            "repaint_plan": "repaint_plan.json",
            "applied_repaint_plan": "applied_repaint_plan.json",
            "staged_files": list(stage_names[:-1]),
        }
        (stage / "repaint_stage.json").write_text(
            json.dumps(stage_document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(stage, output_project)
        stage = None
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)

    return RepaintStageManifest(
        source_project=source_project,
        output_project=output_project,
        source_audio=source_audio,
        files=tuple(output_project / name for name in stage_names),
        stage_file=output_project / "repaint_stage.json",
    )


def build_repaint_plan(
    spec: SongSpec,
    analysis: Mapping[str, Any],
    findings: list[dict[str, Any]],
    *,
    material_defects: Mapping[str, Any] | None = None,
    tail_guard_bars: float = DEFAULT_TAIL_GUARD_BARS,
    prefer_bar_level: bool = False,
) -> dict[str, Any]:
    """Choose the next repaint window and the constraints to render it under.

    ``tail_guard_bars`` requests render headroom past the song grid so ACE-Step
    writes its ending outside the scored bars; ``prefer_bar_level`` promotes a
    narrow bar window over the whole section when the defect is localized.
    """

    guard_bars = validate_guard_bars(tail_guard_bars)
    diagnostics = _section_diagnostics(spec, analysis)
    conformance_selection = max(
        diagnostics,
        key=lambda item: (item["priority_score"], item["start_bar"]),
    )
    defect_candidate = _discontinuity_candidate(spec, material_defects, guard_bars)
    if defect_candidate is not None:
        defect_bar = int(defect_candidate["defect_bar"])
        selected = next(
            item
            for item in diagnostics
            if int(item["start_bar"]) <= defect_bar <= int(item["end_bar"])
        )
        candidates = [defect_candidate]
        recommended_selector = "bars"
        chosen = defect_candidate
        window = resolve_repaint_window(
            spec,
            bar_range=f"{chosen['start_bar']}:{chosen['end_bar']}",
            tail_guard_bars=guard_bars,
        )
        revision_prompt = _discontinuity_revision_prompt(spec, chosen)
        selection_reason = str(chosen["reason"])
    else:
        selected = conformance_selection
        section_name = str(selected["section_name"])
        candidates = _bar_level_candidates(spec, analysis, selected, guard_bars)
        recommended_selector = (
            "bars" if candidates and _defect_is_localized(selected) else "section"
        )
        if prefer_bar_level and candidates:
            chosen = candidates[0]
            window = resolve_repaint_window(
                spec,
                bar_range=f"{chosen['start_bar']}:{chosen['end_bar']}",
                tail_guard_bars=guard_bars,
            )
        else:
            window = resolve_repaint_window(
                spec,
                section_name=section_name,
                tail_guard_bars=guard_bars,
            )
        revision_prompt = _targeted_revision_prompt(spec, selected, findings, window=window)
        selection_reason = _selection_reason(selected)
    available_components = sum(
        selected[name] is not None
        for name in ("observed_mean_energy", "chord_match_ratio", "chord_coverage")
    )
    return {
        "plan_version": REPAINT_PLAN_VERSION,
        "execution_state": "prepared_not_rendered",
        "song_spec_sha256": _song_spec_sha256(spec),
        "analysis_audio_sha256": analysis.get("sha256"),
        "source_audio": _safe_source_audio(analysis),
        "selection": window.to_dict(),
        "selection_confidence": (
            "high"
            if defect_candidate is not None or available_components >= 3
            else "medium"
            if available_components >= 2
            else "low"
        ),
        "selection_reason": selection_reason,
        "section_diagnostics": diagnostics,
        "bar_level_candidates": candidates,
        "recommended_selector": recommended_selector,
        "revision_prompt": revision_prompt,
        "ace_step_options": {
            "task_type": "repaint",
            "audio_cover_strength": 1.0,
            "cover_noise_strength": 0.0,
            "repaint_mode": "balanced",
            "repaint_strength": 0.65,
            "repaint_latent_crossfade_frames": 10,
            "repaint_wav_crossfade_sec": (
                DISCONTINUITY_WAV_CROSSFADE_SEC
                if defect_candidate is not None
                else 0.25
            ),
            "chunk_mask_mode": "explicit",
            "tail_guard_bars": guard_bars,
        },
        "safety": {
            "original_audio_mutated": False,
            "render_started": False,
            "requires_separate_output_project": True,
        },
    }


def load_repaint_plan(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"repaint plan not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("repaint plan root must be an object")
    if payload.get("plan_version") != REPAINT_PLAN_VERSION:
        raise ValueError(f"unsupported repaint plan version: {payload.get('plan_version')!r}")
    selection = payload.get("selection")
    options = payload.get("ace_step_options")
    if not isinstance(selection, dict) or not isinstance(options, dict):
        raise ValueError("repaint plan requires selection and ace_step_options objects")
    if str(selection.get("selector", "section")) == "section" and not str(
        selection.get("section_name", "")
    ).strip():
        raise ValueError("repaint plan selection requires section_name")
    if str(selection.get("selector", "section")) == "bars" and not (
        isinstance(selection.get("start_bar"), int)
        and isinstance(selection.get("end_bar"), int)
    ):
        raise ValueError("bar-level repaint plan selection requires start_bar and end_bar")
    if options.get("task_type") != "repaint":
        raise ValueError("repaint plan task_type must be repaint")
    if not str(payload.get("revision_prompt", "")).strip():
        raise ValueError("repaint plan requires revision_prompt")
    return payload


def song_spec_sha256(spec: SongSpec) -> str:
    return _song_spec_sha256(spec)


def _section_diagnostics(
    spec: SongSpec,
    analysis: Mapping[str, Any],
) -> list[dict[str, Any]]:
    sections_data = analysis.get("sections")
    harmony_data = analysis.get("harmony")
    sections = sections_data if isinstance(sections_data, Mapping) else {}
    harmony = harmony_data if isinstance(harmony_data, Mapping) else {}
    planned_raw = sections.get("planned_sections")
    planned = planned_raw if isinstance(planned_raw, list) else []
    planned_by_name = {
        str(item.get("name")): item
        for item in planned
        if isinstance(item, Mapping) and item.get("name") is not None
    }
    bars_raw = sections.get("bars")
    energy_bars = bars_raw if isinstance(bars_raw, list) else []
    chords_raw = harmony.get("chords")
    chords = chords_raw if isinstance(chords_raw, Mapping) else {}
    chord_bars_raw = chords.get("bars")
    chord_bars = chord_bars_raw if isinstance(chord_bars_raw, list) else []
    boundary_raw = sections.get("planned_boundary_matches")
    boundary_matches = boundary_raw if isinstance(boundary_raw, list) else []

    diagnostics: list[dict[str, Any]] = []
    for index, section in enumerate(spec.arrangement):
        start_bar = section.start_bar + 1
        end_bar = section.start_bar + section.length_bars
        planned_section = planned_by_name.get(section.name, {})
        observed_energy = _number(planned_section.get("observed_mean_energy"))
        energy_error = (
            round(observed_energy - section.energy, 4)
            if observed_energy is not None
            else None
        )

        section_chords = [
            item
            for item in chord_bars
            if isinstance(item, Mapping)
            and isinstance(item.get("bar"), int)
            and start_bar <= int(item["bar"]) <= end_bar
        ]
        reliable = [item for item in section_chords if item.get("reliable") is True]
        compared = [item for item in section_chords if isinstance(item.get("match"), bool)]
        chord_coverage = (
            round(len(reliable) / section.length_bars, 4)
            if section_chords
            else None
        )
        chord_match = (
            round(sum(item["match"] is True for item in compared) / len(compared), 4)
            if compared
            else None
        )

        boundary_match: bool | None = None
        if index > 0:
            boundary = next(
                (
                    item
                    for item in boundary_matches
                    if isinstance(item, Mapping)
                    and item.get("planned_after_bar") == section.start_bar
                ),
                None,
            )
            if boundary is not None:
                boundary_match = bool(boundary.get("matched_within_one_bar"))

        terminal_collapse = None
        if index == len(spec.arrangement) - 1:
            last_bar = next(
                (
                    item
                    for item in energy_bars
                    if isinstance(item, Mapping) and item.get("bar") == end_bar
                ),
                None,
            )
            if last_bar is not None:
                last_energy = _number(last_bar.get("normalized_energy"))
                if last_energy is not None:
                    terminal_collapse = round(max(0.0, section.energy - last_energy), 4)

        energy_deficit = abs(energy_error) if energy_error is not None else 0.5
        if terminal_collapse is not None:
            energy_deficit = min(1.0, energy_deficit + 0.5 * terminal_collapse)
        chord_deficit = 1.0 - chord_match if chord_match is not None else 0.5
        readability_deficit = 1.0 - chord_coverage if chord_coverage is not None else 0.5
        boundary_deficit = 0.0 if boundary_match in {None, True} else 1.0
        priority = (
            0.30 * energy_deficit
            + 0.35 * chord_deficit
            + 0.25 * readability_deficit
            + 0.10 * boundary_deficit
        )
        diagnostics.append(
            {
                "section_name": section.name,
                "start_bar": start_bar,
                "end_bar": end_bar,
                "target_energy": section.energy,
                "observed_mean_energy": observed_energy,
                "energy_error": energy_error,
                "terminal_energy_collapse": terminal_collapse,
                "chord_coverage": chord_coverage,
                "chord_match_ratio": chord_match,
                "opening_boundary_match": boundary_match,
                "priority_score": round(priority * 100.0, 2),
            }
        )
    return diagnostics


def _defect_is_localized(selected: Mapping[str, Any]) -> bool:
    """True when only the section's final bars are wrong, not the section itself."""

    collapse = _number(selected.get("terminal_energy_collapse"))
    if collapse is None or collapse < TERMINAL_COLLAPSE_THRESHOLD:
        return False
    energy_error = _number(selected.get("energy_error"))
    return energy_error is not None and abs(energy_error) <= LOCALIZED_ENERGY_TOLERANCE


def _bar_level_candidates(
    spec: SongSpec,
    analysis: Mapping[str, Any],
    selected: Mapping[str, Any],
    guard_bars: float,
) -> list[dict[str, Any]]:
    """Narrow windows covering only the collapsed bars at the section tail.

    Repainting one bad bar in isolation gives the model no musical context, so a
    candidate is widened to :data:`MIN_BAR_CANDIDATE_BARS` where the section
    allows it.
    """

    sections_data = analysis.get("sections")
    sections = sections_data if isinstance(sections_data, Mapping) else {}
    bars_raw = sections.get("bars")
    energy_bars = bars_raw if isinstance(bars_raw, list) else []
    if not energy_bars:
        return []

    section_start = int(selected["start_bar"])
    section_end = int(selected["end_bar"])
    target_energy = _number(selected.get("target_energy"))
    if target_energy is None:
        return []
    by_bar = {
        int(item["bar"]): item
        for item in energy_bars
        if isinstance(item, Mapping) and isinstance(item.get("bar"), int)
    }

    collapsed_start = section_end + 1
    for bar in range(section_end, section_start - 1, -1):
        entry = by_bar.get(bar)
        energy = _number(entry.get("normalized_energy")) if isinstance(entry, Mapping) else None
        if energy is None or target_energy - energy < TERMINAL_COLLAPSE_THRESHOLD:
            break
        collapsed_start = bar
    if collapsed_start > section_end:
        return []

    start_bar = max(section_start, min(collapsed_start, section_end - MIN_BAR_CANDIDATE_BARS + 1))
    window = resolve_repaint_window(
        spec,
        bar_range=f"{start_bar}:{section_end}",
        tail_guard_bars=guard_bars,
    )
    candidate = window.to_dict()
    candidate["section_name"] = str(selected["section_name"])
    candidate["collapsed_bars"] = list(range(collapsed_start, section_end + 1))
    candidate["reason"] = (
        f"bars {collapsed_start}-{section_end} fall at least "
        f"{TERMINAL_COLLAPSE_THRESHOLD:.2f} below the section target energy "
        f"{target_energy:.2f}; widened to {section_end - start_bar + 1} bars for context"
    )
    return [candidate]


def _discontinuity_candidate(
    spec: SongSpec,
    defects: Mapping[str, Any] | None,
    guard_bars: float,
) -> dict[str, Any] | None:
    """Map a measured click to a small bar window that actually contains it."""

    if not isinstance(defects, Mapping):
        return None
    findings_raw = defects.get("findings")
    findings = findings_raw if isinstance(findings_raw, list) else []
    finding = next(
        (
            item
            for item in findings
            if isinstance(item, Mapping)
            and item.get("code") == "discontinuity"
            and item.get("severity") in {"blocking", "warning"}
        ),
        None,
    )
    measurements_raw = defects.get("measurements")
    measurements = measurements_raw if isinstance(measurements_raw, Mapping) else {}
    at_sec = _number(measurements.get("max_sample_jump_at_sec"))
    if finding is None or at_sec is None or at_sec < 0.0:
        return None

    bar_duration = spec.song.target_duration_sec / spec.song.total_bars
    if at_sec >= spec.song.target_duration_sec:
        defect_bar = spec.song.total_bars
    else:
        defect_bar = int(at_sec / bar_duration) + 1
    start_bar = max(1, defect_bar - 1)
    end_bar = min(spec.song.total_bars, start_bar + MIN_BAR_CANDIDATE_BARS - 1)
    start_bar = max(1, end_bar - MIN_BAR_CANDIDATE_BARS + 1)
    window = resolve_repaint_window(
        spec,
        bar_range=f"{start_bar}:{end_bar}",
        tail_guard_bars=guard_bars,
    )
    candidate = window.to_dict()
    candidate.update(
        {
            "defect_code": "discontinuity",
            "defect_at_sec": round(at_sec, 3),
            "defect_bar": defect_bar,
            "defect_value": _number(finding.get("value")),
            "defect_threshold": _number(finding.get("threshold")),
            "reason": (
                f"measured discontinuity at {at_sec:.3f} s falls in bar {defect_bar}; "
                f"bars {start_bar}-{end_bar} include context on both sides and keep "
                "the defect inside the repaint mask"
            ),
        }
    )
    return candidate


def _discontinuity_revision_prompt(
    spec: SongSpec,
    candidate: Mapping[str, Any],
) -> str:
    return " ".join(
        (
            (
                f"Repaint only bars {candidate['start_bar']}-{candidate['end_bar']} around "
                f"the measured discontinuity at {float(candidate['defect_at_sec']):.3f} "
                f"seconds in bar {candidate['defect_bar']}."
            ),
            (
                f"Preserve all Audio outside this range and keep {spec.song.bpm:g} BPM, "
                f"{spec.song.key}, {spec.song.time_signature}."
            ),
            (
                "Remove the sample-to-sample discontinuity, keep both splice boundaries "
                "click-free, and preserve the planned arrangement transition and energy "
                "shape across the window."
            ),
        )
    )


def _targeted_revision_prompt(
    spec: SongSpec,
    selected: Mapping[str, Any],
    findings: list[dict[str, Any]],
    *,
    window: Any | None = None,
) -> str:
    section_name = str(selected["section_name"])
    start_bar = int(window.start_bar) if window is not None else int(selected["start_bar"])
    end_bar = int(window.end_bar) if window is not None else int(selected["end_bar"])
    section = next(item for item in spec.arrangement if item.name == section_name)
    scope = (
        f"{section_name} (bars {start_bar}-{end_bar})"
        if start_bar == int(selected["start_bar"]) and end_bar == int(selected["end_bar"])
        else f"bars {start_bar}-{end_bar} of {section_name}"
    )
    sentences = [
        (
            f"Repaint only {scope}. Preserve all Audio "
            f"outside this range and keep {spec.song.bpm:g} BPM, {spec.song.key}, "
            f"{spec.song.time_signature}."
        ),
        (
            f"Make this section reach its planned energy {section.energy:.2f} while preserving "
            "a clean splice at both boundaries."
        ),
    ]
    if (
        selected.get("terminal_energy_collapse") is not None
        and float(selected["terminal_energy_collapse"]) >= 0.25
    ):
        sentences.append(
            f"Maintain intentional musical energy through bar {end_bar}; "
            "avoid an accidental silent tail."
        )
    relevant_codes = {
        "key_alignment",
        "chord_progression_alignment",
        "harmonic_readability",
        "section_boundary_alignment",
        "section_energy_alignment",
    }
    sentences.extend(
        str(item["recommendation"])
        for item in findings
        if item.get("code") in relevant_codes and item.get("recommendation")
    )
    return " ".join(sentences)


def _selection_reason(selected: Mapping[str, Any]) -> str:
    reasons: list[str] = []
    energy_error = _number(selected.get("energy_error"))
    if energy_error is not None:
        direction = "above" if energy_error > 0 else "below"
        reasons.append(f"mean energy is {abs(energy_error):.4f} {direction} target")
    collapse = _number(selected.get("terminal_energy_collapse"))
    if collapse is not None and collapse > 0.0:
        reasons.append(f"terminal energy collapse is {collapse:.4f}")
    coverage = _number(selected.get("chord_coverage"))
    if coverage is not None:
        reasons.append(f"reliable chord coverage is {coverage:.4f}")
    match = _number(selected.get("chord_match_ratio"))
    if match is not None:
        reasons.append(f"chord match ratio is {match:.4f}")
    if selected.get("opening_boundary_match") is False:
        reasons.append("opening boundary was not detected")
    return "; ".join(reasons) if reasons else "insufficient local evidence; deterministic fallback"


def _safe_source_audio(analysis: Mapping[str, Any]) -> dict[str, Any]:
    raw = analysis.get("audio_file")
    result: dict[str, Any] = {"sha256": analysis.get("sha256")}
    if isinstance(raw, str) and raw.strip():
        path = Path(raw)
        if not path.is_absolute() and ".." not in path.parts:
            result["relative_path"] = path.as_posix()
        else:
            result["name"] = path.name
    return result


def _song_spec_sha256(spec: SongSpec) -> str:
    return hashlib.sha256(spec.to_json().encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
