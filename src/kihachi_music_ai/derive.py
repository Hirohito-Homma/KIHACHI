"""Give each genre family its own numbers, instead of one family's for all 1020.

``MusicBrain`` never varied ``drums.kick_density`` (0.72), ``groove.humanize``
(0.18), ``harmonic_rhythm_bars`` (1), ``bass.role``, ``chords.articulation`` or
the drum pattern. Bossa nova and drum & bass came out with the same kick
density and the same machine-tight timing. #6 connected a 1020-genre database
and only two numbers moved through it: tempo and the two mood axes.

The uncomfortable part is what those constants *were*. They are not neutral
defaults -- they were tuned against one brief, the funk/house/dub one in
``example_output``, and then applied to every genre in the database. So the
first row of the table below is not a fudge to keep a test passing: stating
that R&B / Soul / Funk's profile is exactly today's constants is simply saying
out loud whose numbers everyone has been getting.

**What the database cannot tell us.** ``genres.json`` carries names, aliases,
BPM ranges, a meter string, mood tags and a region. It has no density, no
articulation, no harmonic rhythm. Deriving those from ``mood_tags`` would be
inventing a mapping and calling it data. So this is a hand-written table, like
``ableton.LIVE_GENRE_BY_FAMILY``, at family level -- the level the database's
own attributes are actually distinct at (~51 profiles across 1020 rows) -- and
a family with no row here keeps the caller's existing constant rather than
receiving a guess.

Pure and stdlib-only.
"""

from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields, replace
from typing import Sequence

from .composer import swing_for_offbeat
from .genres import family_of, find


@dataclass(frozen=True)
class Profile:
    """One family's numbers. ``None`` means "no opinion", not "zero"."""

    kick_density: float | None = None
    humanize: float | None = None
    harmonic_rhythm_bars: int | None = None
    bass_role: str | None = None
    articulation: str | None = None
    drum_pattern: str | None = None
    swing: float | None = None
    progression: str | None = None
    hat_density: float | None = None

    def overlaid_with(self, other: Profile) -> Profile:
        """``other``'s opinions on top of this one's; silence changes nothing."""

        stated = {
            field.name: getattr(other, field.name)
            for field in dataclass_fields(other)
            if getattr(other, field.name) is not None
        }
        return replace(self, **stated)


#: Only ``supporting`` / ``present`` / ``dominant`` carry weight in the audio
#: prompt (``prompt_compiler._role_weight``); anything else silently reads as
#: 0.5, so the table stays inside that vocabulary.
#:
#: Three families deliberately state no ``progression`` and keep the default
#: i-VI-III-VII: R&B / Soul / Funk because it *is* the default (see above),
#: Reggae / Dub / Ska because that shape is already what a one-drop vamp does,
#: and IDM because the family has no harmonic consensus to state.
FAMILY_PROFILES: dict[str, Profile] = {
    # The incumbent. These are the numbers every genre used to get.
    "R&B / Soul / Funk": Profile(0.72, 0.18, 1, "dominant", "short_offbeat_stabs"),
    "House": Profile(0.78, 0.12, 1, "present", "short_offbeat_stabs", "four_on_floor", progression="minor_seven_vamp", hat_density=0.85),
    "Disco": Profile(0.8, 0.2, 1, "dominant", "muted_upstrokes", "four_on_floor", progression="minor_seven_vamp", hat_density=0.8),
    "Techno": Profile(0.85, 0.06, 2, "supporting", "hypnotic_stabs", "four_on_floor", progression="modal_vamp", hat_density=0.92),
    "Trance": Profile(0.8, 0.08, 2, "supporting", "sustained_chords", "four_on_floor", progression="modal_vamp", hat_density=0.9),
    "EDM / Future Bass": Profile(0.8, 0.1, 2, "supporting", "sustained_chords", "four_on_floor", progression="modal_vamp", hat_density=0.85),
    "Hardcore Electronic": Profile(0.9, 0.04, 2, "supporting", "stab_hits", "four_on_floor", progression="modal_vamp", hat_density=0.95),
    "Reggae / Dub / Ska": Profile(0.38, 0.3, 2, "dominant", "offbeat_skank", "one_drop", hat_density=0.45),
    "Jungle / Drum & Bass": Profile(0.5, 0.1, 4, "dominant", "sparse_stabs", "breakbeat", progression="minor_seven_vamp", hat_density=0.9),
    "Breakbeat / Breaks": Profile(0.6, 0.14, 2, "dominant", "chopped_stabs", "breakbeat", progression="minor_seven_vamp", hat_density=0.85),
    "UK Garage / Bass": Profile(0.6, 0.16, 1, "dominant", "clipped_stabs", "two_step", progression="minor_seven_vamp", hat_density=0.8),
    "Hip-Hop / Rap": Profile(0.55, 0.22, 2, "dominant", "laid_back_stabs", "boom_bap", progression="hip_hop_loop", hat_density=0.6),
    "Ambient / Downtempo": Profile(0.18, 0.35, 4, "supporting", "sustained_pads", "sparse_pulse", progression="modal_vamp", hat_density=0.15),
    "IDM / Experimental Electronic": Profile(0.42, 0.24, 2, "present", "fragmented_stabs", "broken_grid", hat_density=0.7),
    "Jazz": Profile(0.3, 0.45, 1, "present", "comped_chords", "swung_ride", progression="ii_v_i", hat_density=0.85),
    "Blues": Profile(0.35, 0.42, 1, "present", "comped_chords", "shuffle", progression="blues_shuffle", hat_density=0.6),
    "Brazilian": Profile(0.45, 0.38, 1, "present", "syncopated_comping", "samba", progression="bossa", hat_density=0.9),
    "Latin": Profile(0.5, 0.34, 1, "present", "montuno", "clave", progression="montuno_latin", hat_density=0.8),
    "Rock": Profile(0.5, 0.3, 2, "supporting", "sustained_power_chords", "backbeat", progression="one_four_five", hat_density=0.55),
    "Punk / Hardcore": Profile(0.65, 0.28, 2, "supporting", "driving_downstrokes", "backbeat", progression="power_riff", hat_density=0.7),
    "Metal": Profile(0.7, 0.12, 2, "supporting", "palm_muted_chugs", "double_kick", progression="power_riff", hat_density=0.8),
    "Country / Americana": Profile(0.45, 0.3, 2, "present", "strummed_chords", "train_beat", progression="one_four_five", hat_density=0.65),
    "Folk": Profile(0.3, 0.4, 2, "present", "strummed_chords", "sparse_pulse", progression="one_four_five", hat_density=0.3),
}


