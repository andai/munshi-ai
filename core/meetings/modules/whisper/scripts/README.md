# whisper/scripts

[`check-isolation.js`](check-isolation.js) — the brick's `gate:isolation` (P2)
check. `@munshi/transcribe-whisper` uses only Node/Web globals, so it must import
nothing external.
