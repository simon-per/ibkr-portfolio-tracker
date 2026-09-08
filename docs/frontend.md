# Client-side analytics and the mobile layout

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Client-side analytics — risk, targets, currency

Four pure `frontend/src/lib/` modules with no endpoint of their own: they compute from series and
positions the page has already fetched, which is why they add **no request and cannot reach Yahoo**.
`portfolioKpis.ts` feeds the Performance tab's two card rows; `rebalance.ts` and
`currencyExposure.ts` feed two panels on the Allocation tab; `forecast.ts` is the Forecast tab's
projection and the baseline it starts from.

**One rule spans all three, and it was a real bug before it was a rule.** `undefined` data means
*not loaded*; an empty array means *nothing held*. Collapsing them lets a panel build a confident
answer out of an outage — the rebalance panel did exactly that, reporting *0 positions outside the
band* above rows reading *Not currently held*, i.e. that nothing needed rebalancing and that held
positions were not held. Unit tests could not see it because the shape needs a saved target;
`e2e/errors.mjs` covers it now. Any new panel here takes `isError` and treats absent data as a
stated failure.

**The two KPI rows this module feeds did not follow it until 2026-08-05**, which is the variant worth
naming: `PerformanceMetricsCards` and `RiskMetricsCards` returned `null` whenever `metrics` was null,
and Dashboard's memos return null when their query fails — so a backend outage did not render an error
state on those rows, it rendered *nothing*, and twelve metrics were simply absent from the page. A row
that disappears is worse than one that fails visibly: a stated failure invites a retry, while a missing
row reads as a feature that was never built. `PortfolioSummaryCards`, in the same directory, had the
correct branch all along. The `null` return survives for the genuine no-data-yet case — an empty
portfolio is not a failure and must not claim to be one — so the two states are now distinct props
rather than one absent value.

**And `e2e/errors.mjs` shows how it hid.** Its count of panels reporting the failure went **8 → 10**
when these were fixed: the two surfaces it existed to cover had never been in its own tally, under an
assertion (`hits >= 4`) loose enough not to notice. A floor that far below the real count cannot fail,
which is the same "passes vacuously" shape as the Sharpe clamp test. It is `>= 10` now.

### Risk metrics (`portfolioKpis.ts`)

Sharpe, Calmar and top-5 weight predate the rest. Volatility, Sortino, beta/correlation, drawdown
detail and Herfindahl effective holdings were added because the tab reported return *per unit of
risk* without ever reporting the risk, and because a top-5 weight cannot tell five equal positions
from one dominant one.

- **Beta is measured only over days when NEITHER series saw a flow.** The benchmark is a flow-matched
  hypothetical carrying the portfolio's own cost-basis line but no `external_flow_eur`, so netting a
  flow out of it means inferring one from the cost delta — the same asymmetry that fabricates a loss
  on every sale date in `externalFlow`'s fallback. That would bias beta on exactly the days the
  portfolio traded. Dropping the pair costs a few days a month and biases nothing. `sampleDays` rides
  along so a thin window declares itself instead of showing a confident slope.
- **Volatility, Sortino *and Sharpe* are `null`, not `0`**, below the minimum sample or with nothing to
  divide by. A `0` meaning "unknown" being read later as a fact is the most repeated bug in this
  codebase.

  **Sharpe was the exception until 2026-08-05**, because it predates the risk row and was missed when
  the other two were written. It returned `0`, and that was reachable in one click: selecting **MTD** in
  the first days of a month leaves 2–3 daily returns, so the card drew a green `0.00` captioned
  *Risk-adjusted return* beside a dashed Volatility and Sortino. Sharpe is the worst possible place for
  this substitution — `0.00` is a *plausible* Sharpe, so unlike a 0% volatility nothing about the number
  invites doubt. Its negligible-volatility branch is `null` now too: return per unit of risk is
  undefined when there is no risk, and a flat series is what a stale feed looks like.

  Its clamp test had also been passing vacuously for the same reason — the old series' σ tripped the
  negligible-volatility early return, so it asserted `|0| <= 10`, true of anything. It now uses a
  high-return/low-σ series that reaches the clamp.

