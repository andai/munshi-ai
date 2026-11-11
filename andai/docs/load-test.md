# Load test with the mock bot

How to find out how many meetings a deployment holds at once, and to check that a dispatch beyond
that is refused as `capacity_exhausted` instead of stalling. The tool is
`andai_core.testing.load`, run through `make -C andai load-test`. It drives **mock meetings only**:
the mock bot's `hold` scenario, no browser, no speech, no real Meet, Teams or Zoom meeting.

## What it does

1. Signs in to andai-core as a member of the load org.
2. Checks that the org's own bot cap is at least N. Each org's platform principal has a cap
   (`ANDAI_ORG_MAX_CONCURRENT_BOTS`, default 3); if it is lower than N it refuses first, and its
   refusal would look like cluster capacity. The run stops with exit 2 and names the cap.
3. Dispatches N Zoom-shaped links (`https://zoom.us/j/<random>?pwd=loadtest&mock=hold&hold_s=S`,
   each a new meeting number) with at most 20 dispatches in flight.
4. Reads every meeting from andai-core every 2 s (`GET /api/v1/meetings/{id}`; andai-core reads its
   own rows, so the gateway's per-IP rate limit is not involved) until each is `recording` or has
   ended, then keeps reading for 10 s. At every read it counts the meetings in `recording` at that
   moment; the largest count is `peak_recording`. A mock meeting that ends after a second is never
   counted as concurrent with the next one.
5. Stops every meeting and waits until all have ended.

## Prerequisites

- **A test stack**, never the live one: `make -C andai load-test` refuses `PROJECT=andai-meet`, and
  refuses a runtime whose `BROWSER_IMAGE` is not the mock bot (the links are random Zoom meeting
  numbers, which a real bot would try to join).
- **The mock bot image** as the meeting bot: `BROWSER_IMAGE=mock-bot:dev`
  (`make -C andai images ONLY=mock-bot` builds it).
- **The runtime cap** to test: `RUNTIME_MAX_ACTIVE_BOTS` on the runtime (Compose) or
  `runtime.maxActiveBots` (chart). `CAP` must equal it; the target checks.
- **The org cap above N**: set `ANDAI_ORG_MAX_CONCURRENT_BOTS` to more than N for andai-core. The
  target runs `make -C andai principals-sync` first, so the principals carry it.
- **A load member**: an account in an org without required 2FA and without a second factor of its
  own, its password in a file (`PASSWORD_FILE`, mode 0600). Use a dedicated org with synthetic data.
- **hold_s** longer than the time all N meetings need to reach `recording` plus 10 s; the mock
  accepts 60–7200 s.

For example, on the e2e project:

```bash
BROWSER_IMAGE=mock-bot:dev RUNTIME_MAX_ACTIVE_BOTS=5 ANDAI_ORG_MAX_CONCURRENT_BOTS=50 \
  make -C andai up PROJECT=andai-e2e ENV_FILE=$PWD/.work/.local/env.e2e10
make -C andai load-test PROJECT=andai-e2e ENV_FILE=$PWD/.work/.local/env.e2e10 \
  N=8 CAP=5 HOLD_S=180 EMAIL=<load member> PASSWORD_FILE=/abs/path/to/password
make -C andai down PROJECT=andai-e2e ENV_FILE=$PWD/.work/.local/env.e2e10
```

## Reading the report

The report goes to stdout and to `REPORT` (default `$TMPDIR/munshi-load-report.json`). It holds
counts and timings only: no links, meeting ids, emails or passwords.

| Field | Meaning |
|---|---|
| `n`, `cap`, `org_cap` | meetings dispatched, the runtime cap, the org cap that applied |
| `expected_recording` | `min(n, cap)` |
| `peak_recording` | most meetings in `recording` at one read |
| `ratio` | `peak_recording / expected_recording`; the run needs at least `threshold` (default 0.95) |
| `latency_to_recording_s` | p50 / p95 / max from dispatch to the first read that saw `recording` (2 s resolution) |
| `refused_capacity` | meetings that failed as `capacity_exhausted` |
| `refused_within_s_max` | slowest refusal, from dispatch; must be at most 10 s |
| `other_failures` | meetings that never recorded for another reason (`http_<status>` for a refused join) |
| `all_terminal_after_stop`, `terminal_after_stop` | every meeting ended after the stop, by status |
| `passed`, `problems` | the verdict and why it failed |

The run passes when the ratio reaches the threshold, every refusal is `capacity_exhausted` within
10 s, the refusals match the dispatches over the cap (each other failure may free one slot), and
every meeting ended after the stop. Exit 0 pass, 1 fail, 2 setup error.

## Kubernetes extras

On a cluster, also check during the peak and after the stop (the staging run records these):

- `kubectl get pods --field-selector=status.phase=Pending -l runtime.managed=true` lists nothing:
  bots over the cap were refused, not left Pending.
- After the stop, `kubectl get pods -l runtime.managed=true` lists no bot Pods: none orphaned.
- `kubectl top nodes` at the peak, for the node's CPU and memory.

Mock-bot Pods need far less than a real bot. A load run may set
`runtime.workloadResources.meetingBot` smaller for the mock (and `runtime.k8sShmSize` to `""`), so
the node fits the planned count; the production defaults stay 2560Mi with a 2Gi `/dev/shm`.
