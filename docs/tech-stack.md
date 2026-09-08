# Tech stack, bundle boundaries, e2e

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Tech stack

**Backend** — FastAPI (async), SQLAlchemy 2.0 + aiosqlite (WAL), Alembic, APScheduler,
`ibflex` **0.15** (pinned), `yfinance` >= 1.1.0, Frankfurter API for FX.
**Frontend** — React 19 + TypeScript + Vite, TanStack Query, Recharts, Tailwind + shadcn/ui.
Vitest runs in `node` by default; component tests opt into jsdom per file with a
`// @vitest-environment jsdom` docblock, because the pure `lib/` tests are the large majority and
paying jsdom's startup for all of them is the wrong default.

**The bundle is code-split, and two of the boundaries are deliberate.** The eight non-default tabs
are `React.lazy` in `Dashboard.tsx` (safe because `TabsContent` returns `null` while inactive, so a
panel is not mounted until selected). **Recharts stays eager** — `PortfolioValueChart`,
`PerformanceAttribution` and `MonthlyDeploymentCard` are all on the default Performance tab, so
deferring it would only move the wait; don't "optimise" it into a lazy chunk. `manualChunks` in
`vite.config.ts` splits `react` / `charts` / `query` mainly for **caching**: the VPS redeploys within
10 minutes of any push and nginx serves `/assets/` `immutable`, so keeping vendor code out of the
app chunk took the per-deploy re-download from 264 kB gzipped to ~52 kB. List chunk members by the
specifier that actually appears in the graph (`react-dom/client`, `react/jsx-runtime`) — naming the
bare packages emits a 0-byte chunk and leaves React in the app bundle.

Because a lazy chunk can 404 after a redeploy (content-hashed names, page held open across a
deploy), every lazy panel is wrapped in `ui/LazyTabPanel.tsx` — a *scoped* boundary. Without it that
rejection reaches `App.tsx`'s app-level boundary and blanks the whole dashboard, which is worse than
the eager import it replaced. Chunk boundaries are a build-output property no unit test can see, so
the end-to-end check is `e2e/lazychunks.mjs`.

**`e2e/` is the browser-check package**, deliberately separate from `frontend/`: `deploy.sh` runs
`npm ci` inside `frontend/` on every `--no-cache` rebuild and Playwright's postinstall pulls ~150 MB
of Chromium, which a 10-minute deploy cadence cannot absorb. Nothing in the deploy path touches it.
It covers what the unit suites structurally cannot see — keyboard/ARIA on the assembled page, the
production CSP, chunk boundaries, and the backend-down pass asserting no surface falls back to an
empty-data message. Read `e2e/README.md` first: **the preconditions differ per script**, and two of
them (`csp`, `chunks`) must run against `vite preview` rather than the dev server, because the dev
server emits an inline react-refresh script that `script-src 'self'` correctly blocks and does not
produce chunk boundaries at all.

---
