from __future__ import annotations

import re
from dataclasses import replace
from typing import Sequence

from .arrangement import build_arrangement
from .derive import pick, pick_int, pick_str, profile_for
from .genres import match_genres, mood_axes, typical_bpm
from .intent import (
    FIRST_HALF,
    LARGE_STRENGTH,
    SECOND_HALF,
    TRAIT_WORDS,
    Traits,
    blend,
    read as read_intent,
)
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
from .theory import DEFAULT_PROGRESSION, beats_per_bar, parse_key, progression_for_key

_BPM_RE = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*BPM", re.IGNORECASE)
_MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*分")
_TIME_SIGNATURE_RE = re.compile(r"(?<!\d)([2-9]|1[0-2])\s*/\s*(2|4|8)(?!\d)")
_BEATS_RE = re.compile(r"([2-9])\s*拍子")
#: Words that name a meter outright. Only the unambiguous ones: "shuffle" and
#: "swing" imply a feel rather than a signature, and the swing field already
#: carries that.
_METER_WORDS = {
    "ワルツ": "3/4",
    "waltz": "3/4",
}

#: How far a stated preference can carry a value the genre already chose. Not
#: the field's own 0 and 1: a brief saying 「暗い」 is describing this song, not
#: asking for the darkest one expressible, and the extremes are where the prompt
#: compiler's bands stop distinguishing anything.
_DARK_POLE = 0.9
_BRIGHT_POLE = 0.1

#: 0.5 is straight and 0.667 is triplet swing, so these are not symmetrical
#: around anything: the whole usable range sits above the straight end, and
#: `prompt_compiler` calls everything at or below 0.52 straight.
_SWUNG_POLE = 0.66
_STRAIGHT_POLE = 0.5

#: `prompt_compiler` splits syncopation into four bands at 0.25/0.5/0.75, and
#: these sit one step inside the outer two: far enough to change the band a
#: brief lands in, not so far that every stated preference reads as the extreme.
_SYNCOPATED_POLE = 0.88
_ON_GRID_POLE = 0.2

#: Every family states a humanize between Hardcore Electronic's 0.04 and Jazz's
#: 0.45, so the loose pole sits above all of them -- otherwise a brief could
#: agree with Jazz and move nothing. 0.7 is +/-6.6 ms of jitter at 110 BPM
#: against the 0.18 default's +/-1.7 ms, and it reaches `prompt_compiler`'s top
#: band. The tight pole is not 0.0: that is a quantiser, not a preference.
_LOOSE_POLE = 0.7
_TIGHT_POLE = 0.02

#: Kick density runs 0.38 (Reggae) to 0.9 (Hardcore Electronic) across the
#: families and hats 0.45 to 0.95, so both poles sit outside every family's
#: value. `composer` multiplies these by the section's own density, so the
#: sparse pole is not 0.0: silence is a section that does not play drums, which
#: the arrangement already decides.
_BUSY_POLE = 0.95
_SPARSE_POLE = 0.3

#: A plain statement moves two thirds of the way to the pole, insistence all of
#: it, a hedge one third. Chosen so a plainly dark brief lands near 0.76 from
#: the 0.48 default -- next to the 0.72 that plainly-stated `dub` has always
#: produced, rather than somewhere new.
_STATED_REACH = {0.5: 1 / 3, 1.0: 2 / 3, 1.5: 1.0}


