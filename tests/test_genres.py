from __future__ import annotations

import unittest

from kihachi_music_ai.ableton import _live_instrument_genre
from kihachi_music_ai.genres import find, load_database, match_genres
from kihachi_music_ai.music_brain import MusicBrain

SEED = "Mutation Funk、DUB、Tech House。110 BPM、D#m。ファンキーなスラップベース。"


class DatabaseTests(unittest.TestCase):
    def test_the_database_loads_and_is_not_trivially_small(self) -> None:
        genres = load_database()

        self.assertGreater(len(genres), 900)
        self.assertTrue(all(g.slug and g.name for g in genres))

    def test_slugs_are_unique_enough_to_address_a_genre(self) -> None:
        slugs = [g.slug for g in load_database()]

        self.assertEqual(len(slugs), len(set(slugs)))


class RecognitionTests(unittest.TestCase):
    def _slugs(self, text: str) -> list[str]:
        return [m.genre.slug for m in match_genres(text)]

    def test_latin_names_are_found_in_prompt_order(self) -> None:
        self.assertEqual(
            self._slugs("Mutation Funk、DUB、Tech House。"),
            ["mutation_funk", "dub", "tech_house"],
        )

    def test_katakana_names_are_found(self) -> None:
        self.assertEqual(self._slugs("ボサノヴァ。"), ["bossa_nova"])
        self.assertEqual(self._slugs("ドラムンベース。"), ["drum_bass"])

    def test_the_longest_name_wins_over_the_one_inside_it(self) -> None:
        # "Tech House" must not also report plain "House".
        self.assertEqual(self._slugs("Tech House"), ["tech_house"])
        self.assertEqual(self._slugs("Dubstep"), ["dubstep"])
        self.assertEqual(self._slugs("アシッドハウス"), ["acid_house"])

    def test_a_short_name_still_matches_on_its_own(self) -> None:
        self.assertEqual(self._slugs("Dub"), ["dub"])

    def test_a_genre_inside_an_unrelated_japanese_word_is_not_matched(self) -> None:
        # ラップ (rap) sits inside スラップベース (slap bass). Matching it turned a
        # bassline description into a hip-hop request.
        self.assertEqual(self._slugs("ファンキーなスラップベース"), [])

    def test_a_family_never_wins_over_the_style_inside_it(self) -> None:
        # "ダブ" is claimed by both the Reggae / Dub / Ska family and Dub itself;
        # the family would have produced a slug the dub send does not know.
        self.assertEqual(self._slugs("ダブ"), ["dub"])
        self.assertEqual(self._slugs("ドラムンベース"), ["drum_bass"])

    def test_an_unrecognised_prompt_matches_nothing(self) -> None:
        self.assertEqual(self._slugs("何も書いていない"), [])


class MusicBrainCompatibilityTests(unittest.TestCase):
    """The database swap must not move the prompt the system was built around."""

    def setUp(self) -> None:
        self.spec = MusicBrain(seed=8).analyze(SEED)

    def test_the_seed_prompt_yields_the_same_genres_and_weights(self) -> None:
        self.assertEqual(
            [(g.name, g.weight) for g in self.spec.style.genres],
            [("mutation_funk", 0.4), ("dub", 0.3), ("tech_house", 0.3)],
        )

    def test_the_decisions_keyed_on_those_names_still_fire(self) -> None:
        self.assertEqual(self.spec.groove.swing, 0.54)
        self.assertEqual(self.spec.drums.pattern, "syncopated_tech_house")

    def test_the_seed_prompt_still_maps_to_edm(self) -> None:
        self.assertEqual(_live_instrument_genre(self.spec), "edm")

    def test_an_unrecognised_prompt_still_falls_back_to_electronic(self) -> None:
        spec = MusicBrain(seed=8).analyze("何も書いていない。110 BPM、Am。")

        self.assertEqual([g.name for g in spec.style.genres], ["electronic"])

    def test_a_prompt_cannot_be_diluted_by_unlimited_genres(self) -> None:
        spec = MusicBrain(seed=8).analyze(
            "Techno, House, Jazz, Funk, Disco, Grime, Dubstep, Samba。110 BPM、Am。"
        )

        self.assertLessEqual(len(spec.style.genres), MusicBrain.MAX_GENRES)
        self.assertAlmostEqual(sum(g.weight for g in spec.style.genres), 1.0, places=6)


