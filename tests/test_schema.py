from __future__ import annotations

import json
import unittest
from pathlib import Path

from kihachi_music_ai.models import SongSpec
from kihachi_music_ai.music_brain import MusicBrain
from test_music_brain import EXAMPLE

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "src/kihachi_music_ai/schema/song_spec.schema.json"
)

# Briefs chosen to exercise the optional fields: the arrangement engine writes
# per-track densities, and instruments only appears when a brief asks for a part
# beyond the core three.
BRIEFS = (
    EXAMPLE,
    EXAMPLE + "5分程度。",
    "ダブとテックハウス。110 BPM、D#m。",
    "テックハウス。128 BPM、Am。アルペジオとシンセリード。",
    "Mutation Funk、DUB。110 BPM、D#m。シンセ、アルペジオ、ボコーダー。",
)


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class SchemaTests(unittest.TestCase):
    def test_schema_is_valid_json_and_declares_the_mandatory_root_keys(self) -> None:
        schema = load_schema()
        mandatory = set(MusicBrain().analyze(BRIEFS[2]).to_dict())

        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(set(schema["required"]), mandatory)

    def test_the_schema_accepts_every_key_a_real_song_spec_writes(self) -> None:
        """The schema forbids unknown properties, so an omission is a rejection.

        This is checked against composed specs rather than against the model's
        field list because the optional fields are omitted when unset: reading
        the dataclass would say they exist, while the schema's job is to accept
        the JSON that actually gets written. The arrangement engine's per-section
        densities had been missing here since it landed, which made the schema
        reject every SongSpec the engine produced -- unnoticed, because nothing
        validated against it.
        """

        schema = load_schema()
        root_properties = set(schema["properties"])
        section_properties = set(schema["properties"]["arrangement"]["items"]["properties"])
        self.assertFalse(schema.get("additionalProperties", True))

        for brief in BRIEFS:
            payload = MusicBrain().analyze(brief).to_dict()

            self.assertEqual(set(payload) - root_properties, set(), f"root, brief {brief!r}")
            for section in payload["arrangement"]:
                self.assertEqual(
                    set(section) - section_properties, set(), f"section, brief {brief!r}"
                )

    def test_stored_projects_still_satisfy_the_schema(self) -> None:
        """Specs on disk are pinned by SHA-256 in repaint plans; they cannot drift."""

        schema = load_schema()
        root_properties = set(schema["properties"])
        section_properties = set(schema["properties"]["arrangement"]["items"]["properties"])
        roots = Path(__file__).resolve().parents[1] / "example_output"
        stored = sorted(roots.glob("*/song_spec.json")) if roots.is_dir() else []
        self.assertTrue(stored, "expected stored example projects to check against")

        for path in stored:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(payload) - root_properties, set(), str(path))
            for section in payload["arrangement"]:
                self.assertEqual(set(section) - section_properties, set(), str(path))
            # and the model still reads them back byte-for-byte
            raw = path.read_text(encoding="utf-8")
            self.assertEqual(SongSpec.from_json(raw).to_json(), raw, str(path))


if __name__ == "__main__":
    unittest.main()
