# andai/portal

The munshi.ai web portal (Next.js app router, Tailwind CSS, shadcn/ui; indigo and saffron theme,
light and dark). Screens: sign-in, two-factor login and setup, password reset, invite acceptance,
the dashboard shell, security settings and team administration (members, teams, security).

The browser talks only to the portal: `/api/*` is proxied to andai-core (`ANDAI_CORE_URL`, read per
request). Every decision comes from andai-core's answers (`lib/api.ts`).

```bash
npm --prefix andai/portal ci && npm --prefix andai/portal run build   # build
npm --prefix andai/portal run typecheck                               # app + e2e types
```

Browser tests: see `e2e/README.md`.
