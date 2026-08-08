"""Mutation engine: repetition that keeps drifting.

Mutation Funk is treated here as an algorithm rather than a genre label. A part
is written once as a base pattern, then each following bar is a mutation of the
*previous* bar rather than an independent roll of the dice, so a section reads as
one idea being pushed further and further:

    A -> A' -> A'' -> A'''

``amount`` decides how many steps change per bar, and the per-operation
probabilities from the SongSpec decide *which* change happens. Two invariants
hold at every amount, matching ``preserve_groove`` / ``preserve_key``:

* anchors (the downbeat, the backbeat) are never dropped or displaced, so the
  groove stays legible however far the pattern drifts;
* pitch is only ever moved by whole octaves, so the harmony is untouched.

Pure and stdlib-only: every function takes its ``random.Random`` explicitly and
returns new tuples, so the same seed always produces the same part.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from typing import Sequence

GRID = 0.25
"""Sixteenth-note grid, in quarter-note beats."""

MUTATIONS_PER_BAR = 0.5
"""At amount 1.0, roughly half of a bar's steps change each bar."""

GHOST_VELOCITY_SCALE = 0.42
MIN_VELOCITY = 1
MAX_VELOCITY = 127


@dataclass(frozen=True)
class Step:
    """One rhythmic slot in a one-bar pattern."""

    position: float
    duration: float
    velocity: int
    octave: int = 0
    anchor: bool = False
    ghost: bool = False

    def __post_init__(self) -> None:
        if self.position < 0.0:
            raise ValueError("step position must not be negative")
        if self.duration <= 0.0:
            raise ValueError("step duration must be positive")
        if not MIN_VELOCITY <= self.velocity <= MAX_VELOCITY:
            raise ValueError("step velocity must be between 1 and 127")


def build_pattern(
    ranked_positions: Sequence[float],
    *,
    density: float,
    minimum: int,
    maximum: int,
    duration: float,
    velocity: int,
    anchors: Sequence[float] = (),
) -> tuple[Step, ...]:
    """Take the first N of a groove-ordered position list, N set by ``density``.

    The list is ordered by how load-bearing each slot is, so raising density adds
    progressively less essential notes instead of reshuffling the pattern.
    """

    if not 0.0 <= density <= 1.0:
        raise ValueError("density must be between 0.0 and 1.0")
    if minimum < 1 or maximum < minimum:
        raise ValueError("invalid step-count bounds")
    count = min(len(ranked_positions), minimum + round(density * (maximum - minimum)))
    chosen = sorted(ranked_positions[:count])
    return tuple(
        Step(
            position=position,
            duration=duration,
            velocity=velocity,
            anchor=position in anchors,
        )
        for position in chosen
    )


def mutate_pattern(
    steps: Sequence[Step],
    *,
    amount: float,
    rng: random.Random,
    syncopation: float = 0.0,
    ghost_probability: float = 0.0,
    octave_jump_probability: float = 0.0,
    space: float = 0.0,
    bar_beats: float = 4.0,
    minimum_steps: int = 1,
) -> tuple[Step, ...]:
    """Change a few steps of ``steps`` and return the result as a new pattern."""

    for name, value in (
        ("amount", amount),
        ("syncopation", syncopation),
        ("ghost_probability", ghost_probability),
        ("octave_jump_probability", octave_jump_probability),
        ("space", space),
    ):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between 0.0 and 1.0")
    current = list(steps)
    if not current or amount <= 0.0:
        return tuple(current)

    changes = round(amount * len(current) * MUTATIONS_PER_BAR)
    weights = (
        ("displace", syncopation),
        ("ghost", ghost_probability),
        ("octave", octave_jump_probability),
        ("drop", space),
        ("accent", 0.35),
    )
    total = sum(weight for _name, weight in weights)
    if total <= 0.0:
        return tuple(current)

    for _ in range(changes):
        operation = _weighted_choice(weights, total, rng)
        index = rng.randrange(len(current))
        step = current[index]
        if operation == "displace" and not step.anchor:
            current[index] = _displace(step, rng, bar_beats)
        elif operation == "ghost":
            ghost = _ghost(step, rng, bar_beats)
            if ghost is not None and not _occupied(current, ghost.position):
                current.append(ghost)
                current.sort(key=lambda item: item.position)
        elif operation == "octave":
            current[index] = replace(step, octave=_next_octave(step.octave, rng))
        elif operation == "drop" and not step.anchor and len(current) > minimum_steps:
            current.pop(index)
        elif operation == "accent":
            current[index] = _accent(step, rng)
    return tuple(sorted(current, key=lambda item: item.position))


