# The Swiss tax report

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Tax report (Swiss framing)

`GET /api/tax/report?year=YYYY` and `.csv`. `year` defaults to and is capped at the current UTC year,
**resolved per request** (`_resolve_year`): it was a module constant evaluated at import, so on
1 January the new year answered 422 and the default served the prior year until the container
happened to restart — found 2026-09-12, pinned by `tests/test_tax_router_year.py`. Switzerland
doesn't tax private capital gains but does tax
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
  ~8% off on a spot check, hence the badge). A closed lot the estimate cannot price or convert near
  its close date is omitted, and since 2026-10-18 (GitHub issue #19) a `warnings[]` line names the
  count, read from `PortfolioService.last_realized_skipped` right after the call — the realized
  totals are then partial, in either direction.

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

**A SELL trade converts through `native_amounts.NativeToBase`** (2026-10-18, GitHub issue #46).
`trades` store proceeds and realized P&L in the trade's own currency, so they take the
native→EUR→base two-step; the realized loop had its own copy (`to_eur_or_none` twice, then
`base_fx.convert` three times). Now it builds one `NativeToBase` per report and converts proceeds
and gain through it, with `cost = proceeds − gain` in base. What that changed:

- **A sale in a non-base currency: identical figures.** The same rate times the same rate;
  `test_a_foreign_sale_converts_exactly_as_the_old_two_step_did` writes the old formula out.
- **A sale in the base currency itself: now exact, where it was a round trip.** A CHF sale under
  a CHF base went CHF→EUR→CHF on two separately rounded rate series, which are not inverses, so
  every figure moved by their product's distance from 1 — inside the ≤0.3% the owner accepted in
  2026-09 (`docs/troubleshooting.md`), and a near miss on the number a reader checks against the
  broker statement. It is its own amount now (CHF 4,500.00 rather than 4,488.75 in the test's
  rates). An EUR sale under an EUR base was already exact. This is the one change to the report's
  figures, and it moves them toward the statement, not away from it.
- **Rate lookups:** one per `(currency, trade date)` instead of two per sale, and none for a
  base-currency sale.

The skip-and-count rule is unchanged: a sale with no rate is omitted and named in `warnings[]`.
`tests/test_eur_conversion_family.py` fails any module outside `native_amounts` that feeds a
to-EUR result into a second `.convert(...)`.

**The loop is `realized_from_sell_trades`, shared with the summary and the closed positions, and
a cancelled sale nets out** (GitHub issue #58). The report lists every converted sale-side row of
the year, so IBKR's `SELL (Ca.)` reversal is a row of its own with **every figure negated**
(quantity, proceeds, cost, gain) and the year's totals net the busted sale to zero. Until then the
busted sale's gain stayed in the realized totals. A cancellation dated in a later year than its
sale nets in that later year — the report sums rows by trade date and does not pair them. The
fallback to the closed-lot estimate is still "no converted sale-side row in the year"; the
mechanism and the sign convention it assumes are in
[data-model-and-valuation.md](data-model-and-valuation.md) (*A cancelled sale is no sale*).

Frontend: `TaxTab.tsx`. It's a filing aid, not tax advice.

**An estimated row's withholding is absent, not 0.00** (2026-10-14; the backend half 2026-10-17).
A yfinance estimate carries a guessed gross and no withholding on record — `compute_dividend_income`
stores `0` because yfinance has none — so its `0` means "not known", and on a DA-1 aid a `0.00` reads
as "nothing was withheld", a claim against the reclaim. So the **service** says it, and every reader
inherits it:

- an estimate's `withholding` is `null`; its `net` keeps the gross fallback (`_net_eur`), so real
  income is not dropped;
- `dividend_totals.withholding` is the recorded withholding only, `null` when every row is an
  estimate, and `dividend_totals.estimated_rows` counts what it leaves out — and what `gross` and
  `net` include;
- each `dividend_by_country` row carries `payments` and `estimated_payments`, and its withholding is
  `null` when all of that country's payments are estimates. DA-1 is filed per country, so which
  countries are incomplete is said per country, not once for the year (the tab used to tag every
  country in a boundary year, complete or not);
- `to_csv()` leaves an unknown withholding blank, marks each estimate `est.` in its own column, says
  on the TOTAL row how many estimates the totals exclude or include, and gives each country its
  estimated-payment count.

The tab reads those fields instead of recounting rows: `—` for an absent withholding, `excl. N est.`
on the withholding total, `incl. N est.` on gross and net when only some rows are estimates (an
estimate-only year has the card's `est.` badge), and `excl. est.` on just the countries with
estimates. `dividend_country_note` is still served but no longer rendered. Tests:
`tests/test_tax_service.py` (`test_an_estimate_only_year_states_no_withholding_anywhere`, the
boundary-year test) and `TaxTab.test.tsx`.

---
