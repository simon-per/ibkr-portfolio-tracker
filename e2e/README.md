# Browser checks

Seven Playwright scripts covering what unit tests structurally cannot see: keyboard and ARIA on the
real page, the production CSP, the shape of the built bundle, how the UI behaves when the backend is
down, and whether the page fits a phone.

These earned their place. The prod-snapshot + browser pass they came from found **three defects that
442 passing backend tests did not** — trades converted with only the EUR→base factor (a CAD realized
gain read 62% high), 67 BUY rows asserting a `0.00` realized result, and fractional share counts
rounding to `0`/`-0`. All three are pinned in `ledger.mjs`.

## Why this is a separate package

It cannot be a `frontend` devDependency: `deploy.sh` runs `npm ci` inside `frontend/` on every
`--no-cache` rebuild, and Playwright's postinstall pulls ~150 MB of Chromium — which would tax a
deploy that already runs every 10 minutes. Nothing in the deploy path touches this directory.

```bash
cd e2e && npm install && npx playwright install chromium
```

## Preconditions differ per script — read these

Most need a dev server on `localhost:5173`. **Check which port Vite actually took**: if 5173 is
occupied it silently moves to 5174, and `frontend/.env` points `VITE_API_URL` at `localhost:8000`,
so only origins listed in `CORS_ORIGINS` work — any other port fails every request with a CORS error
that looks exactly like a backend outage.

Set `SCHEDULER_ENABLED=false` in `backend/.env` for any local run, or starting uvicorn arms the five
daily jobs against the live Flex token and Yahoo.

| Script | Needs |
|---|---|
| `npm run a11y` | dev server **+ backend with data** |
| `npm run sweep` | dev server + backend with data |
| `npm run csp` | `vite preview` on 4173 (**built output**, not the dev server) |
| `npm run ledger` | dev server + backend on a **production snapshot** |
| `npm run errors` | dev server, backend **deliberately stopped** |
| `npm run mobile` | dev server **+ backend with data**, at 390x844 |
| `npm run axis` | backend with **real** data (see below); runs at both viewports |
| `npm run chunks` | `npm run build && npx vite preview --port 4173` in `frontend/`; no backend |

`BASE` and `PREVIEW` override the URLs.

`a11y.mjs` said "backend optional" until 2026-07-31 and it is not: three of its checks need one.
`aria-sort` headers only exist once Fundamentals and Watchlist have rows to sort, the footer assertion
reads `/health`, and a stopped backend fills the console with `ERR_CONNECTION_REFUSED` so the
zero-console-errors check fails too. With no backend it reports 11/14 and every failure is a phantom —
which either sends you chasing an ARIA regression that isn't there or teaches you to ignore red.
Any populated database is enough here (unlike `ledger.mjs` below) — it needs positions, not realistic
ones. **`backend/portfolio.db` is gitignored and untracked**, so a fresh clone has none and the backend
creates an empty one on first start; `a11y` then fails the `aria-sort` and target-input checks for want
of rows, which looks exactly like a regression. Point `DATABASE_URL` at a populated copy first.

`mobile.mjs` is the phone check, and horizontal overflow is the reason it exists: it is a property of
the assembled page at a real width, jsdom loads no CSS so nothing under `frontend/src/` can observe
it, and every other script here opens at 1440px or wider. Per tab it asserts no horizontal page
scroll, **names the offending element and its width** when there is one, that charts fit their box,
and no console errors; then tap targets against WCAG 2.2's 24px floor and that the tab strip stays
pinned.

Two of its assertions are deliberately paired. The lazy way to stop the page scrolling sideways is
`body { overflow-x: hidden }` — which does not remove an overflow but clips it, and which forces the
block axis to `overflow-y: auto` and so kills `position: sticky`. That shortcut passes "no horizontal
scroll" and fails "strip stays pinned". Do not satisfy one without reading the other.

**It passes vacuously with no data**, exactly like `a11y` does: every panel is then an empty state,
and an empty state cannot overflow. Point `DATABASE_URL` at a populated copy first. `SCREENSHOT=1`
writes `mobile-<tab>.png` beside the script.

`axis.mjs` checks that the portfolio chart's Y axis does not reserve space below zero that the data
cannot reach. It ran to −20,000 on a phone while the three default series spanned +1,122 … +65,025 —
padding taken as a share of the whole range, rounded out to a full negative step, leaving a fifth of
the plot permanently blank. `niceTicks.test.ts` pins the arithmetic; this pins the render at a real
viewport, which is the part that was wrong on screen.

It compares the axis against the **live data** rather than a fixed number, so it stays correct if the
portfolio does go negative: a real value below the −5k floor must still be shown, because that floor
is soft by design. Consequently it needs *real* data, not merely populated data — a fixture whose
minimum happens to be negative exercises the other branch. Two selectors are used for the tick
labels because this recharts version renders them in a sibling group rather than under
`.recharts-yAxis`; the obvious selector returns zero nodes, which reads exactly like a chart that
never rendered.

`ledger.mjs` asserts against real account shapes — transfers badged *not money in*, fractional
quantities, a deposit row. The local `backend/portfolio.db` predates trades, cash flows and the IBKR
dividend era, so every assertion in it fails there. Take a snapshot with `sqlite3 .backup` on the
VPS, point `DATABASE_URL` at it, and **delete it afterwards**: it is real account data.

It **narrows to the Cash event type before asserting anything about transfers**, and that is not
cosmetic: the account's only transfer is the in-kind arrival of 2026-01-21, and while the default
`1Y` window still reaches it, that window now holds ~175 rows — so the transfers, being the oldest,
fell past the first page of 100 and the two assertions went red as the account aged rather than
because anything broke. Trades are what accumulate; the 47 cash rows will not. The trade-shaped
assertions (realized-P&L, fractional quantities) therefore run on the unfiltered panel first, and a
narrowing check sits between them so a renamed filter button cannot silently restore the old
behaviour.

`chunks.mjs` runs against the *built* output because chunk boundaries do not exist in the dev
server. It also fails deliberately if someone lazy-loads Recharts — three components on the default
Performance tab use it, so deferring it moves the wait rather than removing it.

`csp.mjs` runs against the built output for a subtler reason: **the dev server cannot pass a strict
CSP and never could.** Vite injects an inline `<script type="module">` for react-refresh, which
`script-src 'self'` blocks — so pointing this at :5173 reports a violation that does not exist in
production, where nginx serves a build containing no inline scripts. A CSP check against a dev
server tests the HMR transport, not the policy.

## Output

Each script prints `PASS`/`FAIL` lines and exits non-zero on any failure, so they compose in a shell
loop. They are not wired into CI: CI has no browser, no snapshot and no backend, and a suite that
cannot run is worse than one you have to invoke.

Screenshots are gitignored — they contain real account data and this repo is public.