GENRE_PROFILES: dict[str, Profile] = {
    # Two numbers used to sit in ``MusicBrain.analyze`` as ``if`` statements on
    # these exact slugs. They are facts about a genre, not about the brief, so
    # they belong beside the other genre numbers -- and stating them here is
    # what makes "which genre decides what" a single question with a single
    # place to look. Both are single-genre opinions their family does not hold:
    # not every R&B / Soul / Funk record swings, and tech house's pattern is
    # more specific than House's four-on-the-floor.
    "mutation_funk": Profile(swing=0.54),
    "tech_house": Profile(drum_pattern="syncopated_tech_house"),
}
"""Opinions attached to one genre rather than to its whole family.

Kept deliberately short. A per-genre row is a claim that 1019 other genres do
not share the number, and the family table is where a claim about a *kind* of
music belongs.
"""


#: A bar of four subdivided in three: the offbeat lands two thirds of the way
#: through the beat instead of halfway.
#:
#: **Not 0.667.** `groove.swing` is a lean rather than a position -- the
#: composer delays an offbeat by `(swing - 0.5) * SWING_REACH_BEATS` -- so 0.667
#: puts the offbeat at 0.558 of the beat, a third of the way to a shuffle. The
#: first version of this constant was 0.667 on the strength of the name alone,
#: and it took playing a blues to hear that it was not shuffling. Asking the
#: composer for the conversion is what keeps the two scales apart.
TRIPLET_SWING = swing_for_offbeat(2 / 3)


def _meter_profile(slug: str) -> Profile | None:
    """What a genre's own meter string says about its feel, if it says anything.

    The database has no articulation and no density (see the module docstring),
    which is why everything else here is hand-written. It does carry a `meter`,
    and 28 of the 1020 rows declare `4/4; 12/8` -- a four-beat bar subdivided in
    three, which is a shuffle. That is the database stating a groove in its own
    words, so reading it is not the same as inventing a mapping from mood tags.

    **`6/8` is deliberately not read.** 81 rows carry it, and a bar of six is a
    different bar rather than a swung four -- the composer works in the parsed
    time signature, so calling it swing would apply a triplet feel to a meter the
    row never claimed. Every one of the 28 is in the Blues family, and Blues
    stated no swing at all before this.
    """

    record = find(slug)
    if record is None or "12/8" not in (record.meter or ""):
        return None
    return Profile(swing=TRIPLET_SWING)


def profile_for(genres: Sequence[tuple[str, float]]) -> Profile:
    """The heaviest genre's family numbers, with per-genre opinions on top.

    The family part is not a blend across genres. Averaging a one-drop with a
    four-on-the-floor produces a kick density belonging to neither, and the
    fields here are not all numbers -- there is no halfway articulation. The
    dominant genre decides and the rest colour the song through the parts that
    *are* continuous (tempo, mood, the trait blends in :mod:`.intent`).

    :data:`GENRE_PROFILES` then overlays, heaviest last so the dominant genre
    wins a disagreement. Any genre in the brief may speak here, not only the
    dominant one: that is how "Mutation Funk, Dub, Tech House" gets tech
    house's drum pattern while funk leads.
    """

    profile = Profile()
    for name, _weight in sorted(genres, key=lambda item: -item[1]):
        family = family_of(name)
        if family and family in FAMILY_PROFILES:
            profile = FAMILY_PROFILES[family]
            break
    # The meter is the genre's own statement, so it outranks its family -- and
    # `GENRE_PROFILES` outranks it in turn, because a row written by hand is
    # someone disagreeing with the database on purpose.
    for name, _weight in sorted(genres, key=lambda item: item[1]):
        derived = _meter_profile(name)
        if derived is not None:
            profile = profile.overlaid_with(derived)
    for name, _weight in sorted(genres, key=lambda item: item[1]):
        stated = GENRE_PROFILES.get(name)
        if stated is not None:
            profile = profile.overlaid_with(stated)
    return profile


def pick(value: float | None, fallback: float) -> float:
    """The family's opinion, or the constant the caller has always used."""

    return fallback if value is None else value


def pick_int(value: int | None, fallback: int) -> int:
    return fallback if value is None else value


def pick_str(value: str | None, fallback: str) -> str:
    return fallback if value is None else value
