"""Read a brief as intent -- traits, with a polarity and a degree -- not keywords.

``MusicBrain`` used to ask five yes/no questions of the prompt (``"スラップ" in
prompt``) and pick one of two constants from each answer. Two things were wrong
with that, and only one of them was a missing feature.

**Negation inverted the request.** ``"スラップじゃなくて指弾きで"`` contains the
substring ``スラップ``, so the old test said yes, and four bass parameters were
set to the opposite of what was asked for. ``"サイケじゃない"`` produced the most
psychedelic setting available. A person could state a preference clearly, in
plain Japanese, and be answered with its inverse.

**Degree had nowhere to land.** ``少しサイケ`` and ``かなりサイケ`` both became
0.82, because 0.82 was the only value on the yes side. The vocabulary for this
already existed in :mod:`.edit` -- ``少し`` and ``かなり`` have moved edit
magnitudes since the beginning -- but it applied only when *correcting* a song,
never when asking for one. The same words worked or did not work depending on
which command you typed them into. This module is where they stop being two
vocabularies.

Deliberately not a parser. It finds the words a brief actually uses, decides
which of them are being refused, and how strongly the rest are being asked for.
Everything else stays the caller's decision.

Pure and stdlib-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

#: Degree words, shared with :mod:`.edit` -- which imports them from here so
#: there is exactly one list. Their edit magnitudes are unchanged, so this move
#: is not an edit-behaviour change; see ``INTENT_ONLY_*`` below for the words
#: this module adds without touching what ``edit`` recognises.
SMALL_WORDS = ("少し", "ちょっと", "やや", "slightly", "a bit", "軽く")
LARGE_WORDS = ("かなり", "大幅", "ずっと", "much", "far", "way", "大胆")

#: Degree words that only make sense when *describing* a song rather than
#: correcting one. "very psychedelic" is a brief; "very increase it" is not.
#: Kept apart so that adding them here cannot silently move an edit magnitude.
INTENT_ONLY_SMALL = ("ほんの", "うっすら", "控えめ", "faintly", "subtly", "lightly")
INTENT_ONLY_LARGE = ("超", "めちゃ", "すごく", "思いきり", "very", "really", "heavily", "extremely")

#: What a plain mention is worth, and what the two modifiers scale it to. A
#: plain mention is 1.0 **on purpose**: callers blend towards the value they
#: used to hardcode, so an unmodified brief still produces exactly the numbers
#: it produced before this module existed.
PLAIN_STRENGTH = 1.0
SMALL_STRENGTH = 0.5
LARGE_STRENGTH = 1.5

#: The one degree word this vocabulary spells as a **suffix**. 「暗め」 asks for
#: somewhat dark, the same request 「少し暗い」 makes with the modifier in front,
#: and only the second was read -- so 「暗めにして」 composed exactly as dark as
#: 「暗くして」.
#:
#: It cannot join ``SMALL_WORDS``: those are searched across the whole span,
#: and 「め」 is a single kana living inside ordinary words (「控えめ」,「まとめ」,
#: 「ダメ」). It means a degree only where it is glued to the mention, so it is
#: read at that one position and nowhere else.
SUFFIX_HEDGE = "め"

#: Where that gluing is a verb instead. 詰め込む -> 詰め込め and 食い込む ->
#: 食い込め are imperatives: 「手数を詰め込め」 asks for **more**, and reading
#: 「め」 there would hedge the opposite of what the brief said. Of the 155
#: surface forms these two are the only ones whose ``{word}め`` is a conjugation
#: rather than a degree, which is why this is an exception list and not a rule.
IMPERATIVE_IN_ME = ("詰め込", "食い込")

#: Japanese puts negation after what it negates; English puts it before. The
#: ``くない`` family was added with the darkness traits: every trait before them
#: was named by a noun, where ``じゃない`` is the negation, and an adjective
#: negates as ``暗くない`` instead. Without it 「暗くないテクノ」 read as a
#: request for darkness -- the exact inversion this module was written to stop.
#: The particle forms (``の無い``, ``が無い``, ``はない``) came in with the
#: section-contrast pair: 「メリハリの無いテクノ」 read as a *request* for it,
#: because the bare ``ない`` in the suffix list has to touch what it negates and
#: a particle is in the way. They are listed with the particle attached rather
#: than as a bare ``無い`` so that ``無い`` alone still cannot reach backwards
#: past an unrelated word.
#: ``すぎない`` sits here rather than in the suffix list below because it is
#: never an ordinary word ending -- unlike a bare ``ない``, which is why that one
#: has to touch what it negates. Requiring adjacency for ``すぎない`` missed the
#: shape a noun trait naturally takes: 「手数が多すぎない」 puts a particle and an
#: adjective between the trait and the negation, and it read as a request.
#: ``避け``, ``排除``, ``厳禁``, ``省い`` and ``省く`` are **verbs of refusal**
#: rather than grammatical negation, and the list had none of them although
#: ENGLISH_NEGATORS has carried ``avoid`` since v0.1. 「スラップは避けて」 read as
#: a request for slap. They were found by sweeping the vocabulary rather than by
#: reading this list -- see the module tests.
#:
#: Three near neighbours were **deliberately left out**, each because it earns a
#: false refusal on an ordinary brief:
#:
#: * ``やめ`` -- 「テンポをはやめて」 is *speed up*, and it contains ``やめて``.
#: * ``以外`` -- 「サイケ以外は暗くして」 names a span to treat differently; it is
#:   not a refusal of the psychedelia, and 「ミニマル以外の要素も入れて」 asks for
#:   *more*.
#: * ``カット`` -- 「フィルターのカットオフ」 is a parameter, not a removal.
#:
#: ``NG`` is worse than any of them and is not a candidate at all: matching is
#: case-folded, so it would fire inside ``song``, ``swing`` and ``strong``.
#:
#: A second refusal cancels the first as of the change that added ``_distinct``:
#: 「暗くなくはない」 and 「スラップ抜きじゃない」 used to read as flat refusals of
#: what they ask for. Two caveats remain, both deliberate.
#:
#: **A double negative is a hedge**, and reads at ``SMALL_STRENGTH`` unless a
#: degree word says otherwise -- see :func:`_hedged_if_cancelled`. The note left
#: here beforehand claimed 「スラップ抜きじゃない」 was a plain request and only
#: 「暗くなくはない」 a hedge, and looked for a rule to tell the two apart. There is
#: no such rule to find: both are litotes, and 「スラップ抜きじゃない」 concedes some
#: slap rather than calling for it.
#:
#: **A negation attached to an antonym is still misread**, and it is a different
#: bug from the one above. 「手数は少なくない」 fires exactly one negator, ``くない``,
#: and it belongs to 「少なく」 -- a word this module does not know -- rather than
#: to 「手数」, the only mention in the clause. So `busy` is refused when the brief
#: is asking for it. Reading that correctly means knowing 少ない is the opposite
#: of 手数, which is semantic knowledge this reader does not have and should not
#: guess at.
JAPANESE_NEGATORS = ("じゃなく", "ではなく", "じゃない", "ではない", "無し", "なし", "抜き", "禁止", "不要", "いらな", "要らな", "いらん", "くない", "くなく", "くありません", "の無い", "のない", "が無い", "がない", "は無い", "はない", "すぎない", "過ぎない", "すぎず", "過ぎず", "厳禁")

#: Refusals that name the **action** rather than the thing. 「ボコーダーは入れ
#: ないで」 is as ordinary as 「ボコーダーは無しで」, and `使わな` was the only verb
#: the list had -- so 「使わないで」 was refused and 「入れないで」, the same
#: sentence about the same thing, was read as a request for it. The bare `ない`
#: cannot reach these: it counts only where it touches the mention, and
#: 「ボコーダーは入れない」 has three characters in between.
#:
#: **These are kept apart because a verb takes an object and a noun form does
#: not.** 「ミニマルにして無駄を省いて」 asks for minimalism and refused it: `省い`
#: looked back past 「無駄」, the noun it actually takes, and landed on
#: 「ミニマル」. So a verb refusal only counts when nothing but particles sits
#: between it and the mention -- see :func:`_attaches`. The noun forms above
#: keep the old reach, because 「多すぎない」 and 「サイケ感の無い」 legitimately
#: cross an adjective or a suffix to get to what they are about.
#:
#: Each verb has to be written down, so the next one nobody thought of is
#: invisible again; the sweep asks the verb shape of every trait, which is the
#: axis `test_every_word_can_be_refused_in_the_ordinary_ways` was missing.
JAPANESE_VERB_NEGATORS = ("避け", "排除", "省い", "省く", "取り除", "抜い", "使わな", "使わず", "使用しな", "使いませ", "要りませ", "入れな", "入れず", "足さな", "足さず", "加えな", "加えず", "混ぜな", "混ぜず", "乗せな", "乗せず", "鳴らさな", "鳴らさず", "弾かな", "弾かず")
ENGLISH_NEGATORS = ("without", "not ", "no ", "never", "avoid", "minus", "sans")

#: Negations that count **only when they touch the mention they follow**.
#: A verb trait negates by simply appending: ``跳ね`` + ``ない``. The list above
#: cannot carry a bare ``ない``, because those entries attach to the nearest
#: mention anywhere earlier in the clause, and 「サイケで切ないやつ」 would then
#: refuse the psychedelia on the strength of an unrelated adjective. Requiring
#: adjacency is what makes the bare form safe: in that phrase the ``ない`` is
#: four characters away from ``サイケ`` and means nothing to it.
#: ``なく`` is the bare ``ない`` in its continuative form and was missing while
#: the bare ``ない`` was present, so 「跳ねないで」 was refused and 「跳ねなくて」 was
#: not. Adjacency is what keeps it safe, exactly as for ``ない``: in
#: 「せわしなく変わる」 the ``なく`` overlaps the ``せわしな`` mention rather than
#: starting where it ends, and in 「手数を少なく」 it is three characters past it.
JAPANESE_SUFFIX_NEGATORS = (
    "しない", "しなく", "しません", "させない", "せず", "ない", "なく", "ず",
)

#: What may sit between a mention and a suffix negator: the adverbial ending
#: that turns the word into what ``する`` is doing. 「スウィングしないで」 was
#: refused because ``しない`` starts where the mention ends, and 「暗くしないで」
#: was read as a **request** for dark because 「く」 was in the way -- an
#: adjective cannot reach ``する`` without one, so requiring bare adjacency
#: excluded every adjective in the vocabulary from the plainest refusal there
#: is. All eight shapes 「{w}くしないで」「{w}にはせずに」… missed, for all 95
#: Japanese surface forms.
#:
#: Empty stays in the list, and the marker may not begin **before** the mention
#: ends: 「せわしなく変わる」 has its ``なく`` overlapping the ``せわしな`` mention
#: rather than following it, and that is what keeps the busy trait from
#: refusing itself.
_ADVERBIAL_MARKERS = ("", "く", "に", "くは", "には")

#: Clause boundaries. Negation does not reach across one, which is what keeps
#: ``"スラップじゃなくて指弾き。サイケに。"`` from turning the whole brief off.
_CLAUSE_SPLIT = re.compile(r"[、。，．,.;；\n\r]+")

#: Text that joins two mentions into one list, so a single negator covers both:
#: ``"スラップとサイケはなし"``. Anything else between them (``"にしてサイケは無し"``)
#: means they are separate statements and only the nearest one is refused.
_JOINERS = re.compile(r"^[\sとやおよびまたは・/／&＆+＋and or,]*$", re.IGNORECASE)

#: Where in the song a clause is talking about. These are `edit`'s own words --
#: it has resolved 「後半」 and 「序盤」 into arrangement spans since v0.1, so a
#: person could name a place when *correcting* a song and not when asking for
#: one. `edit` imports them from here now, the way it imports the degree words,
#: because two lists would let 「後半」 mean different spans in the two commands.
LATER_HALF_WORDS = ("後半", "second half", "later half", "終盤")
EARLIER_HALF_WORDS = ("前半", "first half", "earlier half", "序盤")

SECOND_HALF = "second_half"
FIRST_HALF = "first_half"

#: Traits that can be asked for in one part of the song. A scope is only
#: meaningful where the value it moves exists per section: `SectionSpec` carries
#: densities and an energy, and nothing else in `SongSpec` is written per
#: section. A brief that scopes anything else is not refused -- the trait still
#: applies to the whole song, which is what it meant before scopes existed --
#: and `brief` reports the span word as unread so nobody is told otherwise.
SCOPABLE_TRAITS = frozenset({"busy", "sparse", "contrast", "flat"})

#: The traits a brief can state, and the words that state them. Lifted verbatim
#: from the flags and instrument cues ``MusicBrain`` already had: recognising
#: strictly less than before would be a regression, so nothing was dropped.
TRAIT_WORDS: dict[str, tuple[str, ...]] = {
    "psychedelic": ("サイケ", "psychedelic", "psychedelia"),
    "minimal": ("ミニマル", "minimal", "stripped"),
    "slap": ("スラップ", "slap"),
    "vocoder": ("ボコーダー", "vocoder", "ヴォコーダー"),
    "mutation": ("変態", "mutation", "mutate", "mutated"),
    "sub": ("サブベース", "サブ・ベース", "sub bass", "sub-bass", "subbass", "808"),
    "synth": ("シンセ", "スタブ", "リード", "synth", "stab", "lead"),
    "arp": ("アルペジ", "シーケンス", "arp", "sequence", "sequencer"),
    "dub": ("dub", "ダブ"),
    # Darkness is the one axis a brief could always describe and never reach.
    # `style.darkness` has existed since v0.1 and is read by the prompt
    # compiler, the lyric writer and the Live plan, but only the genre's mood
    # tags and `dub` ever set it -- so 「暗くて疾走感のある」 left the SongSpec
    # carrying its genre default of 0.48, which is what `brief.py`'s opening
    # example is about. These two are a pair rather than one signed trait
    # because refusing is not the same as asking for the opposite: 「暗くない」
    # leaves the genre's own darkness alone, and only 「明るい」 moves it down.
    "dark": ("暗", "ダーク", "dark", "陰鬱", "重苦し", "gloomy", "murky"),
    "bright": ("明る", "ブライト", "bright", "きらびやか", "煌", "luminous"),
    # `groove.swing` reaches the composer's timing, and exactly one genre of the
    # 1021 in the database ever set it (`mutation_funk`, 0.54). Every family
    # including Jazz left it straight, and no word here could say otherwise, so
    # 「シャッフルで」 composed straight eighths and said nothing about it.
    "swung": ("スウィング", "スイング", "シャッフル", "跳ね", "ハネ", "swing", "swung", "shuffle"),
    "straight": ("ストレート", "イーブン", "straight", "even ", "四つ打ち"),
    # `groove.syncopation` reaches the notes twice -- the mutation amount and the
    # drum placement -- and `bass.syncopation` reaches the bass line, so like
    # swing this survives into Live as MIDI. **No** genre of the 1021 sets it:
    # `derive.Profile` has no field for it, and the only thing that ever moved it
    # was the `slap` trait. So 「シンコペを効かせて」 composed the same 0.58 as
    # 「シンコペ無しで」. `edit.py` has had words for it since v0.1, which means
    # the feel was sayable *after* a render and not in the brief that made it.
    "syncopated": ("シンコペ", "syncopat", "食い気味", "食い込", "うねら", "うねる", "うねり", "うねっ", "裏打ち", "offbeat", "off-beat"),
    "on_grid": ("オンビート", "表打ち", "頭打ち", "on-beat", "on the beat", "on-grid"),
    # `groove.humanize` is the opposite case to the two above: every one of the
    # 23 families states it, from Hardcore Electronic's 0.04 to Jazz's 0.45, and
    # still no brief could say it. It reaches the composer's jitter directly --
    # `groove.py` measures the 0.18 default as +/-1.7 ms at 110 BPM -- so it is
    # MIDI, not prompt text, and `_stated_axis` moves it from wherever the genre
    # put it rather than from a constant.
    "loose": ("ヨレ", "よれ", "人間っぽ", "人間的", "手弾き", "生っぽ", "ルーズ", "loose", "human", "hand-played"),
    "tight": ("タイト", "カッチリ", "かっちり", "きっちり", "機械的", "マシンライク", "ジャスト", "tight", "machine", "quantiz"),
    # `drums.kick_density` and `drums.hat_density` decide how many drum notes
    # exist at all, and both are stated by every family (kick 0.38 Reggae to 0.9
    # Hardcore, hat 0.45 to 0.95). The one word that came close was `minimal`,
    # and it does something else entirely: it gates the `minimal` flag on the
    # opening two sections of the arrangement and never touches a density.
    "busy": ("手数", "ぎっしり", "詰め込", "詰まった", "密度が高", "せわしな", "busy", "dense", "relentless"),
    "sparse": ("スカスカ", "疎ら", "まばら", "余白", "間を空け", "隙間", "sparse", "spacious"),
    # `harmony.harmonic_rhythm_bars` is how many bars one chord lasts, and every
    # composer reads it -- bass, chords, arp, synth, sub all pick their chord by
    # it, and `analyzer` and `midi_review` check the render against it. All 23
    # families state it (1, 2 or 4 bars) and no brief could. Unlike every pair
    # above it is an integer on a three-rung ladder, so `music_brain` steps it
    # rather than interpolating.
    "fast_changes": ("コードチェンジが速", "展開が速", "目まぐるし", "せわしなく変わ", "どんどん変わ", "fast changes", "fast-moving chords"),
    "slow_changes": ("ワンコード", "一発もの", "コードを引っ張", "同じコードで", "ずっと同じ和音", "static harmony", "one chord"),
    # `groove.note_length` is the one field in this list that did not exist
    # before the word did. Every other trait here found a number with consumers
    # and no path from the brief; note length had no number at all -- each part
    # carried a duration constant written into `composer` (bass 0.3, kick 0.16,
    # synth 0.18) and nothing could scale them. It is *half* of 疾走感: the
    # other half is how often notes arrive, which `busy` and `swung` already
    # reach, so neither word claims the whole thing.
    "staccato": ("歯切れ", "スタッカート", "短く切", "ぶつ切り", "キレのある", "staccato", "clipped"),
    "legato": ("レガート", "繋げ", "つなげ", "伸ばし", "滑らか", "legato", "sustained"),
    # The first pair that is about the *relation* between sections rather than a
    # value inside one. Every section already carries an energy and three
    # densities, chosen by the arrangement archetypes, and a brief could ask for
    # none of it: 「メリハリのある」 and 「淡々とした」 built the same four
    # sections. This does not read 「ここで視界が開ける」 -- that names a place,
    # and nothing here can hear which section a person means.
    "contrast": ("メリハリ", "起伏", "抑揚", "ダイナミクス", "dynamic range", "contrast"),
    "flat": ("平坦", "淡々", "一定", "均一", "flat", "uniform", "monotone"),
}


@dataclass(frozen=True)
class Trait:
    """One thing the brief said, with how it said it."""

    name: str
    #: ``+1`` asked for, ``-1`` refused.
    polarity: int
    #: ``0.5`` hedged, ``1.0`` plainly stated, ``1.5`` insisted on.
    strength: float
    #: The surface form actually found, so a person can see why we heard this.
    evidence: str
    position: int
    #: Which part of the song the clause was talking about, or ``None`` for the
    #: whole of it. Only set for :data:`SCOPABLE_TRAITS`.
    scope: str | None = None


@dataclass(frozen=True)
class Traits:
    """Everything one brief stated. Absent is a real answer, and it is zero."""

    traits: tuple[Trait, ...] = ()

    def find(self, name: str) -> Trait | None:
        for trait in self.traits:
            if trait.name == name:
                return trait
        return None

    def unscoped(self) -> "Traits":
        """Only what was said about the whole song.

        Every song-level field reads this rather than the whole set, so a brief
        that scopes a statement to one half does not also apply it everywhere.
        A brief with no span words is unchanged, which is why every song made
        before scopes existed still composes byte for byte.
        """

        return Traits(tuple(trait for trait in self.traits if trait.scope is None))

    def within(self, scope: str) -> "Traits":
        """Only what was said about ``scope``, read as if it were the whole brief."""

        return Traits(tuple(trait for trait in self.traits if trait.scope == scope))

    def strength_of(self, name: str) -> float:
        """How much of ``name`` this brief asks for, on a 0-1.5 scale.

        Zero when the brief never mentions it **and** zero when the brief
        refuses it. Those are the same request: for every trait here the
        refused pole is the value the old code already used when the word was
        absent, and inventing something beyond it would be fabrication rather
        than interpretation. What negation buys is not a new value -- it is no
        longer landing on the *opposite* one.
        """

        trait = self.find(name)
        if trait is None or trait.polarity < 0:
            return 0.0
        return trait.strength

    def asked_for(self, name: str) -> bool:
        return self.strength_of(name) > 0.0

    def refused(self, name: str) -> bool:
        trait = self.find(name)
        return trait is not None and trait.polarity < 0

    def names(self) -> tuple[str, ...]:
        return tuple(trait.name for trait in self.traits)


EMPTY = Traits()


def read(prompt: str) -> Traits:
    """Find the traits a brief states, refusals and degrees included."""

    found: list[Trait] = []
    for clause_start, clause in _clauses(prompt):
        found.extend(_read_clause(clause, clause_start))
    found.sort(key=lambda trait: trait.position)
    return Traits(tuple(found))


def _clauses(prompt: str) -> list[tuple[int, str]]:
    clauses: list[tuple[int, str]] = []
    cursor = 0
    for piece in _CLAUSE_SPLIT.split(prompt):
        start = prompt.find(piece, cursor) if piece else cursor
        if piece.strip():
            clauses.append((start, piece))
        cursor = start + len(piece)
    return clauses


def _clause_scope(lowered: str) -> str | None:
    """Which half of the song this clause names, if it names one.

    Deliberately per clause, like negation: 「前半は淡々と、後半でメリハリを」 is
    two statements about two places, and a scope that reached across the comma
    would make the second one overwrite the first.
    """

    for word in LATER_HALF_WORDS:
        if word in lowered:
            return SECOND_HALF
    for word in EARLIER_HALF_WORDS:
        if word in lowered:
            return FIRST_HALF
    return None


def _read_clause(clause: str, offset: int) -> list[Trait]:
    lowered = clause.casefold()
    mentions: list[tuple[int, int, str, str]] = []  # start, end, trait, evidence
    for name, words in TRAIT_WORDS.items():
        span = _first_span(lowered, words)
        if span is not None:
            start, end, evidence = span
            mentions.append((start, end, name, evidence))
    if not mentions:
        return []
    mentions.sort()

    refusals = _refusals(lowered, mentions)
    scope = _clause_scope(lowered)
    return [
        Trait(
            name=name,
            polarity=-1 if refusals.get(index, 0) % 2 else 1,
            strength=_hedged_if_cancelled(
                _hedged_by_suffix(
                    lowered,
                    end,
                    evidence,
                    _strength(
                        lowered,
                        start,
                        floor=mentions[index - 1][1] if index else 0,
                        # Only the last mention reads what trails it; anything
                        # earlier would be claiming a degree word that the next
                        # mention's own backwards search already owns.
                        ceiling=end if index == len(mentions) - 1 else None,
                    ),
                ),
                refusals.get(index, 0),
            ),
            evidence=evidence,
            position=offset + start,
            scope=scope if name in SCOPABLE_TRAITS else None,
        )
        for index, (start, end, name, evidence) in enumerate(mentions)
    ]


def _hedged_by_suffix(lowered: str, end: int, evidence: str, strength: float) -> float:
    """「暗め」 asks for less than 「暗い」, with the degree glued to the word.

    Read only where it touches the mention, because the kana is common and the
    degree is not: 「控えめ」 and 「まとめ」 carry no 「め」 of this kind, and a
    search across the span would find one in both.

    An explicit degree word still wins, the same way it does over litotes in
    :func:`_hedged_if_cancelled` -- this softens ``PLAIN_STRENGTH`` alone, so
    「かなり暗めに」 stays insisted-on and does not read as its own opposite.

    **The model reader does not settle this one.** 「暗めにして。」 returned
    ``1.0, 0.5, 0.5, 1.0, 1.0`` over five runs and eleven readings of the shape
    split six to five, so the hedge is here on the strength of the language
    rather than on an arbitration -- unlike #88 and #89, where every run agreed.
    """

    if strength != PLAIN_STRENGTH or evidence in IMPERATIVE_IN_ME:
        return strength
    return SMALL_STRENGTH if lowered[end:end + len(SUFFIX_HEDGE)] == SUFFIX_HEDGE else strength


def _hedged_if_cancelled(strength: float, refusals: int) -> float:
    """A double negative asks for less than the plain word would.

    Japanese double negation is litotes: 「暗くなくはない」 is *somewhat* dark and
    「スラップ抜きじゃない」 concedes some slap rather than calling for it. Reading
    either as a plain request overshoots -- less far than the refusal this used
    to return, but in the same way, by stating something the brief withheld.

    An explicit degree word still wins. 「かなり」 beside a double negative is a
    person saying how much despite the shape of the sentence, and
    :func:`_strength` only returns ``PLAIN_STRENGTH`` when it found no degree
    word at all, so that is the case to soften.
    """

    if refusals >= 2 and refusals % 2 == 0 and strength == PLAIN_STRENGTH:
        return SMALL_STRENGTH
    return strength


def _first_span(lowered: str, words: Sequence[str]) -> tuple[int, int, str] | None:
    """Where this trait is first mentioned, if it is.

    ASCII words must start a word -- the rule :mod:`.edit` already uses, so
    ``lead`` no longer fires inside ``overloaded``. Japanese has no word
    boundaries, so substring is the only option there.
    """

    best: tuple[int, int, str] | None = None
    for word in words:
        folded = word.casefold()
        if folded.isascii():
            match = re.search(rf"(?<![a-z0-9]){re.escape(folded)}", lowered)
            span = (match.start(), match.end()) if match else None
        else:
            index = lowered.find(folded)
            span = (index, index + len(folded)) if index >= 0 else None
        if span is not None and (best is None or span[0] < best[0]):
            best = (span[0], span[1], word)
    return best


#: What may sit between a mention and a verb that refuses it: **the rest of the
#: word, and then particles**. A mention is the vocabulary's own spelling and a
#: brief writes longer words around it -- 「アルペジ|オ」, 「ダブ|ディレイ」,
#: 「シンセ|リード」, 「ダブ|処理」 -- and that tail carries no particle, because
#: it is the same noun. A particle is what separates one noun from the next, so
#: content appearing *after* one is a different noun and the verb's real object:
#: 「ミニマル|にして無駄を|省いて」.
#:
#: Hence content first, particles after. A gap that puts them the other way
#: round -- any non-kana after a kana -- is the verb talking about something
#: else. The long-vowel mark counts as content, not as a particle: it belongs
#: to 「リード」, and calling it kana split 「シンセリード」 in two.
#:
#: Three kana count as content, because they **join** rather than separate.
#: `の` puts two nouns into one phrase -- 「シンセのリードは省いて」 is about the
#: synth -- and `な` and `い` end an adjective attached to the noun that
#: follows it. Half of this vocabulary is adjectives, and a brief refuses one
#: by describing the thing it does not want: 「暗い曲は避けて」, 「サイケデリック
#: なパッドは避けて」. Calling those endings particles made 曲 and パッド the
#: verb's object and read every one of them as a **request** for the trait.
_PARTICLE_GAP = re.compile(r"^(?:[^\sぁ-ん]|の|な|い)*[\sぁ-ん]*$")


#: Tails a trait word grows while still describing the noun that follows it.
#: `な` and `い` (#88) are the endings a trait word already has; these are the
#: ones a brief adds to it -- 「暗すぎる音」, 「暗そうな曲」, 「暗めの音」,
#: 「ダブっぽい処理」, 「暗くなる音」 -- and each was read as a request for the
#: trait it refuses, because the noun after the tail looked like the verb's
#: own object.
#:
#: **In the attributive form only.** 「ダブっぽい処理は避けて」 refuses `dub`
#: and 「ダブっぽくして低音を足さないで」 asks for it; they differ by the
#: inflection and nothing else, so the tail is listed as 「っぽい」 and not as
#: 「っぽ」. The model reader draws the same line on all eight probed briefs.
#:
#: A list, where #87 and #88 each replaced one. Naming the boundary instead --
#: the case particles and the connectives -- was tried and is the larger,
#: leakier list: it has to hold 「から」「ので」「けど」「し」「なら」, and every one
#: it misses is a **false refusal** (「暗いから低音は足さないで」 asks for dark).
#: Attribution is a closed class in Japanese and separation is not, so the
#: shorter list is the one on the closed side.
#:
#: Longest first where two share a prefix, because the first match wins:
#: 「めの」 stands before 「め」.
_ADJECTIVAL_TAILS = ("すぎる", "すぎた", "そうな", "っぽい", "くなる", "になる", "めの", "め")


def _attaches(lowered: str, end: int, start: int) -> bool:
    """Whether a verb negator at `start` is about the mention ending at `end`.

    What may sit in between: the rest of the word, then particles, with other
    negators anywhere -- 「スラップ抜きじゃない」 needs the last of those.

    The first version allowed hiragana only, which called every compound tail
    a separate noun: 「シンセリードは避けて」, 「アルペジオは省いて」 and
    「ダブディレイも排除して」 all stopped refusing. Listing the vocabulary's own
    words as allowed fixed 「シンセリード」 and neither of the others, because
    「オ」 is not a word and 「ディレイ」 is not in the vocabulary. The boundary
    is the particle, not the script.

    An adjective's ending is not that boundary either. 「暗い曲は避けて」 read
    as a request for `dark`, because 曲 sits after the 「い」 and looked like the
    verb's own object -- the same misreading, one part of speech over, on the
    half of this vocabulary that is adjectives rather than nouns.

    Nor is a tail the brief adds to the word: 「暗すぎる音」, 「暗めの音」,
    「ダブっぽい処理」. Those are stripped from the front of the gap, because a
    tail belongs to the mention it follows -- see :data:`_ADJECTIVAL_TAILS`,
    which also records why the boundary was not named directly instead.
    """

    gap = lowered[end:start]
    for word in JAPANESE_NEGATORS + JAPANESE_VERB_NEGATORS + JAPANESE_SUFFIX_NEGATORS:
        gap = gap.replace(word.casefold(), "")
    for tail in _ADJECTIVAL_TAILS:
        # Anchored to the mention, unlike the negators above: a tail is part of
        # the word it follows, so 「暗|すぎる音」 is one description and the same
        # kana later in the gap belong to somebody else.
        if gap.startswith(tail):
            gap = gap[len(tail):]
            break
    return bool(_PARTICLE_GAP.match(gap))


def _refusals(lowered: str, mentions: Sequence[tuple[int, int, str, str]]) -> dict[int, int]:
    """How many times each mention in this clause is refused.

    The count and not just its parity, because the two carry different things:
    an odd count refuses, and any count of two or more says the brief doubled
    back on itself, which :func:`_hedged_if_cancelled` reads as a hedge.

    Japanese negation attaches to what precedes it, English to what follows, so
    each is resolved to the *nearest* mention in its own direction -- and then
    extended across a list, because ``"スラップとサイケはなし"`` refuses both while
    ``"ミニマルにしてサイケは無し"`` refuses only the second.

    **A second refusal cancels the first.** This used to collect indices into a
    set, so 「スラップ抜きじゃない」 and 「暗くなくはない」 were marked once and read
    as flat refusals of the thing they ask for. Refusals are counted per mention
    now and only an odd count refuses.

    Counting the *matches* would have broken the ordinary case instead: 「ではない」
    is found by both ``ではない`` and ``はない``, and 「スラップではない」 would have
    come out affirmed. So overlapping spans are one refusal, and only spans that
    are genuinely disjoint count twice -- 「暗くなくはない」 is ``くなく`` then
    ``はない``, touching but not overlapping.
    """

    spans: dict[int, list[tuple[int, int]]] = {}

    def refuse(anchor: int, joined: set[int], start: int, end: int) -> None:
        for index in {anchor, *joined}:
            spans.setdefault(index, []).append((start, end))

    for word in JAPANESE_NEGATORS:
        for match in re.finditer(re.escape(word), lowered):
            anchor = _nearest_before(mentions, match.start())
            if anchor is not None:
                joined = _joined_before(lowered, mentions, anchor)
                refuse(anchor, joined, match.start(), match.end())

    for word in JAPANESE_VERB_NEGATORS:
        for match in re.finditer(re.escape(word), lowered):
            anchor = _nearest_before(mentions, match.start())
            if anchor is not None and _attaches(lowered, mentions[anchor][1], match.start()):
                joined = _joined_before(lowered, mentions, anchor)
                refuse(anchor, joined, match.start(), match.end())

    for word in JAPANESE_SUFFIX_NEGATORS:
        for match in re.finditer(re.escape(word), lowered):
            for index, item in enumerate(mentions):
                if match.start() < item[1]:
                    # An overlap, not a suffix: 「せわしなく」 finds ``なく``
                    # inside the mention that ends after it.
                    continue
                if lowered[item[1]:match.start()] in _ADVERBIAL_MARKERS:
                    joined = _joined_before(lowered, mentions, index)
                    refuse(index, joined, match.start(), match.end())

    for word in ENGLISH_NEGATORS:
        for match in re.finditer(rf"(?<![a-z0-9]){re.escape(word)}", lowered):
            anchor = _nearest_after(mentions, match.end())
            if anchor is not None:
                joined = _joined_after(lowered, mentions, anchor)
                refuse(anchor, joined, match.start(), match.end())

    return {index: _distinct(found) for index, found in spans.items()}


def _distinct(spans: Sequence[tuple[int, int]]) -> int:
    """How many separate refusals these matches amount to.

    Overlapping spans are one refusal spelled two ways; disjoint spans are two
    refusals. Touching is not overlapping, which is the whole distinction
    between 「ではない」 (one) and 「なくはない」 (two).
    """

    count = 0
    reach: int | None = None
    for start, end in sorted(spans):
        if reach is None or start >= reach:
            count += 1
            reach = end
        else:
            reach = max(reach, end)
    return count


def _nearest_before(mentions: Sequence[tuple[int, int, str, str]], position: int) -> int | None:
    candidates = [index for index, item in enumerate(mentions) if item[1] <= position]
    return candidates[-1] if candidates else None


def _nearest_after(mentions: Sequence[tuple[int, int, str, str]], position: int) -> int | None:
    for index, item in enumerate(mentions):
        if item[0] >= position:
            return index
    return None


def _joined_before(lowered: str, mentions, anchor: int) -> set[int]:
    joined: set[int] = set()
    index = anchor
    while index > 0 and _JOINERS.match(lowered[mentions[index - 1][1] : mentions[index][0]]):
        index -= 1
        joined.add(index)
    return joined


def _joined_after(lowered: str, mentions, anchor: int) -> set[int]:
    joined: set[int] = set()
    index = anchor
    while index + 1 < len(mentions) and _JOINERS.match(
        lowered[mentions[index][1] : mentions[index + 1][0]]
    ):
        index += 1
        joined.add(index)
    return joined


def _strength(lowered: str, position: int, floor: int = 0, ceiling: int | None = None) -> float:
    """The degree word attached to this mention, if any.

    Both languages put the modifier first (``少しサイケ``, ``slightly
    psychedelic``), so the text before the mention is what is read -- and only
    back to the previous mention, which is what ``floor`` is. That sentence was
    here before the bound was: the search ran to the start of the clause, so
    ``かなり`` in ``"かなりサイケなアルペジオ"`` reached past the psychedelia it
    modifies and made the arpeggio insisted-on too. The docstring's own example
    ``"かなりダブ、サイケも"`` only worked because the comma splits the clause.

    Japanese also hedges *after* the thing, with the noun marked and the degree
    trailing: ``サブベースは少しだけ``. Nothing read that, so a plainly stated
    request came out of a sentence that asked for a little. The trailing text is
    read only for the clause's last mention -- a degree word sitting between two
    mentions belongs to the one that follows it, which the backwards search of
    that next mention already claims.

    Both were found by `agreement.compare_readings`: the model had them right.
    """

    before = lowered[max(0, floor):position]
    large = max((before.rfind(word.casefold()) for word in LARGE_WORDS + INTENT_ONLY_LARGE), default=-1)
    small = max((before.rfind(word.casefold()) for word in SMALL_WORDS + INTENT_ONLY_SMALL), default=-1)
    if large >= 0 or small >= 0:
        return LARGE_STRENGTH if large > small else SMALL_STRENGTH
    if ceiling is None:
        return PLAIN_STRENGTH
    after = lowered[ceiling:]
    large = min(
        (index for index in (after.find(word.casefold()) for word in LARGE_WORDS + INTENT_ONLY_LARGE) if index >= 0),
        default=-1,
    )
    small = min(
        (index for index in (after.find(word.casefold()) for word in SMALL_WORDS + INTENT_ONLY_SMALL) if index >= 0),
        default=-1,
    )
    if large < 0 and small < 0:
        return PLAIN_STRENGTH
    if small < 0:
        return LARGE_STRENGTH
    if large < 0:
        return SMALL_STRENGTH
    return LARGE_STRENGTH if large < small else SMALL_STRENGTH


def blend(low: float, high: float, strength: float) -> float:
    """Interpolate from the refused pole to the requested one.

    ``strength`` 0 gives ``low`` and 1.0 gives ``high`` **exactly**, which is
    the whole reason this is safe to introduce: a brief that states a trait
    plainly still gets the constant that used to be hardcoded for it, so every
    SongSpec written before this module still comes out byte-for-byte the same.
    """

    value = low + (high - low) * max(0.0, strength)
    return round(max(0.0, min(1.0, value)), 6)


def contains(lowered: str, word: str) -> bool:
    """Substring match, but an ASCII word has to start a word.

    Japanese has no word boundaries, so substring is the only option there. A
    bare ASCII substring would match inside unrelated words ("up" in "group"),
    while requiring a boundary at *both* ends would miss ordinary inflection
    ("dense" in "densely"), so only the start is anchored.
    """

    folded = word.casefold()
    if folded.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(folded)}", lowered) is not None
    return folded in lowered


def matches(lowered: str, words: Sequence[str]) -> bool:
    return any(contains(lowered, word) for word in words)