class LiveBucketTests(unittest.TestCase):
    def _bucket(self, prompt: str) -> str:
        return _live_instrument_genre(MusicBrain(seed=8).analyze(prompt + "。110 BPM、Am。"))

    def test_the_family_fallback_replaces_the_blind_pop_default(self) -> None:
        # Each of these matches no keyword. Before the database they arrived as
        # "electronic" and hit the "electro" keyword by accident.
        self.assertEqual(self._bucket("ドラムンベース"), "edm")
        self.assertEqual(self._bucket("シューゲイザー"), "rock")
        self.assertEqual(self._bucket("アンビエント"), "lofi")
        self.assertEqual(self._bucket("ブルーグラス"), "rock")

    def test_keywords_still_decide_every_case_they_used_to(self) -> None:
        self.assertEqual(self._bucket("Deep House"), "edm")
        self.assertEqual(self._bucket("ジャズ"), "jazz")
        self.assertEqual(self._bucket("ヒップホップ"), "hiphop")

    def test_a_family_with_no_honest_bucket_falls_through_to_pop(self) -> None:
        # Brazilian and East Asian have no home among AbletonGPT's seven, and
        # guessing one would be worse than admitting it.
        self.assertEqual(self._bucket("ボサノヴァ"), "pop")
        self.assertEqual(self._bucket("演歌"), "pop")

    def test_every_mapped_family_exists_in_the_database(self) -> None:
        from kihachi_music_ai.ableton import LIVE_GENRE_BY_FAMILY
        from kihachi_music_ai.genres import unknown_families

        self.assertEqual(unknown_families(LIVE_GENRE_BY_FAMILY), ())

    def test_a_family_named_outright_reaches_its_bucket(self) -> None:
        # A top-level row's ``parent`` column is empty, so reading it directly
        # sent the plainest possible prompt to the ``pop`` default.
        self.assertEqual(self._bucket("ディスコ"), "rnb")
        self.assertEqual(self._bucket("Breakbeat"), "edm")

    def test_every_mapped_bucket_is_one_abletongpt_accepts(self) -> None:
        from kihachi_music_ai.ableton import LIVE_GENRE_BY_FAMILY, LIVE_GENRE_KEYWORDS

        self.assertEqual(
            set(LIVE_GENRE_BY_FAMILY.values()) - set(LIVE_GENRE_KEYWORDS), set()
        )


class VocabularyTests(unittest.TestCase):
    """One vocabulary, spelled by the database, wherever a genre is named.

    Every table below repeats a database string as a dict key. A rename in
    ``genres.json`` would not break any of them loudly -- the lookup would
    simply start missing and the caller would keep its default forever. These
    tests are what makes that rename loud.
    """

    def test_the_family_list_comes_from_the_database(self) -> None:
        from kihachi_music_ai.genres import families

        self.assertEqual(len(families()), 37)
        self.assertIn("Reggae / Dub / Ska", families())

    def test_a_top_level_row_is_its_own_family(self) -> None:
        from kihachi_music_ai.genres import family_of

        self.assertEqual(family_of("tech_house"), "House")
        self.assertEqual(family_of("house"), "House")
        self.assertIsNone(family_of("electronic"))

    def test_every_profiled_family_exists_in_the_database(self) -> None:
        from kihachi_music_ai.derive import FAMILY_PROFILES
        from kihachi_music_ai.genres import unknown_families

        self.assertEqual(unknown_families(FAMILY_PROFILES), ())

    def test_every_per_genre_profile_names_a_real_genre(self) -> None:
        from kihachi_music_ai.derive import GENRE_PROFILES

        for slug in GENRE_PROFILES:
            with self.subTest(slug=slug):
                self.assertIsNotNone(find(slug))

    def test_every_lyric_vocabulary_names_a_real_genre(self) -> None:
        from kihachi_music_ai.lyrics import GENRE_WORDS

        for slug in GENRE_WORDS:
            with self.subTest(slug=slug):
                # "electronic" is KIHACHI's own no-match marker rather than a
                # database row, and is the one name allowed not to be one.
                if slug == "electronic":
                    continue
                self.assertIsNotNone(find(slug))


