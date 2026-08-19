"""The 30-brief sweep, kept with the code it audits.

`compare-readings` is how the rule reader gets checked against the model
reader, and it only ever ran over briefs somebody wrote by hand in a session.
Every sweep before this one was written to a scratch directory and vanished
with it, so each round of work on `intent.py` re-paid for a corpus that already
existed -- and the disagreements a change *did not* fix were remembered in prose
or not at all.

Comparison is free and offline: the reading carries its own brief, so nothing
here calls a model or needs a key. Checking the corpus in makes the audit a
test, which is the only form that runs by itself.

**What this pins is a disagreement, not a verdict.** `agreement.py` refuses to
say which reader is right, and so does this: each entry below records that the
two readers still differ and why nobody has closed it. A change that closes one
is supposed to fail here -- delete the entry in the same commit, and say in the
message which reader moved.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from kihachi_music_ai.agreement import (
    AGREE,
    POLARITY,
    RULES_ONLY,
    STRENGTH,
    STRENGTH_ON_A_REFUSAL,
    compare_readings,
)

SWEEP = Path(__file__).resolve().parents[1] / "sweeps" / "2026-08-19"

#: Every disagreement the sweep still holds, and why each one is still open.
#: Eight of the thirty briefs; the other twenty-two agree trait for trait.
KNOWN: dict[tuple[str, str], tuple[str, str]] = {
    ("s01", "synth"): (
        RULES_ONLY,
        "「暗すぎるシンセリードは避けて」: the model says nothing about the lead. "
        "The question of what the brief means was decided on 2026-08-19 -- a "
        "lead is wanted, one that is not too dark -- so the rules now read it "
        "as a plain request rather than a refusal. The disagreement stays "
        "because the model filed no trait at all, and a reader that says "
        "nothing is not the same as one that agrees.",
    ),
    ("s03", "dark"): (
        STRENGTH,
        "「暗めにして」: the 「め」 coin-flip. The same brief read five times came "
        "back 1.0, 0.5, 0.5, 1.0, 1.0, so this is the arbiter's noise and not a "
        "finding to design on.",
    ),
    ("s05", "bright"): (
        STRENGTH,
        "「明るめのシンセで」: the same 「め」 coin-flip, on the other pole.",
    ),
    ("s13", "dark"): (
        POLARITY,
        "「暗さは控えめに」: the model reads it as a refusal at -0.5 when it is a "
        "small *request*. One of the two places in this sweep where the model "
        "is the reader that is wrong.",
    ),
    ("s15", "vocoder"): (
        STRENGTH_ON_A_REFUSAL,
        "「ボコーダーは厳禁で」: model -1.5, rules -1.0. Both refuse it, and a "
        "refusal has one strength -- measured in #86, substituting 1.0 for 1.5 "
        "on every refused trait leaves the SongSpec byte-identical. Pinned "
        "rather than fixed for that reason.",
    ),
    ("s16", "bright"): (
        RULES_ONLY,
        "「暗くなくはない、くらいの明るさで」: the model leaves it unread when 明る "
        "is literally in the brief. The second place the model is wrong.",
    ),
    ("s17", "busy"): (
        POLARITY,
        "「手数は少なくない」: litotes. Known-wrong and pinned since #66 -- the "
        "rules read the double negative as a plain refusal.",
    ),
    ("s19", "arp"): (
        POLARITY,
        "「アルペジは要らないが、シーケンスっぽさは残して」: アルペジ and シーケンス "
        "are both the `arp` trait, so the brief asks one knob to be off and on. "
        "A vocabulary limit, and it belongs in the README's known-limits table "
        "rather than in a fix.",
    ),
}

#: The one brief where the model files a phrase as unsayable that the rules did
#: read. Same cause as ("s19", "arp") -- one trait, two words.
KNOWN_CONTESTED = {"s19": ["シーケンスっぽさは残して"]}


def readings() -> list[tuple[str, dict]]:
    return [
        (path.name, json.loads((path / "intent_reading.json").read_text(encoding="utf-8")))
        for path in sorted((SWEEP / "readings").iterdir())
        if path.is_dir()
    ]


class SweepCorpusTests(unittest.TestCase):
    """The corpus itself: thirty briefs, thirty readings, and what each targets."""

    def test_every_brief_has_the_reading_it_was_read_into(self) -> None:
        lines = [
            line
            for line in (SWEEP / "briefs.tsv").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        stored = readings()

        self.assertEqual(len(lines), 30)
        self.assertEqual(len(stored), 30)
        for line, (name, reading) in zip(lines, stored):
            brief, targets = line.split("\t")
            with self.subTest(name=name):
                self.assertEqual(reading["brief"], brief)
                self.assertTrue(targets.strip(), "a brief with no stated target")

    def test_no_reading_has_drifted_from_the_brief_it_was_read_from(self) -> None:
        """The sha256 the artifact carries, checked here and not only on use.

        `compare_readings` raises on a mismatch, so an edited brief would look
        like a broken comparison rather than a broken corpus.
        """

        for name, reading in readings():
            with self.subTest(name=name):
                self.assertEqual(
                    reading["brief_sha256"],
                    hashlib.sha256(reading["brief"].encode("utf-8")).hexdigest(),
                )


class SweepAgreementTests(unittest.TestCase):
    """What the two readers still disagree about, one entry per disagreement."""

    def test_the_sweep_holds_exactly_the_disagreements_that_are_known(self) -> None:
        found: dict[tuple[str, str], str] = {}
        contested: dict[str, list[str]] = {}
        for name, reading in readings():
            comparison = compare_readings(reading)
            for row in comparison["traits"]:
                if row["status"] != AGREE:
                    found[(name, row["trait"])] = row["status"]
            if comparison["contested_unmapped"]:
                contested[name] = comparison["contested_unmapped"]

        self.assertEqual(
            set(found),
            set(KNOWN),
            "the sweep's disagreements moved: close one by deleting its entry, "
            "and say in the commit message which reader moved",
        )
        for key, status in found.items():
            with self.subTest(key=key):
                self.assertEqual(status, KNOWN[key][0])
        self.assertEqual(contested, KNOWN_CONTESTED)

    def test_the_rest_of_the_sweep_agrees_trait_for_trait(self) -> None:
        """Twenty-two briefs where both readers say the same thing."""

        clean = [
            name
            for name, reading in readings()
            if compare_readings(reading)["disagreements"] == 0
        ]

        self.assertEqual(len(clean), 22)
        self.assertNotIn("s01", clean)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
