from __future__ import annotations

import re
from typing import Sequence

from .arrangement import build_arrangement
from .genres import match_genres, mood_axes, typical_bpm
from .models import (
    CORE_TRACKS,
    EXTRA_TRACKS,
    BassSpec,
    ChordSpec,
    DrumSpec,
    GenreWeight,
    GrooveSpec,
    HarmonySpec,
    SectionSpec,
    SongIdentity,
    SongSpec,
    StyleSpec,
    VocalSpec,
)
from .theory import parse_key, progression_for_key

_BPM_RE = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*BPM", re.IGNORECASE)
_MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*分")


class MusicBrain:
    """Deterministic v0.1 interpreter from a music brief to SongSpec."""

    def __init__(self, *, seed: int = 8) -> None:
        self.seed = seed

    def analyze(self, prompt: str) -> SongSpec:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")

        key, tonic, tonic_pc, mode = parse_key(prompt, default="C minor")
        genres = self._parse_genres(prompt)
        weighted = [(item.name, item.weight) for item in genres]
        bpm = self._parse_bpm(prompt, weighted)
        total_bars = self._total_bars(prompt, bpm)
        duration = total_bars * 4 * 60 / bpm
        lower = prompt.lower()
        psychedelic_requested = "サイケ" in prompt or "psychedelic" in lower
        minimal_requested = "ミニマル" in prompt or "minimal" in lower
        slap_requested = "スラップ" in prompt or "slap" in lower
        vocoder_requested = "vocoder" in lower or "ボコーダー" in prompt
        mutation_requested = "mutation" in lower or "変態" in prompt
        dub_requested = any(item.name == "dub" for item in genres)
        db_darkness, db_psychedelic = mood_axes(weighted)
        instruments = self._instruments(prompt, lower, vocoder_requested)

        sections = self._sections(
            total_bars,
            minimal_requested=minimal_requested,
            psychedelic_requested=psychedelic_requested,
            parts=instruments or CORE_TRACKS,
        )
        progression = progression_for_key(tonic_pc, mode, prefer_flats="b" in tonic)

        return SongSpec(
            spec_version="0.1",
            source_prompt=prompt,
            seed=self.seed,
            song=SongIdentity(
                title="Mutation Signal" if mutation_requested else "KIHACHI Sketch",
                bpm=bpm,
                key=key,
                tonic=tonic,
                tonic_pitch_class=tonic_pc,
                mode=mode,
                time_signature="4/4",
                total_bars=total_bars,
                target_duration_sec=round(duration, 3),
            ),
            style=StyleSpec(
                genres=genres,
                # Prompt evidence first, then the genre's own mood tags, then the
                # old constants. The constants were the same two numbers for
                # every unrecognised style; the tags at least distinguish a
                # nocturnal one from a sunny one.
                darkness=0.72 if dub_requested else (db_darkness or 0.48),
                psychedelic=(
                    0.82 if psychedelic_requested else (db_psychedelic or 0.28)
                ),
            ),
            groove=GrooveSpec(
                swing=0.54 if any(item.name == "mutation_funk" for item in genres) else 0.5,
                syncopation=0.82 if slap_requested else 0.58,
                humanize=0.18,
            ),
            arrangement=sections,
            harmony=HarmonySpec(progression=progression, harmonic_rhythm_bars=1),
            bass=BassSpec(
                role="dominant",
                technique="slap" if slap_requested else "fingered",
                syncopation=0.86 if slap_requested else 0.58,
                mutation=0.78 if mutation_requested else 0.35,
                octave_jump_probability=0.45 if slap_requested else 0.18,
                ghost_note_probability=0.34 if slap_requested else 0.12,
            ),
            drums=DrumSpec(
                pattern="syncopated_tech_house" if "tech_house" in {item.name for item in genres} else "four_on_floor",
                kick_density=0.72,
                hat_density=0.78,
                dub_space=0.62 if dub_requested else 0.2,
            ),
            chords=ChordSpec(
                instrument="dub_chord_stab" if dub_requested else "synth_chord",
                articulation="short_offbeat_stabs",
                dub_delay=0.74 if dub_requested else 0.18,
            ),
            vocal=VocalSpec(
                enabled=vocoder_requested,
                vocoder=vocoder_requested,
                character="dark robotic phrases" if vocoder_requested else "none",
            ),
            instruments=instruments,
        )

    @staticmethod
    def _instruments(prompt: str, lower: str, vocoder_requested: bool) -> tuple[str, ...] | None:
        """Which parts the brief asks for, beyond the core three.

        Returns ``None`` when it asks for nothing extra, so a plain brief still
        produces a SongSpec that serializes exactly as it did before these parts
        existed -- and keeps the SHA-256 repaint plans are pinned to.
        """

        extra: list[str] = []
        if any(word in lower for word in ("sub bass", "sub-bass", "subbass", "808")) or any(
            word in prompt for word in ("サブベース", "サブ・ベース")
        ):
            extra.append("sub")
        if any(word in lower for word in ("synth", "stab", "lead")) or any(
            word in prompt for word in ("シンセ", "スタブ", "リード")
        ):
            extra.append("synth")
        if any(word in lower for word in ("arp", "sequence", "sequencer")) or any(
            word in prompt for word in ("アルペジ", "シーケンス")
        ):
            extra.append("arp")
        if vocoder_requested:
            extra.append("vocoder")
        if not extra:
            return None
        return CORE_TRACKS + tuple(name for name in EXTRA_TRACKS if name in extra)

    @staticmethod
    def _parse_bpm(prompt: str, weighted: Sequence[tuple[str, float]] = ()) -> float:
        """The prompt's tempo, else the genre's typical one, else 120.

        A stated tempo always wins. The flat 120 that used to follow it was the
        same answer for drum & bass and for dub, which the database can now
        separate -- but only where its range is narrow enough to mean anything,
        so most genres still land on 120 rather than on a fabricated number.
        """
        match = _BPM_RE.search(prompt)
        if match:
            return float(match.group(1))
        return typical_bpm(weighted) or 120.0

    #: How many genres one prompt may carry. A long prompt can mention a style
    #: in passing ("less housey than trance"), and every extra genre dilutes the
    #: weights of the ones that were actually asked for.
    MAX_GENRES = 4

    @staticmethod
    def _parse_genres(prompt: str) -> tuple[GenreWeight, ...]:
        """Genres named in the prompt, in prompt order, weighted.

        Recognition comes from the shipped genre database (1020 names plus
        aliases) rather than the three hand-written rules this used to hold.
        Those three collapsed everything else to ``electronic``, which then
        became ``edm`` at the AbletonGPT boundary -- bossa nova included.

        The database slugs the original three to exactly their old names
        (``Tech House`` -> ``tech_house``), so the swing, drum-pattern, dub-send
        and lyric-vocabulary decisions keyed on those names are untouched, and
        the seed prompt still yields the same 0.4/0.3/0.3 split.
        """
        found: list[str] = []
        for match in match_genres(prompt):
            if match.genre.slug not in found:
                found.append(match.genre.slug)
            if len(found) >= MusicBrain.MAX_GENRES:
                break
        if not found:
            # Still ``electronic`` rather than nothing: downstream expects at
            # least one genre, and an unrecognised prompt is not evidence of a
            # specific style.
            found.append("electronic")
        if found == ["mutation_funk", "dub", "tech_house"]:
            weights = (0.4, 0.3, 0.3)
        else:
            weight = round(1.0 / len(found), 6)
            weights = tuple(weight for _ in found)
            weights = (*weights[:-1], round(1.0 - sum(weights[:-1]), 6))
        return tuple(GenreWeight(name=name, weight=weight) for name, weight in zip(found, weights))

    @staticmethod
    def _total_bars(prompt: str, bpm: float) -> int:
        match = _MINUTES_RE.search(prompt)
        if match is None:
            return 32
        requested_seconds = float(match.group(1)) * 60
        raw_bars = requested_seconds * bpm / 240
        return max(8, int(round(raw_bars / 8)) * 8)

    @staticmethod
    def _sections(
        total_bars: int,
        *,
        minimal_requested: bool,
        psychedelic_requested: bool,
        parts: Sequence[str],
    ) -> tuple[SectionSpec, ...]:
        return build_arrangement(
            total_bars,
            minimal_requested=minimal_requested,
            psychedelic_requested=psychedelic_requested,
            parts=parts,
        )
