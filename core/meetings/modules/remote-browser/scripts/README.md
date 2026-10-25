# remote-browser/scripts

[`check-isolation.js`](check-isolation.js) — the brick's `gate:isolation` (P2) check.
`@munshi/remote-browser` is a Node brick: only `playwright` / `playwright-extra` (the persistent-context
launch) + Node builtins (child_process/fs/path) — never another brick's internals, never the bot/service.
