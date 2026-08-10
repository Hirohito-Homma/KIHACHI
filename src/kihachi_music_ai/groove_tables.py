"""What each drum pattern, articulation and bass role means, in notes.

``SongSpec.drums.pattern`` has carried names like ``one_drop``, ``breakbeat``
and ``boom_bap`` since the family profiles went in. Until this module, the only
thing that ever read the field was :mod:`.prompt_compiler`, which puts it into
the sentence handed to ACE-Step. The MIDI composer never looked at it: reggae,
drum & bass, hip-hop and metal all came out as a four-on-the-floor kick with a
clap on 2 and 4, because that shape was written into ``compose_drums`` as
literals. So the genre database moved the audio prompt and left the notes where
they were.

This is the table that gives the names notes. It is deliberately small in what
it describes -- kick slots, the backbeat, the hat grid, one accent -- because
that is exactly what ``compose_drums`` already did with constants, and widening
the kit (toms, rides that play triplets, a real clave) is a change to the
composer rather than to a table.

**Two entries are copies of the old constants on purpose.**
``four_on_floor`` and ``syncopated_tech_house`` state today's numbers, so every
song that already resolved to one of them keeps its notes to the byte -- which
is what ``tests/test_golden_midi.py`` pins. Tech house's syncopation reaches
the notes through ``groove.syncopation`` and the mutation engine, not through a
different kick shape, and inventing one here would have moved the one song this
project has rendered and analysed.

Pure and stdlib-only.
"""

from __future__ import annotations

from dataclasses import dataclass

#: General MIDI percussion slots. The kit is four sounds wide because that is
#: what the composer plays; a name that would need a fifth (a samba surdo, a
#: jazz ride playing triplets) is approximated with these rather than given a
#: pitch the rest of the pipeline has never seen.
KICK = 36
SIDE_STICK = 37
SNARE = 38
CLAP = 39
CLOSED_HAT = 42
OPEN_HAT = 46
RIDE = 51


@dataclass(frozen=True)
class DrumPattern:
    """One groove, in the terms ``compose_drums`` works in.

    ``kick_positions`` is groove-ordered like every other position list in the
    composer: the earlier a slot appears, the more load-bearing it is, so a
    denser section adds inessential kicks instead of rewriting the groove.
    """

    kick_positions: tuple[float, ...]
    kick_steps: tuple[int, int]
    kick_anchors: tuple[float, ...] = (0.0,)
    #: Empty when the groove has no backbeat at all -- ambient and samba do not
    #: get a silent snare, they get no snare.
    backbeat_positions: tuple[float, ...] = (1.0, 3.0)
    backbeat_pitch: int = CLAP
    hat_pitch: int = CLOSED_HAT
    hat_offset: float = 0.5
    #: The finest grid the hats ever use. Density thins from here rather than
    #: switching between two grids -- see :func:`hat_positions`.
    hat_step: float = 0.5
    #: The coarsest, reached at density 0. Never denser than ``hat_step``.
    hat_sparse_step: float = 1.0
    #: ``None`` when the groove does not lift its last eighth in loud sections.
    accent_position: float | None = 3.75
    accent_pitch: int = OPEN_HAT

    def __post_init__(self) -> None:
        low, high = self.kick_steps
        if low < 1 or high < low:
            raise ValueError(f"invalid kick step bounds: {self.kick_steps}")
        if not self.kick_positions:
            raise ValueError("a pattern needs at least one kick slot")
        if high > len(self.kick_positions):
            raise ValueError("kick_steps asks for more slots than the pattern has")
        unanchored = set(self.kick_anchors) - set(self.kick_positions[: low])
        if unanchored:
            # An anchor outside the slots the lowest density keeps is an anchor
            # that silently disappears in quiet sections -- and anchors are what
            # the mutation engine refuses to drop or displace, so it would be a
            # groove that stays legible only when it is loud.
            raise ValueError(f"anchors are not always played: {sorted(unanchored)}")