def _stated_axis(
    base: float,
    traits: Traits,
    *,
    up: str,
    up_pole: float,
    down: str,
    down_pole: float,
    ceiling: float = 1.0,
) -> float:
    """Let the brief move a value the genre chose, from wherever the genre put it.

    Every ordinary trait blends from a fixed refused pole, because the pole is
    the constant the code used before the trait existed. These axes have no such
    constant -- the value is whatever the genre said -- so this moves *from* it
    and leaves it untouched when the brief says nothing.

    The two directions are separate traits on purpose. `strength_of` reports a
    refusal as 0.0, so 「暗くない」 arrives as no statement at all, which is
    right: refusing one direction does not request the other, and the genre's
    own reading beats both poles.
    """

    raised = traits.strength_of(up)
    lowered = traits.strength_of(down)
    value = base
    # The poles are limits, not targets. Techno's own darkness is 1.0, and
    # reading the pole as a destination made 「暗いテクノ」 *less* dark than
    # 「テクノ」 -- agreeing with the brief moved the value backwards. A
    # statement the genre already satisfies leaves it alone.
    if raised and value < up_pole:
        value = value + (up_pole - value) * _STATED_REACH[raised]
    if lowered and value > down_pole:
        value = value + (down_pole - value) * _STATED_REACH[lowered]
    # Every axis but note length is a unit interval, and `clamp` is the shared
    # one. A ceiling above 1.0 is not a looser rule -- `models` still refuses
    # anything outside the field's own bounds.
    return round(min(max(value, 0.0), ceiling), 6)


def _stated_darkness(base: float, traits: Traits) -> float:
    return _stated_axis(
        base, traits, up="dark", up_pole=_DARK_POLE, down="bright", down_pole=_BRIGHT_POLE
    )


#: Surface forms that state a feel outright, so a genre they also name cannot
#: be read as evidence for that feel. Only the two rhythm axes the database's
#: groove column reaches; a word like 「ダブ」 names an effect and a genre and is
#: not this shape.
_FEEL_WORDS = frozenset(TRAIT_WORDS["swung"]) | frozenset(TRAIT_WORDS["straight"])


def _stated_swing(base: float, traits: Traits) -> float:
    """Whether this song swings, which until now only one genre could say.

    `groove.swing` reaches the composer's own timing, so it is one of the few
    style numbers that survives into Live as MIDI rather than as prompt text.
    It was set by `mutation_funk` alone -- every family including Jazz left it
    at 0.5 -- and no word in a brief touched it, so 「シャッフルで」 and
    「ジャズ」 both composed straight eighths. The comment on `_METER_WORDS`
    declines to read `swing` as a time signature on the grounds that this field
    already carries the feel; that is only true as of now.
    """

    return _stated_axis(
        base,
        traits,
        up="swung",
        up_pole=_SWUNG_POLE,
        down="straight",
        down_pole=_STRAIGHT_POLE,
    )


#: The values the 23 families actually use: 13 sit at 2 bars per chord, 8 at 1
#: and 2 at 4. A brief moves along this ladder rather than between two poles,
#: because half a bar per chord is not a thing the composers can play -- every
#: one of them indexes the progression with `bar // harmonic_rhythm_bars`.
_HARMONIC_RHYTHM_LADDER = (1, 2, 4)


def _stated_harmonic_rhythm(base: int, traits: Traits) -> int:
    """How long one chord lasts, which every family states and no brief could.

    The first stated field here that is not a float, and the difference matters:
    a hedge and a plain statement both move **one rung**, because there is no
    value between two rungs to land on. Only insistence goes to the end of the
    ladder. Refusing a direction moves nothing, as everywhere else.
    """

    faster = traits.strength_of("fast_changes")
    slower = traits.strength_of("slow_changes")
    if not faster and not slower:
        return base
    ladder = _HARMONIC_RHYTHM_LADDER
    index = min(range(len(ladder)), key=lambda i: abs(ladder[i] - base))
    if faster:
        index = 0 if faster == LARGE_STRENGTH else max(0, index - 1)
    if slower:
        index = len(ladder) - 1 if slower == LARGE_STRENGTH else min(len(ladder) - 1, index + 1)
    return ladder[index]


#: A staccato that still sounds and a legato that still articulates. Not 0.25
#: and 2.0, the field's own bounds: those are where `models` stops accepting a
#: value, not where a brief means to land.
_STACCATO_POLE = 0.45
_LEGATO_POLE = 1.6


#: How far the sections are pushed apart, or pulled together. A plain statement
#: half again the spread the archetypes chose; insistence doubles it; the flat
#: end can reach a genuinely level song, because 「淡々と」 is a thing people
#: mean literally.
_CONTRAST_SPREAD = {0.5: 1.2, 1.0: 1.5, 1.5: 2.0}
_FLAT_SPREAD = {0.5: 0.7, 1.0: 0.4, 1.5: 0.0}

