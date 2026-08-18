from __future__ import annotations

import unittest

from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.music_brain import MusicBrain

EXAMPLE = (
    "Mutation Funk、DUB、Tech House。110 BPM、D#m。ファンキーなスラップベース。"
    "前半ミニマル、後半サイケデリック。Vocoderを使用。"
)


class MusicBrainTests(unittest.TestCase):
    def test_example_is_interpreted_as_expected(self) -> None:
        spec = MusicBrain(seed=8).analyze(EXAMPLE)
        self.assertEqual(spec.song.title, "Mutation Signal")
        self.assertEqual(spec.song.bpm, 110.0)
        self.assertEqual(spec.song.key, "D# minor")
        self.assertEqual(spec.song.total_bars, 32)
        self.assertEqual(spec.harmony.progression, ("D#m", "B", "F#", "C#"))
        self.assertEqual(spec.bass.technique, "slap")
        self.assertTrue(spec.vocal.vocoder)

    def test_genre_weights_and_section_arc(self) -> None:
        spec = MusicBrain().analyze(EXAMPLE)
        weights = {item.name: item.weight for item in spec.style.genres}
        self.assertEqual(weights, {"mutation_funk": 0.4, "dub": 0.3, "tech_house": 0.3})
        self.assertTrue(all(section.minimal for section in spec.arrangement[:2]))
        self.assertTrue(all(not section.minimal for section in spec.arrangement[2:]))
        self.assertGreater(spec.arrangement[-1].psychedelic, spec.arrangement[0].psychedelic)

    def test_song_spec_round_trip(self) -> None:
        original = MusicBrain().analyze(EXAMPLE)
        restored = SongSpec.from_json(original.to_json())
        self.assertEqual(restored, original)

    def test_duration_request_rounds_to_eight_bar_boundary(self) -> None:
        spec = MusicBrain().analyze("Tech House、120 BPM、A minor、5分")
        self.assertEqual(spec.song.total_bars % 8, 0)
        self.assertGreater(spec.song.total_bars, 100)

    def test_empty_prompt_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MusicBrain().analyze("  ")


class RefusalTests(unittest.TestCase):
    """A refused trait used to be read as a request for it."""

    def test_a_refused_technique_is_not_the_technique_chosen(self) -> None:
        spec = MusicBrain().analyze("Tech House。スラップじゃなくて指弾きで。")

        self.assertEqual(spec.bass.technique, "fingered")

    def test_refusing_slap_lands_on_every_value_slap_would_have_raised(self) -> None:
        refused = MusicBrain().analyze("Tech House。スラップじゃなくて指弾きで。")
        silent = MusicBrain().analyze("Tech House。")

        self.assertEqual(refused.bass.syncopation, silent.bass.syncopation)
        self.assertEqual(refused.bass.ghost_note_probability, silent.bass.ghost_note_probability)
        self.assertEqual(
            refused.bass.octave_jump_probability, silent.bass.octave_jump_probability
        )
        self.assertEqual(refused.groove.syncopation, silent.groove.syncopation)

    def test_a_refused_part_is_not_written(self) -> None:
        spec = MusicBrain().analyze("Tech House。アルペジオは無しで。")

        self.assertIsNone(spec.instruments)

    def test_a_refused_vocoder_leaves_the_song_instrumental(self) -> None:
        spec = MusicBrain().analyze("Tech House。vocoderなしで。")

        self.assertFalse(spec.vocal.enabled)
        self.assertFalse(spec.vocal.vocoder)


class DegreeTests(unittest.TestCase):
    """"少し" and "かなり" now land somewhere, and a plain mention lands where it did."""

    def test_a_plain_mention_still_gives_the_old_constant(self) -> None:
        spec = MusicBrain().analyze("Tech House。サイケに。")

        self.assertEqual(spec.style.psychedelic, 0.82)

    def test_hedging_lands_below_a_plain_mention(self) -> None:
        hedged = MusicBrain().analyze("Tech House。少しサイケ。")
        plain = MusicBrain().analyze("Tech House。サイケに。")
        silent = MusicBrain().analyze("Tech House。")

        self.assertLess(hedged.style.psychedelic, plain.style.psychedelic)
        self.assertGreater(hedged.style.psychedelic, silent.style.psychedelic)

    def test_insisting_lands_above_it(self) -> None:
        insisted = MusicBrain().analyze("Tech House。かなりサイケ。")
        plain = MusicBrain().analyze("Tech House。サイケに。")

        self.assertGreater(insisted.style.psychedelic, plain.style.psychedelic)
        self.assertLessEqual(insisted.style.psychedelic, 1.0)

    def test_degree_does_not_leak_into_an_unmodified_brief(self) -> None:
        """The pinned brief hedges nothing, so this whole layer is a no-op on it."""

        spec = MusicBrain(seed=8).analyze(EXAMPLE)

        self.assertEqual(spec.bass.mutation, 0.78)
        self.assertEqual(spec.bass.syncopation, 0.86)
        self.assertEqual(spec.groove.syncopation, 0.82)
        self.assertEqual(spec.style.psychedelic, 0.82)
        self.assertEqual(spec.style.darkness, 0.72)
        self.assertEqual(spec.drums.dub_space, 0.62)
        self.assertEqual(spec.chords.dub_delay, 0.74)


