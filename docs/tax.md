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

**A withholding IBKR corrects later reaches DA-1** (2026-10-18, issue #60). A refund or
correction posted after its dividend (30% corrected to 15%, a fund's year-end adjustment) is folded
into that dividend's withholding at ingest — the nearest preceding IBKR dividend of the security in
the **same tax year** — so the report reads the corrected withholding with no logic of its own. A
correction with no dividend in reach, a January one for December's tax among them, stays out of
every year and is named in `warnings[]` of the sync run that first records it. The mechanics are in docs/dividends.md
(*A withholding posted after its dividend*).

**An estimated row's withholding is absent, not 0.00** (2026-10-14; the backend half 2026-10-17).
A yfinance estimate carries a guessed gross and no withholding on record — `compute_dividend_income`
stores `0` because yfinance has none — so its `0` means "not known", and on a DA-1 aid a `0.00` reads
as "nothing was withheld", a claim against the reclaim. So the **service** says it, and every reader
inherits it:

- an estimate's `withholding` is `null`; its `net` keeps the gross fallback (`_net_eur`), so real
  income is not dropped. **Deliberately not `IncomeSplit`**, the gross-less-ladder net every other
  reader shows for an estimate since 2026-10-18 (docs/dividends.md, issue #61): that net subtracts
  an *assumed* withholding, and here gross − net would publish the assumption as the very figure
  this report refuses to state. So an estimate year's net on this tab is its gross, and the
  Dividends tab's is lower by the ladder rate — two readings, each `est.`-tagged, reasoned on
  both sides (`tax_service.py`, and the exemption in `tests/test_dividend_estimate_net.py`);
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

**DA-1 credits only the treaty rate, so each country row splits its withholding** (2026-10-18,
GitHub issue #62). The table summed the raw withholding per country under a DA-1 heading, but DA-1
credits foreign withholding only up to the rate the Swiss treaty leaves the source country (the
*residual* rate); anything above it is reclaimed from that country's own authority and is not
creditable here. Korea withholds 22% and Taiwan 21% against 15%, so those rows overstated the claim
by the difference. Each `dividend_by_country` row now also carries:

- `rate_pct` — the recorded withholding as a share of the **recorded** payments' gross
  (`percentages.share_pct`; an estimate's gross beside no withholding would dilute it);
- `residual_pct` — the treaty rate from `app/services/swiss_treaty_rates.py`;
- `creditable` = Σ min(withheld, gross × residual) and `excess` = withheld − creditable, **per
  payment** and then summed, since min() does not add across payments (30% and 0% on equal
  grosses is 15 + 0 creditable, not 15% of the sum).

The rules: a **country is in the table only where its rate is known with confidence**, each entry
citing its treaty article — an unlisted country's row has `residual_pct`, `creditable` and
`excess` null rather than a guessed split. A payment that withheld **nothing** needs no rate (0 is
creditable, 0 is excess, whatever the treaty), so an unlisted country still splits exactly when it
withheld nothing. An estimate is left out of the split exactly as it is left out of the
withholding, and a country whose payments are all estimates has every one of the four null.
Switzerland is deliberately absent: Swiss withholding is refunded through the securities statement,
not DA-1. Taiwan (15%, art. 10(2) of the 2007 agreement) was added on review with medium
confidence — confirm against the ESTV's DA-1 country list before a filing relies on it.
The fields are in the CSV (`Rate (%)`, `Treaty rate (%)`, `Creditable`, `Excess`, blank when null)
and the tab shows Rate / Creditable / Excess, the treaty rate as the Rate cell's hover.

**The country is the one that withheld, read off IBKR's withholding line** — `… PER SHARE - US
TAX`. The ISIN prefix is the issuer's domicile, which is wrong for an ADR (a Taiwanese company's
US-listed ADR has a US ISIN and is withheld by Taiwan) and for some funds. `ibkr_service` already
carried the line's description; `sync_dividends_from_cash_transactions` now parses the country
(`parse_ibkr_withholding_country`) into `dividend_payments.withholding_country` (migration
`c9f6e1a3b5d7`), one per row — two different countries on one payment's lines store NULL rather
than a pick — and the report keys on it, else on the ISIN prefix
(`swiss_treaty_rates.withholding_country`). Rows ingested before the column fill in when a
statement covering them is ingested again: the upsert rewrites every field, so a Client Portal
download through `app/cli/ingest_flex_xml.py` backfills with no token spend. Until then they fall
back to the prefix. Tests: `tests/test_tax_da1_split.py`,
`test_a_reingested_statement_backfills_the_withholding_country`.

**The pillar 3a figure is guarded like the Steuerwert** (GitHub issue #63). Its
`holdings_snapshot_as_of(only_accounts=…)` call sat outside the `try` the IBKR snapshot has, so a
failure there answered 500 for the whole report, and its partial case was never read. Now a raise
makes `pillar3a.holdings_value` **null** plus a warning (the tab dashes it, the CSV says
`unavailable`), and a non-empty `last_snapshot_skipped` right after that call names the omitted 3a
securities in a warning about the **3a figure** — never in the Steuerwert's wealth-tax warning,
which still must not mention an excluded security.

**Every realized and holdings row carries `isin` and `security_id`**, so the tab's avatars are
logos rather than monograms (`TickerAvatar` prefers the id). `holdings_snapshot_as_of` and
`realized_rows_from_closed_lots` put both on their rows; a SELL trade linked to no security has
both null. The CSV gains an ISIN column on both tables.

**The IBKR cash balance at the same date stands beside the holdings total** as `cash_balance`
(signed: a margin debit is debt and reduces declared wealth), from
`CashService.uninvested_cash(base_fx, holdings_as_of, account=IBKR)` — the one cash read, scoped by
account so pillar 3a money, outside the Steuerwert like 3a holdings, cannot leak in. It is not
added into `holdings_snapshot_total`; it is a separate line on the return. **Absent when unknown**:
no ledger at all (silently — nothing is wrong), an unconvertible IBKR trade (with a warning naming
the count; a 3a trade does not count against it), or the read raising (a warning, never a 500).
Tests: `tests/test_tax_year_end.py`, `test_one_accounts_cash_carries_no_other_accounts_money`.

**The current year carries its dividends through 31 December** as `projected` (GitHub issue
#77): `expected_gross` and `expected_withholding` still to come, and `gross` and `withholding`
= received + expected. **Current year only** (the year of `utc_today()`); a past year's figures
are all in, so it is `null` there and the forecast is not even asked. The expected part is the
dividend forecast's own year row, `get_dividend_breakdown(...)["growth"]["annual"]`'s
`forecast_net_eur` and `forecast_withholding_eur` for the year, asked with this report's
`base_fx` and `as_of=today` — **never a second projection**, which would copy the cadence, the
accruals, the overdue fold and the withholding ladder and wait to disagree with the Dividends
tab. The forecast is net of each security's ladder rate and its withholding is net × r / (1 − r)
(docs/dividends.md), so expected gross = forecast net + forecast withholding. Received is this
report's own `dividend_totals`: `gross` includes estimates as that total does, and
`withholding` is the recorded withholding plus the expected one — **null when no withholding is
recorded** (an estimate-only year), like the total it extends. No forecast row for the year is
a real zero expected. A forecast that raises makes `projected` null plus a `warnings[]` line,
never a 500. The CSV adds a `THROUGH YYYY-12-31` row under the dividend TOTAL, in its Gross and
Withholding columns, marked `est.`; the tab shows two `est.` tiles. Pure DB, like the breakdown.
Tests: `tests/test_tax_projected.py`.

## Swiss residency (the report counts Swiss days only)

The report is the **Swiss** one, but the owner became a Swiss taxpayer part-way through a year.
Income and sales before the move belong to the previous country's return, so the
first day of Swiss residence is a setting, `app_settings.swiss_tax_residency_from` (ISO date;
`PUT /api/settings/swiss-residency`, a popover on the Tax tab; **unset means resident throughout**,
which is how the report behaved before it existed). Never a constant in code or a date in a
document: read `/api/settings`.

The report's window is `[max(1 Jan, swiss_from), 31 Dec]` (`start` in `get_tax_report`), and it is
the **one** `start` every windowed section already used, so nothing has a second copy of the cut:

- **Dividends** by pay date (ex-date for an estimate), so a payment on the move day itself counts and
  the day before does not. DA-1's per-country table is built from the same rows.
- **Realized gains** by trade date, and the closed-lot fallback with the same window.
- **Pillar 3a contributions** by flow date.
- **KS 36 indicators** are measured from the move: purchases, sales and the lowest cash balance are
  the window's, and the opening base is the day **before** it (`opening_as_of`), not the prior 31
  December — the circular's turnover is against the holdings at the start of the tax period.
- **The Steuerwert is not cut.** Wealth is valued at 31 December (or today) whenever the owner
  arrived; `holdings_as_of`, the holdings and `cash_balance` are what they would be without the
  setting (`test_the_year_end_base_is_not_cut_by_the_move`).
- `projected` (the year's remaining dividends) is the forecast's and starts from today, which is
  never before the move: unaffected.

**A year that ended before the move is not a Swiss report at all** (`_before_residency`): every
figure absent or empty, `dividend_source`, `realized_source`, the Steuerwert and the cash balance
`null` (a wealth-tax base for a year with no Swiss wealth tax is no figure), one `warnings[]` line
saying why, and the tab shows that line alone. That response is built apart from the full one, so
`test_a_year_before_the_move_answers_the_whole_report_shape` pins its key set against the full
report's in both directions. `residency` rides on every report: `swiss_from` (the setting) and
`covered_from` (the first day counted, `null` for that early year). The CSV names the window under
the warnings.

The setting is refused outside 2000-01-01 .. today: a move that has not happened is not a
residency, and the report's year is capped at the current one. Not modelled, deliberately: a
departure date (the owner has not left), the previous country's tax of any kind, and a mid-year split of the
Steuerwert.

## Securities-dealer indicators (ESTV Kreisschreiben 36)

A private investor's capital gains are tax-free, but the ESTV can reclassify a frequent trader
as a professional securities dealer, whose gains are income. KS 36 lists safe-harbour criteria;
`trader_criteria` (GitHub issue #78) puts three of them as **figures** per year — never a
verdict ("safe", "dealer"), and no threshold printed beside them. The arithmetic is
`app/services/trader_criteria.py` (pure) and `TaxService._trader_criteria`. Every figure is
**IBKR-side only** (the report's `exempt_accounts` filter on trades, lots and the opening
snapshot; `account=IBKR` on cash), so pillar 3a moves none of them
(`test_pillar3a_moves_none_of_them`).

- **Coverage first.** If no IBKR cash event (`CashService.balance_events(account=IBKR)`: trades,
  flows, IBKR dividends, measured levels) is dated on or before the year's end — the holdings
  were at another broker — every indicator is **null**, not "no": nothing about that year's
  sales or margin is on record. A year the record enters part-way is covered from its first
  event; the months before it are not seen.
- **`turnover_multiple`** is the circular's own test: *"Das Transaktionsvolumen beträgt pro
  Kalenderjahr insgesamt nicht mehr als das Fünffache des Wertschriften- und
  Guthabenbestandes zu Beginn der Steuerperiode"* — the year's transaction volume against the
  securities and cash balances at the start of the tax period, safe up to five times. So
  `turnover_multiple` = (`purchases` + `sales_proceeds`) ÷ (`opening_securities` +
  `opening_cash`), two decimals (`trader_criteria.turnover_multiple`); the tab prints it to one
  (`1.8×`) and states no threshold.
  - `purchases`: the year's buy-side rows (`buy_sign`, the mirror of `sale_sign`) summed as
    stored and negated, through the report's own `NativeToBase` — a BUY's proceeds are
    negative and its CANCELBUY's the same amount positive, so a busted purchase nets out. Null,
    with a warning, when any purchase has no rate.
  - `sales_proceeds`: the realized section's proceeds total (`realized_totals`: every converted
    sale-side row, a cancellation netting its sale). Null when that section is `trades` and
    omitted an unconvertible sale (its warning already says the totals understate);
    `turnover_estimated` when it is the closed-lot estimate. Purchases are always trades.
  - `opening_securities`: `holdings_snapshot_as_of(prior 31 Dec, exclude_accounts=…)`, the
    previous year's Steuerwert base; an empty snapshot is 0. Null, with a warning, when it
    raises; a partial one serves the figure and a warning that the turnover **overstates**.
  - `opening_cash`: the IBKR balance at the prior 31 December, **signed**, through
    `CashService.uninvested_cash(…, account=IBKR)` — a margin debit shrinks the base. Null when
    the balance is unknown (an unconvertible IBKR trade).
  - The multiple is null when any part is null or the base is ≤ 0: a multiple of nothing, or of
    a debt, is no figure.
- **`sales`, `short_held_sales`, `sales_holding_unknown`.** A sale is the standing SELL rows of
  one contract on one trade date (`uncancelled_sales`, then `sales_by_day`): IBKR sends each
  fill of an order as its own row, and a busted sale is no sale. Its lots are the closed lots
  of its security with `close_source='trade'` and `close_date` **equal to the sale date** —
  reconcile stamps a SELL-explained lot with that SELL's date. It is short-held when any of its
  lots opened less than **six calendar months** before the sale (`_add_months`, clamped: a lot
  opened 31 August reaches six months on the last day of February). A sale with no such lot —
  unlinked to a security, a second sale of the security in one reconcile window (its lots carry
  the later date), a closure lost before issue #54 — is **unknown**, counted in
  `sales_holding_unknown`, never guessed short or long; `short_held_sales` is null when every
  sale is unknown. A year whose only disposals are the closed-lot estimate's has `sales` and
  `short_held_sales` null: the trade ledger does not hold them. No sale at all is `0` and `0`.
- **`margin_used`, `min_cash_balance`.** The lowest **end-of-day** IBKR balance over the year
  (1 January to `holdings_as_of`), the balance carried in included, from the same events the
  year-end cash line reads; `margin_used` is that minimum, rounded to cents, below zero. Null
  while an IBKR trade has no exchange rate (`unconverted_trades`: the balance is short by the
  whole trade). Two limits of the derivation, stated: an intraday dip that closes the day
  positive is not seen, and the derived balance (before measured `EquitySummaryInBase` rows)
  is one base-currency figure, so a negative balance in one currency offset by another is not
  seen either, while a same-currency sell-then-buy weeks apart can read a few units negative
  from the two days' different base rates. `min_cash_balance` is served so the size is visible.
- **Derivatives, the circular's fourth trading criterion, are not reported**: `ibkr_service.
  extract_trades` keeps STK executions only, so the ledger can never hold a derivative and the
  answer would always be a dash; it becomes answerable once that ingest records non-STK
  executions (their `assetCategory`, at least that they happened).

A failure anywhere in the block makes `trader_criteria` null plus a warning, never a 500. The
CSV ends with a *Securities-dealer indicators (ESTV KS 36)* block (blank cells for null, `yes`/
`no`); the tab shows a three-tile row. Tests: `tests/test_tax_trader_criteria.py`.

---
