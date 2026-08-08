from __future__ import annotations

import json
import unittest
from pathlib import Path

from kihachi_music_ai.music_brain import MusicBrain
from test_music_brain import EXAMPLE


class SchemaTests(unittest.TestCase):
    def test_schema_is_valid_json_and_covers_model_root(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        schema_path = project_root / "src/kihachi_music_ai/schema/song_spec.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        spec_keys = set(MusicBrain().analyze(EXAMPLE).to_dict())
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(set(schema["required"]), spec_keys)
        self.assertEqual(set(schema["properties"]), spec_keys)


if __name__ == "__main__":
    unittest.main()
