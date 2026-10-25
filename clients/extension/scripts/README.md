# extension/scripts

[`check-isolation.js`](check-isolation.js) — the brick's `gate:isolation` (P2) check.
`@munshi/extension` source-bundles sibling capture bricks via `build.mjs`'s esbuild `alias`
map, so the check allows every import that is intra-package, a builtin, a declared dep, or
a `@munshi/*` specifier explicitly wired in `build.mjs` — and flags an undeclared dep or an
import of an **unwired** brick (which would silently fail to bundle).
