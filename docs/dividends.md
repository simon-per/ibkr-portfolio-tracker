# Dividends — history, forecast, forward yield, growth

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Dividends — history and forecast

`GET /api/dividends/breakdown?year=&forecast=` → `DividendService.get_dividend_breakdown()`, rendered by
`DividendsTab.tsx`: net dividends by month **stacked by symbol**, a year filter (All time + the years
with data), a Forecast toggle, and a per-stock table (payouts, net, projected, trailing-12M yield).
Unlike `/summary` it **never enqueues a sync**, so it cannot reach Yahoo (rule 1) — everything comes
from `dividend_payments`, `taxlots`, `market_prices` and `exchange_rates`.

**The boundary itself leaked one dividend per security until 2026-08-05, and the reason is the
splice's own premise.** The rule keeps estimates strictly *before* the first IBKR payment — but the
two sources file the **same** payment under different dates, yfinance under its ex-date and IBKR under
its pay-date. So the first IBKR payment's own estimate sits before the boundary and is kept, beside
the IBKR row it duplicates. Measured on production (boundary 2026-02-18, ASML held on two exchanges):
`02-09` and `02-10` estimates surviving next to two `02-18` IBKR rows — four rows for two dividends,
**13.7% of the year's dividend income**, on every reader that splices at once (breakdown, summary
card, XIRR inflows, DA-1 income, ledger).