DEFAULT_PATTERN = "four_on_floor"

DRUM_PATTERNS: dict[str, DrumPattern] = {
    # The incumbent: these are the literals ``compose_drums`` used to hold, and
    # what every one of the 1020 genres used to play.
    "four_on_floor": DrumPattern(
        kick_positions=(0.0, 2.0, 1.5, 3.25, 2.5, 0.75),
        kick_steps=(1, 5),
    ),
    # Identical on purpose; see the module docstring.
    "syncopated_tech_house": DrumPattern(
        kick_positions=(0.0, 2.0, 1.5, 3.25, 2.5, 0.75),
        kick_steps=(1, 5),
    ),
    # Reggae. The whole point of the name is the *absent* downbeat: the kick
    # arrives with the snare on beat 3 and beat 1 is a hole.
    "one_drop": DrumPattern(
        kick_positions=(2.0, 3.5, 0.75),
        kick_steps=(1, 3),
        kick_anchors=(2.0,),
        backbeat_positions=(2.0,),
        backbeat_pitch=SNARE,
        hat_offset=0.5,
        hat_step=0.5,
        accent_position=None,
    ),
    # Amen-shaped: kick on 1 and the "and" of 3, snare on 2 and 4.
    "breakbeat": DrumPattern(
        kick_positions=(0.0, 2.5, 1.75, 3.25, 0.75),
        kick_steps=(2, 5),
        backbeat_positions=(1.0, 3.0),
        backbeat_pitch=SNARE,
        hat_offset=0.25,
        hat_step=0.5,
    ),
    # UK garage: the kick skips beat 3 entirely, which is the whole shuffle.
    "two_step": DrumPattern(
        kick_positions=(0.0, 2.5, 3.5, 1.75),
        kick_steps=(2, 4),
        backbeat_positions=(1.0, 3.0),
        backbeat_pitch=SNARE,
        hat_offset=0.5,
        hat_step=0.5,
    ),
    "boom_bap": DrumPattern(
        kick_positions=(0.0, 2.5, 1.75, 3.5),
        kick_steps=(2, 4),
        backbeat_positions=(1.0, 3.0),
        backbeat_pitch=SNARE,
        hat_offset=0.0,
        hat_step=0.5,
        accent_position=None,
    ),
    # Ambient / downtempo: a pulse, not a beat. No snare at all -- a backbeat is
    # the thing this music is defined by not having.
    "sparse_pulse": DrumPattern(
        kick_positions=(0.0, 2.0),
        kick_steps=(1, 2),
        backbeat_positions=(),
        hat_offset=1.0,
        hat_step=1.0,
        hat_sparse_step=2.0,
        accent_position=None,
    ),
    # IDM: the downbeat is still an anchor, but nothing else lands where a
    # dance grid would put it.
    "broken_grid": DrumPattern(
        kick_positions=(0.0, 1.25, 2.75, 3.5, 0.75, 2.25),
        kick_steps=(2, 6),
        backbeat_positions=(1.75, 3.0),
        backbeat_pitch=SNARE,
        hat_offset=0.25,
        hat_step=0.5,
    ),
    # Jazz: the ride carries the time and the kick only comments. Written
    # straight because the composer has no triplet grid; ``groove.swing`` is
    # what leans it, and the Jazz family sets humanize to 0.45.
    "swung_ride": DrumPattern(
        kick_positions=(0.0, 2.0),
        kick_steps=(1, 2),
        backbeat_positions=(1.0, 3.0),
        backbeat_pitch=SNARE,
        hat_pitch=RIDE,
        hat_offset=0.0,
        hat_step=0.5,
        accent_position=None,
    ),
    "shuffle": DrumPattern(
        kick_positions=(0.0, 2.0, 3.5),
        kick_steps=(2, 3),
        backbeat_positions=(1.0, 3.0),
        backbeat_pitch=SNARE,
        hat_pitch=RIDE,
        hat_offset=0.0,
        hat_step=0.5,
        accent_position=None,
    ),
    # Brazilian: a surdo-shaped kick landing on the second half of each pair,
    # continuous sixteenths above it, and no backbeat.
    "samba": DrumPattern(
        kick_positions=(1.5, 3.5, 0.0, 2.0),
        kick_steps=(2, 4),
        kick_anchors=(1.5,),
        backbeat_positions=(),
        hat_offset=0.0,
        hat_step=0.25,
        hat_sparse_step=0.5,
        accent_position=None,
    ),
    # Latin: the three-side of the son clave on the side stick, where a clave
    # belongs, over a two-beat tumbao kick. One bar, so the pattern repeats the
    # three-side rather than alternating 3-2 -- the composer has no two-bar
    # unit, and a half-stated clave would be worse than a repeated one.
    "clave": DrumPattern(
        kick_positions=(0.0, 2.0, 3.5, 1.5),
        kick_steps=(2, 4),
        backbeat_positions=(0.0, 1.5, 3.0),
        backbeat_pitch=SIDE_STICK,
        hat_offset=0.0,
        hat_step=0.5,
        accent_position=None,
    ),
    "backbeat": DrumPattern(
        kick_positions=(0.0, 2.0, 2.5, 3.5),
        kick_steps=(2, 4),
        backbeat_positions=(1.0, 3.0),
        backbeat_pitch=SNARE,
        hat_offset=0.0,
        hat_step=0.5,
    ),
    # Metal: eighths at every density, sixteenths when the section is dense.
    "double_kick": DrumPattern(
        kick_positions=(0.0, 1.0, 2.0, 3.0, 0.5, 1.5, 2.5, 3.5),
        kick_steps=(4, 8),
        backbeat_positions=(1.0, 3.0),
        backbeat_pitch=SNARE,
        hat_pitch=RIDE,
        hat_offset=0.0,
        hat_step=0.5,
    ),
    # Country: the kick keeps 2/4 time under a continuous sixteenth shuffle.
    "train_beat": DrumPattern(
        kick_positions=(0.0, 2.0, 1.0, 3.0),
        kick_steps=(2, 4),
        backbeat_positions=(1.0, 3.0),
        backbeat_pitch=SNARE,
        hat_offset=0.0,
        hat_step=0.25,
        hat_sparse_step=0.5,
        accent_position=None,
    ),
}


