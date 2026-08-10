from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.music_brain import MusicBrain
from kihachi_music_ai.preferences import (
    EMPTY,
    SHRINK_K,
    Observation,
    compile_preferences,
    harvest,
    load,
)

PROMPT = "Mutation Funk、DUB、Tech House。110 BPM、D#m。ファンキーなスラップベース。"


def _obs(n: int, genre="dub", scope="song", path="bass.mutation", delta=0.2):
    return [Observation(genre=genre, scope=scope, path=path, delta=delta) for _ in range(n)]


class ShrinkageTests(unittest.TestCase):
    def test_one_observation_moves_only_part_of_the_way(self) -> None:
        prefs = compile_preferences(_obs(1))
        prior = next(p for p in prefs.priors if p.genre == "dub")

        self.assertEqual(prior.samples, 1)
        self.assertAlmostEqual(prior.mean_delta, 0.2)
        # a single edit is usually about one song, not a standing preference
        self.assertAlmostEqual(prior.offset, 0.2 * 1 / (1 + SHRINK_K), places=5)
        self.assertLess(prior.offset, 0.2)

    def test_more_evidence_moves_further(self) -> None:
        offsets = [
            next(p for p in compile_preferences(_obs(n)).priors if p.genre == "dub").offset
            for n in (1, 5, 20)
        ]

        self.assertLess(offsets[0], offsets[1])
        self.assertLess(offsets[1], offsets[2])
        self.assertLess(offsets[2], 0.2)  # never overshoots the observed mean

    def test_opposite_corrections_cancel_rather_than_accumulate(self) -> None:
        prefs = compile_preferences(_obs(3) + _obs(3, delta=-0.2))
        prior = next(p for p in prefs.priors if p.genre == "dub")

        self.assertEqual(prior.samples, 6)
        self.assertAlmostEqual(prior.offset, 0.0, places=6)


class BackoffTests(unittest.TestCase):
    def test_a_genre_with_no_evidence_falls_back_to_its_family(self) -> None:
        prefs = compile_preferences(_obs(4, genre="tech_house"))

        # deep_house has no evidence of its own but shares the House family
        self.assertGreater(prefs.offset_for(["deep_house"], "song", "bass.mutation"), 0)

    def test_the_specific_genre_wins_over_its_family(self) -> None:
        prefs = compile_preferences(_obs(1, genre="deep_house") + _obs(30, genre="tech_house"))

        specific = prefs.offset_for(["deep_house"], "song", "bass.mutation")
        family = prefs.offset_for(["acid_house"], "song", "bass.mutation")

        self.assertLess(specific, family)  # n=1 is trusted less, and still chosen

    def test_an_unrelated_genre_still_gets_the_global_rollup(self) -> None:
        prefs = compile_preferences(_obs(6, genre="dub"))

        self.assertGreater(prefs.offset_for(["bossa_nova"], "song", "bass.mutation"), 0)

    def test_an_unknown_path_offsets_nothing(self) -> None:
        prefs = compile_preferences(_obs(6))

        self.assertEqual(prefs.offset_for(["dub"], "song", "song.bpm"), 0.0)


class HarvestTests(unittest.TestCase):
    def _project(self, root: Path, changes, genres=("dub",), applied=True):
        project = root / "p"
        project.mkdir(parents=True, exist_ok=True)
        spec = MusicBrain(seed=8).analyze(PROMPT).to_dict()
        spec["style"]["genres"] = [{"name": g, "weight": 1.0} for g in genres]
        (project / "song_spec.json").write_text(json.dumps(spec), encoding="utf-8")
        (project / "applied_spec_edit.json").write_text(
            json.dumps(
                {
                    "execution_state": "applied" if applied else "planned_not_applied",
                    "changes": changes,
                }
            ),
            encoding="utf-8",
        )
        return project

    def test_an_applied_edit_becomes_an_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(
                root,
                [{"scope": "song", "path": "bass.mutation", "from": 0.5, "to": 0.7}],
            )

            observations = harvest(root)

            self.assertEqual(len(observations), 1)
            self.assertAlmostEqual(observations[0].delta, 0.2)

    def test_a_planned_but_unapplied_edit_is_not_learned_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(
                root,
                [{"scope": "song", "path": "bass.mutation", "from": 0.5, "to": 0.7}],
                applied=False,
            )

            self.assertEqual(harvest(root), [])

    def test_a_path_outside_the_allowlist_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root, [{"scope": "song", "path": "song.bpm", "from": 110, "to": 174}])

            self.assertEqual(harvest(root), [])

    def test_an_edit_that_changed_nothing_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(
                root, [{"scope": "song", "path": "bass.mutation", "from": 0.5, "to": 0.5}]
            )

            self.assertEqual(harvest(root), [])

    def test_nothing_to_harvest_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(harvest(Path(tmp)), [])