`_splice_by_era` now also matches estimate to IBKR row **per security, nearest-first, one-to-one, and
bounded by `EX_TO_PAY_MAX_LAG_DAYS`** (30 — Mastercard's 29-day lag is the widest real one here).
Never by amount: one side is gross and the other net, so equal amounts are exactly what cannot be
relied on. One-to-one is what makes the window safe for a monthly payer, whose cycle is shorter than
the window — each IBKR payment consumes at most one estimate, so earlier months survive.
**The width errs deliberately toward keeping.** 45 was tried and matched a genuine dividend 45 days
out; too wide deletes real income from a filing aid (understating taxable income), too narrow leaves
one dividend double-counted (overstating it, visibly, already badged `mixed`). For a filing aid the
understatement is the worse failure.

**The two sources are era-spliced, never mixed or dropped.** `_splice_by_era()` keeps
`yfinance_estimate` rows strictly *before* the first IBKR payment date and IBKR rows from there on.
`get_dividend_summary()` used to call `has_ibkr_dividends()` **unwindowed** and then filter to
`source='ibkr'`, so the moment July 2026's real rows landed, every pre-IBKR month vanished from the
card — the repository's own docstring warns against exactly that. The boundary is reported as
`ibkr_from`.

**`get_dividend_summary()` returns NET, and its `source` is three-way.** Both were wrong on the
Performance tab's *Dividend Income* card until 2026-07-30: the service annotates its own return
`# NET per month`, but the card said "Gross dividend income by month" and footnoted "Estimated gross
dividends via Yahoo Finance — withholding taxes … not reflected" over IBKR actuals net of real tax, so
it silently disagreed with the Dividends tab and anyone reconciling DA-1 read net income as pre-tax.
`total_gross_eur` / `total_withholding_eur` / `source` / `ibkr_from` were already on the wire and simply
undeclared in `DividendSummaryResponse`. `source` was also binary — `'ibkr'` the moment any IBKR row
existed, while the splice still carries the estimated months ahead of the boundary — and is now the
same `ibkr` | `mixed` | `yfinance_estimate` flag the tax report uses (`_summary_source()`), which the
footnote reads off. `/api/dividends/summary` now carries a `response_model`, but **completing the
model had to come first, and that order is the whole point**: a `response_model` is a *filter*, so
attaching one to a model still declaring five of the ten keys would have deleted the provenance
fields from the wire and blanked the footnote — shipping the exact bug as a hardening change. For
that reason `tests/test_dividend_summary_contract.py` compares the service's key set against the
model's in both directions rather than spot-checking names: an undeclared key is dropped silently,
and a declared-but-unsupplied one silently takes its default (0.00 withholding, estimate
provenance). Add a key to the service and you must add it to the model.

**Only dividends that could have been earned are ingested.** `sync_dividend_data()` skips ex-dates
before the security's earliest lot `open_date` (reported as `pre_ownership_skipped`); a security with no
lots yet keeps its whole history, since there is no cutoff to infer. Before that, yfinance's full
history meant **1355 of 1446 rows** on this account were zero rows reaching back to 1985 — the reason
the card once reported 439 months, and the reason relaxing one read-side filter broke
`/api/dividends/breakdown` outright. Both readers still filter via `DividendService._is_income()`
(gross **or** net positive) and `_net_eur()` falls back to gross for rows predating the
withholding-fields migration, which carry a NULL net — **use those two helpers rather than touching
the columns directly.** Existing junk is removable with `app/cli/prune_empty_dividends.py --dry-run`.
Run on prod 2026-07-29 (`manual_dividend_prune`): 1350 zero rows removed, real payments remain.

**"Carries no income" is not sufficient grounds to delete a row, and treating it as such was a bug
(fixed 2026-07-31).** The forecast infers cadence from the **raw** history — see *Size from
`amount_per_share`, not from income received* below — so a pre-ownership yfinance row is
simultaneously income-free and load-bearing: it is exactly what lets a recently-bought payer project
at all. The old predicate deleted precisely those, silently reverting the "20 payers project" fix
toward the old 15, and the CLI's own docstring claimed it "deletes only rows the readers already
ignore" — true when written, false once the forecast became a reader of them. Prune is now
additionally bounded by **the ingest window it should always have mirrored**: a row goes only when it
is older than `PRE_OWNERSHIP_HISTORY_YEARS` before the security's first lot, i.e. exactly what
`sync_dividend_data` would no longer create. A security with **no lots** is left entirely alone,
matching ingest's own refusal to guess a cutoff. Rows awaiting computation
(`shares_held IS NULL`) are still never touched. Tests:
`tests/test_prune_preserves_forecast_basis.py`.

### A wrong mapping poisons dividends too, and only prices were ever purged

`dividend_payments` rows tagged `yfinance_estimate` are keyed to whatever Yahoo ticker resolved when they
were written. Correcting a mapping does not retire them, and until 2026-07-30 nothing could:
`manage_mappings disable --purge-prices` cleared `market_prices` only, `DividendRepository` had no delete
method at all, and `prune_empty_dividends` never deletes a row carrying income — so a poisoned row
with a plausible positive amount was unreachable by every tool.

That is the second half of the SBI failure. `SBI@TSE` is **SERABI GOLD PLC** (CAD, Toronto). The mapping
was corrected to `SBI.TO` and the prices purged and refetched on 2026-07-27; its two dividend rows,
computed 06-24 and 07-25 under the bare-ticker US listing, survived. Because cadence comes from whichever
series carries `amount_per_share` — and skips the IBKR rows when one exists — **those two rows alone
projected five monthly payouts for a company that does not pay monthly, while its one real payment was
discarded.** Realized income stayed correct throughout, since the era splice drops estimates after
`ibkr_from`; the damage was confined to the forecast, which is why it survived a month unnoticed.

Three things close it:

- **`app/cli/purge_dividend_estimates.py`** — deletes a security's estimates, never an `ibkr` row (those
  carry real withholding and no mapping can invalidate them). `--dry-run`, ambiguous symbols refused,
  records `manual_dividend_purge`. Run on prod 2026-07-30: 2 rows, income unchanged to the cent, forecast
  went 5 payouts → 0. **Nothing projected is the correct outcome** until a genuine series exists.
- **`disable --purge-prices` now purges estimates too**, and `set` warns when a ticker change makes
  existing estimates suspect. `list` flags `DIVIDENDS PREDATE MAPPING`.
- **`find_dividends_predating_their_mapping()`** runs after every market-data sync, warning when a held
  security's estimates were computed before its mapping's `updated_at`. That comparison is why
  `ticker_mappings` gained `created_at`/`updated_at` — it had **no timestamps at all**, which is what made
  "did this data come from the current ticker?" unanswerable for months.

The identity is `(security_id, source, ex_date)`, not `(security_id, ex_date)`: the column holds an
ex-date for yfinance rows and the **pay** date for IBKR ones, and one shared slot let them overwrite each
other whenever a payer's lag landed on another record's date — producing a single row with IBKR's gross
and the estimate's per-share. Mastercard's 29-day lag already exceeds a monthly cycle. The downgrade in
`o8d5f2a9b3c4` refuses (before any DDL) when cross-source same-day rows exist, because re-narrowing a key
over data the wider one allowed is lossy.

