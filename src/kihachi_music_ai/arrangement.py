"""Arrangement Engine: how the song spends its running time.

Before this module the layout was always the same four equal blocks, whatever
the length. That is fine for a 32-bar sketch and wrong for anything longer -- a
five-minute track came out as a 34-bar intro followed by three more 34-bar
blocks, with no breakdown and no second drop.

The engine instead picks a *sequence of section archetypes* sized to the
available 8-bar blocks, and gives every section its own per-track densities and
active-track set. That is what lets "make the second half harder" be a change to
the energy curve rather than a re-roll of the whole song, and what lets a dub
breakdown actually drop the drums instead of merely being marked low-energy.

Backwards compatibility is a hard requirement here: at 32 bars the engine
reproduces the original four-section layout exactly, field for field, because
existing projects pin their repaint plans to the SongSpec SHA-256.

Pure and stdlib-only; no randomness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .models import CORE_TRACKS, TRACK_NAMES, SectionSpec

BLOCK_BARS = 8


@dataclass(frozen=True)
class SectionArchetype:
    """One kind of section, independent of how long the song is."""

    name: str
    energy: float
    psychedelic: float
    minimal: bool
    bass_density: float
    drum_density: float
    chord_density: float
    fx_amount: float
    vocal_probability: float
    mutation: float
    resting_tracks: tuple[str, ...] = ()
    """Which parts sit this section out.

    Named as what *rests* rather than what plays, so an archetype stays correct
    when the song gains parts. Listing the active ones meant that adding synth,
    arp and vocoder silently rested all three everywhere an archetype had named
    a subset -- the breakdown would have gone from "no drums" to "bass and
    chords only" without anyone deciding that.
    """

    blocks: int = 1

    def to_section(
        self,
        start_bar: int,
        length_bars: int,
        parts: Sequence[str] = CORE_TRACKS,
    ) -> SectionSpec:
        return SectionSpec(
            name=self.name,
            start_bar=start_bar,
            length_bars=length_bars,
            energy=self.energy,
            minimal=self.minimal,
            psychedelic=self.psychedelic,
            bass_density=self.bass_density,
            drum_density=self.drum_density,
            chord_density=self.chord_density,
            fx_amount=self.fx_amount,
            vocal_probability=self.vocal_probability,
            mutation=self.mutation,
            active_tracks=(
                None
                if not self.resting_tracks
                else tuple(name for name in parts if name not in self.resting_tracks)
            ),
        )


ARCHETYPES: dict[str, SectionArchetype] = {
    "minimal_intro": SectionArchetype(
        name="minimal_intro", energy=0.25, psychedelic=0.08, minimal=True,
        bass_density=0.18, drum_density=0.45, chord_density=0.15,
        fx_amount=0.30, vocal_probability=0.0, mutation=0.15, blocks=1,
    ),
    "minimal_groove": SectionArchetype(
        name="minimal_groove", energy=0.44, psychedelic=0.18, minimal=True,
        bass_density=0.52, drum_density=0.62, chord_density=0.32,
        fx_amount=0.35, vocal_probability=0.2, mutation=0.28, blocks=2,
    ),
    # Sits clearly below mutation_build: at 0.62 the step into the build was
    # 0.5 dB, which is not an audible section change and did not register as a
    # boundary at all. A groove that a build cannot climb out of is not a groove.
    "mutation_groove": SectionArchetype(
        name="mutation_groove", energy=0.55, psychedelic=0.44, minimal=False,
        bass_density=0.66, drum_density=0.70, chord_density=0.46,
        fx_amount=0.45, vocal_probability=0.5, mutation=0.62, blocks=2,
    ),
    "mutation_build": SectionArchetype(
        name="mutation_build", energy=0.66, psychedelic=0.58, minimal=False,
        bass_density=0.70, drum_density=0.82, chord_density=0.55,
        fx_amount=0.62, vocal_probability=0.45, mutation=0.70, blocks=2,
    ),
    "psychedelic_drop": SectionArchetype(
        name="psychedelic_drop", energy=0.88, psychedelic=0.96, minimal=False,
        bass_density=1.0, drum_density=0.92, chord_density=0.66,
        fx_amount=0.70, vocal_probability=0.6, mutation=0.88, blocks=3,
    ),
    "dub_breakdown": SectionArchetype(
        name="dub_breakdown", energy=0.28, psychedelic=0.82, minimal=False,
        bass_density=0.30, drum_density=0.0, chord_density=0.45,
        fx_amount=1.0, vocal_probability=0.35, mutation=0.55,
        # Dub takes the drums out entirely; that is the contrast the drop needs.
        resting_tracks=("drums",), blocks=2,
    ),
    "final_drop": SectionArchetype(
        name="final_drop", energy=0.95, psychedelic=0.92, minimal=False,
        bass_density=1.0, drum_density=1.0, chord_density=0.60,
        fx_amount=0.75, vocal_probability=0.55, mutation=0.95, blocks=3,
    ),
    "outro": SectionArchetype(
        name="outro", energy=0.22, psychedelic=0.40, minimal=True,
        bass_density=0.35, drum_density=0.20, chord_density=0.25,
        fx_amount=0.55, vocal_probability=0.15, mutation=0.20,
        resting_tracks=("drums",), blocks=1,
    ),
}

# Arcs are chosen by how many 8-bar blocks the song has. The four-block arc is
# the original layout and must stay exactly as it is.
ARCS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (4, ("minimal_intro", "minimal_groove", "mutation_build", "psychedelic_drop")),
    (
        8,
        (
            "minimal_intro",
            "minimal_groove",
            "mutation_build",
            "psychedelic_drop",
            "dub_breakdown",
            "final_drop",
        ),
    ),
    (
        16,
        (
            "minimal_intro",
            "minimal_groove",
            "mutation_groove",
            "mutation_build",
            "psychedelic_drop",
            "dub_breakdown",
            "mutation_build",
            "final_drop",
            "outro",
        ),
    ),
)


def select_arc(total_bars: int) -> tuple[str, ...]:
    """The section sequence for a song of ``total_bars`` bars."""

    if total_bars <= 0:
        raise ValueError("total_bars must be positive")
    blocks = max(1, total_bars // BLOCK_BARS)
    chosen = ARCS[0][1]
    for minimum_blocks, arc in ARCS:
        if blocks >= minimum_blocks:
            chosen = arc
    # Never plan more sections than there are bars to give them.
    while len(chosen) > total_bars:
        chosen = chosen[: max(1, len(chosen) // 2)]
    return chosen


def distribute_bars(
    total_bars: int,
    section_count: int,
    weights: Sequence[int] | None = None,
) -> tuple[int, ...]:
    """Split ``total_bars`` across sections, in 8-bar blocks where possible.

    Dance arrangements are counted in 8-bar phrases, so a 15-bar breakdown is
    simply wrong. When there is at least one block per section, every section
    gets one block and the surplus is shared out by ``weights`` (drops take more
    than intros) using largest-remainder, with any bars left over from a song
    that is not a whole number of blocks going to the final section.

    Below one block per section this falls back to the original even split, so
    the four-section 32-bar layout is bit-for-bit unchanged.
    """

    if section_count <= 0:
        raise ValueError("section_count must be positive")
    if total_bars < section_count:
        raise ValueError("not enough bars for the requested section count")
    if weights is None:
        weights = [1] * section_count
    if len(weights) != section_count:
        raise ValueError("weights must have one entry per section")

    blocks = total_bars // BLOCK_BARS
    if blocks < section_count:
        base = total_bars // section_count
        lengths = [base] * section_count
        lengths[-1] = total_bars - base * (section_count - 1)
        return tuple(lengths)

    allocation = [1] * section_count
    surplus = blocks - section_count
    if surplus:
        total_weight = sum(weights)
        shares = [surplus * weight / total_weight for weight in weights]
        whole = [int(share) for share in shares]
        remaining = surplus - sum(whole)
        order = sorted(
            range(section_count),
            key=lambda index: (shares[index] - whole[index], weights[index]),
            reverse=True,
        )
        for index in order[:remaining]:
            whole[index] += 1
        allocation = [allocation[i] + whole[i] for i in range(section_count)]

    lengths = [count * BLOCK_BARS for count in allocation]
    lengths[-1] += total_bars - blocks * BLOCK_BARS
    return tuple(lengths)


def build_arrangement(
    total_bars: int,
    *,
    minimal_requested: bool = True,
    psychedelic_requested: bool = True,
    arc: Sequence[str] | None = None,
    parts: Sequence[str] = CORE_TRACKS,
) -> tuple[SectionSpec, ...]:
    """Lay out a full arrangement for ``total_bars``.

    ``minimal_requested`` and ``psychedelic_requested`` keep the meaning they
    had in the Music Brain: they gate the ``minimal`` flag on the opening
    sections and the intensity of the main drop.
    """

    names = tuple(arc) if arc is not None else select_arc(total_bars)
    unknown = [name for name in names if name not in ARCHETYPES]
    if unknown:
        raise ValueError(f"unknown section archetype: {unknown}")
    lengths = distribute_bars(
        total_bars, len(names), [ARCHETYPES[name].blocks for name in names]
    )
    section_names = _unique_names(names)

    sections: list[SectionSpec] = []
    start = 0
    for index, (name, length) in enumerate(zip(names, lengths)):
        archetype = ARCHETYPES[name]
        if not psychedelic_requested and name == "psychedelic_drop":
            archetype = _replace_psychedelic(archetype, 0.58)
        section = archetype.to_section(start, length, parts)
        if section_names[index] != name:
            section = _rename(section, section_names[index])
        if not (minimal_requested and index < 2):
            section = _clear_minimal(section)
        sections.append(section)
        start += length
    return tuple(sections)


def _unique_names(names: Sequence[str]) -> tuple[str, ...]:
    """Number repeats, so ``--repaint-section`` can address a section at all.

    An arc may reuse an archetype (a second build before the final drop); two
    sections called ``mutation_build`` would make section lookup ambiguous and
    silently resolve to the first one.
    """

    totals: dict[str, int] = {}
    for name in names:
        totals[name] = totals.get(name, 0) + 1
    seen: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        if totals[name] == 1:
            result.append(name)
            continue
        seen[name] = seen.get(name, 0) + 1
        result.append(f"{name}_{seen[name]}")
    return tuple(result)


def describe_arrangement(sections: Sequence[SectionSpec]) -> list[dict[str, object]]:
    """A flat, printable view of the energy curve and who plays where."""

    return [
        {
            "name": section.name,
            "start_bar": section.start_bar + 1,
            "length_bars": section.length_bars,
            "energy": section.energy,
            "densities": {track: section.density(track) for track in TRACK_NAMES},
            "active_tracks": [track for track in TRACK_NAMES if section.plays(track)],
            "mutation": section.mutation,
            "fx_amount": section.fx_amount,
        }
        for section in sections
    ]


def _replace_psychedelic(archetype: SectionArchetype, value: float) -> SectionArchetype:
    from dataclasses import replace

    return replace(archetype, psychedelic=value)


def _clear_minimal(section: SectionSpec) -> SectionSpec:
    from dataclasses import replace

    return section if not section.minimal else replace(section, minimal=False)


def _rename(section: SectionSpec, name: str) -> SectionSpec:
    from dataclasses import replace

    return replace(section, name=name)
