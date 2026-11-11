# meetings/services/bot/mock — the MOCK BOT (Lane A instrument)

**Concern:** a contract-faithful stand-in for the real bot, so the **backend** can be validated in
isolation (SoC: backend ⊥ worker) at **L3** on the real compose stack — no browser, no STT, no GPU,
runnable anywhere (ARCH §5; P5/P16). It is the instrument the real bot's L4 capture-quality lane
(`meetings/services/bot/eval/`) is deliberately *not*: cheap and broad, reserving the scarce L4 oracle.

**How it stays faithful:** it reuses the bot's REAL `createOrchestrator` + the REAL adapters
(`lifecycle-http` · `transcript-redis` · `acts-redis` + a recording uploader); only the two heavy
ports are faked — `JoinDriver` → `fakeJoinDriver`, `Pipeline` → `fakePipeline` (`scenarios.ts`). The
backend therefore sees prod-identical lifecycle.v1 emission, and the mock **cannot** emit off-contract
— re-proven by `mock.test.ts` (every emission validated vs the sealed `lifecycle.v1`/`transcript.v1`).

**Surface:**
- `scenarios.ts` — the scenario registry + the two fake ports. Scenario via env `MOCK_SCENARIO`.
- `main.ts` — the mock composition root (real adapters + the fakes); the worker entrypoint baked into
  `mock-bot:dev` (swapped in via `BROWSER_IMAGE` for the `MOCK_BOT=1` `gate:compose` run).
- `mock.test.ts` — the L2 fidelity test (offline; `npx tsx mock/mock.test.ts`).

**Scenarios:** `normal · join-timeout · reject · crash · immediate-stop · continue · speak-ack ·
emit-n-segments · slow-join · recording · silence-left-alone · left-alone · evicted · paced ·
hindi-lines · hold` — one per backend behaviour (lifecycle FSM · join-retry · attributable failure ·
max-bots-adjacent · recording receiver · acts round-trip · leave-when-alone · removal by a
participant). Three more serve other checks: `paced` (12 s in the lobby, then 8 lines 1.5 s apart) is
for watching a meeting in a browser, `hindi-lines` (six fixed lines that mix Devanagari and
Roman Hinglish, `HINDI_LINES`, and one recording chunk, then the same end as `emit-n-segments`) is
for transcript normalisation, search and deletion, and `hold` is for load tests: it stays in
`active` with one line every 30 s until the backend stops it or `hold_s` seconds pass (from the
meeting URL, `?mock=hold&hold_s=600`; default 600, clamped to 60–7200), so hundreds of mock
meetings can be live at once. It exists only in this test-only mock; a real bot never reads
`hold_s`. Selection, first match
wins: env `MOCK_SCENARIO`, a bot name `mock:<scenario>`, a `mock=<scenario>` query parameter on the
meeting URL. `normal` also
reports a `chat` disclosure outcome when the invocation carries a `disclosure` (the real
`../src/disclosure.ts` hook and report, with a fake sender).

**Depends on:** `../src/{orchestrator,contracts,config,ports,adapters/*}` (the real bot, reused). It is
**not** a published library (no consumers import it) — it is a test/validation worker.