@dataclass(frozen=True)
class BassRole:
    """How much room the bass takes, by the role the family gives it.

    ``SongSpec.bass.role`` reached only ``prompt_compiler._role_weight``, which
    turns it into a number for the audio prompt. In the MIDI the bass played
    the same line at the same volume whether it was the lead instrument of the
    genre or a root note under a guitar.

    Three roles, because only these three carry weight in that prompt weighting
    and a fourth name would silently read as 0.5 there.
    """

    density_scale: float
    velocity: int
    #: Multiplies the octave-jump probability the SongSpec already carries. A
    #: supporting bass that leaps octaves is not supporting anything.
    octave_jump_scale: float = 1.0


DEFAULT_BASS_ROLE = "dominant"

BASS_ROLES: dict[str, BassRole] = {
    # The incumbent: every song's bass, whatever its role said.
    "dominant": BassRole(density_scale=1.0, velocity=102),
    "present": BassRole(density_scale=0.82, velocity=94, octave_jump_scale=0.7),
    "supporting": BassRole(density_scale=0.62, velocity=86, octave_jump_scale=0.35),
}


def bass_role(name: str) -> BassRole:
    """The playing behind a ``SongSpec.bass.role`` name."""

    return BASS_ROLES.get(name, BASS_ROLES[DEFAULT_BASS_ROLE])


