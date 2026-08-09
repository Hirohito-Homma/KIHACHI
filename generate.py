#!/usr/bin/env python3
"""One command: brief -> SongSpec + MIDI -> ACE-Step -> a file you can play.

    python3 generate.py "ダブとミューテーションファンクの32小節。110BPM、D#マイナー。"

This is a wrapper, not a second client. Everything it does goes through the
adapter the rest of KIHACHI uses (`AceStepClient`, `render_with_ace_step`), so
there is one implementation of the ACE-Step contract to keep correct.

Two choices are made for you, both for reasons the project measured:

* the render is asked for in WAV, whatever you want to listen to. ACE-Step will
  happily hand back MP3, but the analyzer and the defect scan read WAV through
  the stdlib `wave` module -- take MP3 out of the server and the material can
  never be checked again. The MP3 is made locally afterwards, from the WAV;
* tail guard is on. ACE-Step composes a complete song inside its buffer and
  finishes early, leaving the last bar silent. Asking for extra length and
  trimming back is what fixes it, and it is not optional in practice: the first
  render against this server came back with a 2.44 s hole at 67.36 s.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from kihachi_music_ai.adapters.ace_step import (  # noqa: E402
    AceStepClient,
    AceStepConfig,
    AceStepError,
    AceStepOptions,
    render_with_ace_step,
)
from kihachi_music_ai.defects import scan_material  # noqa: E402
from kihachi_music_ai.pipeline import compose_project  # noqa: E402
from kihachi_music_ai.tail_guard import DEFAULT_TAIL_GUARD_BARS  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_PROJECTS = Path("/Volumes/NO NAME/ACE-Step/projects")


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _progress():
    """Report while waiting. A render takes minutes; silence looks like a hang.

    Redraws one line on a terminal, but falls back to a line every 30 s when
    stdout is a pipe or a log -- carriage returns do not overwrite there, they
    just produce one enormous line.
    """

    tty = sys.stdout.isatty()
    last = [-30.0]

    def report(result, waited: float) -> None:
        stage = {0: "queued/running", 2: "failed"}.get(result.status, f"status {result.status}")
        if tty:
            print(f"\r  ... {stage}, {waited:4.0f}s elapsed", end="", flush=True)
        elif waited - last[0] >= 30.0:
            last[0] = waited
            print(f"  ... {stage}, {waited:4.0f}s elapsed", flush=True)

    return report


def _to_mp3(wav: Path, destination: Path) -> Path | None:
    """Encode a listening copy from the WAV, using whatever encoder is installed."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    for command in (
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-b:a", "256k", str(destination)],
        ["lame", "--quiet", "-b", "256", str(wav), str(destination)],
    ):
        try:
            subprocess.run(command, check=True, capture_output=True)
            return destination
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    # afconvert ships with macOS but only encodes AAC, so it is the fallback
    # rather than the first choice: it cannot produce the .mp3 that was asked for.
    aac = destination.with_suffix(".m4a")
    try:
        subprocess.run(
            ["afconvert", "-f", "mp4f", "-d", "aac", "-b", "256000", str(wav), str(aac)],
            check=True,
            capture_output=True,
        )
        return aac
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KIHACHI -> ACE-Step, in one command")
    parser.add_argument("brief", nargs="?", help="natural-language music brief")
    parser.add_argument("--project", type=Path, help="render an existing project instead")
    parser.add_argument("--name", help="output name; defaults to a timestamp")
    parser.add_argument(
        "--library",
        type=Path,
        help=(
            "put the listening copy somewhere else; by default it lands beside "
            "the WAV, so one song stays one directory"
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--seed", type=int, default=8)
    parser.add_argument("--no-lyrics", action="store_true", help="render instrumental")
    parser.add_argument("--inference-steps", type=int, default=60)
    parser.add_argument("--wait-timeout", type=float, default=1800.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--keep-wav-only", action="store_true", help="skip the listening copy")
    args = parser.parse_args(argv)

    if not args.brief and not args.project:
        parser.error("give a brief, or --project to re-render an existing one")

    name = args.name or f"kihachi-{_stamp()}"
    if args.project:
        project = args.project
        print(f"Project: {project}")
    else:
        project = DEFAULT_PROJECTS / name
        if not project.parent.exists():
            project = Path("output") / name
        print(f"Composing: {project}")
        compose_project(args.brief, project, seed=args.seed)

    spec_lines = (project / "prompt.txt").read_text(encoding="utf-8").strip().splitlines()
    print(f"  prompt: {spec_lines[0][:78]}" if spec_lines else "")

    client = AceStepClient(AceStepConfig(base_url=args.base_url))
    sheet = project / "lyrics.txt"
    lyrics = "" if args.no_lyrics else (
        sheet.read_text(encoding="utf-8") if sheet.is_file() else ""
    )
    options = AceStepOptions(
        audio_format="wav",
        inference_steps=args.inference_steps,
        lyrics=lyrics,
        tail_guard_bars=DEFAULT_TAIL_GUARD_BARS,
    )
    print(f"Rendering via {args.base_url} (tail guard {DEFAULT_TAIL_GUARD_BARS} bars)")
    started = time.monotonic()
    try:
        manifest = render_with_ace_step(
            project,
            client,
            options,
            poll_interval=args.poll_interval,
            wait_timeout=args.wait_timeout,
            on_poll=_progress(),
        )
    except AceStepError as error:
        print(f"\nerror: {error}", file=sys.stderr)
        return 2
    elapsed = time.monotonic() - started
    if not manifest.audio_files:
        print("\nerror: ACE-Step returned no audio", file=sys.stderr)
        return 2
    wav = Path(manifest.audio_files[0])
    print(f"\rDone in {elapsed:.0f}s: {wav}{' ' * 24}")

    report = scan_material(wav)
    if report["clean"]:
        print("- material: clean")
    else:
        for finding in report["findings"]:
            if finding["severity"] in {"blocking", "warning"}:
                print(f"- {finding['severity']}: {finding['detail']}")

    if not args.keep_wav_only:
        library = args.library or wav.parent
        listening = _to_mp3(wav, library / f"{name}.mp3")
        if listening is None:
            print("- no encoder found; the WAV above is the only copy")
        else:
            print(f"- listening copy: {listening}")
            print(f'  open "{listening}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