class LookupTests(unittest.TestCase):
    def test_find_returns_the_row_behind_a_slug(self) -> None:
        genre = find("tech_house")

        self.assertIsNotNone(genre)
        self.assertEqual(genre.name, "Tech House")
        self.assertEqual(genre.parent, "House")
        self.assertIsNotNone(genre.bpm_min)

    def test_find_returns_none_for_the_synthetic_fallback(self) -> None:
        # "electronic" is KIHACHI's own no-match marker, not a database row.
        self.assertIsNone(find("electronic"))


if __name__ == "__main__":
    unittest.main()


class NumericLinkTests(unittest.TestCase):
    """The database's numbers, where they carry signal, reach the SongSpec."""

    def _spec(self, prompt: str):
        return MusicBrain(seed=8).analyze(prompt)

    def test_a_stated_tempo_always_wins(self) -> None:
        from kihachi_music_ai.genres import typical_bpm

        # drum & bass would otherwise supply ~172
        self.assertIsNotNone(typical_bpm([("drum_bass", 1.0)]))
        self.assertEqual(self._spec("ドラムンベース。100 BPM、Am。").song.bpm, 100.0)

    def test_a_narrow_genre_range_replaces_the_flat_default(self) -> None:
        self.assertEqual(self._spec("ドラムンベース。Am。").song.bpm, 172.5)
        self.assertEqual(self._spec("ダブ。Am。").song.bpm, 75.0)
        self.assertEqual(self._spec("Tech House。Am。").song.bpm, 126.0)

    def test_a_range_too_wide_to_mean_anything_is_declined(self) -> None:
        from kihachi_music_ai.genres import find, typical_bpm

        bossa = find("bossa_nova")
        self.assertGreater(bossa.bpm_max - bossa.bpm_min, 40)
        self.assertIsNone(typical_bpm([("bossa_nova", 1.0)]))
        # ...so the song keeps the old default rather than a fabricated tempo
        self.assertEqual(self._spec("ボサノヴァ。Am。").song.bpm, 120.0)

    def test_a_zero_floor_is_not_treated_as_a_tempo(self) -> None:
        from kihachi_music_ai.genres import find, typical_bpm

        self.assertEqual(find("ambient").bpm_min, 0)
        self.assertIsNone(typical_bpm([("ambient", 1.0)]))

    def test_tempo_is_weighted_across_the_genres(self) -> None:
        from kihachi_music_ai.genres import typical_bpm

        one = typical_bpm([("dub", 1.0)])
        both = typical_bpm([("dub", 0.5), ("tech_house", 0.5)])

        self.assertIsNotNone(both)
        self.assertGreater(both, one)

    def test_an_unknown_genre_contributes_no_tempo(self) -> None:
        from kihachi_music_ai.genres import typical_bpm

        self.assertIsNone(typical_bpm([("electronic", 1.0)]))

    def test_mood_tags_move_the_psychedelic_axis_off_its_constant(self) -> None:
        from kihachi_music_ai.genres import mood_axes

        _dark, psychedelic = mood_axes([("drum_bass", 1.0)])

        self.assertIsNotNone(psychedelic)
        self.assertNotEqual(self._spec("ドラムンベース。Am。").style.psychedelic, 0.28)

    def test_one_light_genre_cannot_make_the_whole_song_psychedelic(self) -> None:
        from kihachi_music_ai.genres import mood_axes

        alone = mood_axes([("dub", 1.0)])[1]
        diluted = mood_axes([("dub", 0.3), ("tech_house", 0.7)])[1]

        if alone is not None and diluted is not None:
            self.assertLess(diluted, alone)

    def test_silence_on_an_axis_leaves_the_default_rather_than_neutral(self) -> None:
        from kihachi_music_ai.genres import mood_axes

        darkness, psychedelic = mood_axes([("electronic", 1.0)])

        self.assertIsNone(darkness)
        self.assertIsNone(psychedelic)
        spec = self._spec("何も書いていない。Am。")
        self.assertEqual(spec.style.darkness, 0.48)
        self.assertEqual(spec.style.psychedelic, 0.28)

    def test_the_prompt_still_overrides_both_axes(self) -> None:
        spec = self._spec("ドラムンベース。サイケデリック。Am。")

        self.assertEqual(spec.style.psychedelic, 0.82)
        self.assertEqual(self._spec("ダブ。Am。").style.darkness, 0.72)