@dataclass(frozen=True)
class ChordArticulation:
    """How the chord part is played, in the terms ``compose_chords`` works in.

    Same story as :class:`DrumPattern`: ``SongSpec.chords.articulation`` has
    named nineteen different ways to play a chord since the family profiles
    went in, and all nineteen came out as the same 0.2-beat offbeat stab
    because the length and the slots were literals in the composer.

    ``duration`` is in quarter-note beats. ``minimal_duration`` is what a
    section marked minimal uses instead -- a sparse section holds its chords
    longer, which is the one thing the composer already varied here.
    """

    positions: tuple[float, ...]
    steps: tuple[int, int]
    duration: float
    minimal_duration: float
    #: Velocity scale against the part's nominal 82. Skanks and comping sit
    #: back; power chords and downstrokes lean in.
    velocity_scale: float = 1.0
    #: How far apart the voices are spread in velocity, per voice.
    voice_falloff: int = 4

    def __post_init__(self) -> None:
        low, high = self.steps
        if low < 1 or high < low:
            raise ValueError(f"invalid step bounds: {self.steps}")
        if high > len(self.positions):
            raise ValueError("steps asks for more slots than the articulation has")
        if self.duration <= 0.0 or self.minimal_duration <= 0.0:
            raise ValueError("a chord has to last longer than no time at all")


DEFAULT_ARTICULATION = "short_offbeat_stabs"

CHORD_ARTICULATIONS: dict[str, ChordArticulation] = {
    # The incumbent: today's literals, so the rendered song does not move.
    "short_offbeat_stabs": ChordArticulation(
        (1.5, 0.75, 2.75, 3.5, 2.25), (1, 4), 0.2, 0.28
    ),
    # Reggae's defining gesture: chords *only* on the offbeats, nowhere else.
    "offbeat_skank": ChordArticulation(
        (1.5, 3.5, 0.5, 2.5), (2, 4), 0.18, 0.24, velocity_scale=0.9
    ),
    "muted_upstrokes": ChordArticulation(
        (1.5, 3.5, 0.5, 2.5, 1.75, 3.75), (3, 6), 0.14, 0.18, velocity_scale=0.85
    ),
    "hypnotic_stabs": ChordArticulation(
        (1.5, 3.5, 2.75, 0.75), (1, 3), 0.16, 0.2, velocity_scale=0.9
    ),
    "stab_hits": ChordArticulation((0.0, 2.0, 3.0, 1.0), (1, 3), 0.22, 0.3, 1.1),
    "sparse_stabs": ChordArticulation((0.0, 2.5, 1.5), (1, 2), 0.3, 0.5),
    "chopped_stabs": ChordArticulation(
        (0.75, 2.25, 1.5, 3.25, 0.25), (2, 5), 0.12, 0.18
    ),
    "clipped_stabs": ChordArticulation((0.5, 2.5, 1.75, 3.5), (2, 4), 0.12, 0.16),
    "fragmented_stabs": ChordArticulation(
        (0.25, 1.75, 2.5, 3.25, 1.25), (2, 5), 0.14, 0.2
    ),
    # Hip-hop: the chord lands behind the beat and stays a while.
    "laid_back_stabs": ChordArticulation((0.25, 2.25, 1.25, 3.25), (1, 3), 0.5, 0.9),
    # Sustains. These are the ones the old 0.2-beat stab misrepresented most:
    # a pad that lasts a fifth of a beat is not a pad.
    "sustained_chords": ChordArticulation((0.0, 2.0), (1, 2), 2.0, 4.0, 0.9),
    "sustained_pads": ChordArticulation((0.0,), (1, 1), 4.0, 4.0, 0.8),
    "sustained_power_chords": ChordArticulation((0.0, 2.0), (1, 2), 1.9, 3.9, 1.1),
    "palm_muted_chugs": ChordArticulation(
        (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5), (4, 8), 0.22, 0.4, 1.1
    ),
    "driving_downstrokes": ChordArticulation(
        (0.0, 1.0, 2.0, 3.0, 0.5, 1.5), (4, 6), 0.4, 0.6, 1.1
    ),
    "strummed_chords": ChordArticulation(
        (0.0, 2.0, 1.0, 3.0), (2, 4), 0.9, 1.4, 0.95, voice_falloff=7
    ),
    # Jazz comping: irregular, behind the beat, and the voices even out because
    # a comped chord is one gesture rather than a stack of ranked voices.
    "comped_chords": ChordArticulation(
        (1.5, 2.75, 0.5, 3.25), (1, 3), 0.6, 1.0, 0.85, voice_falloff=2
    ),
    "syncopated_comping": ChordArticulation(
        (0.5, 1.75, 3.0, 2.25), (2, 4), 0.35, 0.6, 0.85, voice_falloff=2
    ),
    # Latin montuno: a continuous quaver figure, not a stab.
    "montuno": ChordArticulation(
        (0.0, 0.75, 1.5, 2.5, 3.25, 2.0), (4, 6), 0.3, 0.45, 0.9
    ),
}


