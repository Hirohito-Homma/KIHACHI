from __future__ import annotations

import unittest

from kihachi_music_ai.derive import FAMILY_PROFILES, Profile, pick, pick_str, profile_for
from kihachi_music_ai.music_brain import MusicBrain


class ProfileSelectionTests(unittest.TestCase):
    def test_the_heaviest_genre_decides_the_family(self) -> None:
        dub_led = profile_for([("dub", 0.6), ("deep_house", 0.4)])
        house_led = profile_for([("dub", 0.4), ("deep_house", 0.6)])

        self.assertEqual(dub_led.drum_pattern, "one_drop")
        self.assertEqual(house_led.drum_pattern, "four_on_floor")

    def test_a_genres_own_opinion_wins_over_its_familys(self) -> None:
        # Tech house sits in the House family, whose pattern is four-on-the-floor.
        self.assertEqual(profile_for([("tech_house", 1.0)]).drum_pattern,
                         "syncopated_tech_house")

    def test_a_genres_own_opinion_is_heard_even_when_it_does_not_lead(self) -> None:
        # This is the seed prompt's shape, and it is why the tech house pattern
        # survived moving out of ``MusicBrain`` and into ``GENRE_PROFILES``.
        profile = profile_for([("dub", 0.6), ("tech_house", 0.4)])

        self.assertEqual(profile.drum_pattern, "syncopated_tech_house")
        # ...and the leading genre still decides everything it did not claim.
        self.assertEqual(profile.kick_density, FAMILY_PROFILES["Reggae / Dub / Ska"].kick_density)

    def test_the_heaviest_genre_settles_a_disagreement_between_two_opinions(self) -> None:
        heavier = profile_for([("mutation_funk", 0.6), ("tech_house", 0.4)])

        self.assertEqual(heavier.swing, 0.54)
        self.assertEqual(heavier.drum_pattern, "syncopated_tech_house")

    def test_a_family_with_no_row_has_no_opinion(self) -> None:
        self.assertEqual(profile_for([("electronic", 1.0)]), Profile())

    def test_no_opinion_means_the_caller_keeps_its_constant(self) -> None:
        self.assertEqual(pick(None, 0.72), 0.72)
        self.assertEqual(pick(0.38, 0.72), 0.38)
        self.assertEqual(pick_str(None, "four_on_floor"), "four_on_floor")

    def test_an_unrecognised_genre_falls_through_to_the_defaults(self) -> None:
        self.assertEqual(profile_for([("no_such_genre", 1.0)]), Profile())

    def test_every_declared_role_is_one_the_audio_prompt_weighs(self) -> None:
        """`prompt_compiler._role_weight` silently reads anything else as 0.5."""

        roles = {p.bass_role for p in FAMILY_PROFILES.values() if p.bass_role}
        self.assertLessEqual(roles, {"supporting", "present", "dominant"})


class ThawTests(unittest.TestCase):
    """These six values used to be the same for all 1020 genres."""

    def test_two_genres_no_longer_share_one_kick_density(self) -> None:
        dub = MusicBrain().analyze("ダブ。")
        dnb = MusicBrain().analyze("ドラムンベース。")
        ambient = MusicBrain().analyze("アンビエント。")

        self.assertNotEqual(dub.drums.kick_density, dnb.drums.kick_density)
        self.assertLess(ambient.drums.kick_density, dub.drums.kick_density)

    def test_a_hand_played_family_is_looser_than_a_machine_one(self) -> None:
        jazz = MusicBrain().analyze("ジャズ。")
        techno = MusicBrain().analyze("テクノ。")

        self.assertGreater(jazz.groove.humanize, techno.groove.humanize)

    def test_harmonic_rhythm_follows_the_family(self) -> None:
        ambient = MusicBrain().analyze("アンビエント。")
        house = MusicBrain().analyze("ハウス。")

        self.assertGreater(
            ambient.harmony.harmonic_rhythm_bars, house.harmony.harmonic_rhythm_bars
        )

    def test_tech_house_keeps_its_own_pattern_name(self) -> None:
        """The one genre that already had an answer is not overwritten by a family."""

        spec = MusicBrain().analyze("Tech House。")

        self.assertEqual(spec.drums.pattern, "syncopated_tech_house")

    def test_an_unlisted_family_still_gets_the_original_constants(self) -> None:
        spec = MusicBrain().analyze("何かよく分からない音楽。120 BPM。")

        self.assertEqual(spec.drums.kick_density, 0.72)
        self.assertEqual(spec.groove.humanize, 0.18)
        self.assertEqual(spec.harmony.harmonic_rhythm_bars, 1)
        self.assertEqual(spec.bass.role, "dominant")
        self.assertEqual(spec.chords.articulation, "short_offbeat_stabs")
        self.assertEqual(spec.drums.pattern, "four_on_floor")


if __name__ == "__main__":
    unittest.main()
