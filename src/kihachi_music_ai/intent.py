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
JAPANESE_NEGATORS = ("じゃなく", "ではなく", "じゃない", "ではない", "無し", "なし", "抜き", "禁止", "不要", "いらな", "要らな", "使わな", "くない", "くなく", "くありません", "の無い", "のない", "が無い", "がない", "は無い", "はない", "すぎない", "過ぎない", "すぎず", "過ぎず")
ENGLISH_NEGATORS = ("without", "not ", "no ", "never", "avoid", "minus", "sans")

#: Negations that count **only when they touch the mention they follow**.
#: A verb trait negates by simply appending: ``跳ね`` + ``ない``. The list above
#: cannot carry a bare ``ない``, because those entries attach to the nearest
#: mention anywhere earlier in the clause, and 「サイケで切ないやつ」 would then
#: refuse the psychedelia on the strength of an unrelated adjective. Requiring
#: adjacency is what makes the bare form safe: in that phrase the ``ない`` is
#: four characters away from ``サイケ`` and means nothing to it.
JAPANESE_SUFFIX_NEGATORS = (
    "しない", "しなく", "しません", "させない", "せず", "ない", "ず",
)

#: Clause boundaries. Negation does not reach across one, which is what keeps
#: ``"スラップじゃなくて指弾き。サイケに。"`` from turning the whole brief off.
_CLAUSE_SPLIT = re.compile(r"[、。，．,.;；\n\r]+")

#: Text that joins two mentions into one list, so a single negator covers both:
#: ``"スラップとサイケはなし"``. Anything else between them (``"にしてサイケは無し"``)
#: means they are separate statements and only the nearest one is refused.
_JOINERS = re.compile(r"^[\sとやおよびまたは・/／&＆+＋and or,]*$", re.IGNORECASE)

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


@dataclass(frozen=True)
class Traits:
    """Everything one brief stated. Absent is a real answer, and it is zero."""

    traits: tuple[Trait, ...] = ()

    def find(self, name: str) -> Trait | None:
        for trait in self.traits:
            if trait.name == name:
                return trait
        return None

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

    negated = _negated(lowered, mentions)
    return [
        Trait(
            name=name,
            polarity=-1 if index in negated else 1,
            strength=_strength(
                lowered,
                start,
                floor=mentions[index - 1][1] if index else 0,
                # Only the last mention reads what trails it; anything earlier
                # would be claiming a degree word that the next mention's own
                # backwards search already owns.
                ceiling=end if index == len(mentions) - 1 else None,
            ),
            evidence=evidence,
            position=offset + start,
        )
        for index, (start, end, name, evidence) in enumerate(mentions)
    ]


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


def _negated(lowered: str, mentions: Sequence[tuple[int, int, str, str]]) -> set[int]:
    """Which mentions in this clause are being refused.

    Japanese negation attaches to what precedes it, English to what follows, so
    each is resolved to the *nearest* mention in its own direction -- and then
    extended across a list, because ``"スラップとサイケはなし"`` refuses both while
    ``"ミニマルにしてサイケは無し"`` refuses only the second.
    """

    negated: set[int] = set()

    for word in JAPANESE_NEGATORS:
        for match in re.finditer(re.escape(word), lowered):
            anchor = _nearest_before(mentions, match.start())
            if anchor is not None:
                negated.add(anchor)
                negated.update(_joined_before(lowered, mentions, anchor))

    for word in JAPANESE_SUFFIX_NEGATORS:
        for match in re.finditer(re.escape(word), lowered):
            for index, item in enumerate(mentions):
                if item[1] == match.start():
                    negated.add(index)
                    negated.update(_joined_before(lowered, mentions, index))

    for word in ENGLISH_NEGATORS:
        for match in re.finditer(rf"(?<![a-z0-9]){re.escape(word)}", lowered):
            anchor = _nearest_after(mentions, match.end())
            if anchor is not None:
                negated.add(anchor)
                negated.update(_joined_after(lowered, mentions, anchor))

    return negated


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