- **`concentrationPct` is `null` too, and it was the worst of the three**, because its zero does not
  merely fail to inform — it *reassures*. The tone ladder calls anything under 50% good news, so an
  unpriced portfolio drew a green `0.0%` asserting the five largest holdings are none of the book.
  `herfindahlConcentration` already returned `null` on that exact condition, and the two now share a
  card (the effective count moved into the Top-5 footnote when *Effective Holdings* was replaced), so
  the green zero appeared with the one qualifier that would have explained it silently dropped out.
  Both absences are the same condition, so the footnote says *No priced positions* rather than leaving
  a bare "Concentration risk" under a dash.

  **The same lens caught `_compute_rsi` on the backend, where the stand-in was a *maximum*, not a
  zero.** `avg_loss == 0` returned `100.0`, conflating "an unbroken advance" with "nothing moved at
  all" — RSI is undefined on a flat series, and 100 is the strongest overbought reading the scale
  has. `_compute_buy_score` then scored it **0 of 10** on technical timing, while its own `rsi is
  None` branch scores an unknown at a neutral **5**: the fabricated value was ten points worse than
  admitting the metric was unmeasurable, and the honest branch already existed. Reachable on a
  halted, delisted or fixed-NAV listing, which the watchlist is far more exposed to than the
  portfolio. Every earlier instance of this lens found a zero, so grepping for a suspicious `0`
  would not have found this one.

  **The lens worth reusing:** when a metric can be unknown, ask what its *stand-in value would claim*.
  A `0` volatility looks broken and gets noticed; a `0` Sharpe and a `0%` concentration both look like
  answers, and the concentration one looks like a *good* answer. Severity tracks plausibility, not
  magnitude.

  **The ladder's top rung is a stand-in that makes a claim in *prose*, and the drawdown pair was
  it** (fixed 2026-08-17). `maxDrawdownPct` returned `0` with no measurable returns and
  `RiskMetricsCards` renders a zero max as *"Never below its opening value"* — green, because the
  current drawdown is zero too. Both are `number | null` now, with `sampleDays` beside them; a
  measured zero keeps saying "never fell", because there the sentence is true. Same sweep:
  **`winRate` had counted an unvaluable holding as a losing one** (valued at 0.00, so its gain is
  `−cost`) and is now `null` with the excluded count named. Note where the zeros in this family come
  from — `0` for unknown, `100` for `_compute_rsi`, and now a *sentence* — so the question is what
  the stand-in asserts, never what value it happens to be.
- **`dailyReturnSeries` exists because `dailyReturns` drops days with nothing to divide by**, so the
  nth return is not the nth calendar point. Indexing the input by return position to name a
  drawdown's peak picks the wrong day.
- **The *current* drawdown leads and the worst one is the footnote.** Showing only the max reads as a
  live warning long after the recovery.
- **`winRate` judges only what could be valued**, and reports how many it left out. It excludes on
  the two-clause predicate `market_price === null || market_value_eur <= 0`, shared in spirit with
  `rebalance.ts` — a missing FX rate leaves the price populated and zeroes the value, so the
  one-clause form reads the holding as priced and gives it a real-looking loss.

### Target allocation and drift (`rebalance.ts`)

Targets live in **localStorage**, following `ForecastTab`'s precedent. Deliberately not a table plus
an endpoint: `/api/` is proxied publicly and every write is auth-gated, so a route that stores
portfolio intent is a larger surface than this earns. The cost — targets do not follow you to another
browser — is stated in the panel. `readTargets()` drops a stored value that is not a usable percent
rather than coercing it, because one NaN propagates into every drift on the page, and survives
corrupt JSON rather than taking the tab down.

Four rules, each a wrong number the other way:

- **A missing target means unmanaged, never 0%.** Reading absence as zero advises liquidating every
  holding whose target has not been set — all of them on first use. Clearing the input therefore
  *removes* the target; `0` means "hold none of this", and those are different instructions.
- **Targets are never renormalised to 100%.** The shortfall is reported instead. Scaling invents a
  target nobody chose, and the invented one moves whenever an *unrelated* target is edited.
- **An unpriced position has no weight rather than a zero weight** — and "unpriced" means **either** of
  the two ways the backend fails to value one. It values a holding with no cached price at 0.00, so naive
  drift advises buying its entire target when the position may be the largest one held: the SBI shape.

  **`market_price === null` alone does not catch it, which was a live gap until 2026-08-05.** When the
  price resolves but its **FX rate** does not, `get_positions_breakdown` sets `market_value_eur = 0.0`
  and leaves `market_price` populated — so the holding read as *priced*, took a 0% weight, and drift
  advised buying the whole target. Same bug, second route. A missing FX rate is not hypothetical:
  Frankfurter cannot serve TWD at all, which is the entire reason `WARM_CURRENCIES` exists.

  Both `rebalance.ts` and `currencyExposure.ts` therefore test `market_price === null ||
  market_value_eur <= 0`. The old narrow predicate was justified in a comment by "a *priced* holding
  genuinely worth zero is a real 0%, e.g. a fully-sold one" — **and that premise was false**:
  `get_positions_breakdown` selects `is_open == True`, so a sold-out security has no open lots and never
  reaches the client at all. Two tests asserted the wrong behaviour on the strength of it. What a zero
  value on a priced position actually means is the FX failure above, or a zero-quantity open lot, which
  is a data anomaly that should equally not drive advice.

  Note this is a *narrower* question than `summary.unpriced_holdings`, which is the backend's own count
  and the one to trust for the headline — the client predicate exists only to decide per-position
  weighting.
