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

from dataclasses import dataclass
from typing import Sequence

from .genres import find as find_genre


@dataclass(frozen=True)
class Profile:
    """One family's numbers. ``None`` means "no opinion", not "zero"."""

    kick_density: float | None = None
    humanize: float | None = None
    harmonic_rhythm_bars: int | None = None
    bass_role: str | None = None
    articulation: str | None = None
    drum_pattern: str | None = None


#: Only ``supporting`` / ``present`` / ``dominant`` carry weight in the audio
#: prompt (``prompt_compiler._role_weight``); anything else silently reads as
#: 0.5, so the table stays inside that vocabulary.
FAMILY_PROFILES: dict[str, Profile] = {
    # The incumbent. These are the numbers every genre used to get.
    "R&B / Soul / Funk": Profile(0.72, 0.18, 1, "dominant", "short_offbeat_stabs"),
    "House": Profile(0.78, 0.12, 1, "present", "short_offbeat_stabs", "four_on_floor"),
    "Disco": Profile(0.8, 0.2, 1, "dominant", "muted_upstrokes", "four_on_floor"),
    "Techno": Profile(0.85, 0.06, 2, "supporting", "hypnotic_stabs", "four_on_floor"),
    "Trance": Profile(0.8, 0.08, 2, "supporting", "sustained_chords", "four_on_floor"),
    "EDM / Future Bass": Profile(0.8, 0.1, 2, "supporting", "sustained_chords", "four_on_floor"),
    "Hardcore Electronic": Profile(0.9, 0.04, 2, "supporting", "stab_hits", "four_on_floor"),
    "Reggae / Dub / Ska": Profile(0.38, 0.3, 2, "dominant", "offbeat_skank", "one_drop"),
    "Jungle / Drum & Bass": Profile(0.5, 0.1, 4, "dominant", "sparse_stabs", "breakbeat"),
    "Breakbeat / Breaks": Profile(0.6, 0.14, 2, "dominant", "chopped_stabs", "breakbeat"),
    "UK Garage / Bass": Profile(0.6, 0.16, 1, "dominant", "clipped_stabs", "two_step"),
    "Hip-Hop / Rap": Profile(0.55, 0.22, 2, "dominant", "laid_back_stabs", "boom_bap"),
    "Ambient / Downtempo": Profile(0.18, 0.35, 4, "supporting", "sustained_pads", "sparse_pulse"),
    "IDM / Experimental Electronic": Profile(0.42, 0.24, 2, "present", "fragmented_stabs", "broken_grid"),
    "Jazz": Profile(0.3, 0.45, 1, "present", "comped_chords", "swung_ride"),
    "Blues": Profile(0.35, 0.42, 1, "present", "comped_chords", "shuffle"),
    "Brazilian": Profile(0.45, 0.38, 1, "present", "syncopated_comping", "samba"),
    "Latin": Profile(0.5, 0.34, 1, "present", "montuno", "clave"),
    "Rock": Profile(0.5, 0.3, 2, "supporting", "sustained_power_chords", "backbeat"),
    "Punk / Hardcore": Profile(0.65, 0.28, 2, "supporting", "driving_downstrokes", "backbeat"),
    "Metal": Profile(0.7, 0.12, 2, "supporting", "palm_muted_chugs", "double_kick"),
    "Country / Americana": Profile(0.45, 0.3, 2, "present", "strummed_chords", "train_beat"),
    "Folk": Profile(0.3, 0.4, 2, "present", "strummed_chords", "sparse_pulse"),
}


def profile_for(genres: Sequence[tuple[str, float]]) -> Profile:
    """The profile of the heaviest genre whose family has one.

    Not a blend across genres. Averaging a one-drop with a four-on-the-floor
    produces a kick density belonging to neither, and the fields here are not
    all numbers -- there is no halfway articulation. The dominant genre decides
    and the rest colour the song through the parts that *are* continuous
    (tempo, mood, the trait blends in :mod:`.intent`).
    """

    for name, _weight in sorted(genres, key=lambda item: -item[1]):
        entry = find_genre(name)
        family = entry.parent if entry else None
        if family is None and entry is not None and entry.level == "genre":
            # A top-level row is its own family.
            family = entry.name
        if family and family in FAMILY_PROFILES:
            return FAMILY_PROFILES[family]
    return Profile()


def pick(value: float | None, fallback: float) -> float:
    """The family's opinion, or the constant the caller has always used."""

    return fallback if value is None else value


def pick_int(value: int | None, fallback: int) -> int:
    return fallback if value is None else value


def pick_str(value: str | None, fallback: str) -> str:
    return fallback if value is None else value
