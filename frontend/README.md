# Portfolio Analyzer — web app

React 19 + TypeScript + Vite, TanStack Query, Recharts, Tailwind. The backend is the
FastAPI service in `../backend`; this app only reads and renders what it serves.

```bash
npm ci
npm run dev          # http://localhost:5173, /api proxied to localhost:8000
npx tsc -b           # typecheck
npm run test         # vitest: pure lib tests in node, component tests in jsdom
npm run build        # production bundle in dist/
```

Read `../docs/local-development.md` before pointing a dev server at real data — two
read endpoints can reach Yahoo — and `../docs/frontend.md` before changing anything here.

## Where things are

| Path | What |
|---|---|
| `src/index.css` | Theme tokens (light and dark), the chart palettes, global chart CSS |
| `src/components/ui/` | The primitives every tab is built from: `Card`, `KpiCard`/`KpiPanel`, `DataTable` (one column description, a table on desktop and cards on a phone), `SegmentedControl`, `CollapsibleCardHeader`, `TickerAvatar` |
| `src/components/` | One file per tab and per panel; `Dashboard.tsx` assembles the stock view, `CryptoDashboard.tsx` the crypto one |
| `src/lib/` | Pure logic with tests beside it: KPIs and returns, forecasting, rebalancing, colours, formatting, the API client |

Formatting goes through `src/lib/utils.ts` (currency, dates, axis labels — all pinned to
`en-US`), and a chart's grid and tooltip through `src/lib/chartTooltip.ts`. Several
`*.test.ts` files scan the source for hand-rolled copies of these, so a new chart or table
that rolls its own fails the suite.