- **Targets key on `security_id`, not symbol**, because identity is `isin + exchange` and ASML is two
  securities.

Two further refusals. An empty plan is **not** `balanced` — vacuous truth renders *nothing to do* on
a portfolio nobody has configured. And `judgedCount` exists because counting only rows *outside* the
band cannot tell an all-clear from an empty comparison: zero judged rows must say so, which is also
wrong-with-a-healthy-backend when every target sits on an unpriced holding.

### Currency exposure (`currencyExposure.ts`)

`securities.currency` is the currency a listing **trades** in. For a direct holding that is also the
economic exposure; for a fund it need not be, and here often is not — a EUR-listed S&P 500 tracker is
quoted in EUR and carries USD risk. Folding it into the EUR bucket is confidently backwards on
exactly the positions that prompt the question.

**Nothing is re-attributed, and the ETF look-through table cannot fix it**: `app/etf_mappings.py` maps
*regions*, and regions do not determine currency — "Europe" spans EUR/GBP/CHF/SEK, "Asia Pacific"
spans JPY/AUD/HKD/TWD. Funds are counted where they trade, with their share of the book named on
screen and the reason given, so the rows cannot be mistaken for an FX position. The fund set comes
from the ETF bucket of the allocation response already on the page, since `Position` carries no asset
type; matching is by symbol, so a stock sharing a held fund's ticker would be flagged — accepted,
because the flag is a caveat rather than a figure.

Unpriced positions are excluded and counted, as above, and `foreignQuotedPct` returns `null` rather
than `0` when nothing is priced: no positions is an unknown exposure, not an unhedged-free one.

### The forecast (`forecast.ts`)

The Forecast tab's compound-growth projection, and the baseline it starts from. It was inline in
`ForecastTab.tsx` until 2026-09-08, written out **four times** — the horizon table, the scenario
cards, the sampled chart series and that series' hand-copied final point — and the tab had no tests
at all. Two rules, each a wrong number on screen first:

- **Money In starts at what was paid in, never at what the book is worth.** The chart's grey band
  was seeded with `total_market_value_eur` and then grew by the monthly contribution, so it asserted
  that every gain the portfolio had ever made was contributed and the gap to the green band showed
  only *future* gains. The baseline is `windows['all'].money_in_eur` from
  `/api/portfolio/contributions` — the app's one answer to "how much have I paid in", the figure the
  ContributionsStrip and the value chart's Money In line both publish, already in Dashboard's query
  cache so it costs no request. Never `total_cost_basis_eur`, which is *deployed* and counts a
  rotation twice.
- **`Portfolio Value = Money In + Investment Gains`**, on every row and every point, on the rounded
  figures (`gains` is the rounded remainder — the `fund_residual_eur` rule). So "Investment Gains" is
  every gain including those already made, and the year-0 gap between the bands is today's unrealised
  profit. Before, the *table's* column of the same name was a different quantity from the chart
  band's — `monthlyContribution × months` with no seed at all — and the three columns did not sum.

Three consequences. **The seed is Total Value** (holdings + cash) whenever `cashIsTracked`, because
money in counts a deposit the moment it lands whether or not it is invested yet, so measuring it
against holdings alone understates today's gain by exactly the idle balance — and it makes the
Current button agree with the hero card. **"Start from 0" zeroes both sides**: it is the from-scratch
scenario, and carrying today's money in would report a negative gain of that size. **A baseline that
could not be loaded is `null`, never 0**: the grey band is omitted, Money In and Gains read `—`, and a
notice under the chart says so, while Portfolio Value — which never needed the baseline — is
unaffected. Falling back to 0 or to the start value would redraw the very lie the module removes.
The nominal/12 monthly rate and end-of-period annuity are unchanged and conventional.

Tests: `src/lib/portfolioKpis.test.ts`, `src/lib/rebalance.test.ts`,
`src/lib/currencyExposure.test.ts`, `src/lib/forecast.test.ts`, plus jsdom tests beside each
component and three checks in `e2e/errors.mjs`.

---

## The mobile layout — one description, two renderings

The app is built to work at **390x844**, and the rule that keeps it that way is that a table and its
phone equivalent come from **one** `Column[]`, not two hand-written trees.

