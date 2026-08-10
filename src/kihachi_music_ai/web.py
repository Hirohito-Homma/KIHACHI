"""The brief screen: type a brief, see what KIHACHI made of it.

Everything this project does starts from one sentence of Japanese or English,
and until now the only way to hand it that sentence was ``kihachi compose``,
which writes five files into a directory and prints a summary. That is a fine
way to *keep* a song and a poor way to find out what a wording does. Changing
"ダブ" to "レゲエ" changes the drum pattern, the chord articulation, the
progression and the hat density, and seeing that took a compose, a diff and a
cleanup.

So this screen reads a brief and writes nothing. It shows the SongSpec and,
next to it, which decision came from where -- the genre database, the family
profile, the brief's own words -- because the interesting failure is never "the
program crashed", it is "it heard a genre I did not mean".

**Stdlib only, like the rest of the package.** No framework, no build step, no
CDN: one HTML string and ``http.server``. The page is served from memory and
the server binds to localhost, because nothing here authenticates anything and
a brief is the user's unpublished work.
"""

from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .arrangement import describe_arrangement
from .composer import compose_tracks
from .genres import family_of, match_genres
from .midi import build_midi_bytes
from .models import SongSpec
from .music_brain import MusicBrain
from .prompt_compiler import compile_audio_prompt, render_brief

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420

#: A brief this long is not a brief. The limit is here so one request cannot
#: make the server spend a minute matching 1020 genre names against a novel.
MAX_PROMPT_CHARS = 2000


def read_brief(prompt: str, *, seed: int = 8) -> dict[str, Any]:
    """Everything the screen shows for one brief. Writes nothing to disk."""

    spec = MusicBrain(seed=seed).analyze(prompt)
    tracks = compose_tracks(spec)
    return {
        "spec": spec.to_dict(),
        "reading": _reading(prompt, spec),
        "arrangement": describe_arrangement(spec.arrangement),
        "audio_prompt": compile_audio_prompt(spec),
        # The same structured brief ``compose`` writes as prompt.json, so the
        # screen and the CLI hand a renderer the identical file.
        "render_brief": render_brief(spec),
        "parts": [
            {
                "name": name,
                "notes": len(notes),
                "midi_base64": base64.b64encode(
                    build_midi_bytes(
                        notes,
                        track_name=f"KIHACHI {name.title()}",
                        bpm=spec.song.bpm,
                        key=spec.song.key,
                    )
                ).decode("ascii"),
            }
            for name, notes in tracks.items()
        ],
    }


def _reading(prompt: str, spec: SongSpec) -> list[dict[str, str]]:
    """Why each headline decision came out the way it did.

    Every line here is read back off the SongSpec or recomputed with the same
    function that produced it. Nothing is inferred a second time: a screen that
    explains a decision differently from how it was made is worse than one that
    explains nothing.
    """

    matched = match_genres(prompt)
    if matched:
        heard = "、".join(f"{m.matched} → {m.genre.name}" for m in matched)
    else:
        heard = "ジャンル名を認識できず（electronic として扱いました）"
    lead = spec.style.genres[0].name
    # ``family_of``, not the ``parent`` column: a top-level row is its own
    # family, and reading the column shows "—" for exactly the briefs that name
    # a family outright.
    family = family_of(lead) or "—"
    return [
        {"label": "聞き取ったジャンル", "value": heard},
        {"label": "主ジャンルのファミリ", "value": f"{lead} / {family}"},
        {
            "label": "テンポ",
            "value": (
                f"{spec.song.bpm:g} BPM"
                + ("（ブリーフが指定）" if "BPM" in prompt.upper() else "（ジャンルの典型値か既定値）")
            ),
        },
        {"label": "キーと拍子", "value": f"{spec.song.key} / {spec.song.time_signature}"},
        {"label": "進行", "value": " - ".join(spec.harmony.progression)},
        {"label": "ドラムパターン", "value": spec.drums.pattern},
        {"label": "コードの奏法", "value": spec.chords.articulation},
        {"label": "ベースの役割", "value": f"{spec.bass.role} / {spec.bass.technique}"},
        {
            "label": "尺",
            "value": f"{spec.song.total_bars} 小節 / {spec.song.target_duration_sec:.1f} 秒",
        },
        {"label": "パート", "value": "、".join(spec.parts())},
    ]


EXAMPLE_BRIEFS = (
    "Mutation Funk、DUB、Tech House。110 BPM、D#m。ファンキーなスラップベース。",
    "ジャズ。Am。3分程度。",
    "レゲエ。少しサイケデリックに。",
    "3拍子のフォーク。Am。",
    "Death Metal。5分程度。",
)


