"""The brief screen, over a real socket.

The handler is tested through an actual request rather than by calling its
methods, because the parts worth breaking are the ones ``http.server`` owns:
status codes, the body length, and what happens to a brief the brain rejects.
"""

from __future__ import annotations

import base64
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from kihachi_music_ai.web import (
    EXAMPLE_BRIEFS,
    MAX_PROMPT_CHARS,
    BriefHandler,
    read_brief,
    render_page,
)


class ReadBriefTests(unittest.TestCase):
    def test_a_brief_comes_back_as_a_spec_a_reading_and_playable_parts(self) -> None:
        data = read_brief("レゲエ。Am。")

        self.assertEqual(data["spec"]["drums"]["pattern"], "one_drop")
        self.assertTrue(data["reading"])
        self.assertTrue(data["audio_prompt"])
        self.assertEqual([part["name"] for part in data["parts"]], ["bass", "drums", "chords"])
        for part in data["parts"]:
            self.assertGreater(part["notes"], 0)
            # A real MIDI file, not a placeholder: MThd is the header chunk.
            self.assertTrue(base64.b64decode(part["midi_base64"]).startswith(b"MThd"))

    def test_the_reading_never_disagrees_with_the_spec_it_explains(self) -> None:
        data = read_brief("ジャズ。Am。")
        values = {row["label"]: row["value"] for row in data["reading"]}

        self.assertEqual(values["ドラムパターン"], data["spec"]["drums"]["pattern"])
        self.assertEqual(values["コードの奏法"], data["spec"]["chords"]["articulation"])
        self.assertEqual(values["進行"], " - ".join(data["spec"]["harmony"]["progression"]))

    def test_the_seed_is_honoured(self) -> None:
        self.assertEqual(read_brief("テクノ。Am。", seed=42)["spec"]["seed"], 42)

    def test_every_example_on_the_screen_actually_reads(self) -> None:
        for brief in EXAMPLE_BRIEFS:
            with self.subTest(brief=brief):
                self.assertTrue(read_brief(brief)["parts"])

    def test_the_page_asks_for_nothing_from_anywhere_else(self) -> None:
        page = render_page()

        self.assertNotIn("http://", page.replace("http://127.0.0.1", ""))
        self.assertNotIn("https://", page)
        self.assertNotIn("<script src", page)

    def test_a_brief_cannot_inject_markup_into_the_page(self) -> None:
        # The examples are interpolated into the document as attributes.
        self.assertNotIn('data-brief="<', render_page())


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), BriefHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _post(self, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}/api/brief",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_the_page_is_served(self) -> None:
        with urllib.request.urlopen(f"{self.base}/", timeout=30) as response:
            body = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn("KIHACHI", body)

    def test_an_unknown_path_is_a_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{self.base}/secrets", timeout=30)

        self.assertEqual(caught.exception.code, 404)

    def test_a_brief_round_trips(self) -> None:
        status, data = self._post({"prompt": "テクノ。Am。", "seed": 8})

        self.assertEqual(status, 200)
        self.assertEqual(data["spec"]["drums"]["pattern"], "four_on_floor")

    def test_an_empty_brief_is_a_message_rather_than_a_stack_trace(self) -> None:
        status, data = self._post({"prompt": "   "})

        self.assertEqual(status, 400)
        self.assertIn("空", data["error"])

    def test_an_overlong_brief_is_refused(self) -> None:
        status, data = self._post({"prompt": "テクノ。" * MAX_PROMPT_CHARS})

        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_a_malformed_body_is_refused(self) -> None:
        request = urllib.request.Request(
            f"{self.base}/api/brief",
            data=b"not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=30)

        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
