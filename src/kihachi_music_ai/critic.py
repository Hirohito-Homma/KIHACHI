"""Critic: interpret structured reviewer evidence into findings and prompts.

The critic does not re-read MIDI, re-run density diagnostics, or re-score
alignment. It consumes the reviewer's collected evidence and alignment output.
"""

from __future__ import annotations

from typing import Any

from .review_contract import EvidenceStatus, ReviewPhase
from .models import SongSpec
from .spectrum import DULL_LOW_TO_HIGH, MASKING_BASS_SHARE

CRITIC_VERSION = "0.1"

_DEFECT_ADVICE = {
    "silent_gap": (
        "The take has a hole. If it sits at the end, render with --tail-guard-bars "
        "so the model writes its ending past the song grid; elsewhere, repaint the "
        "bars around the gap."
    ),
    "clipping": "Lower the render level or the LoRA scale before reusing this take.",
    "dc_offset": "Remove the offset with a high-pass before splicing or layering.",
    "crushed_dynamics": "Transients are squashed; this take will not sit under others.",
    "phase_cancellation": "The channels partly cancel; check it in mono before committing.",
    "discontinuity": (
        "Likely a click. If it lands on a repaint boundary, raise "
        "--repaint-wav-crossfade-sec."
    ),
}


def critique_evidence(
    spec: SongSpec,
    *,
    phase: ReviewPhase,
    analysis: dict[str, Any] | None,
    audio_alignment: dict[str, Any] | None,
    midi_review: dict[str, Any] | None,
    defects: dict[str, Any] | None,
    audio_analysis_status: EvidenceStatus,
    midi_status: EvidenceStatus,
    defects_status: EvidenceStatus,
) -> dict[str, Any]:
    """Turn collected evidence into findings and a revision prompt."""

    findings: list[dict[str, Any]] = []
    if phase is ReviewPhase.GENERATION_REVIEW and analysis is not None:
        findings.extend(audio_alignment_findings(spec, analysis))
        findings.extend(midi_findings(analysis, midi_review))
        findings.extend(defect_findings(defects))
        spectrum = analysis.get("spectrum")
        findings.extend(balance_findings(spectrum))
    elif phase is ReviewPhase.MIDI_ONLY and midi_review is not None:
        findings.extend(midi_only_findings(midi_review))

    revision_prompt = revision_prompt_for_findings(spec, findings, phase=phase)
    return {
        "critic_version": CRITIC_VERSION,
        "phase": phase.value,
        "evidence_status": {
            "audio_analysis": audio_analysis_status.value,
            "midi": midi_status.value,
            "material_defects": defects_status.value,
        },
        "findings": findings,
        "revision_prompt": revision_prompt,
        "audio_alignment": audio_alignment,
    }


