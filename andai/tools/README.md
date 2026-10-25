# andai/tools

Developer tools for the munshi.ai repository.

- `run-suites.sh OUT.json [--only SUBSTR]... [--skip-install]` — discovers and runs every unit test
  suite in the repo (pnpm/npm packages with a `test` script and uv projects with a `tests/` folder)
  and writes one JSON result per suite (`suite`, `dir`, `kind`, `status`, `failing_tests`, ...) to
  `OUT.json`, with per-suite logs next to it. Use it to compare test results before and after a
  large change.