#: The section numbers that say how *much* is happening. `psychedelic`,
#: `mutation`, `fx_amount` and `vocal_probability` say what *kind*, and pushing
#: those apart would be a different request than the one 「メリハリ」 makes.
_SECTION_LEVELS = ("energy", "bass_density", "drum_density", "chord_density")


def _scoped_sections(
    sections: tuple[SectionSpec, ...], traits: Traits
) -> tuple[SectionSpec, ...]:
    """Apply what the brief said about one half of the song, to that half.

    The split is `len(arrangement) // 2`, which is `edit`'s split -- the two
    commands now share the words, so they had better share the span they mean.

    Only the per-section densities move. A scoped `busy` cannot raise
    `drums.kick_density`, because that number is the kit for the whole song and
    raising it in the second half would raise it in the first.
    """

    midpoint = len(sections) // 2
    for scope, indexes in (
        (FIRST_HALF, range(0, midpoint)),
        (SECOND_HALF, range(midpoint, len(sections))),
    ):
        here = traits.within(scope)
        if not here.traits:
            continue
        moved = list(sections)
        for index in indexes:
            section = sections[index]
            changes = {}
            for field in ("bass_density", "drum_density", "chord_density"):
                base = getattr(section, field)
                if base is None:
                    continue
                changes[field] = _stated_density(base, here)
            changes["energy"] = _stated_density(section.energy, here)
            moved[index] = replace(section, **changes)
        spread = _stated_contrast(tuple(moved[index] for index in indexes), here)
        for offset, index in enumerate(indexes):
            moved[index] = spread[offset]
        sections = tuple(moved)
    return sections


def _stated_contrast(
    sections: tuple[SectionSpec, ...], traits: Traits
) -> tuple[SectionSpec, ...]:
    """Push the sections apart, or pull them together, around their own average.

    The first thing a brief can state here that is not a value inside one
    section: it is the *relation* between them. The archetypes in
    `arrangement.py` already choose an energy and three densities per section,
    and no word reached any of it, so 「メリハリのある」 and 「淡々とした」 built
    the same four sections.

    Deliberately a scale around the mean rather than a new field. The shape the
    archetypes chose is the song's own; this says how far to commit to it, and
    a factor of 1.0 leaves every number exactly where it was.
    """

    stated = traits.strength_of("contrast")
    level = traits.strength_of("flat")
    if stated:
        factor = _CONTRAST_SPREAD[stated]
    elif level:
        factor = _FLAT_SPREAD[level]
    else:
        return sections
    if len(sections) < 2:
        return sections
    means = {
        name: sum(getattr(section, name) or 0.0 for section in sections) / len(sections)
        for name in _SECTION_LEVELS
    }
    spread: list[SectionSpec] = []
    for section in sections:
        changes = {}
        for name in _SECTION_LEVELS:
            value = getattr(section, name)
            if value is None:
                # An unset density already means "follow the energy", and it
                # keeps following the energy that was just moved.
                continue
            changes[name] = round(clamp(means[name] + (value - means[name]) * factor), 6)
        spread.append(replace(section, **changes))
    return tuple(spread)


def _stated_note_length(traits: Traits) -> float:
    """How long each note is held -- the one trait here with no number waiting.

    Every other stated field already existed with consumers and no path from
    the brief. Note length had no field: each part carried a duration constant
    written into `composer` (bass 0.3, kick 0.16, synth 0.18), so 「歯切れよく」
    had nothing to set. 1.0 is exactly those constants, which is why adding the
    field changes no existing song.
    """

    return _stated_axis(
        1.0,
        traits,
        up="legato",
        up_pole=_LEGATO_POLE,
        down="staccato",
        down_pole=_STACCATO_POLE,
        ceiling=2.0,
    )


def _stated_density(base: float, traits: Traits) -> float:
    """How many drum notes exist, which only the arrangement could influence.

    `drums.kick_density` and `drums.hat_density` decide the note count itself.
    The nearest word was `minimal`, and it does something else: it gates the
    `minimal` flag on the opening two sections and never reaches a density. So
    a brief could ask for a minimal *opening* and not for a sparse *kit*.
    """

    return _stated_axis(
        base, traits, up="busy", up_pole=_BUSY_POLE, down="sparse", down_pole=_SPARSE_POLE
    )