def chord_articulation(name: str) -> ChordArticulation:
    """The way of playing behind a ``SongSpec.chords.articulation`` name.

    Unknown names fall back, for the same reason :func:`drum_pattern` does.
    """

    return CHORD_ARTICULATIONS.get(name, CHORD_ARTICULATIONS[DEFAULT_ARTICULATION])


def hat_positions(
    groove: DrumPattern, density: float, bar_beats: float = 4.0
) -> tuple[float, ...]:
    """Hat slots for this density, thinning continuously from fine to coarse.

    ``compose_drums`` used to pick one of two grids with a threshold::

        hat_step = 0.5 if hat_density * section_density >= 0.3 else 1.0

    which meant every value above 0.3 wrote identical MIDI. ``hat_density``
    looked like a control and was a switch, and that is exactly why
    ``MusicBrain`` pinned it at 0.78 for all 1020 genres rather than letting
    the families set it: varying a number that cannot move the notes is worse
    than admitting it is fixed.

    Here the full grid is laid out at ``hat_step`` and then thinned, dropping
    the weakest slots first -- offbeats before beats, and later offbeats before
    earlier ones -- until only the ``hat_sparse_step`` skeleton is left at
    density 0. Every step of density in between removes one hat, so the field
    is continuous in the only sense that matters: a change to it changes the
    file.
    """

    if not 0.0 <= density <= 1.0:
        raise ValueError("density must be between 0.0 and 1.0")
    full: list[float] = []
    position = groove.hat_offset
    while position < bar_beats:
        full.append(round(position, 6))
        position += groove.hat_step
    if not full:
        return ()
    skeleton: list[float] = []
    position = groove.hat_offset
    while position < bar_beats:
        skeleton.append(round(position, 6))
        position += groove.hat_sparse_step
    keep = len(skeleton) + round(density * (len(full) - len(skeleton)))
    if keep >= len(full):
        return tuple(full)
    protected = set(skeleton)
    # Weakest last: an offbeat goes before a beat, and of two equals the later
    # one goes first, so thinning keeps the front of the bar legible.
    droppable = sorted(
        (slot for slot in full if slot not in protected),
        key=lambda slot: (slot % 1.0 == 0.0, -slot),
    )
    dropped = set(droppable[: len(full) - keep])
    return tuple(slot for slot in full if slot not in dropped)


def drum_pattern(name: str) -> DrumPattern:
    """The groove behind a ``SongSpec.drums.pattern`` name.

    An unknown name falls back to four-on-the-floor rather than raising. The
    field is a free string in a spec that may have been written by an older
    version or edited by hand, and a song that will not compose is worse than
    one whose kick is more ordinary than its label.
    """

    return DRUM_PATTERNS.get(name, DRUM_PATTERNS[DEFAULT_PATTERN])
