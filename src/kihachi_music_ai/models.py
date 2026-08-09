from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def _unit_interval(name: str, value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")


@dataclass(frozen=True)
class GenreWeight:
    name: str
    weight: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("genre name must not be empty")
        _unit_interval("genre weight", self.weight)


@dataclass(frozen=True)
class SongIdentity:
    title: str
    bpm: float
    key: str
    tonic: str
    tonic_pitch_class: int
    mode: str
    time_signature: str
    total_bars: int
    target_duration_sec: float

    def __post_init__(self) -> None:
        if not 30.0 <= self.bpm <= 300.0:
            raise ValueError("bpm must be between 30 and 300")
        if self.mode not in {"major", "minor"}:
            raise ValueError("mode must be major or minor")
        if not 0 <= self.tonic_pitch_class <= 11:
            raise ValueError("tonic_pitch_class must be between 0 and 11")
        if self.total_bars <= 0:
            raise ValueError("total_bars must be positive")


@dataclass(frozen=True)
class StyleSpec:
    genres: tuple[GenreWeight, ...]
    darkness: float
    psychedelic: float

    def __post_init__(self) -> None:
        if not self.genres:
            raise ValueError("at least one genre is required")
        if abs(sum(item.weight for item in self.genres) - 1.0) > 0.001:
            raise ValueError("genre weights must add up to 1.0")
        _unit_interval("darkness", self.darkness)
        _unit_interval("psychedelic", self.psychedelic)


@dataclass(frozen=True)
class GrooveSpec:
    swing: float
    syncopation: float
    humanize: float

    def __post_init__(self) -> None:
        _unit_interval("swing", self.swing)
        _unit_interval("syncopation", self.syncopation)
        _unit_interval("humanize", self.humanize)


CORE_TRACKS = ("bass", "drums", "chords")
"""The parts every song has. A SongSpec that names no instruments writes these."""

EXTRA_TRACKS = ("synth", "arp", "vocoder")
"""Parts written only when the brief asks for them.

They are opt-in rather than default-on because ``SectionSpec.plays`` treats an
unset ``active_tracks`` as "everything plays": switching these on by default
would make every SongSpec ever written suddenly grow three parts.
"""

TRACK_NAMES = CORE_TRACKS + EXTRA_TRACKS
DENSITY_FIELDS = {
    "bass": "bass_density",
    "drums": "drum_density",
    "chords": "chord_density",
}


def _section_from_dict(data: dict[str, Any]) -> SectionSpec:
    fields = dict(data)
    tracks = fields.get("active_tracks")
    if tracks is not None:
        fields["active_tracks"] = tuple(tracks)
    return SectionSpec(**fields)


@dataclass(frozen=True)
class SectionSpec:
    name: str
    start_bar: int
    length_bars: int
    energy: float
    minimal: bool
    psychedelic: float
    # Arrangement-engine detail. Every one of these is optional and falls back to
    # the section energy, and `to_dict` omits whatever is unset, so a SongSpec
    # written before the engine existed still serializes to identical bytes --
    # and therefore keeps the SHA-256 that repaint plans are pinned to.
    bass_density: float | None = None
    drum_density: float | None = None
    chord_density: float | None = None
    fx_amount: float | None = None
    vocal_probability: float | None = None
    mutation: float | None = None
    active_tracks: tuple[str, ...] | None = None

    _OPTIONAL_FIELDS = (
        "bass_density",
        "drum_density",
        "chord_density",
        "fx_amount",
        "vocal_probability",
        "mutation",
        "active_tracks",
    )

    def __post_init__(self) -> None:
        if self.start_bar < 0 or self.length_bars <= 0:
            raise ValueError("section bars must be non-negative and non-empty")
        _unit_interval("section energy", self.energy)
        _unit_interval("section psychedelic", self.psychedelic)
        for field_name in (
            "bass_density",
            "drum_density",
            "chord_density",
            "fx_amount",
            "vocal_probability",
            "mutation",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _unit_interval(f"section {field_name}", float(value))
        if self.active_tracks is not None:
            if not self.active_tracks:
                raise ValueError("section active_tracks must not be empty")
            unknown = set(self.active_tracks) - set(TRACK_NAMES)
            if unknown:
                raise ValueError(f"unknown section track name: {sorted(unknown)}")

    def density(self, track: str) -> float:
        """How busy ``track`` is here, defaulting to the section energy."""

        if track not in TRACK_NAMES:
            raise ValueError(f"unknown track name: {track!r}")
        field_name = DENSITY_FIELDS.get(track)
        value = None if field_name is None else getattr(self, field_name)
        return self.energy if value is None else float(value)

    def plays(self, track: str) -> bool:
        """Whether ``track`` sounds in this section; everything plays by default."""

        if track not in TRACK_NAMES:
            raise ValueError(f"unknown track name: {track!r}")
        return self.active_tracks is None or track in self.active_tracks

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for field_name in self._OPTIONAL_FIELDS:
            if payload[field_name] is None:
                payload.pop(field_name)
        if isinstance(payload.get("active_tracks"), tuple):
            payload["active_tracks"] = list(payload["active_tracks"])
        return payload


@dataclass(frozen=True)
class HarmonySpec:
    progression: tuple[str, ...]
    harmonic_rhythm_bars: int

    def __post_init__(self) -> None:
        if not self.progression:
            raise ValueError("harmony progression must not be empty")
        if self.harmonic_rhythm_bars <= 0:
            raise ValueError("harmonic_rhythm_bars must be positive")


@dataclass(frozen=True)
class BassSpec:
    role: str
    technique: str
    syncopation: float
    mutation: float
    octave_jump_probability: float
    ghost_note_probability: float

    def __post_init__(self) -> None:
        for field_name in (
            "syncopation",
            "mutation",
            "octave_jump_probability",
            "ghost_note_probability",
        ):
            _unit_interval(field_name, float(getattr(self, field_name)))


@dataclass(frozen=True)
class DrumSpec:
    pattern: str
    kick_density: float
    hat_density: float
    dub_space: float

    def __post_init__(self) -> None:
        _unit_interval("kick_density", self.kick_density)
        _unit_interval("hat_density", self.hat_density)
        _unit_interval("dub_space", self.dub_space)


@dataclass(frozen=True)
class ChordSpec:
    instrument: str
    articulation: str
    dub_delay: float

    def __post_init__(self) -> None:
        _unit_interval("dub_delay", self.dub_delay)


@dataclass(frozen=True)
class VocalSpec:
    enabled: bool
    vocoder: bool
    character: str


@dataclass(frozen=True)
class SongSpec:
    spec_version: str
    source_prompt: str
    seed: int
    song: SongIdentity
    style: StyleSpec
    groove: GrooveSpec
    arrangement: tuple[SectionSpec, ...]
    harmony: HarmonySpec
    bass: BassSpec
    drums: DrumSpec
    chords: ChordSpec
    vocal: VocalSpec
    # Which parts this song is made of. ``None`` means the core three, and
    # ``to_dict`` omits the field entirely in that case, so every SongSpec
    # written before instruments existed still serializes to identical bytes --
    # and keeps the SHA-256 that repaint plans are pinned to.
    instruments: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.spec_version != "0.1":
            raise ValueError("this prototype supports SongSpec version 0.1")
        if not self.source_prompt.strip():
            raise ValueError("source_prompt must not be empty")
        if not self.arrangement:
            raise ValueError("arrangement must not be empty")
        cursor = 0
        for section in self.arrangement:
            if section.start_bar != cursor:
                raise ValueError("arrangement sections must be contiguous and ordered")
            cursor += section.length_bars
        if cursor != self.song.total_bars:
            raise ValueError("arrangement length must equal song.total_bars")
        if self.instruments is not None:
            unknown = set(self.instruments) - set(TRACK_NAMES)
            if unknown:
                raise ValueError(f"unknown instrument: {sorted(unknown)}")
            missing = set(CORE_TRACKS) - set(self.instruments)
            if missing:
                raise ValueError(
                    f"instruments must include the core parts; missing {sorted(missing)}"
                )

    def parts(self) -> tuple[str, ...]:
        """The parts to compose, in a stable order."""

        if self.instruments is None:
            return CORE_TRACKS
        return tuple(name for name in TRACK_NAMES if name in self.instruments)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["arrangement"] = [section.to_dict() for section in self.arrangement]
        if self.instruments is None:
            del payload["instruments"]
        else:
            payload["instruments"] = list(self.instruments)
        return payload

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent) + "\n"

    def write_json(self, path: Path) -> None:
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SongSpec:
        style_data = data["style"]
        return cls(
            spec_version=str(data["spec_version"]),
            source_prompt=str(data["source_prompt"]),
            seed=int(data["seed"]),
            song=SongIdentity(**data["song"]),
            style=StyleSpec(
                genres=tuple(GenreWeight(**item) for item in style_data["genres"]),
                darkness=float(style_data["darkness"]),
                psychedelic=float(style_data["psychedelic"]),
            ),
            groove=GrooveSpec(**data["groove"]),
            arrangement=tuple(_section_from_dict(item) for item in data["arrangement"]),
            harmony=HarmonySpec(
                progression=tuple(data["harmony"]["progression"]),
                harmonic_rhythm_bars=int(data["harmony"]["harmonic_rhythm_bars"]),
            ),
            bass=BassSpec(**data["bass"]),
            drums=DrumSpec(**data["drums"]),
            chords=ChordSpec(**data["chords"]),
            vocal=VocalSpec(**data["vocal"]),
            instruments=(
                tuple(data["instruments"]) if data.get("instruments") is not None else None
            ),
        )

    @classmethod
    def from_json(cls, payload: str) -> SongSpec:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("SongSpec JSON root must be an object")
        return cls.from_dict(data)

