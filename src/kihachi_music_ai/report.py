"""A page for the one decision this system deliberately refuses to make.

`revise` ends by ranking takes and saying "listen before choosing" -- and then
offers no way to listen, and nothing to compare. This builds that: one
self-contained HTML file per set of candidates, with each take playable, its
waveform drawn, its planned section boundaries marked on it, and its defects
marked where they actually happen.

It is a report, not a control panel. Nothing here renders, adopts, deletes or
edits; it reads finished projects and writes one file. That keeps it
deterministic enough to test, and keeps the adoption decision where it belongs,
which is with someone who has heard the takes.

Pure and stdlib-only. Audio is linked relatively rather than embedded -- a take
is 13 MB as WAV, and a page with three of them inside it is not a page.
"""

from __future__ import annotations

import html
import json
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

REPORT_VERSION = "0.1"
WAVE_COLUMNS = 900
"""Columns in the drawn waveform. Wide enough to see a section, cheap to compute."""

PLAYABLE = (".mp3", ".m4a", ".wav")
"""Preferred order for the playable copy: the small one first."""


@dataclass(frozen=True)
class Candidate:
    project_dir: Path
    name: str
    alignment: float
    grade: str
    defects: tuple[dict[str, Any], ...]
    scanned: bool
    measurements: dict[str, Any]
    audio_file: Path | None
    playable: Path | None
    duration_sec: float
    peaks: tuple[tuple[float, float], ...]
    section_marks: tuple[tuple[float, str], ...]
    defect_marks: tuple[tuple[float, str, str], ...]

    @property
    def blocking(self) -> int:
        return sum(1 for item in self.defects if item["severity"] == "blocking")

    @property
    def usable(self) -> bool:
        """Known to have no blocking defect. Unscanned is not the same as clean."""

        return self.scanned and self.blocking == 0

    @property
    def standing(self) -> int:
        """0 known good, 1 never measured, 2 known bad. Ranking sorts on this."""

        if not self.scanned:
            return 1
        return 0 if self.blocking == 0 else 2


def _duration(audio_path: Path) -> float:
    """Length from the file itself.

    Not from the defect scan: a take that was never scanned still has a length,
    and reading it from the measurements alone left unscanned takes at zero --
    which silently dropped every section marker off their waveform.
    """

    with wave.open(str(audio_path), "rb") as source:
        rate = source.getframerate()
        return source.getnframes() / rate if rate else 0.0


