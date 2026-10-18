# Tech stack, bundle boundaries, e2e

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Tech stack

**Backend** — FastAPI (async), SQLAlchemy 2.0 + aiosqlite (WAL), Alembic, APScheduler with a
persistent job store, `ibflex` **0.15** (pinned — it aborts on unknown attributes, hence
`_sanitize_flex_xml`), `yfinance` >= 1.1.0, Frankfurter for FX with `open.er-api.com` as the
bounded fallback. Pins are in `backend/requirements.txt` and were moved to current on
2026-09-08; `pip-audit` or an OSV query against them is worth running when they age again.
**Frontend** — React 19 + TypeScript + Vite, TanStack Query, Recharts, Tailwind + shadcn/ui.
Vitest runs in `node` by default; component tests opt into jsdom per file with a
`// @vitest-environment jsdom` docblock, because the pure `lib/` tests are the large majority and
paying jsdom's startup for all of them is the wrong default.

**Node 22** in three places that move together: `setup-node` in `.github/workflows/ci.yml`, the
NodeSource line in `deploy.sh` (fresh hosts only; an installed Node is left alone) and
`engines` in `frontend/package.json`, which makes `npm ci` warn `EBADENGINE` on an older one.
**Static checks in CI:** ESLint (`npm run lint`) and ruff with correctness rules only
(`backend/ruff.toml`: `E9`, `F63`, `F7`, `F82`). Ruff is not in `requirements.txt` (that is what
the image installs), so CI pins its version. `F821` is ignored in `app/models/` only:
SQLAlchemy resolves the string annotations there (`Mapped["Security"]`) through its registry.
Workflow actions are pinned to commit shas with the tag as a comment.

**The bundle is code-split, and two of the boundaries are deliberate.** The eight non-default tabs
are `React.lazy` in `Dashboard.tsx` (safe because `TabsContent` returns `null` while inactive, so a
panel is not mounted until selected). **Recharts stays eager** — `PortfolioValueChart`,
`PerformanceAttribution` and `MonthlyDeploymentCard` are all on the default Performance tab, so
deferring it would only move the wait; don't "optimise" it into a lazy chunk. `manualChunks` in
`vite.config.ts` splits `react` / `charts` / `query` mainly for **caching**: the VPS redeploys within
minutes of any push and nginx serves `/assets/` `immutable`, so keeping vendor code out of the
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
produce chunk boundaries at all. **Install it with `npm ci`**: `e2e/package-lock.json` is tracked
since 2026-10-18 and pins Playwright (1.62.1) and with it the Chromium build. Before that, each
`npm install` could resolve a different one, and the pixel and axis checks moved with it.
Bump it deliberately (`npm install playwright@<version>` in `e2e/`, then
`npx playwright install chromium`).

---
