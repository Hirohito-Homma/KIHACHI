"""Which sections should carry no vocal, and the repaint that makes them so.

Telling the model not to sing does not work. Measured 2026-08-13 against
`acestep-v15-turbo`: the `[inst]` tag in the lyric sheet and the `no vocal`
trait in the prompt's Arrangement line went out together for the same section,
and the model sang over it anyway.

Withholding the words does work. A repaint carries its own `lyrics` field, so
rendering one section with `--no-lyrics` rewrites that stretch with nothing to
sing -- verified on the same server, where bars 25-32 came back instrumental
where they had sung a `[chorus]` before.

This module closes the gap between the two facts: the lyric sheet already knows
which sections were meant to be instrumental, so nobody should have to read it
and retype section names into a command. The rule is not reimplemented here --
:func:`kihachi_music_ai.lyrics.build_lyrics` is asked directly, so a change to
how the writer decides silence cannot drift away from what gets repainted.

Read-only and stdlib-only. It renders nothing: a repaint is minutes of GPU and
overwrites the take, so the decision to run one stays with the caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .lyrics import TAG_INSTRUMENTAL, build_lyrics
from .models import SongSpec


@dataclass(frozen=True)
class InstrumentalSection:
    """One section the lyric sheet left wordless, located in bars."""

    name: str
    start_bar: int
    end_bar: int
    length_bars: int
    energy: float
    vocal_probability: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InstrumentalPlan:
    """The sections to repaint wordless, and how to do it."""

    project: str
    vocal_enabled: bool
    sections: tuple[InstrumentalSection, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrumental_plan_version": "0.1",
            "scope": "reports_which_sections_should_carry_no_vocal_renders_nothing",
            "project": self.project,
            "vocal_enabled": self.vocal_enabled,
            "reason": self.reason,
            "sections": [section.to_dict() for section in self.sections],
        }

    def commands(self, *, base_url: str, audio_file: str = "audio/ace-step-01.wav") -> list[str]:
        """One repaint command per section, in the order they appear in the song.

        Each is a separate render against the take the previous one produced, so
        they are listed rather than joined: running them is sequential, and a
        caller who wants only some of them should be able to take those lines.
        """

        return [
            "python3 -m kihachi_music_ai ace-step render {project} "
            "--task-type repaint --repaint-section {name} --no-lyrics "
            "--source-audio {project}/{audio} --base-url {base_url} --overwrite".format(
                project=self.project,
                name=section.name,
                audio=audio_file,
                base_url=base_url,
            )
            for section in self.sections
        ]


def plan_instrumental_sections(project_dir: Path | str) -> InstrumentalPlan:
    """Report the sections whose lyric sheet entry is ``[inst]``. Writes nothing."""

    project_dir = Path(project_dir)
    spec_path = project_dir / "song_spec.json"
    if not spec_path.is_file():
        raise FileNotFoundError(f"SongSpec not found: {spec_path}")
    spec = SongSpec.from_json(spec_path.read_text(encoding="utf-8"))

    sheet = build_lyrics(spec)
    by_name = {section.name: section for section in spec.arrangement}
    wordless: list[InstrumentalSection] = []
    for entry in sheet.sections:
        if entry.tag != TAG_INSTRUMENTAL:
            continue
        section = by_name.get(entry.section_name)
        if section is None:
            # The sheet is built from the same arrangement, so this cannot
            # normally happen; skipping beats inventing a bar range for it.
            continue
        wordless.append(
            InstrumentalSection(
                name=section.name,
                start_bar=section.start_bar,
                end_bar=section.start_bar + section.length_bars - 1,
                length_bars=section.length_bars,
                energy=section.energy,
                vocal_probability=section.vocal_probability,
            )
        )

    if not spec.vocal.enabled:
        reason = (
            "the SongSpec has no vocal at all, so the whole render is already "
            "instrumental and no repaint is needed"
        )
    elif wordless:
        reason = (
            "the lyric sheet left these sections wordless; the model ignores an "
            "instruction not to sing, so the words have to be withheld instead"
        )
    else:
        reason = "the lyric sheet gives every section words"
    return InstrumentalPlan(
        project=str(project_dir),
        vocal_enabled=spec.vocal.enabled,
        sections=tuple(wordless),
        reason=reason,
    )


def write_instrumental_plan(
    project_dir: Path | str,
    *,
    overwrite: bool = False,
) -> Path:
    """Save the plan beside the project as ``instrumental_plan.json``."""

    project_dir = Path(project_dir)
    plan = plan_instrumental_sections(project_dir)
    destination = project_dir / "instrumental_plan.json"
    if destination.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite instrumental plan: {destination}")
    destination.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination
