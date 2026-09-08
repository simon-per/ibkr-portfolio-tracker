# The Swiss tax report

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Tax report (Swiss framing)

`GET /api/tax/report?year=YYYY` and `.csv`. Switzerland doesn't tax private capital gains but does tax
dividend income and allows reclaiming foreign withholding via **DA-1** — so the report leads with
dividend income + withholding, then realized gains, then a year-end holdings snapshot (Steuerwert).

Two honesty flags, both badged in the UI and CSV:
- `dividend_source`: `ibkr` (real withholding) vs `yfinance_estimate` (gross guess, no withholding)
  vs `mixed` (the boundary year). Dividends are **era-spliced with the same `_splice_by_era` the
  card uses** — estimates strictly before the globally first IBKR payment (`dividend_ibkr_from`),
  IBKR rows from there on — then windowed to the year. Both simpler schemes were bugs: a global
  "prefer ibkr" made 2025 report **0.00 labelled authoritative**, and a per-year boolean dropped
  the boundary year's real January income (the ledger starts mid-February). A per-year *boundary*
  would be wrong too — yfinance stores a dividend under its ex-date, IBKR under its pay date, so
  it would resurrect the double-count. The two sources are never summed for the same period; each
  income row carries its `source`.
- `realized_source`: `trades` (IBKR FIFO) vs `closed_lot_estimate` (market price at close date — was
  ~8% off on a spot check, hence the badge)

**A partial Steuerwert says so.** `holdings_snapshot_as_of` omits a lot it cannot price or
convert — correct, and stated below — but until 2026-08-05 it omitted it *silently*, so a wealth-tax
base missing a holding was served as though it covered the book. The report already handled the
snapshot **raising** (`holdings_snapshot_total` becomes `None`, plus a warning), and that asymmetry
is the bug: total failure was loud, partial failure was mute, which is backwards. A missing figure
reads as a fault; a plausible one reads as an answer — the same reason the timeline's `+15.3%` was
more dangerous than its `−100%`, and it matters more here than anywhere else in the app because this
number goes on a tax return. `PortfolioService.last_snapshot_skipped` is a per-run latch (same shape
as `MarketDataService.rate_limited`) naming the securities dropped; the report turns it into a
`warnings[]` line and still serves the figure, because a partial base is the best available and must
not be confused with the `None` reserved for no base at all. Note the latch resets on the
**early-return** path too — an empty snapshot inheriting a previous date's skip list would report the
wrong date's completeness.

**Steuerwert is valued at 31 December**, not today: `holdings_snapshot_as_of()` rebuilds the holdings
for `holdings_as_of` (= 31 Dec for a past year, today for the current one) using the same
`open_date`/`close_date` window as `_calculate_daily_value`, so it can't disagree with the portfolio
timeline. Positions with no resolvable price near that date are **omitted**, not counted as zero.

**A figure this report cannot justify is absent, not invented** (both halves were bugs until
2026-07-30, and both broke the rule the rest of the codebase follows: skip the row, log it, report it).

- `_to_eur()` returned the **unconverted foreign amount** when FX failed, so a TWD sale whose
  trade-date rate fell outside `FALLBACK_MAX_AGE_DAYS` read ~35× high while `realized_source` still
  said `trades` — the badge that means *authoritative* — with no `logger` call anywhere on the path. It
  returns `None` now and the realized loop omits the row. If **every** SELL is unconvertible the
  closed-lot approximation takes over (it reads `cost_basis_eur`, converted at ingest, so it needs no
  trade-date rate) and the warning says *that* rather than claiming a hole.
- An `except Exception: pass` turned any snapshot failure into a Steuerwert of **0.00**, served 200
  with the note still claiming the holdings were valued at that date's closes. `holdings_snapshot_total`
  is now **`None`** on failure — a missing wealth-tax base, not a zero one — alongside
  `holdings_snapshot_error`, and the note says so.

`warnings[]` on the report is the surface for both. It rides on a **successful** response, so it is
structurally invisible unless rendered: `TaxTab` shows it as a banner and `to_csv()` writes a WARNINGS
block. Tests: `tests/test_tax_service.py`, plus the shape assertions in `tests/test_api_smoke.py`.

Frontend: `TaxTab.tsx`. It's a filing aid, not tax advice.

---