class StatedDarknessTests(unittest.TestCase):
    """`style.darkness` was reachable only through the genre until 2026-08-17."""

    @staticmethod
    def darkness(prompt: str) -> float:
        return MusicBrain(seed=1).analyze(prompt).style.darkness

    def test_a_brief_that_says_nothing_keeps_the_genre_reading(self) -> None:
        """Ambient's tags are entirely bright, and it now reads as bright.

        It read 0.48 -- the neutral default -- until 2026-08-18, because the
        call site wrote `db_darkness or 0.48` and ambient's reading was
        exactly 0.0. Every one of the 253 rows at that end was silently
        neutral; `mood_axes` distinguishes None from 0.0 precisely so the
        caller does not have to guess, and `or` threw that away.
        """

        self.assertEqual(self.darkness("アンビエント。"), 0.333)

    def test_saying_it_moves_it_and_the_degree_decides_how_far(self) -> None:
        self.assertEqual(self.darkness("少し暗いアンビエント。"), 0.522)
        self.assertEqual(self.darkness("暗いアンビエント。"), 0.711)
        self.assertEqual(self.darkness("かなり暗いアンビエント。"), 0.9)

    def test_brightness_is_its_own_trait_and_moves_the_other_way(self) -> None:
        self.assertEqual(self.darkness("明るいアンビエント。"), 0.177667)
        self.assertEqual(self.darkness("かなり明るいアンビエント。"), 0.1)

    def test_agreeing_with_a_dark_genre_still_moves_it_further(self) -> None:
        """This was the pole problem, and now there is no pole to be at.

        Techno read 1.0 -- one dark tag out of three, which the old ratio
        could not tell from three of three -- so 「暗いテクノ」 had nowhere to
        go and the first draft of `_stated_axis` answered it *below* plain
        「テクノ」. The share of the row's vocabulary puts techno at 0.667, and
        agreeing with it is an increase again.
        """

        plain = self.darkness("テクノ。")
        stated = self.darkness("暗いテクノ。")

        self.assertEqual(plain, 0.667)
        self.assertEqual(stated, 0.822333)
        self.assertGreater(stated, plain)
        self.assertGreater(self.darkness("かなり暗いテクノ。"), stated)

    def test_nothing_reaches_either_pole_on_its_tags_alone(self) -> None:
        """Only a row whose every tag takes one side can, and none does.

        204 rows sat at 1.0 on a single word before this -- Big Band among
        them, as dark as anything in the database, from `nocturnal`.
        """

        from kihachi_music_ai.genres import load_database, mood_axes

        readings = [mood_axes([(g.slug, 1.0)])[0] for g in load_database()]
        stated = [value for value in readings if value is not None]

        self.assertEqual(len(stated), 608)
        self.assertEqual([value for value in stated if value in (0.0, 1.0)], [])
        self.assertEqual(max(stated), 0.833)
        self.assertEqual(min(stated), 0.167)

    def test_refusing_darkness_is_not_asking_for_brightness(self) -> None:
        """The genre's own reading beats either pole when the brief only says no."""

        self.assertEqual(self.darkness("暗くないテクノ。"), 0.667)
        self.assertEqual(self.darkness("明るくないアンビエント。"), 0.333)

    def test_swing_was_reachable_by_one_genre_of_a_thousand(self) -> None:
        """`groove.swing` drives the composer's timing, so this reaches the MIDI.

        Only `mutation_funk` ever set it. Every family including Jazz left it at
        0.5, so 「シャッフルで」 and 「ジャズ」 both composed straight eighths.
        """

        def swing(prompt: str) -> float:
            return MusicBrain(seed=1).analyze(prompt).groove.swing

        self.assertEqual(swing("テクノ。"), 0.5)
        self.assertEqual(swing("少し跳ねるテクノ。"), 0.553333)
        self.assertEqual(swing("シャッフルで、テクノ。"), 0.606667)
        self.assertEqual(swing("かなりスウィングさせて、テクノ。"), 0.66)
        self.assertEqual(swing("スウィングしないテクノ。"), 0.5)
        self.assertEqual(swing("ストレートなテクノ。"), 0.5)

    def test_syncopation_was_reachable_by_no_genre_at_all(self) -> None:
        """Swing had one genre of a thousand; this had none.

        `derive.Profile` has no syncopation field, so all 1021 genres leave both
        values at the constant, and the only thing that ever moved them was the
        `slap` trait. Both reach the composed notes.
        """

        def groove(prompt: str) -> float:
            return MusicBrain(seed=1).analyze(prompt).groove.syncopation

        self.assertEqual(groove("テクノ。"), 0.58)
        self.assertEqual(groove("少しだけシンコペを効かせたテクノ。"), 0.68)
        self.assertEqual(groove("シンコペを効かせたテクノ。"), 0.78)
        self.assertEqual(groove("かなりうねるテクノ。"), 0.88)
        self.assertEqual(groove("オンビートで、テクノ。"), 0.326667)
        self.assertEqual(groove("かなり表打ちのテクノ。"), 0.2)
        # A refusal is not a request for the other pole, here as everywhere.
        self.assertEqual(groove("シンコペ無しのテクノ。"), 0.58)

    def test_the_bass_hears_the_same_word_as_the_groove(self) -> None:
        """`bass.syncopation` is the twin field, and slap starts it higher."""

        spec = MusicBrain(seed=1).analyze("スラップベースのファンク。オンビートで。")
        self.assertEqual(spec.groove.syncopation, 0.406667)
        self.assertEqual(spec.bass.syncopation, 0.42)

    def test_humanize_moves_from_whatever_the_family_stated(self) -> None:
        """Every family states this one, and no brief could disagree with it.

        Hardcore Electronic sits at 0.04 and Jazz at 0.45, so the loose pole is
        above all of them: a brief that agrees with Jazz still has somewhere to
        go, and the tight pole is 0.02 rather than 0.0 because a quantiser is
        not a preference.
        """

        def humanize(prompt: str) -> float:
            return MusicBrain(seed=1).analyze(prompt).groove.humanize

        self.assertEqual(humanize("テクノ。"), 0.06)
        self.assertEqual(humanize("少しヨレたテクノ。"), 0.273333)
        self.assertEqual(humanize("手弾きっぽいテクノ。"), 0.486667)
        self.assertEqual(humanize("ジャズ。"), 0.45)
        self.assertEqual(humanize("かなり人間っぽいジャズ。"), 0.7)
        self.assertEqual(humanize("タイトなジャズ。"), 0.163333)
        self.assertEqual(humanize("かっちりしたテクノ。"), 0.033333)
        self.assertEqual(humanize("ヨレないテクノ。"), 0.06)

    def test_drum_density_is_sayable_and_minimal_is_a_different_word(self) -> None:
        def kit(prompt: str) -> tuple[float, float]:
            drums = MusicBrain(seed=1).analyze(prompt).drums
            return drums.kick_density, drums.hat_density

        self.assertEqual(kit("テクノ。"), (0.85, 0.92))
        self.assertEqual(kit("少しスカスカなテクノ。"), (0.666667, 0.713333))
        self.assertEqual(kit("かなり余白のあるダブ。"), (0.3, 0.3))
        self.assertEqual(kit("ダブ。"), (0.38, 0.45))
        # `minimal` gates the arrangement's opening sections and nothing here.
        self.assertEqual(kit("ミニマルなテクノ。"), (0.85, 0.92))
        self.assertEqual(kit("スカスカじゃないテクノ。"), (0.85, 0.92))

    def test_asking_a_saturated_kit_for_more_changes_the_number_and_not_the_notes(self) -> None:
        """The honest half of this: upwards, the pattern hits its own ceiling.

        Techno already sits at 0.85/0.92 and `build_pattern` is already at
        `groove.kick_steps[1]`, so 「かなり手数の多いテクノ」 raises the spec to
        0.95 and composes the same 381 drum notes. Downwards it works from the
        same starting point, and upwards it works from a genre with room.
        """

        from kihachi_music_ai.composer import COMPOSERS

        def drums(prompt: str) -> int:
            spec = MusicBrain(seed=8).analyze(prompt)
            return len(COMPOSERS["drums"](spec))

        self.assertEqual(drums("テクノ。8小節。"), 381)
        self.assertEqual(drums("かなり手数の多いテクノ。8小節。"), 381)
        self.assertEqual(drums("スカスカなテクノ。8小節。"), 320)
        self.assertEqual(drums("ダブ。8小節。"), 240)
        self.assertEqual(drums("手数の多いダブ。8小節。"), 272)

    def test_harmonic_rhythm_steps_a_ladder_instead_of_blending(self) -> None:
        """The first stated field that is an integer, so degrees work differently.

        There is no value between one bar per chord and two, so a hedge and a
        plain statement both move one rung and only insistence goes to the end.
        """

        def bars(prompt: str) -> int:
            return MusicBrain(seed=1).analyze(prompt).harmony.harmonic_rhythm_bars

        self.assertEqual(bars("テクノ。"), 2)
        self.assertEqual(bars("展開が速いテクノ。"), 1)
        self.assertEqual(bars("少し展開が速いテクノ。"), 1)
        self.assertEqual(bars("ワンコードのテクノ。"), 4)
        # Drum & bass starts at 4, so one rung down is 2 rather than 1.
        self.assertEqual(bars("ドラムンベース。"), 4)
        self.assertEqual(bars("展開が速いドラムンベース。"), 2)
        self.assertEqual(bars("かなり目まぐるしく変わるドラムンベース。"), 1)
        self.assertEqual(bars("ワンコードじゃないテクノ。"), 2)

    def test_harmonic_rhythm_reaches_every_pitched_part_and_no_drum(self) -> None:
        from kihachi_music_ai.composer import COMPOSERS

        base = MusicBrain(seed=8).analyze("テクノ。8小節。")
        fast = MusicBrain(seed=8).analyze("展開が速いテクノ。8小節。")
        for part in ("bass", "chords", "synth", "arp", "vocoder"):
            with self.subTest(part=part):
                before, after = COMPOSERS[part](base), COMPOSERS[part](fast)
                changed = sum(1 for x, y in zip(before, after) if x.pitch != y.pitch)
                self.assertEqual(changed, len(before) // 2)
        # The sub follows the chord itself, so it gains notes rather than moving.
        self.assertEqual(len(COMPOSERS["sub"](base)), 16)
        self.assertEqual(len(COMPOSERS["sub"](fast)), 32)
        self.assertEqual(COMPOSERS["drums"](base), COMPOSERS["drums"](fast))

    def test_note_length_is_the_one_trait_with_no_number_waiting_for_it(self) -> None:
        def held(prompt: str) -> float:
            return MusicBrain(seed=1).analyze(prompt).groove.note_length

        self.assertEqual(held("テクノ。"), 1.0)
        self.assertEqual(held("少しスタッカート気味のテクノ。"), 0.816667)
        self.assertEqual(held("歯切れのいいテクノ。"), 0.633333)
        self.assertEqual(held("繋げて弾くジャズ。"), 1.4)
        self.assertEqual(held("かなりレガートなアンビエント。"), 1.6)
        self.assertEqual(held("歯切れよくないテクノ。"), 1.0)

    def test_note_length_scales_the_durations_and_legato_stops_at_the_next_note(self) -> None:
        from kihachi_music_ai.composer import COMPOSERS

        base = MusicBrain(seed=8).analyze("テクノ。8小節。")
        short = MusicBrain(seed=8).analyze("歯切れのいいテクノ。8小節。")
        long_ = MusicBrain(seed=8).analyze("かなりレガートなテクノ。8小節。")
        for part in ("bass", "chords", "arp"):
            with self.subTest(part=part):
                written = COMPOSERS[part](base)
                clipped = COMPOSERS[part](short)
                self.assertEqual(len(written), len(clipped))
                for before, after in zip(written, clipped):
                    self.assertAlmostEqual(after.duration_beats, before.duration_beats * 0.633333)
        # The arp's notes are close enough together that 1.6x would overlap, so
        # the cap bites: every held note stops at the next one rather than past.
        held = COMPOSERS["arp"](long_)
        starts = sorted({note.start_beats for note in held})
        for note in held:
            later = [start for start in starts if start > note.start_beats]
            if later:
                self.assertLessEqual(note.start_beats + note.duration_beats, later[0] + 1e-9)

    def test_a_spec_that_never_asked_still_serialises_without_the_field(self) -> None:
        """1.0 is what every part was always written with, so it is not news."""

        spec = MusicBrain(seed=1).analyze("テクノ。")
        self.assertNotIn("note_length", spec.to_dict()["groove"])
        stated = MusicBrain(seed=1).analyze("歯切れのいいテクノ。")
        self.assertEqual(stated.to_dict()["groove"]["note_length"], 0.633333)

    def test_contrast_is_a_relation_between_sections_not_a_value_in_one(self) -> None:
        """The archetypes chose the shape; this says how far to commit to it."""

        def energies(prompt: str) -> tuple[float, ...]:
            spec = MusicBrain(seed=8).analyze(prompt)
            return tuple(section.energy for section in spec.arrangement)

        self.assertEqual(energies("テクノ。32小節。"), (0.25, 0.44, 0.66, 0.88))
        self.assertEqual(energies("メリハリのあるテクノ。32小節。"), (0.09625, 0.38125, 0.71125, 1.0))
        self.assertEqual(energies("淡々としたテクノ。32小節。"), (0.4345, 0.5105, 0.5985, 0.6865))
        # 「かなり淡々と」 is meant literally: every section on the same level.
        self.assertEqual(len(set(energies("かなり淡々としたテクノ。32小節。"))), 1)
        self.assertEqual(energies("メリハリの無いテクノ。32小節。"), (0.25, 0.44, 0.66, 0.88))

    def test_contrast_reaches_the_note_counts_and_saturates_upwards(self) -> None:
        from kihachi_music_ai.composer import COMPOSERS

        def per_section(prompt: str) -> list[int]:
            spec = MusicBrain(seed=8).analyze(prompt)
            notes = COMPOSERS["drums"](spec) + COMPOSERS["bass"](spec) + COMPOSERS["chords"](spec)
            counts = []
            for section in spec.arrangement:
                low = section.start_bar * 4
                high = low + section.length_bars * 4
                counts.append(sum(1 for note in notes if low <= note.start_beats < high))
            return counts

        plain = per_section("テクノ。32小節。")
        self.assertEqual(plain, [130, 167, 184, 196])
        self.assertEqual(per_section("メリハリのあるテクノ。32小節。"), [114, 167, 184, 223])
        self.assertEqual(per_section("かなり淡々としたテクノ。32小節。"), [178, 175, 175, 174])
        # Insisting does not widen it further: the outer sections are already
        # against 0 and 1, so the extra factor has nowhere to go.
        insisted = per_section("かなりメリハリのあるテクノ。32小節。")
        self.assertEqual(max(insisted) - min(insisted), 109)

    def test_a_scoped_statement_reaches_only_its_half(self) -> None:
        from kihachi_music_ai.composer import COMPOSERS

        def per_section(prompt: str) -> list[int]:
            spec = MusicBrain(seed=8).analyze(prompt)
            notes = COMPOSERS["drums"](spec) + COMPOSERS["bass"](spec) + COMPOSERS["chords"](spec)
            counts = []
            for section in spec.arrangement:
                low = section.start_bar * 4
                high = low + section.length_bars * 4
                counts.append(sum(1 for note in notes if low <= note.start_beats < high))
            return counts

        self.assertEqual(per_section("テクノ。32小節。"), [130, 167, 184, 196])
        self.assertEqual(
            per_section("前半は淡々と、後半は手数を多く。テクノ。32小節。"), [130, 159, 224, 223]
        )
        self.assertEqual(per_section("終盤だけスカスカに。テクノ。32小節。"), [130, 167, 151, 158])

    def test_a_scoped_statement_leaves_the_song_wide_number_alone(self) -> None:
        """`drums.kick_density` is the kit for the whole song. Raising it in the
        second half would raise it in the first, so a scoped brief must not."""

        scoped = MusicBrain(seed=8).analyze("後半は手数を多く。テクノ。32小節。")
        self.assertEqual(scoped.drums.kick_density, 0.85)
        self.assertEqual(scoped.drums.hat_density, 0.92)
        # Unscoped, the same words move the kit exactly as they did before.
        plain = MusicBrain(seed=8).analyze("手数の多いテクノ。32小節。")
        self.assertEqual(plain.drums.kick_density, 0.916667)

    def test_the_brief_the_coverage_module_opens_with_now_moves(self) -> None:
        ambient = (
            "アンビエント。110 BPM、D#m。2分程度。きらびやかで高域中心、繊細。"
            "ベースは控えめで薄い。パーカッションは軽く、シェイカーとハイハット中心。"
        )

        self.assertEqual(self.darkness(ambient), 0.177667)


if __name__ == "__main__":
    unittest.main()

