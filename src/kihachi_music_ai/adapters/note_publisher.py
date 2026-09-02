"""Turn an Obsidian dev log into note article drafts via a local Ollama model.

Four fixed "departments" run in sequence: draft, edit, video storyboard, and
release packaging. Like the intent reader (ADR-0011), this adapter talks to a
model but does not touch SongSpec or MIDI. It is optional, lives under
``adapters/``, and imports ``httpx`` only inside the call so the core stays
stdlib-only (ADR-0001).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

OLLAMA_API_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen2.5:7b"
REQUEST_TIMEOUT_SEC = 300.0

OUTPUT_FILES = {
    "draft": "01_draft.md",
    "article": "02_note_article.md",
    "video": "03_video_storyboard.md",
    "package": "04_release_package.md",
}

PROMPTS = {
    "描き始める部署": """あなたは【描き始める部署】のライターです。
提供された開発ログから、音楽プロデューサーが熱狂する要素を抽出しドラフトを作成してください。
・開発の葛藤（PR22マージ時のトラブル、AIのMIDI暴走など）
・技術的突破（SongSpec JSON構文の確立、ACE-Step 1.5連携）
・出音の変化（ファンク、スラップベース、KORG MS-20等のサウンド）""",
    "編集する部署": """あなたは【編集する部署】のエディターです。
ドラフトを元に、最先端を求める音楽プロデューサー向けのnote有料記事を執筆してください。
・見出しは『』、強調は""を使用。
・前半（無料）は『AIにDAWを奪われた夜』等のエモーショナルな物語と音の証明。
・後半（有料：980円）は「SongSpecのJSON設計図」と「PythonによるAbleton制御の核心」。
・次回キット販売時の1,000円割引クーポンの案内を含めること。""",
    "動画を作る部署": """あなたは【動画を作る部署】のディレクターです。
note記事を元に、Xやnote埋め込み用の「15〜30秒ショート動画絵コンテ」を作成してください。
・秒数ごとのシーン展開
・Ableton Live 12の画面操作指示
・BGM指定（ファンキーなスラップベース等）""",
    "まとめる部署": """あなたは【まとめる部署】のマネージャーです。
全成果物を統合し、note入稿前の最終確認パッケージを作成してください。
・noteタイトル案（3つ）
・ハッシュタグ（#Ableton #AI音楽 #DTM 等）
・販売価格設定（980円）
・チェックリスト（APIキーや機密情報のマスク確認、音源リンク確認）""",
}

DEPARTMENT_ORDER = (
    "描き始める部署",
    "編集する部署",
    "動画を作る部署",
    "まとめる部署",
)

GenerateFn = Callable[[str, str, str], Awaitable[str]]
StepCallback = Callable[[int, str, str, str], Awaitable[None] | None]


async def call_ollama(
    model: str,
    system_prompt: str,
    user_content: str,
    *,
    api_url: str = OLLAMA_API_URL,
    timeout: float = REQUEST_TIMEOUT_SEC,
) -> str:
    """POST to Ollama's ``/api/generate`` endpoint and return the response text."""

    payload = {
        "model": model,
        "system": system_prompt,
        "prompt": user_content,
        "stream": False,
    }
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "httpx is not installed. It is an optional dependency so the core "
            "stays standard-library only (ADR-0001): "
            "pip install 'kihachi-music-ai[publisher]'"
        ) from exc

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(api_url, json=payload)
            response.raise_for_status()
            data = response.json()
            return str(data.get("response", ""))
        except httpx.ConnectError:
            return (
                "エラー: Ollamaサーバーに接続できません。"
                "`ollama serve` が起動しているか確認してください。"
            )
        except Exception as exc:
            return f"推論エラー: {exc}"


async def _notify(
    callback: StepCallback | None,
    index: int,
    department: str,
    output_key: str,
    content: str = "",
) -> None:
    if callback is None:
        return
    result = callback(index, department, output_key, content)
    if result is not None:
        await result


async def run_pipeline(
    raw_log: str,
    *,
    model: str = DEFAULT_MODEL,
    api_url: str = OLLAMA_API_URL,
    generate: GenerateFn | None = None,
    on_step_start: StepCallback | None = None,
    on_step_complete: StepCallback | None = None,
) -> dict[str, str]:
    """Run all four departments and return their markdown outputs."""

    if not raw_log.strip():
        raise ValueError("開発ログが空です")

    generate = generate or (
        lambda m, system, user: call_ollama(m, system, user, api_url=api_url)
    )

    outputs: dict[str, str] = {}
    steps = (
        ("描き始める部署", "draft", raw_log),
        ("編集する部署", "article", None),
        ("動画を作る部署", "video", None),
        ("まとめる部署", "package", None),
    )
    for index, (department, key, fixed_input) in enumerate(steps, start=1):
        await _notify(on_step_start, index, department, key, "")
        if key == "article":
            user_content = outputs["draft"]
        elif key == "video":
            user_content = outputs["article"]
        elif key == "package":
            user_content = (
                f"【記事本文】\n{outputs['article']}\n\n"
                f"【動画構成】\n{outputs['video']}"
            )
        else:
            user_content = fixed_input or ""
        outputs[key] = await generate(model, PROMPTS[department], user_content)
        await _notify(on_step_complete, index, department, key, outputs[key])
    return outputs


def write_outputs(outputs: dict[str, str], output_dir: Path) -> list[Path]:
    """Write pipeline outputs to ``output_dir`` and return the paths written."""

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for key, filename in OUTPUT_FILES.items():
        path = output_dir / filename
        path.write_text(outputs[key], encoding="utf-8")
        written.append(path)
    return written


def pipeline_steps() -> list[dict[str, Any]]:
    """Metadata for UI progress labels: index, department name, output key."""

    keys = ("draft", "article", "video", "package")
    return [
        {"index": index, "department": department, "output_key": key}
        for index, (department, key) in enumerate(
            zip(DEPARTMENT_ORDER, keys, strict=True), start=1
        )
    ]