### The forecast — four rules that were each a bug first

**Size from `amount_per_share`, not from income received.** The payout schedule belongs to the company,
not to how long we have held it. Keying on realized income meant a payer bought weeks ago looked like a
non-payer, and **only 15 of 36 held securities could be forecast** — TSMC, Samsung, SK Hynix, HPE and the
**SOXQ ETF** each had 20–59 per-share records and projected nothing. Now 20 payers project.

**Infer cadence from ONE dated series.** The same dividend is stored twice — yfinance under its ex-date,
IBKR under its pay date, weeks apart — which halves the apparent gap: ASML's quarterly schedule read as
74 days, 5 payouts a year instead of 4. Deduplication cannot fix it, because Mastercard's ex-to-pay lag
of 29 days exceeds a monthly payer's whole cycle. Where yfinance rows exist (`amount_per_share is not
null`, ≥2 of them) they alone define the schedule; IBKR rows still supply the net amounts.

**The cost of that rule, and the guards added 2026-07-30:** the chosen series is trusted absolutely,
*including over the IBKR rows it then discards*. So two bad estimate rows can define a schedule outright
— which is exactly what SBI did (see *A wrong mapping poisons dividends too* below). The rule stays,
because the alternative resurrects the double-count; what changed is that a thin or suspect inference now
declares itself. `forecast_samples` and `forecast_cadence_days` ride on each breakdown row (badged at
n≤2), and `find_dividends_predating_their_mapping()` warns when the rows came from an older ticker.
**Earlier revisions of this file cited "SBI's monthly read as 28 days" as an example here. That was the
poisoned data, not a real schedule — don't reinstate it.**

**Step by the calendar.** Dividends pay on a day of the month, so a fixed day-step drifts — 31 days gives
a monthly payer 11 payouts a year instead of 12, and 91 days walked a quarterly payer from the 15th to
the 14th to the 13th. A gap near a calendar period snaps to it (`CALENDAR_PERIODS`), keeping the
schedule's own day and clamping at month end; anything else keeps day-stepping.

**`days_held_in_ttm` is the union of the lot intervals inside the trailing year**, not
`as_of - min(open_date)`. Those agree only while a holding is unbroken, and diverge in the one case
the flag exists for: sell out entirely, rebuy months later, and the older form still reported a full
year because the *first* purchase was a year ago — so a yield built from two partial stretches of
income was presented unqualified. Intervals are `[open_date, close_date)`, matching the
exclude-on-close convention, and overlapping lots are merged so three lots open across one month
count as one month held rather than three. Pinned from all three sides in
`tests/test_dividend_breakdown.py`: the gap case, the overlap case, and a continuously-held position
that must keep reporting full coverage.

**Judge staleness from *now*, not from the horizon.** The stopped-payer guard compares against `as_of`,
because the distance to a future horizon is a property of the question. Otherwise asking about 2027 made
every payer look stopped and returned an empty year.

`forecast_basis` reports which amount was used: `net` when a dividend has actually been received (net of
withholding), `gross_estimate` when only yfinance's gross per-share exists — the latter runs a little
high and the UI badges it. **"Actually received" means an IBKR row.** A `yfinance_estimate` row with
shares held also carries `gross > 0`, but `compute_dividend_income` writes its net as gross with zero
withholding, so dividing that by the shares gives the gross per-share figure straight back — and until
2026-09-12 `_forecast_inputs` stamped it `net` (SK Hynix read `net` on production with no IBKR payout
on record; most payers took this path, since the IBKR duplicate of a yfinance per-share row is
dropped). The `net_ps` branch now requires `p.source == "ibkr"`; an estimate row keeps counting toward
the cadence with no per-share figure, which the projector already handles. Future years are selectable (`years` offers `as_of.year + 1`) and a future
year is forecast in full rather than from today.

**Accumulating ETFs correctly show nothing** — DBPG, EMIM, IWDA, SXR8, VWCE, XAIX, XNAS (the `1C`/`ACC`
suffixes), alongside genuine non-payers (AMD, Amazon, Arista, NU, Credo, Ondas). Verified rather than
assumed: each has 600+ cached prices, so the Yahoo ticker resolves and the empty dividend series is real.
**Don't "fix" their absence.**

**Forecasts are inferred, because nothing forward-looking is cached** — no announced dividends
anywhere, and the fundamentals/earnings tables carry no dividend fields. `dividend_forecast.py` is a
pure module (no DB, no network, fast unit tests): cadence is the **median gap** between recent
payments, the amount the **median** of recent payments scaled to the current holding — median so one
special dividend doesn't inflate every projection. It refuses rather than guesses: nothing held, fewer
than two payments, a gap outside 20–400 days, or a payer that has already skipped ~2.5 cycles all
project nothing. IBKR rows carry no `amount_per_share` and a `0` `shares_held` sentinel, so the
scaling falls back to shares held at the pay date, then to the unscaled amount.
Tests: `tests/test_dividend_forecast.py`, `tests/test_dividend_breakdown.py`.

**One projection pass, sliced per consumer.** `_forecast_inputs()` assembles the cadence/per-share
inputs once, and `project_dividends()` then runs a single wide horizon (to the end of *next* calendar
year) which each reader filters: the chart, the rolling next-12-months figure, and the per-year
comparison. This is safe only because the projection steps deterministically from the last known
payment, so a wide projection sliced to a window equals projecting that window directly.
**The chart's reach is deliberately narrower than the projection's** — without a selected year it
still stops at 31 December. Coupling the two tripled the all-time chart's forecast total (46 → 162)
by pulling next year's payments into it.

### The forward yield — the portfolio's dividend rate

`forward_yield` on `/api/dividends/breakdown`, rendered as the *Dividend Yield* and *Yield on Cost*
cards on the Performance tab (they replaced *Effective Holdings*, which moved into the *Top 5 Weight*
footnote). Projected next-12-month income over market value, and the same income over cost basis.

**It reuses `growth.next_12m_eur`'s projection rather than a Yahoo dividend field, and that is not a
shortcut.** Nothing stores one: `dividendYield` / `dividendRate` / `payoutRatio` appear nowhere in the
backend, and `fundamental_metrics` has no dividend column. Adding one would need a migration plus a
fundamentals pass to populate — that table is written only on demand, so the cards would read blank
until someone spent ~5 Yahoo requests per security — and it would put a *second* annual-dividend-rate
implementation beside the forecast, which is this file's opening warning. `yfinance`'s
`dividendYield` has also silently changed scale (fraction vs percent) between releases.

**Weighting is arithmetic, not code.** A market-value-weighted average of per-security yields *is*
Σ(income)/Σ(value) — `Σ (Vᵢ/V × Dᵢ/Vᵢ) == Σ Dᵢ / V`, every non-payer entering at zero. So the service
divides two figures it already holds and nothing is weighted by hand. `forward_yield_pct` on each
breakdown row is the audit of the headline; the *Fwd yield* column on the Dividends tab shows it.

Four rules, each of which would be a wrong number the other way:

- **An unpriced holding is excluded from both sides.** `portfolio_service` values a position with no
  cached price at 0.00, so leaving it in adds its projected income to the numerator and nothing to the
  denominator — reading the yield **high**. The SBI shape, and the same refusal `rebalance.ts` makes.
  Its cost basis *is* known and is dropped anyway, because the gap between the two cards is only
  readable as appreciation while both denominators cover the same securities. Counted as
  `unpriced_holdings` and named on the card.
- **A zero numerator yields nothing, not 0.00%.** The whole object is `None`. Three states produce a
  zero — no projection was run, nothing held has a schedule, or the one security that does is unpriced
  and was excluded — and a 0.00% reports all three as *this portfolio pays no dividends*. The last is
  the dangerous one, and it is what a green suite served first: the smoke fixture's only forecaster is
  its unpriced TSMC row.
- **`paying_holdings: 0` is the same lie in miniature**, which is why absence is carried by the object
  and not by its fields. A count of 0 of 0 holdings on an account with 36 is worse than no answer.
- **It is a sibling of `growth`, not a member.** `growth` is defined as derived from the unwindowed
  payment *history*; a figure that moves with a market price is neither growth nor history, and
  `DividendKpiCards` — which answers *is this growing* — must not be handed a valuation ratio. Both are
  year-invariant, and `test_api_smoke.py` pins both from either end.

**Yield on cost is forward-over-cost, and was trailing-over-cost until 2026-08-05.** That is the same
numerator as the yield beside it, so the gap between the two is the holding's own appreciation and
nothing else. The old definition divided *income already received* by the *current* cost, and those
describe different positions the moment the position size changes inside the window:

- **Adding to a holding** divides a small position's income by the finished position's cost, so the
  figure reads far too low. Live on **nine of fifteen rows** when it was found — MCO showed 0.35%
  against a real forward 0.84%, SPGI 0.53% against 0.93% — and *unbadged*, because the `†`
  partial-coverage marker was only ever on the trailing yield column, never on yield on cost.
- **Selling and rebuying** is the same defect at its most extreme: the income was earned on shares
  bought cheaply and then divided by the cost of the shares that replaced them, so the result is
  neither the old holding's yield nor the new one's. Under the current definition a rebuy at a higher
  price lowers yield on cost to exactly the new cost's rate, which is the honest answer — more capital
  committed for the same income.
- **Trimming** runs the error the other way: the full position's income over the remnant's cost.

It also silently disagreed with the Performance tab's *Yield on Cost* card, which has divided the
forward projection by cost since it shipped — one name, two definitions, on two screens.
Pinned equal on a single-security book by `test_the_row_and_the_card_agree_on_yield_on_cost`.

`basis` is the same three-way flag as elsewhere (`net` | `mixed` | `gross_estimate`) with
`gross_estimate_eur` quantifying it, because a projection sized from yfinance gross per-share deducts
no withholding — and a yield is the figure most likely to be checked against a broker's own, which
quotes gross. The card shows it as *projected, part gross* in the **footnote**: a caveat reachable only
by hovering does not exist on a touch device, which `DividendsTab` already learned once.

Note the deliberate asymmetry on a row: `forward_yield_pct` always covers the next twelve months while
`forecast_net_eur` beside it is bounded by the selected window, so a row can show **no forecast and a
real yield** when its next payment falls past the year being viewed. Don't reconcile them — keying the
yield to the window would make it read 5/12 of the truth when asked in August.
Tests: the forward-yield block in `tests/test_dividend_breakdown.py`.

### Growth — MoM / YoY, and the five ways it lies

`growth` and `upcoming` on `/api/dividends/breakdown`, rendered as the KPI strip, the per-year panel
and the calendar (`DividendKpiCards`, `DividendYearComparison`, `DividendCalendar`, `DeltaChip`).

Computed from the **unwindowed** history, deliberately: with `?year=2026` the response carries no
2025 months, so no client could derive year-over-year at all. It is byte-identical whichever year is
selected, and pinned that way. Keeping it server-side also keeps the era splice and the per-date FX
projection in one place instead of growing a second implementation to drift.

**Rolling 12 months leads; raw MoM cannot.** This account's payers are quarterly, so March pays and
April does not: month-over-month swings ±90% on cadence alone and says nothing about the portfolio.
MoM survives as a labelled figure on the latest realized month and in the chart tooltip.

Each of these was a wrong number before it was a rule:

1. **YTD is compared day-for-day.** Jan 1 → today against Jan 1 → *the same calendar day* last year.
   Measuring a part year against a whole prior year turned +527% into +99%. 29 February has no
   counterpart, so `_same_day_last_year()` falls back to the 28th.
2. **The first year of income is coverage-limited.** It starts whenever the first dividend landed, not
   in January — seven months here — so its successor's percentage overstates growth and carries
   `yoy_vs_partial` (badged `†`). The first year itself gets no percentage.
3. **The TTM comparison straddles the era splice.** The current 12 months are IBKR actuals while the
   prior 12 are yfinance estimates: comparable in size, not in provenance. `ttm_crosses_era` says so
   rather than presenting a change of source as growth.
4. **Forecast is never silently compared against measured.** `next_12m_vs_ttm_pct` and any annual row
   containing projection are marked `est.`.
5. **A zero base yields `null`, never a percentage.** Growth against zero is undefined, not large, and
   a quarterly payer produces zero months constantly. `_pct()` returns None and the UI renders a dash.
   Per-month growth is realized-only for the same reason — a projected month's "change" would be an
   artifact of the forecast's own flat median.

The per-year panel is the one surface that mixes measured and projected into a single bar, so with the
Forecast toggle **off** it rebuilds from realized income alone (`lib/dividendGrowth.ts`), which is the
only client-side growth arithmetic and copies the server's two rules exactly: adjacent years only,
never divide by zero. Tests: `tests/test_dividend_growth.py`, `src/lib/dividendGrowth.test.ts`,
`src/lib/delta.test.ts`.

---
