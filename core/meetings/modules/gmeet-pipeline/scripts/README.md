# gmeet-pipeline/scripts

[`check-isolation.js`](check-isolation.js) — the brick's `gate:isolation` (P2) check.
`@munshi/gmeet-pipeline` may import only the shared engine
(`@munshi/transcribe-{buffer,whisper}`) and declared devDeps — never another brick's
internals.
