"""Learn the defaults a person keeps correcting, from the edits they already made.

Every applied edit is a labelled correction that the system wrote down itself:
``{"section": "psychedelic_drop", "path": "mutation", "from": 0.88, "to": 1.0}``
plus the SongSpec it applied to. Read enough of those and the recurring ones stop
being edits and become what the defaults should have been.

This is the answer to a problem the corpus work could not solve. Measured against
both FMA and AcousticBrainz, genre does **not** predict tempo (separation 0.26,
with per-genre spreads of ~35 BPM), so no amount of public audio was going to
tell KIHACHI what a dub breakdown should feel like. One person's own edits do,
because they are that person's taste stated in the system's own units, with no
licensing, no octave errors and no genre-label mismatch.

**This never fires on its own.** KIHACHI promises that a seed reproduces a song,
repaint plans are pinned to ``song_spec_sha256``, and a regression test locks the
MIDI byte-for-byte. Priors that drifted silently would break all three. So they
are compiled by an explicit command into a versioned file, applied only when that
file is passed in, and recorded in the SongSpec when they were. Same seed plus
same preferences file means the same song, still.

Pure and stdlib-only.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .genres import find as find_genre

PREFERENCES_VERSION = "0.1"

#: How much evidence it takes to move a default most of the way. With ``k``
#: samples the offset is applied at half strength, so a single edit nudges
#: rather than rewrites. Editing is a normal part of using the system, and most
#: edits are about one song rather than a standing preference -- the shrinkage
#: is what keeps a one-off from becoming a rule.
SHRINK_K = 4.0

#: Paths an edit can move, from ``edit.QUALITY_TARGETS``. Anything outside this
#: is ignored rather than learned, so a future edit target has to be opted in
#: here deliberately.
LEARNABLE_PATHS = frozenset(
    {
        "mutation",
        "energy",
        "fx_amount",
        "density",
        "bass_density",
        "drums_density",
        "chords_density",
        "bass.mutation",
        "bass.syncopation",
        "bass.ghost_note_probability",
        "bass.octave_jump_probability",
        "groove.syncopation",
        "drums.dub_space",
        "chords.dub_delay",
    }
)


@dataclass(frozen=True)
class Observation:
    """One parameter move a person actually asked for."""

    genre: str
    scope: str
    path: str
    delta: float


@dataclass(frozen=True)
class Prior:
    genre: str
    scope: str
    path: str
    samples: int
    mean_delta: float

    @property
    def offset(self) -> float:
        """The part of the mean move that the evidence supports so far."""
        return round(self.mean_delta * self.samples / (self.samples + SHRINK_K), 6)


@dataclass(frozen=True)
class Preferences:
    version: str
    fingerprint: str
    priors: tuple[Prior, ...]

    def offset_for(self, genres: Sequence[str], scope: str, path: str) -> float:
        """The learned offset for this context, most specific evidence first.

        A genre, then the database family it belongs to, then everything. The
        backoff is why the 1020-genre hierarchy is worth having here: a person
        will never edit enough tech house *specifically* to learn from, but they
        will edit enough House.
        """
        for key in self._keys(genres):
            for prior in self.priors:
                if prior.genre == key and prior.scope == scope and prior.path == path:
                    return prior.offset
        return 0.0

    @staticmethod
    def _keys(genres: Sequence[str]) -> list[str]:
        keys: list[str] = list(genres)
        for slug in genres:
            entry = find_genre(slug)
            if entry and entry.parent:
                family = "family:" + entry.parent
                if family not in keys:
                    keys.append(family)
        keys.append("*")
        return keys

    def to_dict(self) -> dict[str, Any]:
        return {
            "preferences_version": self.version,
            "fingerprint": self.fingerprint,
            "priors": [
                {
                    "genre": p.genre,
                    "scope": p.scope,
                    "path": p.path,
                    "samples": p.samples,
                    "mean_delta": round(p.mean_delta, 6),
                    "offset": p.offset,
                }
                for p in self.priors
            ],
        }


EMPTY = Preferences(version=PREFERENCES_VERSION, fingerprint="", priors=())


def harvest(projects_dir: Path) -> list[Observation]:
    """Read every applied edit under ``projects_dir``.

    The training data is already on disk: ``apply-edit`` writes
    ``applied_spec_edit.json`` next to the song it produced, and the song's own
    ``song_spec.json`` says which genres it was. Nothing new is logged, and
    nothing is collected that the person did not already choose to keep.
    """

    projects_dir = Path(projects_dir)
    observations: list[Observation] = []
    for edit_path in sorted(projects_dir.glob("*/applied_spec_edit.json")):
        try:
            edit = json.loads(edit_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if edit.get("execution_state") != "applied":
            continue
        spec_path = edit_path.parent / "song_spec.json"
        genres = _genres_of(spec_path)
        if not genres:
            continue
        observations.extend(_observations(edit, genres))
    return observations


def _genres_of(spec_path: Path) -> list[str]:
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = (spec.get("style") or {}).get("genres") or []
    return [str(item.get("name")) for item in entries if item.get("name")]


def _observations(edit: Mapping[str, Any], genres: Sequence[str]) -> list[Observation]:
    found: list[Observation] = []
    for change in edit.get("changes") or []:
        path = str(change.get("path", ""))
        if path not in LEARNABLE_PATHS:
            continue
        try:
            delta = float(change["to"]) - float(change["from"])
        except (KeyError, TypeError, ValueError):
            continue
        if delta == 0:
            continue
        scope = str(change.get("scope", "song"))
        # One edit on a three-genre song is evidence about each of them, at the
        # song's own weighting -- but weights are not in the change record, so
        # each genre gets the observation and the sample counts sort it out.
        for genre in genres:
            found.append(Observation(genre=genre, scope=scope, path=path, delta=delta))
    return found


def compile_preferences(observations: Iterable[Observation]) -> Preferences:
    """Aggregate observations into priors, including the family and global rollups."""

    buckets: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for obs in observations:
        buckets[(obs.genre, obs.scope, obs.path)].append(obs.delta)
        entry = find_genre(obs.genre)
        if entry and entry.parent:
            buckets[("family:" + entry.parent, obs.scope, obs.path)].append(obs.delta)
        buckets[("*", obs.scope, obs.path)].append(obs.delta)

    priors = [
        Prior(
            genre=genre,
            scope=scope,
            path=path,
            samples=len(deltas),
            mean_delta=sum(deltas) / len(deltas),
        )
        for (genre, scope, path), deltas in sorted(buckets.items())
        if deltas
    ]
    digest = hashlib.sha256(
        json.dumps(
            [[p.genre, p.scope, p.path, p.samples, round(p.mean_delta, 6)] for p in priors],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return Preferences(PREFERENCES_VERSION, digest, tuple(priors))


def load(path: Path | str | None) -> Preferences:
    """Read a compiled preferences file; a missing path means learn nothing."""

    if path is None:
        return EMPTY
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    version = str(payload.get("preferences_version", ""))
    if version != PREFERENCES_VERSION:
        raise ValueError(
            "unsupported preferences version: %r (expected %s)"
            % (version, PREFERENCES_VERSION)
        )
    priors = tuple(
        Prior(
            genre=str(item["genre"]),
            scope=str(item["scope"]),
            path=str(item["path"]),
            samples=int(item["samples"]),
            mean_delta=float(item["mean_delta"]),
        )
        for item in payload.get("priors", [])
    )
    return Preferences(version, str(payload.get("fingerprint", "")), priors)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