def render_page() -> str:
    """The whole screen: one document, no external requests."""

    examples = "".join(
        f'<button type="button" class="chip" data-brief="{_escape(brief)}">{_escape(brief)}</button>'
        for brief in EXAMPLE_BRIEFS
    )
    return _PAGE.replace("{{EXAMPLES}}", examples)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class BriefHandler(BaseHTTPRequestHandler):
    """Two routes: the page, and the brief it posts back."""

    server_version = "KIHACHI/0.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", render_page().encode("utf-8"))
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
        if self.path != "/api/brief":
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 64_000:
            self._json(400, {"error": "リクエストが空か、大きすぎます"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            prompt = str(payload["prompt"])
            seed = int(payload.get("seed", 8))
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            self._json(400, {"error": "リクエストを読めませんでした"})
            return
        if not prompt.strip():
            self._json(400, {"error": "ブリーフが空です"})
            return
        if len(prompt) > MAX_PROMPT_CHARS:
            self._json(400, {"error": f"ブリーフは {MAX_PROMPT_CHARS} 文字までです"})
            return
        try:
            self._json(200, read_brief(prompt, seed=seed))
        except ValueError as error:
            # A brief the brain refuses is a normal outcome of typing, not a
            # server fault: the message is the useful part of the screen.
            self._json(400, {"error": str(error)})

    def log_message(self, fmt: str, *args: Any) -> None:
        """Quiet by default; a brief is not something to print to a terminal."""

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page loads nothing from anywhere else, so say so.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
        )
        self.end_headers()
        self.wfile.write(body)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the screen until interrupted."""

    server = ThreadingHTTPServer((host, port), BriefHandler)
    print(f"KIHACHI brief screen: http://{host}:{server.server_port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


_PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KIHACHI — ブリーフ</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #fbfaf8; --panel: #ffffff; --ink: #1a1917; --muted: #6b6660;
  --line: #e4e0d9; --accent: #8a5a2b; --code: #f4f1ec;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161513; --panel: #1e1d1a; --ink: #eceae6; --muted: #9a938a;
    --line: #302e2a; --accent: #d9a066; --code: #131210;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.7 ui-sans-serif, system-ui, "Hiragino Sans", "Noto Sans JP", sans-serif;
}
header { padding: 2rem 1.5rem 1rem; max-width: 68rem; margin: 0 auto; }
h1 { font-size: 1.35rem; margin: 0 0 .25rem; letter-spacing: .02em; }
header p { margin: 0; color: var(--muted); font-size: .9rem; }
main { max-width: 68rem; margin: 0 auto; padding: 0 1.5rem 4rem; }
.panel {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 1.25rem; margin-bottom: 1.25rem;
}
textarea {
  width: 100%; min-height: 6.5rem; resize: vertical; padding: .75rem;
  font: inherit; color: inherit; background: var(--code);
  border: 1px solid var(--line); border-radius: 8px;
}
textarea:focus, input:focus, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.row { display: flex; gap: .75rem; align-items: center; flex-wrap: wrap; margin-top: .75rem; }
label { font-size: .85rem; color: var(--muted); }
input[type=number] {
  width: 5.5rem; padding: .4rem .5rem; font: inherit; color: inherit;
  background: var(--code); border: 1px solid var(--line); border-radius: 6px;
}
button.go {
  padding: .5rem 1.25rem; font: inherit; font-weight: 600; cursor: pointer;
  background: var(--accent); color: #fff; border: 0; border-radius: 6px;
}
button.go[disabled] { opacity: .55; cursor: progress; }
.chips { display: flex; gap: .4rem; flex-wrap: wrap; margin-top: .75rem; }
.chip {
  font: inherit; font-size: .8rem; cursor: pointer; padding: .25rem .6rem;
  background: transparent; color: var(--muted);
  border: 1px solid var(--line); border-radius: 999px;
}
.chip:hover { color: var(--ink); border-color: var(--accent); }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; }
@media (max-width: 720px) { .cols { grid-template-columns: 1fr; } }
h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
     color: var(--muted); margin: 0 0 .75rem; font-weight: 600; }
dl { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: .4rem .9rem; }
dt { color: var(--muted); font-size: .85rem; white-space: nowrap; }
dd { margin: 0; font-size: .9rem; word-break: break-word; }
pre {
  margin: 0; padding: .9rem; background: var(--code); border-radius: 8px;
  overflow: auto; max-height: 26rem; font-size: .78rem; line-height: 1.55;
}
table { width: 100%; border-collapse: collapse; font-size: .85rem; }
th, td { text-align: left; padding: .3rem .5rem; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: .78rem; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
/* Without widths the note-count column stretches to fill the panel and the
   number ends up an inch from the header it belongs to. */
#parts-table th:nth-child(1), #parts-table td:nth-child(1) { width: 8rem; }
#parts-table th:nth-child(2), #parts-table td:nth-child(2) { width: 6rem; }
#arrangement-table th:nth-child(1), #arrangement-table td:nth-child(1) { width: 40%; }
#arrangement-table th:nth-child(2), #arrangement-table td:nth-child(2) { width: 5rem; }
.bar { height: .5rem; background: var(--accent); border-radius: 2px; opacity: .8; }
a.dl { color: var(--accent); text-decoration: none; font-size: .82rem; }
a.dl:hover { text-decoration: underline; }
.error { color: #b3261e; font-size: .9rem; }
@media (prefers-color-scheme: dark) { .error { color: #f2b8b5; } }
.hidden { display: none; }
</style>
</head>
<body>
<header>
  <h1>KIHACHI — ブリーフ</h1>
  <p>一文を渡して、何がどう読まれたかを見る画面です。ファイルは書きません。</p>
</header>
<main>
  <div class="panel">
    <textarea id="brief" placeholder="例: レゲエ。Am。少しサイケデリックに。" autofocus></textarea>
    <div class="row">
      <button class="go" id="go">読む</button>
      <label for="seed">シード</label>
      <input type="number" id="seed" value="8" min="0" max="999999">
      <span class="error hidden" id="error"></span>
    </div>
    <div class="chips">{{EXAMPLES}}</div>
  </div>

  <div id="result" class="hidden">
    <div class="cols">
      <div class="panel">
        <h2>読み取り</h2>
        <dl id="reading"></dl>
      </div>
      <div class="panel">
        <h2>編成</h2>
        <table id="arrangement-table"><thead><tr><th>セクション</th><th>小節</th><th>エネルギー</th></tr></thead>
        <tbody id="arrangement"></tbody></table>
      </div>
    </div>
    <div class="panel">
      <h2>パート</h2>
      <table id="parts-table"><thead><tr><th>パート</th><th>ノート数</th><th></th></tr></thead>
      <tbody id="parts"></tbody></table>
    </div>
    <div class="panel">
      <h2>音声プロンプト <a class="dl" id="promptdl" download="prompt.json">prompt.json</a></h2>
      <pre id="audio"></pre>
    </div>
    <div class="panel">
      <h2>SongSpec <a class="dl" id="specdl" download="song_spec.json">ダウンロード</a></h2>
      <pre id="spec"></pre>
    </div>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
const brief = $("brief"), seed = $("seed"), go = $("go"), error = $("error"), result = $("result");

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => { brief.value = chip.dataset.brief; read(); });
});
go.addEventListener("click", read);
brief.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") read();
});

function show(message) {
  error.textContent = message;
  error.classList.toggle("hidden", !message);
}

async function read() {
  const prompt = brief.value.trim();
  if (!prompt) { show("ブリーフを入力してください"); return; }
  show(""); go.disabled = true; go.textContent = "読んでいます…";
  try {
    const response = await fetch("/api/brief", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, seed: Number(seed.value) || 0 }),
    });
    const data = await response.json();
    if (!response.ok) { show(data.error || "読めませんでした"); return; }
    render(data);
  } catch (failure) {
    show("サーバーに繋がりませんでした");
  } finally {
    go.disabled = false; go.textContent = "読む";
  }
}

function render(data) {
  const reading = $("reading");
  reading.textContent = "";
  for (const row of data.reading) {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = row.label; dd.textContent = row.value;
    reading.append(dt, dd);
  }

  const arrangement = $("arrangement");
  arrangement.textContent = "";
  for (const section of data.arrangement) {
    const tr = document.createElement("tr");
    const name = document.createElement("td"); name.textContent = section.name;
    const bars = document.createElement("td"); bars.className = "num";
    bars.textContent = `${section.start_bar}–${section.start_bar + section.length_bars - 1}`;
    const energy = document.createElement("td");
    const bar = document.createElement("div");
    bar.className = "bar"; bar.style.width = `${Math.round(section.energy * 100)}%`;
    bar.title = section.energy.toFixed(2);
    energy.append(bar);
    tr.append(name, bars, energy);
    arrangement.append(tr);
  }

  const parts = $("parts");
  parts.textContent = "";
  for (const part of data.parts) {
    const tr = document.createElement("tr");
    const name = document.createElement("td"); name.textContent = part.name;
    const count = document.createElement("td"); count.className = "num";
    count.textContent = part.notes.toLocaleString();
    const cell = document.createElement("td");
    const link = document.createElement("a");
    link.className = "dl"; link.textContent = `${part.name}.mid`;
    link.download = `${part.name}.mid`;
    link.href = `data:audio/midi;base64,${part.midi_base64}`;
    cell.append(link);
    tr.append(name, count, cell);
    parts.append(tr);
  }

  $("audio").textContent = data.audio_prompt;
  const brief = JSON.stringify(data.render_brief, null, 2);
  $("promptdl").href = "data:application/json;charset=utf-8," + encodeURIComponent(brief);
  const json = JSON.stringify(data.spec, null, 2);
  $("spec").textContent = json;
  $("specdl").href = "data:application/json;charset=utf-8," + encodeURIComponent(json);
  result.classList.remove("hidden");
}
</script>
</body>
</html>
"""
