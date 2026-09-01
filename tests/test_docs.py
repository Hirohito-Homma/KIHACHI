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


if __name__ == "__main__":
    unittest.main()