PHRASE_BARS = 4
PHRASE_DRIFT = 0.6


def mutation_series(
    base: Sequence[Step],
    *,
    bars: int,
    amount: float,
    rng: random.Random,
    phrase_bars: int = PHRASE_BARS,
    **options: float,
) -> tuple[tuple[Step, ...], ...]:
    """One pattern per bar, drifting within a phrase and across phrases.

    Compounding every bar for a whole section is not how this music is built and
    it decays: several operations only ever remove steps, so a long section
    thins out with no way back. Dance arrangements move in 4-bar phrases, so the
    series works on two levels -- inside a phrase each bar mutates from the bar
    before it, and each new phrase restates a *lightly* drifted version of the
    previous phrase's opening:

        A A' A'' A''' | B B' B'' B''' | C ...   where B = mutate(A), C = mutate(B)

    The first bar is always the untouched base, so a section still states its
    idea before deforming it, and the pattern can never decay without bound.
    """

    if bars < 0:
        raise ValueError("bars must not be negative")
    if phrase_bars <= 0:
        raise ValueError("phrase_bars must be positive")
    generations: list[tuple[Step, ...]] = []
    phrase_base = tuple(base)
    current = phrase_base
    for index in range(bars):
        if index % phrase_bars == 0:
            if index:
                phrase_base = mutate_pattern(
                    phrase_base, amount=amount * PHRASE_DRIFT, rng=rng, **options
                )
            current = phrase_base
        else:
            current = mutate_pattern(current, amount=amount, rng=rng, **options)
        generations.append(current)
    return tuple(generations)


def _weighted_choice(
    weights: Sequence[tuple[str, float]],
    total: float,
    rng: random.Random,
) -> str:
    threshold = rng.random() * total
    cumulative = 0.0
    for name, weight in weights:
        cumulative += weight
        if threshold < cumulative:
            return name
    return weights[-1][0]


def _displace(step: Step, rng: random.Random, bar_beats: float) -> Step:
    offset = GRID if rng.random() < 0.5 else -GRID
    position = step.position + offset
    if not 0.0 <= position < bar_beats:
        position = step.position - offset
    if not 0.0 <= position < bar_beats:
        return step
    return replace(step, position=round(position, 4))


def _ghost(step: Step, rng: random.Random, bar_beats: float) -> Step | None:
    position = step.position + GRID
    if position >= bar_beats:
        position = step.position - GRID
    if position < 0.0:
        return None
    velocity = max(MIN_VELOCITY, round(step.velocity * GHOST_VELOCITY_SCALE))
    return Step(
        position=round(position, 4),
        duration=min(step.duration, GRID * 0.5),
        velocity=velocity,
        octave=step.octave,
        ghost=True,
    )


def _next_octave(octave: int, rng: random.Random) -> int:
    if octave > 0:
        return 0
    if octave < 0:
        return 0
    return 12 if rng.random() < 0.75 else -12


def _accent(step: Step, rng: random.Random) -> Step:
    delta = rng.choice((-18, -9, 9, 16))
    velocity = max(MIN_VELOCITY, min(MAX_VELOCITY, step.velocity + delta))
    return replace(step, velocity=velocity)


def _occupied(steps: Sequence[Step], position: float) -> bool:
    return any(abs(step.position - position) < 1e-6 for step in steps)
