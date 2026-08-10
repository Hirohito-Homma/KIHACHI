from __future__ import annotations

import re
from typing import Sequence

from .arrangement import build_arrangement
from .derive import pick, pick_int, pick_str, profile_for
from .genres import match_genres, mood_axes, typical_bpm
from .intent import Traits, blend, read as read_intent
from .preferences import EMPTY as NO_PREFERENCES, Preferences, clamp
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

    def __init__(self, *, seed: int = 8, preferences: Preferences | None = None) -> None:
        self.seed = seed
        # Absent by default, and an absent set of priors offsets nothing, so a
        # MusicBrain built the old way produces byte-identical output.
        self.preferences = preferences or NO_PREFERENCES

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
        # What the brief actually asks for, refusals and degrees included. Each
        # ``strength`` is 0.0 when unmentioned or refused, 1.0 when plainly
        # stated -- and 1.0 blends to exactly the constant this used to
        # hardcode, so a brief that hedges nothing produces the song it always
        # produced.
        traits = read_intent(prompt)
        psychedelic = traits.strength_of("psychedelic")
        minimal_requested = traits.asked_for("minimal")
        slap = traits.strength_of("slap")
        slap_requested = slap > 0
        vocoder_requested = traits.asked_for("vocoder")
        mutation = traits.strength_of("mutation")
        mutation_requested = mutation > 0
        # Dub is a genre, so the genre database decides it, not the wording --
        # but a brief that refuses dub outright still overrules the match.
        dub_requested = any(item.name == "dub" for item in genres) and not traits.refused("dub")
        dub = 1.0 if dub_requested else 0.0
        db_darkness, db_psychedelic = mood_axes(weighted)
        # The dominant genre's family, where the shipped table has one for it.
        # Everything it declines to answer keeps the constant used below.
        profile = profile_for(weighted)
        instruments = self._instruments(traits, vocoder_requested)
        # Learned offsets, if any were supplied. ``tune`` is the identity when
        # the preferences are empty, which is the default.
        slugs = [item.name for item in genres]

        def tune(path: str, value: float) -> float:
            return clamp(value + self.preferences.offset_for(slugs, "song", path))

        sections = self._sections(
            total_bars,
            minimal_requested=minimal_requested,
            psychedelic_requested=psychedelic > 0,
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
                darkness=blend(db_darkness or 0.48, 0.72, dub),
                psychedelic=blend(db_psychedelic or 0.28, 0.82, psychedelic),
            ),
            groove=GrooveSpec(
                swing=0.54 if any(item.name == "mutation_funk" for item in genres) else 0.5,
                syncopation=tune("groove.syncopation", blend(0.58, 0.82, slap)),
                humanize=pick(profile.humanize, 0.18),
            ),
            arrangement=sections,
            harmony=HarmonySpec(
                progression=progression,
                harmonic_rhythm_bars=pick_int(profile.harmonic_rhythm_bars, 1),
            ),
            bass=BassSpec(
                role=pick_str(profile.bass_role, "dominant"),
                technique="slap" if slap_requested else "fingered",
                syncopation=tune("bass.syncopation", blend(0.58, 0.86, slap)),
                mutation=tune("bass.mutation", blend(0.35, 0.78, mutation)),
                octave_jump_probability=tune(
                    "bass.octave_jump_probability", blend(0.18, 0.45, slap)
                ),
                ghost_note_probability=tune(
                    "bass.ghost_note_probability", blend(0.12, 0.34, slap)
                ),
            ),
            drums=DrumSpec(
                # Tech house keeps its own name wherever it appears, not only
                # when it leads: the string is pinned and the family table is
                # coarser than the one genre that already had an answer.
                pattern=(
                    "syncopated_tech_house"
                    if "tech_house" in {item.name for item in genres}
                    else pick_str(profile.drum_pattern, "four_on_floor")
                ),
                kick_density=pick(profile.kick_density, 0.72),
                # hat_density is left alone on purpose. composer.py thresholds
                # it at 0.3 to choose 8th or 16th hats, so every value above
                # that produces identical MIDI and only the prompt wording
                # moves. Varying it here would look like control and not be
                # any. Making the composer read it continuously is its own
                # change.
                hat_density=0.78,
                dub_space=tune("drums.dub_space", blend(0.2, 0.62, dub)),
            ),
            chords=ChordSpec(
                instrument="dub_chord_stab" if dub_requested else "synth_chord",
                articulation=pick_str(profile.articulation, "short_offbeat_stabs"),
                dub_delay=tune("chords.dub_delay", blend(0.18, 0.74, dub)),
            ),
            vocal=VocalSpec(
                enabled=vocoder_requested,
                vocoder=vocoder_requested,
                character="dark robotic phrases" if vocoder_requested else "none",
            ),
            instruments=instruments,
            # Only when priors actually took part. Empty preferences leave the
            # field out, and the SongSpec bytes unchanged.
            preferences_fingerprint=self.preferences.fingerprint or None,
        )

    @staticmethod
    def _instruments(traits: Traits, vocoder_requested: bool) -> tuple[str, ...] | None:
        """Which parts the brief asks for, beyond the core three.

        Returns ``None`` when it asks for nothing extra, so a plain brief still
        produces a SongSpec that serializes exactly as it did before these parts
        existed -- and keeps the SHA-256 repaint plans are pinned to.

        A part is either written or it is not, so degree does not apply here --
        but refusal does. ``"アルペジオは無しで"`` used to add the arp track.
        """

        extra = [name for name in ("sub", "synth", "arp") if traits.asked_for(name)]
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
