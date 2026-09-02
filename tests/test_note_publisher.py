from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kihachi_music_ai.adapters import note_publisher
from kihachi_music_ai.adapters.note_publisher import (
    DEPARTMENT_ORDER,
    OUTPUT_FILES,
    call_ollama,
    run_pipeline,
    write_outputs,
)
from kihachi_music_ai.cli import main


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_pipeline_chains_each_department_in_order(self) -> None:
        seen: list[tuple[str, str]] = []

        async def fake_generate(model: str, system: str, user: str) -> str:
            seen.append((system[:6], user[:20]))
            if "ライター" in system:
                return "draft body"
            if "エディター" in system:
                self.assertEqual(user, "draft body")
                return "article body"
            if "ディレクター" in system:
                self.assertEqual(user, "article body")
                return "video body"
            self.assertIn("article body", user)
            self.assertIn("video body", user)
            return "package body"

        outputs = await run_pipeline(
            "dev log text",
            model="test-model",
            generate=fake_generate,
        )

        self.assertEqual(
            outputs,
            {
                "draft": "draft body",
                "article": "article body",
                "video": "video body",
                "package": "package body",
            },
        )
        self.assertEqual(len(seen), 4)

    async def test_empty_log_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await run_pipeline("   ")

    async def test_step_callbacks_fire_in_order(self) -> None:
        events: list[str] = []

        async def fake_generate(_model: str, _system: str, _user: str) -> str:
            return "x"

        async def on_start(index: int, department: str, _key: str, _content: str) -> None:
            events.append(f"start:{index}:{department}")

        async def on_complete(
            index: int, department: str, key: str, content: str
        ) -> None:
            events.append(f"done:{index}:{key}:{content}")

        await run_pipeline(
            "log",
            generate=fake_generate,
            on_step_start=on_start,
            on_step_complete=on_complete,
        )

        self.assertEqual(events[0], f"start:1:{DEPARTMENT_ORDER[0]}")
        self.assertEqual(events[1], "done:1:draft:x")
        self.assertEqual(events[-1], "done:4:package:x")


class OutputFileTests(unittest.TestCase):
    def test_write_outputs_creates_four_markdown_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            paths = write_outputs(
                {
                    "draft": "d",
                    "article": "a",
                    "video": "v",
                    "package": "p",
                },
                out,
            )
            self.assertEqual(len(paths), 4)
            for key, name in OUTPUT_FILES.items():
                path = out / name
                self.assertTrue(path.exists())
                self.assertEqual(path.read_text(encoding="utf-8"), {"draft": "d", "article": "a", "video": "v", "package": "p"}[key])


class CallOllamaTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_error_returns_a_helpful_message(self) -> None:
        fake_httpx = mock.MagicMock()
        fake_httpx.ConnectError = type("ConnectError", (Exception,), {})
        fake_client = mock.AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.post.side_effect = fake_httpx.ConnectError()
        fake_httpx.AsyncClient.return_value = fake_client

        with mock.patch.dict("sys.modules", {"httpx": fake_httpx}):
            message = await call_ollama("m", "system", "user")

        self.assertIn("Ollama", message)

    async def test_success_returns_the_response_field(self) -> None:
        fake_response = mock.Mock()
        fake_response.json.return_value = {"response": "generated text"}
        fake_response.raise_for_status = mock.Mock()

        fake_client = mock.AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.post.return_value = fake_response

        fake_httpx = mock.MagicMock()
        fake_httpx.AsyncClient.return_value = fake_client
        fake_httpx.ConnectError = type("ConnectError", (Exception,), {})

        with mock.patch.dict("sys.modules", {"httpx": fake_httpx}):
            text = await call_ollama("m", "system", "user")

        self.assertEqual(text, "generated text")


class CliTests(unittest.TestCase):
    def test_note_publish_is_registered(self) -> None:
        with mock.patch("kihachi_music_ai.cli.song.serve_note_publisher") as serve:
            code = main(["note-publish", "--port", "9090"])
        self.assertEqual(code, 0)
        serve.assert_called_once()
        self.assertEqual(serve.call_args.args[1], 9090)


class ImportTests(unittest.TestCase):
    def test_prompts_cover_all_departments(self) -> None:
        for department in DEPARTMENT_ORDER:
            self.assertIn(department, note_publisher.PROMPTS)


if __name__ == "__main__":
    unittest.main()