def balance_findings(spectrum: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Where a take's spectrum falls outside what this generator normally does."""

    if not spectrum:
        return []
    bands = spectrum.get("bands", {})
    findings: list[dict[str, Any]] = []
    ratio = spectrum.get("low_to_high_ratio")
    if ratio is not None and ratio > DULL_LOW_TO_HIGH:
        findings.append(
            {
                "code": "dull_high_end",
                "severity": "medium",
                "evidence": (
                    f"low-to-high energy ratio {ratio:g} against a corpus median of "
                    f"21.9; only {bands.get('high', {}).get('share', 0.0):.1%} of the "
                    "energy is above 6 kHz"
                ),
                "recommendation": (
                    "Compare against another take before repainting: repeated "
                    "repaints of the same material smear the top end, and the "
                    "chunked render is the other take that lands here."
                ),
            }
        )
    bass_share = bands.get("bass", {}).get("share")
    if bass_share is not None and bass_share > MASKING_BASS_SHARE:
        findings.append(
            {
                "code": "bass_masking",
                "severity": "medium",
                "evidence": (
                    f"{bass_share:.1%} of the energy is in 60-250 Hz, past the "
                    f"{MASKING_BASS_SHARE:.0%} the corpus reaches"
                ),
                "recommendation": (
                    "Everything above the bass is being buried. Check whether the "
                    "other parts are audible at all before working on the mix."
                ),
            }
        )
    return findings


def defect_findings(defects: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Surface usability defects next to conformance."""

    if defects is None:
        return []
    findings: list[dict[str, Any]] = []
    for defect in defects.get("findings", []):
        if defect.get("severity") not in {"blocking", "warning"}:
            continue
        findings.append(
            {
                "code": f"material_{defect['code']}",
                "severity": "high" if defect["severity"] == "blocking" else "medium",
                "evidence": defect["detail"],
                "recommendation": _DEFECT_ADVICE.get(
                    defect["code"],
                    "Inspect the audio at the reported position before using this take.",
                ),
            }
        )
    return findings


def midi_findings(
    analysis: dict[str, Any],
    midi_review: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Separate composition errors from audio detection limits."""

    if midi_review is None:
        return []
    findings: list[dict[str, Any]] = []
    harmony = midi_review["harmony"]
    written = min(
        _number(harmony.get("bass_root_match_ratio")) or 0.0,
        _number(harmony.get("chord_tone_match_ratio")) or 0.0,
    )
    heard = _number(analysis.get("song_spec_comparison", {}).get("progression_match_ratio"))
    heard = 0.0 if heard is None else heard

    if written >= 0.95 and heard < 0.5:
        findings.append(
            {
                "code": "harmony_written_but_not_detected",
                "severity": "info",
                "evidence": (
                    f"The written MIDI plays the SongSpec progression exactly "
                    f"(match {written:.4f}), while the audio analysis reads "
                    f"{heard:.4f} from the finished mix."
                ),
                "recommendation": (
                    "Treat the audio chord score as a detection limit, not a "
                    "composition error. Do not repaint to 'fix' the progression; "
                    "improve separation around chord attacks if the chords should "
                    "become audible."
                ),
            }
        )
        findings.extend(_midi_composition_findings(midi_review, include_harmony=False))
    elif written < 0.95:
        findings.extend(_midi_composition_findings(midi_review))
    else:
        findings.extend(_midi_composition_findings(midi_review, include_harmony=False))

    return findings


def midi_only_findings(midi_review: dict[str, Any]) -> list[dict[str, Any]]:
    """MIDI-phase findings without audio-dependent interpretation."""

    return _midi_composition_findings(midi_review)


def _midi_composition_findings(
    midi_review: dict[str, Any],
    *,
    include_harmony: bool = True,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    harmony = midi_review["harmony"]
    if include_harmony and (
        (_number(harmony.get("bass_root_match_ratio")) or 0.0) < 0.95
        or (_number(harmony.get("chord_tone_match_ratio")) or 0.0) < 0.95
    ):
        findings.append(
            {
                "code": "midi_harmony_misaligned",
                "severity": "high",
                "evidence": (
                    f"The written MIDI itself departs from the SongSpec progression "
                    f"(bass-root match {harmony['bass_root_match_ratio']:.4f}, "
                    f"chord-tone match {harmony['chord_tone_match_ratio']:.4f})."
                ),
                "recommendation": (
                    "Fix the composition before rendering again; no repaint can "
                    "correct harmony that was never written."
                ),
            }
        )

    key = midi_review["key"]
    if key["out_of_key_notes"]:
        findings.append(
            {
                "code": "midi_out_of_key_notes",
                "severity": "medium",
                "evidence": (
                    f"{key['out_of_key_notes']} of {key['pitched_notes']} pitched MIDI "
                    f"notes fall outside {key['key']}."
                ),
                "recommendation": "Constrain the composer to the SongSpec scale.",
            }
        )

    empty = midi_review["coverage"]["empty_bars"]
    if empty:
        findings.append(
            {
                "code": "midi_empty_bars",
                "severity": "medium",
                "evidence": f"Tracks with silent bars: {empty}.",
                "recommendation": (
                    "Check the arrangement densities; a silent bar in the MIDI will "
                    "read as an energy collapse downstream."
                ),
            }
        )
    return findings


def audio_alignment_findings(spec: SongSpec, analysis: dict[str, Any]) -> list[dict[str, Any]]:
    comparison = analysis.get("song_spec_comparison", {})
    harmony = analysis.get("harmony", {})
    chords = harmony.get("chords", {})
    sections = analysis.get("sections", {})
    findings: list[dict[str, Any]] = []

    duration_delta = _number(comparison.get("duration_delta_sec"))
    if duration_delta is None or abs(duration_delta) > 0.25:
        findings.append(
            _finding(
                "action",
                "duration_alignment",
                f"Duration delta is {duration_delta} seconds.",
                f"Keep the render at {spec.song.target_duration_sec:.3f} seconds ({spec.song.total_bars} bars).",
            )
        )

    tempo_delta = _number(comparison.get("tempo_delta_bpm"))
    if tempo_delta is None or abs(tempo_delta) > 1.0:
        findings.append(
            _finding(
                "action",
                "tempo_alignment",
                f"Tempo delta is {tempo_delta} BPM.",
                f"Lock the rhythmic pulse to {spec.song.bpm:g} BPM without half-time or double-time ambiguity.",
            )
        )

    key_status = comparison.get("key_status")
    key_confidence = _number(comparison.get("key_confidence"))
    observed_key = comparison.get("observed_key")
    if key_status != "match":
        severity = "warning" if key_status in {"low_confidence", "not_detected"} else "action"
        findings.append(
            _finding(
                severity,
                "key_alignment",
                f"Observed key candidate is {observed_key} at confidence {key_confidence}; status is {key_status}.",
                f"Anchor section openings and bass pedals on {spec.song.tonic}; make {spec.song.key} unambiguous.",
            )
        )

    chord_match = _number(comparison.get("progression_match_ratio"))
    progression = " - ".join(spec.harmony.progression)
    if chord_match is None or chord_match < 0.5:
        findings.append(
            _finding(
                "action",
                "chord_progression_alignment",
                f"Reliable-bar progression match is {chord_match}.",
                f"State one clear chord per bar in the repeating progression {progression}; keep delay tails below the next change.",
            )
        )

    coverage = _number(chords.get("confident_bar_coverage"))
    if coverage is None or coverage < 0.5:
        findings.append(
            _finding(
                "warning",
                "harmonic_readability",
                f"Confident chord coverage is {coverage}.",
                "Reduce harmonic masking from vocals, distortion, reverb, and dub delay around chord attacks.",
            )
        )

    boundary_recall = _number(comparison.get("section_boundary_recall"))
    planned_boundaries = sections.get("planned_boundaries_after_bar", [])
    if boundary_recall is None or boundary_recall < 0.67:
        findings.append(
            _finding(
                "action",
                "section_boundary_alignment",
                f"Planned-boundary recall is {boundary_recall}; planned boundaries are after bars {planned_boundaries}.",
                "Mark each planned boundary with a clear dropout, fill, riser, or density change.",
            )
        )

    energy_correlation = _number(comparison.get("section_energy_correlation"))
    if energy_correlation is None or energy_correlation < 0.5:
        target_arc = " → ".join(f"{section.name} {section.energy:.2f}" for section in spec.arrangement)
        findings.append(
            _finding(
                "action",
                "section_energy_alignment",
                f"Section-energy correlation is {energy_correlation}.",
                f"Follow a clearly rising energy arc: {target_arc}.",
            )
        )
    return findings


def revision_prompt_for_findings(
    spec: SongSpec,
    findings: list[dict[str, Any]],
    *,
    phase: ReviewPhase,
    for_repaint: bool = False,
) -> str:
    header = (
        f"Revision pass: keep {spec.song.bpm:g} BPM, {spec.song.key}, "
        f"{spec.song.time_signature}, {spec.song.total_bars} bars."
    )
    if for_repaint and phase is ReviewPhase.GENERATION_REVIEW:
        midi_codes = {
            "harmony_written_but_not_detected",
            "midi_harmony_misaligned",
            "midi_out_of_key_notes",
            "midi_empty_bars",
        }
        recommendations = [
            item["recommendation"]
            for item in findings
            if item["code"] not in midi_codes
        ]
    elif phase is ReviewPhase.MIDI_ONLY:
        recommendations = [
            item["recommendation"]
            for item in findings
            if item.get("severity") in {"high", "medium", "action"}
        ]
    else:
        recommendations = [item["recommendation"] for item in findings]
    if not recommendations:
        return header + " Preserve the current SongSpec alignment and refine sound quality only."
    return " ".join([header, *recommendations])


def repaint_revision_prompt(revision_prompt: str) -> str:
    """Return ``revision_prompt`` unchanged; kept for a stable reviewer call site."""

    return revision_prompt


def _finding(severity: str, code: str, evidence: str, recommendation: str) -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "evidence": evidence,
        "recommendation": recommendation,
    }


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
