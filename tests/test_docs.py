from __future__ import annotations

import unittest
from pathlib import Path


class CopilotSpecificationTests(unittest.TestCase):
    def test_rebuild_prompt_is_present_and_contains_contract_anchors(self) -> None:
        path = Path(__file__).parents[1] / "docs" / "COPILOT_REBUILD_PROMPT.md"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        for anchor in (
            "SongSpec",
            "標準ライブラリのみ",
            "Audio-to-MIDI",
            "path traversal",
            "11. optional LLM/ACE-Step adapters",
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, text)
        readme = (path.parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn("transcription_version=0.3", readme)

        repo = path.parents[1]
        for relative in (
            "src/kihachi_music_ai/models.py",
            "src/kihachi_music_ai/composer.py",
            "src/kihachi_music_ai/transcribe.py",
            "src/kihachi_music_ai/cli/parser.py",
            "docs/adr/0001-standalone-core.md",
            "docs/adr/0008-stem-separation-boundary.md",
        ):
            with self.subTest(path=relative):
                self.assertTrue((repo / relative).is_file())
        for number in range(1, 15):
            matches = list((repo / "docs" / "adr").glob(f"{number:04d}-*.md"))
            with self.subTest(adr=number):
                self.assertEqual(len(matches), 1)


if __name__ == "__main__":
    unittest.main()