def _stated_humanize(base: float, traits: Traits) -> float:
    """How hand-played the timing is, which every genre states and no brief could.

    The mirror of `_stated_syncopation`: all 23 families set `groove.humanize`
    and none set syncopation, yet neither was reachable from a brief. It feeds
    the composer's jitter directly, so 「手弾きっぽく」 changes the MIDI rather
    than the prompt, and `midi_review` reads the result back out.
    """

    return _stated_axis(
        base, traits, up="loose", up_pole=_LOOSE_POLE, down="tight", down_pole=_TIGHT_POLE
    )


def _stated_syncopation(base: float, traits: Traits) -> float:
    """Whether this song pushes off the grid, which no genre could ever say.

    `groove.syncopation` reaches the notes twice -- it scales the mutation amount
    and the drum placement -- and its `bass.syncopation` twin phrases the bass
    line, so this is MIDI rather than prompt text. `derive.Profile` has no field
    for it, so all 1021 genres leave it at the constant, and the only thing that
    ever moved it was `slap`. `edit.py` could already change it after a render;
    now the brief can ask for it before one.
    """

    return _stated_axis(
        base,
        traits,
        up="syncopated",
        up_pole=_SYNCOPATED_POLE,
        down="on_grid",
        down_pole=_ON_GRID_POLE,
    )


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
        time_signature = self._parse_time_signature(prompt)
        bar_beats = beats_per_bar(time_signature)
        total_bars = self._total_bars(prompt, bpm, bar_beats)
        duration = total_bars * bar_beats * 60 / bpm
        # What the brief actually asks for, refusals and degrees included. Each
        # ``strength`` is 0.0 when unmentioned or refused, 1.0 when plainly
        # stated -- and 1.0 blends to exactly the constant this used to
        # hardcode, so a brief that hedges nothing produces the song it always
        # produced.
        traits = read_intent(prompt)
        # Everything below this line is a number for the whole song, so it reads
        # only what the brief said about the whole song. 「後半は手数を多く」 must
        # not raise the kit in the first half, and the sections take the rest.
        song_traits = traits.unscoped()
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
        # 「スウィング」 is the `swung` trait word *and* the Swing genre's alias,
        # and since the database's groove column reached `swing` (PR #79) the
        # same word was being counted twice: once as the feel the brief asked
        # for, and once as a jazz genre whose own feel then overrode it.
        # 「スウィングしないテクノ」 came out swung, and 「かなりスウィングさせて」
        # composed exactly what plain 「スウィングさせて」 did, because the genre
        # sat above whatever the degree word asked for.
        #
        # A word already read as a feel is not also evidence for the feel of a
        # genre it happens to name. The genre stays -- it is still a real
        # match, and `dub` depends on that -- it just does not get to answer
        # the question its own name asked.
        spoken_feel = {
            match.genre.slug
            for match in match_genres(prompt)
            if match.matched in _FEEL_WORDS
        }
        feel_profile = (
            profile_for([item for item in weighted if item[0] not in spoken_feel])
            if spoken_feel
            else profile
        )
        instruments = self._instruments(traits, vocoder_requested)
        # Learned offsets, if any were supplied. ``tune`` is the identity when
        # the preferences are empty, which is the default.
        slugs = [item.name for item in genres]

        def tune(path: str, value: float) -> float:
            return clamp(value + self.preferences.offset_for(slugs, "song", path))

        sections = _scoped_sections(
            _stated_contrast(
                self._sections(
                    total_bars,
                    minimal_requested=minimal_requested,
                    psychedelic_requested=psychedelic > 0,
                    parts=instruments or CORE_TRACKS,
                ),
                traits.unscoped(),
            ),
            traits,
        )
        progression = progression_for_key(
            tonic_pc,
            mode,
            prefer_flats="b" in tonic,
            # The family's own harmony. Until this was passed, every genre in
            # the database played the same four triads: a jazz brief got the
            # comping and the swung ride and then comped i-VI-III-VII.
            shape=pick_str(profile.progression, DEFAULT_PROGRESSION),
        )

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
                time_signature=time_signature,
                total_bars=total_bars,
                target_duration_sec=round(duration, 3),
            ),
            style=StyleSpec(
                genres=genres,
                # Prompt evidence first, then the genre's own mood tags, then the
                # old constants. The constants were the same two numbers for
                # every unrecognised style; the tags at least distinguish a
                # nocturnal one from a sunny one.
                #
                # `is None`, not `or`: `mood_axes` returns None for "the tags
                # said nothing" and a number for "the tags said this", and 0.0
                # is a number. Written with `or`, every genre whose tags were
                # entirely bright -- 253 rows -- read as the neutral default
                # instead of as bright.
                darkness=_stated_darkness(
                    blend(0.48 if db_darkness is None else db_darkness, 0.72, dub),
                    song_traits,
                ),
                psychedelic=blend(
                    0.28 if db_psychedelic is None else db_psychedelic, 0.82, psychedelic
                ),
            ),
            groove=GrooveSpec(
                swing=_stated_swing(pick(feel_profile.swing, 0.5), song_traits),
                syncopation=tune(
                    "groove.syncopation",
                    _stated_syncopation(blend(0.58, 0.82, slap), song_traits),
                ),
                humanize=_stated_humanize(pick(profile.humanize, 0.18), song_traits),
                note_length=_stated_note_length(song_traits),
            ),
            arrangement=sections,
            harmony=HarmonySpec(
                progression=progression,
                harmonic_rhythm_bars=_stated_harmonic_rhythm(
                    pick_int(profile.harmonic_rhythm_bars, 1), song_traits
                ),
            ),
            bass=BassSpec(
                role=pick_str(profile.bass_role, "dominant"),
                technique="slap" if slap_requested else "fingered",
                syncopation=tune(
                    "bass.syncopation",
                    _stated_syncopation(blend(0.58, 0.86, slap), song_traits),
                ),
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
                # when it leads. That is now said in ``derive.GENRE_PROFILES``
                # with the rest of the genre numbers, rather than as an ``if``
                # here on one slug.
                pattern=pick_str(profile.drum_pattern, "four_on_floor"),
                kick_density=_stated_density(pick(profile.kick_density, 0.72), song_traits),
                # Left pinned at 0.78 for every genre until the composer
                # stopped thresholding it at 0.3 to pick one of two hat grids.
                # While it was a switch, varying it here would have looked like
                # control without being any; now each step of it removes or
                # restores a hat, so the families may speak.
                hat_density=_stated_density(pick(profile.hat_density, 0.78), song_traits),
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
    def _parse_time_signature(prompt: str) -> str:
        """The meter the brief states, else 4/4.

        Only what the brief says. The genre database has a ``meter`` column,
        but not one of its 1020 rows names a single signature on its own --
        every non-4/4 row is a list ("4/4; 3/4", "variable; often 2/4, 3/4,
        4/4"), and picking one of those would be inventing a fact rather than
        reading one. So the database sits this out, exactly the way it sits out
        tempo when its range is too wide to mean anything.
        """

        match = _TIME_SIGNATURE_RE.search(prompt)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
        match = _BEATS_RE.search(prompt)
        if match:
            # "3拍子" is three quarter notes; the denominator is not stated and
            # 4 is what it means in every ordinary use of the phrase.
            return f"{match.group(1)}/4"
        lowered = prompt.lower()
        for word, signature in _METER_WORDS.items():
            if word in lowered:
                return signature
        return "4/4"

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
    def _total_bars(prompt: str, bpm: float, bar_beats: float = 4.0) -> int:
        match = _MINUTES_RE.search(prompt)
        if match is None:
            return 32
        requested_seconds = float(match.group(1)) * 60
        # A bar is ``bar_beats`` beats, not always four: at 120 BPM a 3/4 bar
        # lasts 1.5 seconds, so asking for five minutes needs a third more bars
        # than the old constant 240 (= 4 beats x 60) would have given.
        raw_bars = requested_seconds * bpm / (60 * bar_beats)
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