class DeterminismTests(unittest.TestCase):
    """The guarantee this whole design is shaped around."""

    def test_no_preferences_reproduces_the_original_song_exactly(self) -> None:
        before = MusicBrain(seed=8).analyze(PROMPT)
        after = MusicBrain(seed=8, preferences=EMPTY).analyze(PROMPT)

        self.assertEqual(before.to_dict(), after.to_dict())
        self.assertEqual(before.bass.mutation, 0.78)

    def test_the_same_preferences_give_the_same_song_every_time(self) -> None:
        prefs = compile_preferences(_obs(9))
        first = MusicBrain(seed=8, preferences=prefs).analyze(PROMPT)
        second = MusicBrain(seed=8, preferences=prefs).analyze(PROMPT)

        self.assertEqual(first.to_dict(), second.to_dict())

    def test_preferences_actually_move_the_default(self) -> None:
        prefs = compile_preferences(_obs(9))
        tuned = MusicBrain(seed=8, preferences=prefs).analyze(PROMPT)

        self.assertGreater(tuned.bass.mutation, 0.78)

    def test_a_learned_value_never_leaves_the_valid_range(self) -> None:
        prefs = compile_preferences(_obs(500, delta=5.0))
        tuned = MusicBrain(seed=8, preferences=prefs).analyze(PROMPT)

        self.assertLessEqual(tuned.bass.mutation, 1.0)
        self.assertGreaterEqual(tuned.bass.mutation, 0.0)

    def test_the_fingerprint_changes_when_the_evidence_does(self) -> None:
        self.assertNotEqual(
            compile_preferences(_obs(3)).fingerprint,
            compile_preferences(_obs(4)).fingerprint,
        )

    def test_a_round_trip_through_disk_keeps_the_same_song(self) -> None:
        prefs = compile_preferences(_obs(9))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            path.write_text(json.dumps(prefs.to_dict()), encoding="utf-8")

            reloaded = load(path)

            self.assertEqual(reloaded.fingerprint, prefs.fingerprint)
            self.assertEqual(
                MusicBrain(seed=8, preferences=prefs).analyze(PROMPT).to_dict(),
                MusicBrain(seed=8, preferences=reloaded).analyze(PROMPT).to_dict(),
            )

    def test_a_future_preferences_version_is_refused_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            path.write_text(json.dumps({"preferences_version": "9.9", "priors": []}))

            with self.assertRaises(ValueError):
                load(path)

    def test_load_of_nothing_is_the_empty_set(self) -> None:
        self.assertEqual(load(None), EMPTY)


class ProvenanceTests(unittest.TestCase):
    """A tuned song has to say what tuned it, or the seed no longer identifies it."""

    def test_an_untuned_song_does_not_mention_preferences_at_all(self) -> None:
        payload = MusicBrain(seed=8).analyze(PROMPT).to_dict()

        self.assertNotIn("preferences_fingerprint", payload)

    def test_a_tuned_song_records_the_fingerprint_that_moved_it(self) -> None:
        prefs = compile_preferences(_obs(9))
        payload = MusicBrain(seed=8, preferences=prefs).analyze(PROMPT).to_dict()

        self.assertEqual(payload["preferences_fingerprint"], prefs.fingerprint)

    def test_the_fingerprint_survives_a_round_trip_through_json(self) -> None:
        prefs = compile_preferences(_obs(9))
        spec = MusicBrain(seed=8, preferences=prefs).analyze(PROMPT)

        restored = SongSpec.from_json(spec.to_json())

        self.assertEqual(restored.preferences_fingerprint, prefs.fingerprint)
        self.assertEqual(restored.to_dict(), spec.to_dict())


if __name__ == "__main__":
    unittest.main()
