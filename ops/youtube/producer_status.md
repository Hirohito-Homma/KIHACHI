# Producer status (day 20) — 2026-09-22T04:14:27.509533+00:00

ACE-Step down (ports 8000/8001/7860/8080 unreachable). No `projects/*/audio/`.

**Skip re-slice** — both projects already have local MIDI/slice artifacts.

When endpoint is up:
```bash
python3 -m kihachi_music_ai audio-slice projects/mutation-signal-premiere
python3 -m kihachi_music_ai audio-slice projects/mutation-signal-process-short
```
Then hand to packager for `--overwrite`.
