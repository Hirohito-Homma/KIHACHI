# Producer status — 2026-09-05T04:05:45.121970+00:00

## ACE-Step
Unreachable on this host (ports 8001, 7860, 8000, 8188).

## Queue handling
| Project | Local slice | Audio | Action this shift |
|---------|-------------|-------|-------------------|
| mutation-signal-premiere | yes | no | hold — do not re-slice |
| mutation-signal-process-short | yes | no | hold — do not re-slice |

## Decision
Skip duplicate `local-slice`. Wait for ACE-Step, then `audio-slice` both projects before packager overwrite.
