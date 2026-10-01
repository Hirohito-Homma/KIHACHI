"""Ask a local Ollama model to write release copy for a package.

The model writes prose about a song; it never decides anything. What it gets
is the same facts the template description already prints (title, genres,
tempo, key, lyrics excerpt), and what comes back is a draft a human edits
before publishing -- `authorize_package` stays the only publish gate.

Stdlib only (ADR-0001): Ollama speaks plain HTTP on localhost, so `urllib`
is enough and nothing is added to the core's dependencies. No key, no paid
API, no model download: if the server is down or the model is missing, the
call raises `OllamaUnavailable` and the caller falls back to the template.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_MODEL = "gemma4"
HOST_ENV = "OLLAMA_HOST"
DEFAULT_HOST = "http://127.0.0.1:11434"

TIMEOUT_SECONDS = 900.0
"""An Intel Mac on CPU can take minutes for a few hundred tokens."""


class OllamaUnavailable(RuntimeError):
    """The server could not be reached, or it returned nothing usable."""


TARGETS: dict[str, tuple[str, ...]] = {
    "youtube": (
        "Write a YouTube description for the track below.",
        "- First a short Japanese paragraph (2-4 sentences), then the same in English.",
    ),
    "streaming": (
        "Write a pitch for streaming playlist editors (Spotify, Apple Music) for the track below.",
        "- English only, one paragraph, at most 450 characters.",
        "- Say what the track sounds like and which playlists or settings it fits.",
    ),
    "stock": (
        "Write a listing for a royalty-free BGM stock site (Audiostock, BOOTH) for the track below.",
        "- Japanese only: one sentence of description, then one line starting",
        "  '用途:' with 3-5 concrete scenes it suits, comma separated.",
    ),
}
"""What each release channel needs; the shared rules below apply to all."""


def build_prompt(facts: dict[str, Any], target: str = "youtube") -> str:
    """The exact prompt sent, built offline so it can be tested."""

    if target not in TARGETS:
        raise ValueError(f"unknown copy target: {target}")
    task, *shape = TARGETS[target]
    lines = [
        "You write release copy for an independent electronic music artist, KIHACHI.",
        task,
        "",
        "Rules:",
        *shape,
        "- Describe mood and use from the facts only; if a mood or use is given, follow it.",
        "- Do not invent facts: no release dates, labels, collaborators, awards, or links.",
        "- No hashtags, no emoji, no headings, no calls to subscribe.",
        "",
        "Facts:",
    ]
    for key in ("title", "genres", "bpm", "key", "duration", "mood", "use"):
        value = facts.get(key)
        if value:
            lines.append(f"- {key}: {value}")
    lyrics = facts.get("lyrics")
    if lyrics:
        lines.append("- lyrics excerpt:")
        lines.extend(f"  {line}" for line in str(lyrics).strip().splitlines()[:8])
    return "\n".join(lines) + "\n"


def generate(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    host: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    base = (host or os.environ.get(HOST_ENV) or DEFAULT_HOST).rstrip("/")
    if not base.startswith("http"):
        base = f"http://{base}"
    body = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.7}}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise OllamaUnavailable(f"ollama request failed: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise OllamaUnavailable(f"ollama returned an error: {payload.get('error') if isinstance(payload, dict) else payload}")
    text = str(payload.get("response") or "").strip()
    if not text:
        raise OllamaUnavailable("ollama returned an empty response")
    return text