def _peaks(audio_path: Path, columns: int = WAVE_COLUMNS) -> tuple[tuple[float, float], ...]:
    """Min/max per column, straight from the WAV. Mono-summed, scaled to -1..1."""

    with wave.open(str(audio_path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        frames = source.getnframes()
        if width != 2 or frames == 0:
            return ()
        per_column = max(1, frames // columns)
        full = 32768.0
        result: list[tuple[float, float]] = []
        for _ in range(min(columns, frames // per_column)):
            raw = source.readframes(per_column)
            if not raw:
                break
            low = high = 0.0
            # Step over one channel only: the shape is what matters, and reading
            # every sample of a five-minute stereo take to draw 900 columns is
            # work nobody sees.
            stride = channels * 2 * max(1, per_column // 400)
            for offset in range(0, len(raw) - 1, stride):
                value = int.from_bytes(raw[offset : offset + 2], "little", signed=True) / full
                low = min(low, value)
                high = max(high, value)
            result.append((low, high))
    return tuple(result)


def _section_marks(project_dir: Path) -> tuple[tuple[float, str], ...]:
    """Planned section starts, in seconds. These are intent, not detection."""

    spec_path = project_dir / "song_spec.json"
    if not spec_path.is_file():
        return ()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    bpm = float(spec["song"]["bpm"])
    numerator, denominator = (int(p) for p in spec["song"]["time_signature"].split("/", 1))
    bar_seconds = numerator * (4.0 / denominator) * 60.0 / bpm
    return tuple(
        (section["start_bar"] * bar_seconds, str(section["name"]))
        for section in spec["arrangement"]
    )


def _defect_marks(defects: Sequence[dict[str, Any]], measurements: dict[str, Any]):
    """Where each defect happens, when the scan located it.

    Only the located ones get a mark. A crushed crest factor or a DC offset is a
    property of the whole take and pointing at a moment would be a lie.
    """

    positions = {
        "silent_gap": "longest_silence_at_sec",
        "discontinuity": "max_sample_jump_at_sec",
    }
    marks: list[tuple[float, str, str]] = []
    for item in defects:
        key = positions.get(item["code"])
        at = measurements.get(key) if key else None
        if at is None:
            continue
        marks.append((float(at), item["code"], item["severity"]))
    return tuple(marks)


def _playable(audio_file: Path | None, project_dir: Path) -> Path | None:
    """The small copy of *this* take, or the take itself.

    Two traps here. A rendered project keeps `ace-step-01.untrimmed.wav` beside
    the trimmed take -- that is the material the tail guard cut down, and playing
    it would present the bug the guard exists to fix as the result. And the
    listening copy is not always named after the WAV, so a same-stem match is
    tried first and a loose one only after excluding the untrimmed file.
    """

    if audio_file is None:
        return None
    for suffix in PLAYABLE:
        same_stem = audio_file.with_suffix(suffix)
        if same_stem.is_file():
            return same_stem
    for suffix in PLAYABLE[:-1]:  # a stray WAV is too likely to be the wrong one
        loose = sorted(
            item for item in audio_file.parent.glob(f"*{suffix}")
            if item.is_file() and ".untrimmed." not in item.name
        )
        if loose:
            return loose[0]
    return audio_file if audio_file.is_file() else None


def load_candidate(project_dir: Path) -> Candidate:
    """Read one finished project. Requires a review; the rest is optional."""

    project_dir = Path(project_dir)
    review_path = project_dir / "generation_review.json"
    if not review_path.is_file():
        raise FileNotFoundError(f"no review to report on: {review_path}")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    alignment = review["alignment"]

    defects_payload = review.get("material_defects")
    if defects_payload is None:
        defects_path = project_dir / "material_defects.json"
        defects_payload = (
            json.loads(defects_path.read_text(encoding="utf-8"))
            if defects_path.is_file()
            else None
        )
    scanned = defects_payload is not None
    defects_payload = defects_payload or {"findings": [], "measurements": {}}
    findings = tuple(
        item for item in defects_payload.get("findings", [])
        if item.get("severity") in {"blocking", "warning"}
    )
    measurements = defects_payload.get("measurements", {})

    audio_file = None
    analysis_path = project_dir / "audio_analysis.json"
    if analysis_path.is_file():
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        candidate = project_dir / analysis.get("audio_file", "")
        audio_file = candidate if candidate.is_file() else None

    peaks = _peaks(audio_file) if audio_file is not None else ()
    return Candidate(
        project_dir=project_dir,
        name=project_dir.name,
        alignment=float(alignment["score"]),
        grade=str(alignment["grade"]),
        defects=findings,
        scanned=scanned,
        measurements=measurements,
        audio_file=audio_file,
        playable=_playable(audio_file, project_dir),
        duration_sec=(
            float(measurements.get("duration_sec") or 0.0)
            or (_duration(audio_file) if audio_file is not None else 0.0)
        ),
        peaks=peaks,
        section_marks=_section_marks(project_dir),
        defect_marks=_defect_marks(findings, measurements),
    )


def rank(candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
    """Usable takes first, then by alignment. Same rule the revision log uses."""

    return tuple(sorted(candidates, key=lambda item: (item.standing, -item.alignment)))


def _waveform_svg(candidate: Candidate) -> str:
    width, height = 900, 120
    middle = height / 2
    if not candidate.peaks:
        return '<p class="muted">no waveform: the audio for this take is not on disk</p>'
    step = width / len(candidate.peaks)
    body = [
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'class="wave" role="img" aria-label="waveform of {html.escape(candidate.name)}">'
    ]
    points = []
    for index, (low, high) in enumerate(candidate.peaks):
        x = index * step
        points.append(f'<rect x="{x:.2f}" y="{middle + low * middle:.2f}" '
                      f'width="{max(step, 0.9):.2f}" '
                      f'height="{max((high - low) * middle, 0.6):.2f}" />')
    body.append(f'<g class="peaks">{"".join(points)}</g>')

    duration = candidate.duration_sec or 1.0
    for at, name in candidate.section_marks:
        if at <= 0:
            continue
        x = min(width, at / duration * width)
        body.append(f'<line class="section" x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{height}" />')
        body.append(
            f'<text class="label" x="{x + 3:.1f}" y="12">{html.escape(name)}</text>'
        )
    for at, code, severity in candidate.defect_marks:
        x = min(width, at / duration * width)
        body.append(
            f'<line class="defect {severity}" x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{height}" />'
        )
        body.append(
            f'<text class="defect-label {severity}" x="{x + 3:.1f}" y="{height - 4}">'
            f'{html.escape(code)} @ {at:.1f}s</text>'
        )
    body.append("</svg>")
    return "".join(body)


def _relative(target: Path, base: Path) -> str:
    """A path the page can follow after the whole tree is moved or copied.

    ``Path.relative_to`` refuses to walk upwards, and takes live in sibling
    directories -- rounds are written beside their source, not inside it. An
    absolute file:// URI would work today and break the first time the folder
    is moved or handed to someone else.
    """

    try:
        return os.path.relpath(target.resolve(), base.resolve())
    except ValueError:  # different drive on Windows: nothing relative exists
        return target.resolve().as_uri()


_SCRIPT = r"""<script>
// Builds a command. It never runs one -- this page has no way to reach the
// machine it describes, and that is the point: the takes are candidates and the
// instruction is something you decide after listening.
//
// Kept out of the page template deliberately: written inside an f-string this
// passed through two layers of escaping, which turned \n into a real newline
// inside a JS string literal and quietly ate a backslash out of the shell
// quoting. One layer, raw string, no escaping to reason about.
function shellQuote(text) {
  // POSIX single-quoting: everything inside '...' is literal, and a single quote
  // is closed, escaped and reopened. Instructions here are prose and will carry
  // spaces and punctuation a shell would otherwise read.
  return "'" + text.replace(/'/g, "'\\''") + "'";
}
for (const form of document.querySelectorAll('.instruct')) {
  const input = form.querySelector('input');
  const out = form.querySelector('.command');
  const copy = form.querySelector('.copy');
  const project = form.dataset.project;
  const render = () => {
    const text = input.value.trim();
    if (!text) { out.textContent = ''; copy.hidden = true; return; }
    out.textContent =
      'python3 -m kihachi_music_ai edit ' + shellQuote(project) + ' ' + shellQuote(text) +
      '\n# 確認してから: python3 -m kihachi_music_ai apply-edit ' +
      shellQuote(project) + ' ' + shellQuote(project + '-v2');
    copy.hidden = false;
    copy.textContent = 'copy';
  };
  input.addEventListener('input', render);
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(out.textContent);
      copy.textContent = 'copied';
    } catch (error) {
      // file:// pages do not always get clipboard access; select it instead so
      // the keyboard still works.
      const range = document.createRange();
      range.selectNodeContents(out);
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      copy.textContent = 'press Cmd/Ctrl-C';
    }
  });
}
</script>"""


def build_report(
    candidates: Sequence[Candidate],
    *,
    base_dir: Path,
    title: str = "KIHACHI candidates",
    stopped_because: str | None = None,
    decision: dict[str, Any] | None = None,
) -> str:
    """One self-contained page. Audio is linked, never embedded."""

    ordered = rank(candidates)
    rows = []
    for position, item in enumerate(ordered, start=1):
        if not item.scanned:
            defects = (
                '<li class="unknown">not scanned &mdash; run <code>analyze</code> '
                "before trusting this row</li>"
            )
        else:
            defects = (
                "".join(
                    f'<li class="{html.escape(d["severity"])}">'
                    f'<strong>{html.escape(d["code"])}</strong> {html.escape(d["detail"])}</li>'
                    for d in item.defects
                )
                or '<li class="clean">no defects found</li>'
            )
        player = (
            f'<audio controls preload="metadata" src="{html.escape(_relative(item.playable, base_dir))}"></audio>'
            if item.playable is not None
            else '<p class="muted">nothing playable on disk for this take</p>'
        )
        rows.append(
            f'''<article class="take{' unusable' if item.standing == 2 else ''}">
  <header>
    <span class="rank">#{position}</span>
    <h2>{html.escape(item.name)}</h2>
    <span class="score">{item.alignment:.2f}</span>
    <span class="grade">{html.escape(item.grade)}</span>
    {'<span class="flag">blocking defect</span>' if item.standing == 2 else ''}
    {'<span class="unknown">not scanned</span>' if not item.scanned else ''}
  </header>
  {player}
  {_waveform_svg(item)}
  <ul class="defects">{defects}</ul>
  <form class="instruct" data-project="{html.escape(str(item.project_dir.resolve()))}"
        onsubmit="return false">
    <label>このテイクへの指示
      <input type="text" placeholder="Dropのベースだけもっと変態的に" spellcheck="false">
    </label>
    <pre class="command" aria-live="polite"></pre>
    <button type="button" class="copy" hidden>copy</button>
  </form>
</article>'''
        )

    note = (
        f'<p class="stopped">stopped because {html.escape(stopped_because)}</p>'
        if stopped_because
        else ""
    )
    if decision is None:
        decision_note = ""
        decision_lede = "Nothing is adopted here; choose by listening."
    else:
        selected = decision.get("selected") or {}
        selected_name = html.escape(str(selected.get("name", "unknown")))
        reason = html.escape(str(decision.get("reason", "")))
        audio_status = decision.get("audio_status") or {}
        status = str(audio_status.get("status", "unverified"))
        if status == "current":
            status_text = "selected Audio SHA-256 still matches"
        elif status == "changed":
            status_text = "warning: selected Audio changed after this decision"
        elif status == "missing":
            status_text = "warning: selected Audio is missing"
        else:
            status_text = "selected Audio SHA-256 was not rechecked"
        decision_note = (
            '<p class="decision">Current human listening decision: '
            f'<strong>{selected_name}</strong> &mdash; {reason}<br>'
            f'<span>{html.escape(status_text)}</span></p>'
        )
        decision_lede = (
            "This page does not move or replace audio; the recorded listening "
            "decision is shown below."
        )
    return f"""<!doctype html>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; --fg: #16181d; --bg: #fbfbfc; --line: #d8dae0;
           --muted: #6c7280; --wave: #4d6fd0; --sec: #9aa3b2; --warn: #b3690a; --block: #c0362c; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg: #e6e8ec; --bg: #14161a; --line: #2c3038; --muted: #9199a6;
             --wave: #7b9bf0; --sec: #5c6577; --warn: #e0942f; --block: #ef6a5e; }}
  }}
  body {{ margin: 0 auto; padding: 2rem 1.25rem 4rem; max-width: 62rem;
         font: 15px/1.6 ui-sans-serif, system-ui, sans-serif; color: var(--fg); background: var(--bg); }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  .lede {{ color: var(--muted); margin: 0 0 2rem; max-width: 46rem; }}
  .stopped {{ color: var(--muted); font-size: .9rem; margin: -1.5rem 0 2rem; }}
  .decision {{ border-left: 3px solid var(--wave); padding: .45rem .75rem;
               margin: -1rem 0 1.5rem; background: color-mix(in srgb, var(--wave) 8%, transparent); }}
  .take {{ border: 1px solid var(--line); border-radius: 10px; padding: 1rem 1.1rem;
           margin-bottom: 1.25rem; }}
  .take.unusable {{ border-color: color-mix(in srgb, var(--block) 45%, var(--line)); }}
  .unknown {{ color: var(--muted); font-size: .85rem; }}
  .take header {{ display: flex; align-items: baseline; gap: .6rem; flex-wrap: wrap; margin-bottom: .7rem; }}
  .take h2 {{ font-size: 1rem; margin: 0; font-weight: 600; flex: 1 1 auto; }}
  .rank {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
  .score {{ font-size: 1.25rem; font-variant-numeric: tabular-nums; }}
  .grade {{ color: var(--muted); }}
  .flag {{ color: var(--block); font-size: .85rem; }}
  audio {{ width: 100%; margin-bottom: .6rem; }}
  .wave {{ width: 100%; height: 120px; display: block; overflow: hidden; }}
  .peaks rect {{ fill: var(--wave); }}
  .section {{ stroke: var(--sec); stroke-width: 1; stroke-dasharray: 3 3; }}
  .label {{ fill: var(--muted); font-size: 9px; }}
  .defect {{ stroke-width: 1.5; }}
  .defect.warning {{ stroke: var(--warn); }}
  .defect.blocking {{ stroke: var(--block); }}
  .defect-label {{ font-size: 9px; }}
  .defect-label.warning {{ fill: var(--warn); }}
  .defect-label.blocking {{ fill: var(--block); }}
  ul.defects {{ list-style: none; padding: 0; margin: .7rem 0 0; font-size: .9rem; }}
  ul.defects li {{ padding: .15rem 0; }}
  li.blocking {{ color: var(--block); }}
  li.warning {{ color: var(--warn); }}
  li.clean, .muted {{ color: var(--muted); }}
  .instruct {{ margin-top: .9rem; border-top: 1px solid var(--line); padding-top: .8rem; }}
  .instruct label {{ display: block; font-size: .85rem; color: var(--muted); }}
  .instruct input {{ display: block; width: 100%; margin-top: .3rem; padding: .45rem .6rem;
                     font: inherit; color: var(--fg); background: transparent;
                     border: 1px solid var(--line); border-radius: 6px; }}
  pre.command {{ margin: .6rem 0 .4rem; padding: .6rem .7rem; overflow-x: auto;
                 background: color-mix(in srgb, var(--fg) 6%, transparent);
                 border-radius: 6px; font-size: .82rem; white-space: pre; }}
  pre.command:empty {{ display: none; }}
  .copy {{ font: inherit; font-size: .82rem; padding: .3rem .7rem; cursor: pointer;
           color: var(--fg); background: transparent;
           border: 1px solid var(--line); border-radius: 6px; }}
</style>
<h1>{html.escape(title)}</h1>
<p class="lede">Ranked with takes that have no blocking defect first, then by how
closely they followed the SongSpec. That score cannot hear whether a take is any
good &mdash; changing only the seed has moved it by 33 points &mdash; so it
orders candidates and nothing more. {decision_lede}</p>
{note}
{decision_note}
{"".join(rows)}
{_SCRIPT}
"""