`ui/DataTable.tsx` renders a real `<table>` inside `ScrollableTable` at `>=sm` and a card list below
it, from the same descriptors. Fifteen tables times two renderings would be thirty places a
column can be added to one and not the other — the dominant failure mode above, in its worst form:
a diverging calculation eventually produces a number someone notices, whereas a column missing from
the phone produces *nothing at all*, on a device the author is not looking at. So `mobile` defaults
to `'detail'` (a new column reaches the phone unless someone explicitly hides it), `cell` takes the
view it is rendering into rather than being duplicated, and two test files hold the line:
`DataTable.test.tsx` drives both modes from one fixture, and `tableFamily.test.tsx` pins the
conventions across column sets plus a source scan for any raw `<table>` outside `ui/`.

**It never owns sort state.** The three sorting tables have genuinely bespoke comparators — nulls-last
over a string|number union, a rating consensus through a score table, a memoised portfolio total —
and pulling them in would need a `sortValue` per column, a fourth thing to keep in step with `cell`.
Rows arrive sorted. The contract every table already implemented and nothing stated: `onSort(active)`
flips direction, which is what makes the phone's single direction button correct.

Three tables deliberately stay tables, each argued in its own file: `CurrencyExposureCard` (the
comparison *between* rows is the point, and there is no identity column to promote), and the two
12-month x N-year matrices (`MonthlyReturnsHeatmap`, `DividendSummary`). They get a designed
`min-w-*` instead, because `ScrollableTable` applies none — which is why eighteen columns were being
*squeezed to min-content* rather than scrolled, and why its edge fades were describing a problem they
did not cause.

**Four things here were each a bug first:**

- **`justify-center` on a flex overflow container hides its leading items.** A centred row wider than
  its scroller overflows on *both* sides and `scrollLeft` cannot go negative, so the first tab sat
  ~150px off the left edge, permanently untappable. The tab strip is `justify-start` below `sm`.
- **A grid item defaults to `min-width: auto`** and so refuses to shrink below its content's
  min-content width. One wide table made a single-column track 392px inside a 358px page. The track
  yields, not the card: `[&>*]:min-w-0` on the paired grids.
- **A responsive base class loses to nothing at `>=640px`.** `p-4 sm:p-6` on `Card` would put
  `sm:p-6` inside a media query, where it beats a plain call-site `p-0` — and the sixteen KPI cards
  that then passed `text-sm` to `CardTitle` are two since `ui/KpiCard.tsx` was extracted, while
  fourteen sites override card padding. Hence `--card-padding` and
  `--card-title-size` as custom properties, in the bracket form (`p-[var(...)]`) so tailwind-merge
  still classifies them and a call site still wins.
- **`useMediaQuery`'s no-`matchMedia` fallback is desktop, and that is an invariant.** jsdom
  implements neither `matchMedia` nor `ResizeObserver` nor `scrollIntoView`. Falling back to desktop
  is what lets every pre-existing component test keep seeing the `<table>` it was written against,
  needs no vitest setup file, and keeps `e2e/a11y.mjs`'s `aria-sort` count non-zero. Flip it and two
  component tests plus an e2e check fail on day one.

**`MAX_RANGE_DAYS` is the other cross-language constant**, and it drifts with no other symptom:
`dateRanges.ts` clamps the ALL button to `365 * 5` precisely so it never asks
`/api/portfolio/value-over-time` for a span the router's own `max_days = 365 * 5` returns 400 for. The
clamp lands *on* the boundary — the client sends exactly `max_days` and the server compares with `>` —
so an off-by-one on either side breaks ALL for anyone with enough history while both suites stay
green. `tests/test_range_limit_agreement.py` reads both files and pins them **equal**, not merely
compatible: a client that shrank to one year would satisfy "not larger" and silently truncate.

`lib/breakpoints.ts` holds the two boundaries as numbers because a Recharts axis width is a prop and
a card list is a different DOM tree — neither is expressible as a `sm:` utility. Everything that
*can* stay in CSS does. `breakpoints.test.ts` pins those constants against Tailwind's own scale and
fails if `tailwind.config.js` ever gains a `screens` override, since one boundary written down in two
languages is the same failure mode again.

**`e2e/mobile.mjs` is the only thing that can see this class of bug** — horizontal overflow is a
property of the assembled page at a real width, and jsdom loads no CSS. Read its docblock before
"fixing" an overflow: `body { overflow-x: hidden }` clips rather than fixes, kills `position: sticky`,
and makes the check pass vacuously. Two assertions are paired specifically to catch that.

Chart heights are CSS on a wrapper plus `height="100%"`, hoisted to a module constant per file so the
chart and its loading/error/empty states cannot drift. `PerformanceAttribution` is the exception and
keeps a numeric height: its height is *data*-driven, one row per security, so a CSS height would
squash thirty bars into 240px.

---
