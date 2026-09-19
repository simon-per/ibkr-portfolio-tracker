# Working state

> Since 2026-09-08 the *Shipped …* write-ups this file cites live in
> [docs/shipped-log.md](docs/shipped-log.md), and the CLAUDE.md sections it cites by name live in
> `docs/<topic>.md` (CLAUDE.md is the index). This file keeps only what is current: what needs a
> human, what is being watched, what is accepted, what is next, and the local-dev traps.

**Last updated: 2026-09-19.** Latest, live on `ec35713` and verified: **a projected dividend
survives its own date.** The pay-date work below rescued dividends yfinance had already *recorded*; this one rescues
the ones it has not. Yahoo writes a dividend into its series the day AFTER the ex-date, so between
the projection being deleted on its own date and that row arriving, the payment is in nothing —
which is what happened to VT, ex 2026-09-18 and invisible on the 19th, its cadence having named the
date correctly a quarter earlier. A projection whose date has passed is now kept, badged `pending`
on the calendar and in no total, and emitted only when nothing else records the dividend: no
estimate row, no accrual, no cash inside the lag window, and shares actually held on the ex-date.
It expires at `PENDING_MAX_AGE_DAYS`, because an inference nothing ever confirms has to stop
claiming. **An accrual now does not** — `_open_accruals` had been ageing those out at the same 90
days against its own docstring, hiding a liability IBKR was still asserting.

Also live on `bb2bb48` and verified: **a forecast sized from
Yahoo's gross per-share now deducts an assumed withholding of 15%**
(`DEFAULT_DIVIDEND_NET_FACTOR = 0.85`).
Such a projection used to be gross served in a field called `net_eur`: labelled honestly, and still
overstated — so the forward yield, the next-12-months figure and every pending calendar entry read
high by whatever tax the payer will actually withhold. `_estimated_net_from_gross` is the one helper,
applied at the two read paths that select the gross fallback (`_forecast_inputs`, and an unpaid Yahoo
estimate emitted straight to the calendar), before currency conversion and rounding. **Stored history,
broker-reported net, accrual `netAmount`, actual cash and every shared income reader are untouched**,
and because each read starts from the stored gross the factor cannot compound — both pinned, the
second by re-reading and comparing. The `net` | `mixed` | `gross_estimate` wire values are unchanged
for compatibility, but `gross_estimate` now means *estimated net derived from gross*, and the
captions say "assumed withholding" rather than "runs a little high".

**The factor is a single global assumption, and this book is not single-jurisdiction.** 15% is the
US and Dutch treaty rate; German, Korean and Taiwanese payers withhold materially more, so those
projections still read high, just less so. A *measured* rate is derivable from data already stored —
`withholding_tax_eur / gross_amount_eur` on IBKR rows, grouped by the ISIN-prefix country the DA-1
report already groups by — and that is the honest version of this. Not built: see *Worth doing next*.

Previously, live on `c791fd9` and verified against the API: **a dividend
is now dated when the cash is expected, and stays visible until it arrives.** Every projected date was an **ex-date** —
the cadence comes from yfinance's ex-date series and nothing shifted it — while `upcoming[].date`
and `next_pay_date` were named as pay dates and the calendar headed *Expected next*. Combined with
`horizon_start = as_of + 1` (a projection is deleted on its own date) and the era splice (which
drops the estimate recording that same payment), a dividend that had gone ex was in **no figure at
all** until IBKR posted the cash. Measured on production before the fix: six held securities at
once, the oldest 22 days in, NVDA with another twelve to go on a 21-day lag.

Three sources now date a projection, each named on the wire as `pay_date_source`: an IBKR
**accrual** (`<OpenDividendAccruals>` — the only record carrying an ex-date and a pay date
together, free on the statement already pulled, one portal tick away — see *Needs a human*), a
**measured lag** per security (`median(pay − ex)` paired out of the raw history by the matcher the
era splice already runs; every IBKR payment on record paired, and the per-security spread is a day
or two), or the **ex-date**, now saying that is what it is rather than implying otherwise. A
payment that has gone ex and not been paid is badged *payment pending* on the calendar and
deliberately in no total — the cash has not arrived, so counting it would credit the account with
money it has not been paid. `months[]`, `ttm_series`, `growth` and the toggle's closed-prefix
guarantee are untouched in shape; the figures move because the dates are now pay dates, which is
the fix. Accruals live in their own table and never reach `dividend_payments`. Rules in
`docs/dividends.md`; what to check live is in *Watch after the next deploy*.

Two things the work turned up. **`match_estimates_to_ibkr` is one function now** — the splice wants
the matched estimates to drop and the lag wants their distances kept, off the identical pairing, and
a second copy is the file's own opening warning. And **IBKR does send `exDate` on ordinary dividend
cash transactions; `ibflex` 0.15 does not model it, so the sanitizer has been dropping it on every
sync** — reading it would make the lag exact, but it needs parsing outside `ibflex` and is
backward-looking, so it closes nothing the accruals do not (recorded in `docs/flex-and-sync.md` so
it is not rediscovered as new). `test_flex_attr_coverage.py` also had a hole the work walked into:
its extractor map was hand-kept, so a new extractor was not merely unchecked but *invisibly*
unchecked — there is a family test for that now, and three extractors that were never covered are.

1,546 backend and 673 frontend tests pass, plus TypeScript, build and lint (the 16 lint errors are
pre-existing and in files this did not touch). **The accrual path has still never seen a real
statement** — it is exercised by fixtures only and the Flex section is off, so every live date
currently comes from a measured lag or the ex-date.

Before that, and live since `c791fd9`: **the Dividends growth figure now
answers the range you selected.** `d4640d4` shipped it yesterday as a deliberately unwindowed
six-month rate anchored at the projection horizon, so it read `Jan 27 – Dec 27` while the reader
was looking at 2026 — the owner's verdict was "not really usable", and he was right: a rate that
ignores the filter above it is not a rate for anything on screen. It is now **CMGR** between the
first and last rolling window actually displayed, plus **CAGR** when that span reaches twelve
months, in one strip with the two endpoint totals beside it and no prose. Computed client-side in
`lib/dividendPace.ts` from `ttmPoints` — that array already *is* the displayed set, range-sliced by
the server and stripped of open windows when Forecast is off, so "which windows are showing" stays
stated once instead of being restated in Python. The server keeps one new field,
`ttm_coverage_start`, which is the only thing a windowed response cannot work out for itself.
`DividendTtmPace` and the two `ttm_pace_*` fields are gone.

On this book every range reaching back to the first covered window starts at CHF 3.77 — the account
mid-funding — and reports +15% to +28% a month. That is arithmetically true and nearly meaningless,
so it carries `†` plus four words of visible text; narrowing the range gives the clean read (2026
+18.3%/mo with the forecast, +14.5% without). Rules in `docs/dividends.md`; what to check live is in
*Watch after the next deploy*.

Live on `d4640d4` (which still carries the unwindowed pace this replaces): **the Dividends TTM
view is a stacked column chart that folds the forecast in.** It answers "which holdings are carrying this"
rather than drawing one line, treats a projected payment as real (the Forecast toggle still governs
it), works on a future year, and drops the months it cannot cover instead of leaving empty space.
The rolling series moved off `months[]` onto its own `ttm_series`, because on All time it runs to
the projection horizon while `months[]` must keep its narrower cap — the one that exists because
coupling them once tripled the all-time forecast total. Both charts now come out of one
`DividendStackChart` and one ranking, so a symbol cannot change colour between views. Rules in
`docs/dividends.md`; what to check live is in *Watch after the next deploy*.

Three things the work turned up that are worth knowing. **Ranking over summed rolling windows would
have ordered symbols by *when* they paid, not how much** — a January payment falls in twelve windows
and a December one in one, a 12× swing that would have quietly reordered the existing monthly chart;
the fix is to score a symbol by its widest single window. **Extending the series on the forecast
*flag* rather than on a projection existing** would have drawn a decline to zero that never happened,
the same shape this service once served as `next_12m_vs_ttm_pct: -100.0`. And **ending the series at
the last projected payment** put All time in October while `year=` for the same year ran to December;
it ends at the horizon now, so every range builds one identical series and only the slice differs.

The stacked TTM was verified on production after deploy (see *Watch after the next deploy*); the
growth figure has not been. Locally: 1,522 backend and 665 frontend tests pass, plus TypeScript,
build and lint. The backend count FELL by eight against `d4640d4` and that is the whole pace
removal — nine tests plus the auto-generated `DividendTtmPace` contract-drift case, against two
added for `ttm_coverage_start`; the collected-id diff was checked rather than the totals.
Browser checks at 1440px and 390px plus dark, replaying the five live `d4640d4` payloads so every
range was verified against real figures — no overflow, no console errors, no network.
Earlier the same day: **Monthly/TTM and the Last 24 months range** shipped as `c0ba465` and is live;
`AGENTS.md` is an exact copy of the root `CLAUDE.md`. Unrelated UI restyle changes already in the
working tree were preserved and remain uncommitted.

Previously shipped: **an Analytics tab — where the return came from.** The
change in Total Value split into money paid in, price, FX, dividends, fees/interest and a named
remainder (waterfall for the range, stacked bars per year, legs summing exactly to end − start);
the same gains folded onto sectors and countries through the look-through; 12-month rolling
Sharpe/volatility/beta and a drawdown ledger; closed positions with IBKR's realized figure and the
quote's move since the sale. Three pure-database routes under `/api/performance`, no new upstream
call. `PortfolioService.attribution_rows` is now the single per-security window loop the
Performance tab's attribution chart and this tab both read. Rules in
`docs/performance-analytics.md`; what to check live is in *Watch after the next deploy*.
Brinson allocation/selection is deliberately not built — it needs per-sector benchmark data the
database does not hold (*Worth doing next*). Morning health check: nothing unpushed, both suites
green, production on the latest commit. Previous latest: **a bug sweep with three parallel read-only hunts
(backend valuation, frontend, sync/ops) found 32 verified defects; 25 shipped, the rest are
recorded below rather than fixed.** Production was healthy throughout and every sum identity
the public API exposes held — the defects were in the paths today's data does not exercise
and in the codebase's own conventions. **The most serious arrived last, as a retraction**:
the sync/ops hunt had listed "no path can issue two SendRequests for one slot" as verified,
then read the installed `ibflex` source and withdrew it — `client.request_statement` re-sends
the same GET up to three times on a 5 s timeout, each a new statement generation, so with the
outer retry budget one scheduled slot could issue twelve. `send_flex_request` now issues one
GET with a (connect, read) timeout pair and a read timeout fails fast like a `1001` (rule 2 in
CLAUDE.md carries it). **Its first live SendRequest is still to come: the 09-12 18:00 slot was `skipped` because an earlier sync that morning had already spent the day's generation, so the 2026-09-13 18:00 Berlin `full_sync` is the first real test.**
Afternoon, a design change at the owner's request: **the 3a Emerging Markets fund prices from
its sibling share class** (`price_source = sibling`: statement NAV × the NT class's daily
moves), so the monthly upload stopped being a pricing deadline — see *Needs a human* and the
activation step under *Watch after the next deploy*. The
ones that were live: the timeline stamped
`cash_source: ibkr` on its tail while the summary said `mixed`, so the chart dropped its caveat
(fixed: the label is the service's verdict); the dividend forecast labelled yfinance gross
estimates `net` (fixed: `net` only from IBKR rows — expect `forward_yield.basis` to move
toward `gross_estimate`); the dividend fetch and the benchmark warm-up recorded a Yahoo 429
as `status: success` with no warning and were never passed to `_collect_warnings`; the
scheduled CINS/SEDOL identity pass ran unbounded against its own docstring; `POST
/api/dividends/sync` had no cooldown; the Alpha Vantage fallback fired on a rate limit; a
non-ASCII `X-API-Key` was a 500; the tax router froze its year ceiling at process start; and
the finpension re-import would have crashed on a Yahoo bar within about two uploads. Frontend:
drawdowns and the Dashboard's period percentages read `0` on a pre-inception range, the chart
tooltip printed `0.00` for a missing benchmark point, the look-through never rendered
`unvaluable_positions`, and eight smaller ones. Details in *Shipped 2026-09-12* in
`docs/shipped-log.md`; what to watch is in *Watch after the next deploy*. **Two owner decisions
from the session are now rules**: FX drifts under 0.3% are accepted, not bugs (closes the old
*Worth doing next* item 0 — see *Known rough edges*); and the auto-deploy rollback's inability
to undo a migration-bearing deploy is deferred to *Needs a human*.

Before that (2026-09-08, night): **the look-through keeps itself current, the
benchmark cache is gone, a deploy no longer takes the site down for the build, and the
backend's January-2024 dependency pins are current.** An audit found seven stale-basket
warnings on every market-data run for two weeks, `starlette 0.35` with 14 advisories behind a
public `/api/`, `deploy.sh` running `down` before `build --no-cache`, and a
`benchmark_timeline_cache` nothing read. All four shipped together; details in *Shipped
2026-09-08 (late)*, and what to watch after the deploy is in *Watch after the next deploy*.
**Verifying them then found a fifth, older problem: every deploy had been discarding the
un-checkpointed tail of the database** — `portfolio.db` is a file bind mount, so SQLite's WAL
lives in the container layer and dies with `docker compose down`. Mitigated the same night
with checkpoints on shutdown, before `down`, and before backups; the durable fix (mount the
directory) is *Worth doing next* item 0 and needs the owner present.

Earlier the same day: **the Forecast tab's Money In starts at what was paid
in, not at what the book is worth.** The grey "Total Contributions" band was seeded with the
holdings' market value — every gain ever made, presented as contributed — while the table column
of the same name carried no seed at all, so one name meant two numbers on one tab and the
columns did not sum. Both are **Money In** now, read from the contributions endpoint's all-time
figure; `Portfolio Value = Money In + Investment Gains` on every row and point; the seed is
Total Value where cash is tracked; and a baseline that fails to load is refused rather than
drawn as zero. The tab had no tests and has 22. Details in *Shipped 2026-09-08*. **Verified live
on `1cc84b5`** at 1280 and 390: the year-0 tooltip reads the money-in and Total Value figures the
API serves, the table rows sum, and the bands run the full horizon once the draw animation settles.

Same evening: **the Look-through tab's warning block is collapsed and last on the page.** Ten
near-identical "basket is N days old" sentences sat in an amber block directly under the KPI
cards and took half the screen above the company table. At the owner's request the itemised
notes now live inside the *Fund coverage* card at the bottom, collapsed by default with a
one-line count in its header; the Coverage KPI keeps the qualifier and names every condition
present rather than only the most severe. The module docstring records why this does not
break the "caveat outside every collapsible" rule.

Before that (2026-09-07, evening): **the benchmark line starts where the portfolio line does,
on every range.** Both were absolute and inception-based, so on 3M the
benchmark's first point carried two years of relative performance while the portfolio's was its
own value that day, and most of the visible gap was history the chart did not draw. The chart
now asks for `anchor=window`: the hypothetical is seeded with the portfolio's Total Value on the
first day of the range and fed only the contributions after it — a seeded walk, not a scale or a
shift, so a deposit inside the window still buys index shares. Verified in a browser against a
snapshot at 1440 and 390: the two lines start at the same pixel on 3M, 1Y and ALL, the API first
points agree to a fraction of a cent, and on ALL the window series ends within 0.0011% of the
inception one. Beta is unchanged on flow-free days by construction; the same change fixed a
pre-existing contamination where deposit-only days counted as benchmark returns (three in the
current 3M window, one a +1.7% jump). Details in *Shipped 2026-09-07*.

Before that, the same day: **the contribution surfaces publish one figure —
money in — and nothing else.** The chart shows money in; it did not until 09-06, because
`monthly[]` carried only gross deployment, which counts a rotation twice by design — so
the Ireland→US ETF switch drew **30,617 CHF "deployed" in August against 7,211 actually
paid in**, and September read **3,639 against zero**. Both were correct and neither
answered the question the card asks. Deployed then shipped as a *second bar*, plus the
strip's `/suffix`, and came out again on 09-07 at the owner's request: they read these two
surfaces for their contribution rate, and the second figure was answering a question they
were not asking. The data agreed — the two series are the **identical number in 20 of 29
months**, and differ trivially in five more. Deployed, released and net are all still in
the tooltip, which matters: money in comes from the deposit ledger and deployed from tax
lots, so deployed is the only independent check on a transfer misbooked as a deposit.
A second bug fell out of the 09-06 work: the series was keyed on months with *tax-lot*
activity, so a month with a deposit and no purchase had **no row at all** — live since the
3a deposits landed a week before their purchases. Details in *Shipped 2026-09-06 (late)*.

Before that, the same day: **the portfolio holds a second account, live on
production.** A Swiss Pillar 3a with finpension is ingested from a transaction CSV and
folded into the same tables — same charts, totals, allocation, look-through, contributions
and returns, no second tab. Deployed 17:00 UTC, imported 17:05, verified end to end.
Money in went **53,829.74 → 55,587.74 CHF** (+1,758.00, exactly the deposits) and the book
**70,853 → 72,613 CHF**.

Three things about it are worth knowing before reading further.

**It was a precondition before it was a feature.** `reconcile_taxlots` deleted *every*
open tax lot not in the statement it was holding, so the first IBKR sync after a 3a lot
existed would have deleted it and booked a fictitious disposal. Two sibling paths had the
same shape. That scoping shipped alone, as a no-op on an IBKR-only database.

**The finpension `Balance` column is an oracle, and it earned its keep twice.** Replaying
our own bookings against it per row makes a silently dropped category structurally
impossible; the derived cash came to **20.023586 CHF**, finpension's own closing balance to
the cent. And the *statement NAVs* are a second oracle: asked to price the EM holding, the
obvious Yahoo candidate is a perfect name match, the wrong share class, and **+49%** — the
SBI failure, caught only because the provider had published what the shares were worth.

**An end-to-end run found a latent bug that was never 3a-specific.** Every contributions
window was clamped to the first *tax lot*, so a deposit before the first purchase read as
0.00 money in. Invisible with one account, because in-kind transferred lots predate every
deposit by years; a retirement account is the opposite and commoner shape.

Before that (2026-08-26): **the app tracks uninvested cash, so a restructuring is no
longer drawn as a collapse.** Selling 25,136 CHF of positions on 08-21 and redeploying 12,682 on
08-24 took holdings 68,342 → 43,631 → 56,161, and the value chart drew a 36% cliff over a period in
which the account lost nothing. The headline card reported **56,708 CHF for an account worth
68,921** — an 18% understatement — because ~12,229 CHF was sitting in cash and nothing recorded it.
The chart now pairs **Total Value** (holdings + cash) with **Money In**, and neither line steps on a
trade. Details in *Shipped 2026-08-26*.

Two things about it worth knowing before reading further. **The risk metrics were never wrong** —
`dailyReturnSeries` nets the flow out, so 08-21 reads +0.76% and the year's flow-adjusted max
drawdown is −11.49% from March. Only the picture and the totals were. And the balance is **derived**
until someone ticks one box in the Flex portal — see *Needs a human*, which is the one open item
from this session.

Before that (2026-08-24, late): **the Positions table published a fabricated −100%
loss, on the one screen you open to find out which holding.** An unvaluable position is valued at
0.00, so its gain is `−cost` — printed in red at `0.00%` weight with no marker, forty pixels below
KPI cards already saying *"N unpriced, not judged"*. Last member of the `unpriced_holdings` family,
and the one it should have started with. Shipped with `formatMarketCap`, which existed
byte-identically in two files. Details in *Shipped 2026-08-24 (late)*.

Before that, the same day: **the Flex Query period is not what this repo said
it was, and a threshold derived from the wrong number cried wolf.** The live query is
`Last 30 Calendar Days`; CLAUDE.md recorded `N=3` and `FLEX_GENERATION_GAP_WARN_DAYS` was the 2 that
implies, so a routine two-day gap produced a banner saying trades were "about to become unreachable"
with ~28 days of slack in hand. The threshold is now measured off the statement IBKR actually
served. Same session: the 08-21 rotation's two replacement funds were **undeclared**, so a
Nasdaq-100 ETF was being drawn as a company. Details in *Shipped 2026-08-24*.

Before that: **the Allocation tab carried both of this codebase's
signature bugs at once** — it answered "is this a fund?" by ticker while the Look-through tab
answered by ISIN, and its three charts silently dropped a holding they could not value while every
slice claimed to be a "% of portfolio". Details in *Shipped 2026-08-17 (night, second pass)*.

Before that, the same evening: **a bug sweep found four more members of the
`unpriced_holdings` family and the look-through's baskets finally have an alarm.** The four are all
the same shape this file keeps rediscovering — a figure computed from an incomplete valuation and
served as a measurement — and the worst of them rendered a *green* reassurance: with a stalled feed
covering the whole selected range, Current Drawdown read `0.00%` captioned "Never below its opening
value". Details in *Shipped 2026-08-17 (night)*. Nothing here needs an operational step; it is code
plus tests.

Before that, the same day: **every held fund decomposes — coverage 98.97%,
`uncovered_fund_eur` 0.00**, and `OPENFIGI_API_KEY` is live on the VPS. Two funds get there by proxy: VWCE via VT and DBPG via VOO, both at the
owner's instruction and both badged. DBPG's 2x leverage is *stated* rather than scaled, since
scaling a bucket would break the partition. Before that, the same day: **VWCE borrows VT's basket** — and the ~8% a broad fund's weights fall short by is explained on
screen as the rounding it is. Measured on a snapshot of production: coverage goes **78.6% → 87.0%**
from the proxy alone, before the four new baskets are even imported, and the partition still closes
to the cent. The proxy is stated everywhere the real thing would be (amber *Via VT* badge, a
`warnings[]` line, the Coverage card staying amber), because "every fund decomposed" over a borrowed
basket would be a reassuring zero. `OPENFIGI_API_KEY` is now set locally and verified against the
live API; **it still has to go on the VPS** — see *Needs a human*.

Before that (2026-08-16, evening): **the look-through treemap clusters by sector, and
four more funds have an automated basket route.** SOXQ, GRID, QTUM and SMH were listed here as
needing a hand download; all four turned out to have keyless, login-free routes. Watch for one thing
on prod: three of the four US issuers publish a **CINS or a SEDOL instead of an ISIN**, which fold
only after `resolve_identities --constituents` runs. Details in *Shipped 2026-08-16 (evening)*.

Before that, the same day: **the Look-through tab got its two charts** — a treemap of company
exposure and a composition bar showing how much of the book the look-through can attribute at all.
Both refuse to renormalise: the treemap carries the truncated tail and the unattributed remainder as
tiles, so a tile's area really is its share of the whole. Details in *Shipped 2026-08-16*.

Before that (2026-08-14): **a Look-through tab — company-level exposure, with ETFs
decomposed into their constituents and one company rendered as one row.** Three of the four largest
true positions were previously invisible *as positions*, because one company occupies several rows:
against a snapshot of production it read ASML, Alphabet (across three listings and five funds),
Amazon, Meta and Nvidia as the top five. Nothing is renormalised, so the coverage gap is stated on
screen rather than hidden. Details in *Shipped 2026-08-14*.

Before that (2026-08-08): **the Flex sync no longer asks IBKR for a statement it has
already made today**, which is what "it always errors out" was. IBKR issues about one generation per
US-Eastern day, so two of three slots plus every manual Sync press failed by construction — and each
failure spent `Code=1025` lockout budget. The guard skips instead, and the **primary IBKR slot moved
to 18:00 Berlin at the owner's request** (see *Shipped 2026-08-08*, including why that is against the
evidence and what to watch). Yahoo repricing hours are untouched.

Before that (2026-08-07): the Monthly Returns table was blank from December 2025 to May 2026 and its
"YTD" covered six weeks, because **MBGL's tax lots predate the spinoff that created it** — one 0.2%
holding made 166 days unvaluable. Also: the two "avg monthly" contributions figures were asked about
and **reconcile exactly** — only their labels were ambiguous (see *Shipped 2026-08-07 (evening)*);
and `full_sync`'s 730-day market-data pass no longer sits behind its IBKR half, which had silently
stopped it running since 08-03. `git log --oneline origin/main..main` is the only trustworthy count
of what is unpushed, and it is the reason that phrase is not a number.

Before that, the /loop audit's batch was pushed and deployed.
Newest first: VT, GRID and QTUM are mapped in the ETF look-through table ahead of the statement that
created them; a backend outage no longer
deletes the twelve
Performance-tab metrics from the page instead of reporting that it failed; the Activity ledger no
longer lists every
dividend twice (it was the one reader missing the era splice, overstating dividend income 72%); yield on
cost is a Positions column; the permanent 27-attribute sync banner is gone; yield on cost no longer
falls when you add to a holding; Beta shows a value for the first time (β 1.03 / r 0.74 vs S&P 500 — it
was refused by an FX artefact, never by a thin window); the Performance tab reports the portfolio's
dividend rate (*Dividend Yield* and *Yield on Cost*, replacing *Effective Holdings*, which moved into
the *Top 5 Weight* footnote); the breakdown endpoint has the contract test its eight nested models never
had; market data reprices seven times a day; the chart's negative axis is clamped; the app's own INFO
logging reaches the container log; the sixteen KPI cards are one component; and the deploy guard covers
every slot.

`CLAUDE.md` is the durable guide — architecture, invariants, and the rules that were each a bug
first. **This file is the perishable half**: where the work actually stands, what is known-broken,
and what is worth doing next. Read both before you start; **leave this one accurate before you stop**
— CLAUDE.md's *Keeping STATUS.md current* says exactly when and what.

Rules for keeping it useful: **delete entries once they stop being true** rather than accumulating
history, and **describe figures rather than publishing them** (the repo is public, the base currency
is user-switchable, and a pasted total goes stale silently — check the API or the DB instead).
*Recent sessions* at the bottom is the single exception to the first rule, and it is capped at five.

---

## Needs a human

- **Tick "Open Dividend Accruals" in the Flex Query, and the dividend calendar stops guessing
  when the cash arrives.** On query `App_OpenLots` (1389408), add the **Open Dividend Accruals**
  section. There are no field checkboxes worth fussing over — the parser reads `conid`, `exDate`,
  `payDate`, `quantity`, `grossAmount`, `tax`, `netAmount` and `currency`, and tolerates any of them
  missing. Everything else is built and deployed: `extract_dividend_accruals` reads it,
  `sync_dividend_accruals` replaces the set wholesale, and the calendar prefers an announced pay
  date over an inferred one the moment rows arrive. No code change, no redeploy.

  **Why it is worth doing.** It is the only record IBKR publishes carrying an ex-date and a pay
  date on one row, so it is the only way to know when a declared dividend will actually be paid.
  Without it the calendar adds a lag measured from history, which works for the securities that
  have already been paid through IBKR and falls back to the ex-date for the rest — about half the
  ex-dated payers on the day this shipped, and disproportionately the ones sitting in the blind
  window, because a position that has never been paid has nothing to measure.

  **Why it is not urgent.** The feature works without it and says which date source it used, per
  payment. An empty table is the supported default.

  Like the Cash Report edit, **the portal edit itself resets the day's Flex generation**, so it
  buys a free extra sync rather than costing one.

- **The auto-deploy rollback cannot undo a deploy that ran a migration — deferred 2026-09-12,
  owner present for the fix.** `ops/auto-deploy.sh`'s failure branch does `git reset --hard
  "$LOCAL"` and re-runs `deploy.sh`; it never restores the snapshot `backup-db.sh` just took
  (its path only goes to the log). A failed deploy that had already run `alembic upgrade head`
  leaves `alembic_version` at a revision the reverted tree no longer has, the container's
  `alembic upgrade head && uvicorn` short-circuits, `restart: unless-stopped` crash-loops, and
  the script logs `CRITICAL: rollback also failed` — on exactly the class of deploy the rollback
  exists for. The fix: `backup-db.sh` prints `$DEST` on stdout, auto-deploy captures it, and the
  rollback branch restores it over `backend/portfolio.db` after `reset --hard` and before
  `deploy.sh`; rehearse in `tests/test_deploy_rollback.py`; then refresh `/root/backup-db.sh`
  and `/root/auto-deploy.sh` on the VPS by **atomic rename** (see the deploy-guard entry under
  *Watching* for why not `install`). Until then, treat any `CRITICAL: rollback also failed` as
  "restore the newest `/root/ibkr-backups/<date>/` snapshot by hand, then redeploy".

- **Upload the finpension export when it has new transactions in it — no pricing deadline
  any more.** Since 2026-09-12 neither 3a fund depends on the upload for its price:
  `CH0117044948` prices from Yahoo directly and `CH1529078078` from its sibling share class
  (`0P0000S0OE.SW`, statement NAV × the sibling's moves — `docs/pillar3a.md`, *Prices*). The
  40-day "upload a newer statement" warning and the day-59 drop-out applied to a `manual`
  fund, and there is none left once the sibling mapping is set on production (see *Watch
  after the next deploy*). What an upload still brings is the *transactions*: each buy is a
  new NAV that re-anchors the derived prices and checks the sibling still tracks (a warning
  names the gap if not). The run is three commands:

  ```bash
  scp transaction_report_YYYYMMDD.csv root@portfolio.srv1211053.hstgr.cloud:/root/p3a.csv
  ssh … 'docker cp /root/p3a.csv backend-portfolio-backend-1:/tmp/p3a.csv'
  ssh … 'docker exec backend-portfolio-backend-1 python -m app.cli.import_finpension_csv /tmp/p3a.csv --dry-run'
  ```

  Then drop `--dry-run`. It replaces the account wholesale and refuses a file shorter than
  what is stored, so a truncated download cannot quietly delete history.

- **Enable the Cash Report section in the Flex Query, and the cash balance stops being ours.**
  On query `App_OpenLots` (1389408), add the **Cash Report** section and tick **Base Currency
  Summary** plus the fields `Currency`, `To Date`, `Ending Cash` and `Level of Detail`. Everything
  else is already built and deployed: `extract_cash_report` reads it, `resolve_cash_balances`
  normalises it, `cash_balances` stores it, and `CashService._apply_measured` prefers it the moment
  rows arrive. Nothing needs a code change or a redeploy — the next successful sync picks it up.

  **Base Currency Summary, not Currency Breakout.** The breakout works too, but it emits one row
  per currency and this account holds five, so the total has to be assembled by converting each at
  the report date — one missing FX rate and the whole date is discarded. The summary row arrives
  already summed. Ticking both is harmless: the summary is preferred rather than added to its own
  breakout.

  There is also an **Equity Summary in Base** section, which is strictly better where it exists —
  one row per *day* rather than one per statement period — and the code prefers it automatically.
  Add it too if the section list offers it; Cash Report is the one that is always there.

  **Why it is worth doing.** The derived balance is built from trades, deposits and dividends, so it
  structurally cannot see broker interest, account fees or the spread on an FX conversion. Measured
  on production: it agreed with an independent derivation to a tenth of a percent, and it also sat
  at about **−250 CHF** for months while the account was otherwise fully deployed. That is small
  (0.36%) and it is one-directional, so it grows. IBKR's own end-of-day figure includes all three.

  **Why it is not urgent.** The feature works without it, and says so — `cash_source` is `derived`
  and the chart, the hero card and the positions table all print the caveat in prose. The number is
  right to well under a percent.

  **DONE and live on production, 2026-08-26.** The section was enabled, a statement
  downloaded and ingested offline, and the row is on the server:
  `2026-08-25 | CHF | 12,501.583565`. IBKR's figure is **289 CHF above** what the ledger
  derived (12,212.62) — the accumulated broker interest, fees and FX spread the
  derivation structurally cannot see, in the direction and roughly the size the design
  predicted. From 08-25 onward `cash_source` reads `ibkr`; before it, `derived`. Nothing
  further is needed unless the query is edited again.

  Two things to expect when it is enabled, neither of which is a fault:
  - **One visible step in the cash line on the first measured day.** That is the accumulated
    interest/fees/spread being corrected, not a jump in the balance.
  - **Measured history starts that day and never reaches back.** The Flex window is bounded, so
    every point before it stays `derived`, and `cash_source` is reported per chart point rather than
    per response for exactly this reason.

  It is also the one thing that resets the day's Flex generation, so the edit itself buys a free
  extra sync — see *Sync schedule* in CLAUDE.md.

- **Decide what the Flex Query period should actually be — the portal and this repo disagree.**
  Measured 2026-08-24: the live query is `Last 30 Calendar Days` (the portal download's header says
  `period="Last30CalendarDays"`, and `data_from`/`data_to` on successful API syncs span 30 days,
  consistently, back through 07-01). CLAUDE.md recorded it as **narrowed to N=3 on 2026-08-06, "a
  deliberate choice by the account owner, reaffirmed after the trade-off was put to them"**. Either
  that portal edit was never applied or it was set back; nothing in `sync_runs` shows a 3-day
  statement at any point.

  **Nothing is broken either way** — the code now measures the window rather than believing a
  constant, so the alarm is correct at whatever N is in force, and 30 days is the *safer* of the two
  (weeks of recovery margin instead of two days). But the file asserted a decision the account was
  not running, so the decision is worth re-making explicitly:

  - **Keep 30.** More margin against failed syncs, and the reason it was narrowed — `Code=1001` at
    the request step — has not recurred in the way it did under Year-to-Date. Nothing to do.
  - **Re-narrow to 3.** Faster statement generation, but the entire margin becomes two consecutive
    failed days, and 08-23/08-24 was exactly such a pair. Requires a portal edit, which also resets
    the day's generation — the one thing observed to do so.

  Whichever is chosen, do not re-introduce a hardcoded threshold: `flex_window_days()` reads it.

- **Add the HSTS header on the host nginx.** Found 2026-09-08: production sends CSP, nosniff,
  X-Frame-Options and Referrer-Policy but no `Strict-Transport-Security`. TLS terminates on the
  VPS's own nginx (`/etc/nginx/sites-*`), which is not in the repo, so this is one `add_header`
  line there — start with a short `max-age` and raise it, since the header is sticky in browsers.

- **The look-through's baskets refresh themselves since 2026-09-08, so this stopped being a
  chore.** The 18:00 `full_sync` fetches any basket older than its issuer's cadence through the
  same fetchers the CLI uses, keeps the previous one on any failure, and runs the CINS/SEDOL
  identity pass after a replacement plus a bounded ISIN pass every evening. What a human still
  does: hand-import a file an issuer only publishes by email (VWCE, if its real basket is ever
  wanted instead of VT's), and read the 18:00 run's `lookthrough_result` when a stale warning
  survives past a day. The by-hand commands are unchanged and still safe at any hour:

  ```bash
  docker exec backend-portfolio-backend-1 python -m app.cli.fetch_etf_baskets --all --out /tmp/baskets
  #   then the import line it prints per fund (--dry-run first). VT's line carries a *.json glob and
  #   `docker exec` runs no shell, so wrap that one in `sh -c` or list the 21 pages.
  docker exec backend-portfolio-backend-1 python -m app.cli.resolve_identities --constituents
  ```

  **Order matters**: the second step resolves the CINS/SEDOL identifiers the first stores, so running
  it first leaves GRID's and QTUM's companies unfolded. A **basket re-import deliberately clears**
  those resolutions (it keeps the raw identifier), so it is the second half of every basket refresh.
  The resolve step **commits once at the end** — do not deploy while it is running, and do not use
  `pgrep` to check on it (not installed in the container; it always reports one match). Read the
  `manual_identity_resolve` row in `sync_runs` instead.

  **VT will keep refusing until Vanguard's cluster serves one snapshot.** Not a fault to force past —
  see *Ran on production 2026-08-17*. Its stored basket is kept and VT publishes month-end.

- **`OPENFIGI_API_KEY` is DONE — set on the VPS and read by the app** (2026-08-17 evening,
  `configured: True, length: 36`). Nothing is outstanding here. Note it was never blocking: the
  identity backfill that took unresolved value from 4,281 to 516 CHF ran **keyless**, and GLEIF's one
  request per ISIN is what costs the ~37 minutes, not OpenFIGI's batch size.

  Two traps it cost, both now in CLAUDE.md. The app reads **`backend/.env`**, never the repo-root
  `.env` — `config.py` resolves `BACKEND_ROOT / ".env"` and compose resolves `env_file` relative to
  itself, which is also `backend/`. And a hand-run `docker compose up -d` must carry
  `GIT_COMMIT=<sha>`, or `/health` reports `commit: "unknown"` on current code and never recovers
  until the next push.

- **VWCE's basket is CLOSED as a task — the VT proxy is the accepted answer.** Do not re-open it,
  do not chase Vanguard for the real file, and do not reinstate the prohibition this entry used to
  carry. The owner instructed the proxy on 2026-08-17 and closed the follow-up the same day, with a
  reason that makes it more than a stopgap: **the Ireland-domiciled sleeve is being rotated into
  US-domiciled ETFs for tax reasons**, so VWCE is on its way to becoming VT. Borrowing VT's basket
  approximates a position that is converging on it.

  It is badged as a proxy on every surface and errs *low* — see CLAUDE.md's *A borrowed basket*. If a
  real file ever does arrive, `import_etf_basket` stores it and the proxy is never consulted again;
  there is nothing to un-declare.

  **The old prohibition, and why it fell.** This entry used to say "do not ship VT's basket as a
  proxy", on the grounds that the overlap had never been quantified. It has been: the sleeve VWCE
  omits is almost entirely the 8,007 of VT's 10,032 rows published at 0.00% weight, and VT spreads
  the shared names over a *wider* index, so the proxy understates and the shortfall lands in the
  residual — the direction chosen everywhere else here.

  **Ruled out on 2026-08-14, each verified — kept only so nobody re-investigates:** the US profile
  API that serves VT rejects the Irish fund id; live EU holdings are GraphQL-only with no key in the
  delivered HTML; Euronext carries no constituent data and the LSE page is an empty SPA shell; there
  is no EU analogue to N-PORT; the product page publishes ten holdings with no ISIN column; and
  Vanguard Funds plc's disclosure policy makes complete holdings request-only by email. The
  semiannual report PDF lists every holding but carries **zero ISINs**, and this pipeline is
  ISIN-keyed.

- **SOXQ, GRID, QTUM and SMH are no longer on this list.** All four have adapters as of
  2026-08-16 — the "single-page app" reading was wrong for each of them in a different way, see
  *Shipped 2026-08-16 (evening)*. **DBPG never was on it** — it is excluded by design, not for want
  of data.

- **Two credentials are knowingly unrotated, both by owner decision, and they are different things.** Neither is a task. What IS open is one re-exposure, noted below.
  - **`API_ADMIN_TOKEN`** was exposed into an agent transcript on 2026-08-07 (a `pgrep -af` printed
    a curl command line carrying the header). The owner was asked and **chose to accept that one** —
    do not re-raise the 08-07 incident.

    **It happened a second time on 2026-08-17** — an agent ran `od -c` over the last 60 bytes of the
    VPS `backend/.env` to check for a trailing newline, and the file's last line is that token, so
    most of its tail is in that transcript. **The owner was asked and accepted this one too**, on the
    reasoning that the exposure is a local `.jsonl` transcript plus the vendor's logs rather than
    anything reachable from the internet — unlike the 07-28 Flex leak, which was genuinely served over
    HTTPS. *Do not re-raise either.* Both credentials and both exposures are now settled; there is no
    open security item.

    **The lesson is the part worth keeping, and it is cheap: never dump bytes from a file that holds
    secrets.** `tail -c 1 | xxd` answers the trailing-newline question without printing a value, and
    appending defensively answers it without asking at all.
  - **The IBKR Flex token** was the other one, and it is now **also accepted rather than rotated** — the owner decided on 2026-08-17. See the entry below for the blast radius that makes that reasonable. Neither credential is an open task; only the 08-17 `API_ADMIN_TOKEN` re-exposure is still unruled.

- **Pushing is NOT blocked for an agent, despite what this file claimed until 2026-08-04.** The
  entry here said it was "refused by the permission classifier". It was tried, and it worked
  (`169b7e5..5093be5`). So do not treat unpushed work as a human step by default — check
  `git log --oneline origin/main..main` and ship it. What *does* warrant asking first: anything
  touching the deploy machinery or the VPS, and landing a push within ~10 minutes of a Berlin slot
  (which `ops/finish-deploy.sh` and, since 2026-08-04, the installed guard both check for you).

  `ops/finish-deploy.ps1` (PowerShell) and `ops/finish-deploy.sh` (Git Bash) are equivalent twins
  that run push / token / guard in the only safe order and skip whatever is already done.
  **Keep the two in step if you change either.** Both take Berlin time from a real timezone
  database rather than the shell, because Git Bash on Windows silently ignores `TZ=` and returns
  UTC — a two-hour error in the direction that permits a collision.

  Note the scripts define `$k`/`$h` shorthand *inside one session*; pasting a later command into a
  fresh shell silently passes empty strings and `scp`/`ssh` print usage. Use literal paths, or
  re-run the script.

- **The IBKR Flex token will NOT be rotated — the owner decided this on 2026-08-17.** Recorded so it
  is not rediscovered and re-raised as new: *do not bring this up again unless the owner does.*

  The history, so the decision stays informed rather than just remembered: the token travelled as a
  `t=` URL parameter into `sync_runs.message` and was served by the public
  `/api/scheduler/history` until the 2026-07-28 scrub. `app/redact.py` now redacts on write *and* on
  read, so it cannot recur — but redaction cannot un-leak what was already reachable.

  What an exposed Flex token permits, since that is what the decision turns on: the Flex **Web
  Service is read-only reporting**. It cannot place trades, move cash or change the account. It can
  pull this account's statements, and it can spend the token's generation budget — which is how
  `Code=1025` lockouts happen, so the practical damage is *our own syncs stopping*, not money moving.
  That bounded blast radius is what makes accepting it defensible.

  **The tell if it ever is being used by someone else**: successful generations appearing in
  `sync_runs` at hours no job runs, or a `1025` lockout with no failed attempts of ours preceding it.
  `find_flex_generation_gap` would surface the second one within 2 ET days.
- **The 2025 tax-year backfill is CLOSED — there is nothing at IBKR to backfill.** The owner
  confirmed on 2026-08-17 that they did not use IBKR before 2026: the holdings arrived by in-kind
  transfer from Trading 212, Scalable Capital and Trade Republic in early 2026. So IBKR holds no 2025
  executions, dividends or cash transactions, and a 2025 Flex statement would generate empty. 2025's
  `dividend_source='yfinance_estimate'` is the only source that exists, and the flag saying so is the
  feature working. **Do not change the Flex Query period chasing this**, and do not re-raise it as an
  open task. Pre-2026 realized gains and Steuerwert have the same ceiling for the same reason; the lot
  cost basis that *does* reach back came across with the transfer, which is why contributions splice at
  `coverage_from` rather than trusting the ledger for those years.

## Watching

- **The IBKR primary slot is 18:00 Berlin (12:00 ET), and nine ET days say it works.** Shipped
  2026-08-08 at the owner's explicit request against the measurements available then. Measured
  2026-08-17: **12:00 ET succeeded seven days straight (08-09 → 08-15)**, failed 08-16 and was
  recovered by 00:00 Berlin the same ET day, and failed again 08-17 with the recovery slot still to
  come. 7 of 9, against 1-of-8 for the retired 20:00 Berlin slot. The old reading blamed
  mid-session; the confound was that those slots also ran *after* the day's generation was spent.
  CLAUDE.md's *The 18:00 Berlin slot* now carries the table. It still captures no extra trades,
  because the Flex window rolls at midnight ET rather than at generation time.

  **The transition is the confusing part, so expect it.** On the deploy day the old 06:00 Berlin
  slot has already spent that ET day's generation, so the first 18:00 run will record
  `skipped` / `already_generated_today`. That is the guard working. The **first real test is the
  following evening**, and until then there can be up to ~36 h between generations — inside the
  3-day window, but check `max(trade_date)` if anything looks stale.

  What to look for in `/api/scheduler/history` (the field is named `type`, not `sync_type`):

  - a daily `full_sync` at 18:00 Berlin with `ibkr_result.status == "success"`
  - `ibkr_retry_1` at 00:00 Berlin recording `skipped` with `reason: "already_generated_today"` —
    **not** `error`. An `error` there means 18:00 failed and the recovery attempt also did.
  - `ibkr_retry_2` gone from the job list: `_prune_unknown_jobs` evicts it from the persistent
    store on first boot. If it is still there, the deploy did not take.

  **If generations start failing, the fix is to move `FULL_SYNC_HOUR` back to 6** — 06:00 Berlin is
  00:00 ET, the instant the window rolls, and it succeeded 8/10 standalone. Recovery in the
  meantime is a browser download through `app/cli/ingest_flex_xml.py`, which is idempotent and
  spends no token budget.

- **Every piece of look-through data is a one-off snapshot, and nothing renews any of it.** This is
  the weakest part of the feature and the thing most likely to mislead later, because the numbers
  stay confidently on screen while the baskets behind them age. Populated on production 2026-08-14:
  6 baskets, 16,138 constituent rows, 505 identities — **and the four adapters added 2026-08-16 have
  not been run against production at all yet** (see *Needs a human*). Decay clocks:

  | data | issuer republishes | badged `†` after | renewed by |
  |---|---|---|---|
  | XNAS, XAIX (DWS) | **daily** *(publishes no as-of; fetch date stands in)* | 7 days | nobody |
  | SXR8, IWDA, EMIM (BlackRock) | **daily** | 7 days | nobody |
  | SOXQ, GRID, QTUM, SMH (the four new) | **daily** | 7 days | nobody |
  | VT (Vanguard US) | month-end, ~6wk lag | 75 days | nobody |
  | VWCE | not published at all | permanently amber | a hand download |
  | DBPG | — | never (excluded by design) | n/a |
  | ISIN identities | LEIs never change | nothing warns | nobody |

  **So nine of the ten baskets go stale within a week of a fetch**, the coverage card reads
  *"N baskets ageing"*, and no job does anything about it. Refresh is `fetch_etf_baskets --all` plus
  the import lines it prints, **then `resolve_identities --constituents`** — a re-import clears the
  CINS/SEDOL resolutions on purpose.

  **Identity drift is the quieter half.** A positive answer is cached forever, which is right — but
  a fund rebalance introduces constituent ISINs nobody has asked about, and **a newly bought
  security's ISIN is never resolved either**, so it will not fold with an existing holding of the
  same company until `resolve_identities --constituents` is re-run. `unresolved_value_eur` (3.0% of
  the book on 08-14) drifts upward silently. Same shape as the documented "run
  `POST /api/allocation/sync` by hand after buying" rough edge.

  **There is an alarm now, as of 2026-08-17 (night).** `find_stale_etf_baskets()` runs after every
  market-data sync and contributes to `warnings[]`, per adapter, for held funds only — so the decay
  above tells you rather than only badging a tab someone has to open. What it deliberately does
  **not** say anything about: a fund excluded by design, and one whose only route is a hand download
  with nothing to borrow. Neither can be cleared by running anything.

  **It has never fired on production**, because it shipped after the 08-17 CLI run left every basket
  fresh. When to expect it, computed from the registry rather than guessed:

  | fund | judged on | expectation | first warns |
  |---|---|---|---|
  | IWDA, SXR8, EMIM, XNAS, XAIX, SMH, SOXQ, GRID, QTUM | own basket, as-of ~08-17 | 7 days | **~08-24** |
  | VOO, and DBPG through it | VOO's basket, as-of 07-31 | 75 days | ~10-14 |
  | VT, and VWCE through it | VT's basket, as-of 06-30 (its refresh refused) | 75 days | **~09-13** |

  So a quiet `warnings[]` before 08-24 is correct, and **VWCE staying quiet while the nine are
  flagged is also correct** — it is judged by Vanguard's cadence because that is whose file it
  reads, not by the 45-day default its own `manual` adapter would have given it. If nothing appears
  after 08-24, check the market-data job's `details`: the detector is separately guarded, so an
  exception in it is a log line rather than a failed sync. The *second* half of item 3 — a
  staleness-guarded automatic refresh — is still open.

- **The TSMC ADR override is about to fire for the first time.** `ISSUER_OVERRIDES` folds
  `US8740391003` (the TSM ADR) into the Taiwanese ordinary, pinned from both sides in
  `test_company_identity.py`, but **zero stored baskets contained the ADR** (checked 08-14: EMIM and
  VT both hold the *ordinary*). SOXQ and SMH now have adapters, and both hold the ADR — SOXQ under
  CUSIP `874039100` → `US8740391003`, SMH under a published ISIN. **Check TSMC appears once, not
  twice, on the first look-through read after those two are imported to production.** It has never
  been exercised outside a test.

- **`find_flex_generation_gap` has never fired.** New on 2026-08-08: it warns after 2 ET days with
  no successful IBKR sync, which is the actual margin under a 3-day Flex window — `find_stale_ibkr_sync`
  at 7 days fires four days after the trades are gone. It runs from the market-data job, so it
  surfaces while Flex is refusing. With only two attempts per ET day, this is the alarm that matters;
  if it appears in `warnings[]`, act rather than waiting.

- **`full_sync`'s market-data half is decoupled from its IBKR half and has not yet been observed
  working.** Deployed 2026-08-07 06:32 Berlin. Note the guard makes a *failed* IBKR half rarer,
  so this may now be observed less often rather than more — a `skipped` half is not the case it
  was written for. It is invisible on a morning IBKR *succeeds*, because
  the old gate would have let it through anyway — so the first real evidence is **the next 08:00
  Berlin run whose `ibkr_result` is an error**, which on current form is most of them.

  What to look for in `/api/scheduler/history` (note the field is named `type`, not `sync_type`):

  - `details.market_result` is a real object, **not `null`** — that was the whole bug
  - top-level `status` is still `error`, so the Yahoo half succeeding does not mask a refused
    statement and `find_stale_ibkr_sync` keeps counting correctly
  - any `warnings[]` the market pass raises now actually appear; a *skipped* step emitted none, which
    is why `find_stale_priced_securities` was silent on exactly the mornings IBKR refused

  If `market_result` is `null` beside an error status, the deploy did not take — check `/health`'s
  commit against `origin/main`. Background under *Shipped 2026-08-07* in `docs/shipped-log.md`.

- **The three new ETFs landed on 2026-08-07 at the 06:00 Berlin slot, exactly as predicted.** VT 10 @
  160.50, GRID 10 @ 190, QTUM 3 @ 149.85 and 1 more META @ 589.12, all dated 2026-08-06. Production
  went 36 → **39 positions**, 979 → **983 open lots**, 40 → **43 securities**, `max(trade_date)`
  2026-07-29 → **2026-08-06**. `unpriced_holdings` read **3** in the gap before pricing, with the
  yellow notice above the KPI cards — the completeness signal from the 08-06 batch working on its
  first real occasion. The look-through mappings and the asset-type fix shipped ahead of the
  statement all held; nothing needed doing when it arrived.

  **The asset-type fix is confirmed on live data, which is worth more than the mutation test.** All
  three carry `asset_type='Stock'` (the column default) with `sector` and `country` NULL, exactly the
  state that used to draw a mapped fund as a Stock in one chart and an ETF in the other two — and all
  three are now distributed as `is_etf_contribution` across sector *and* geography, with the
  asset-type chart agreeing. All three breakdowns sum to 100% (two read 100.01, which is 2dp
  rounding). **So no `POST /api/allocation/sync` is needed for them**; the columns stay NULL and that
  is fine for a mapped fund.

  Two things worth keeping from how it got there, both now in CLAUDE.md:

  - **`whenGenerated` is US Eastern, and the window rolls at midnight ET.** A statement downloaded
    at **05:40 Berlin on 08-07** still read `to=20260805`, because that is 23:40 the previous day in
    New York — it looked like today's file and was yesterday's. The 06:00 Berlin job twenty minutes
    later (00:00 ET) got the missing day. **Check `toDate` in the header before ingesting a manual
    download**; the generation time tells you nothing.
  - That file was ingested anyway, at the owner's request, and was a clean no-op: 979 lots synced,
    **0 closed, 0 skipped**. Worth knowing it was only safe *because* production still lacked the
    three — an 08-05 snapshot ingested **after** they exist would drive them through
    `reconcile_taxlots`' heuristic branch and close them dated 2026-08-05.

- **The Flex Query period is `Last N Calendar Days` with N=3, down from 30 since 2026-08-06.** The
  owner's call, reaffirmed after the trade-off was put to them, so it is settled rather than open —
  but it changes the failure math and the write-up in CLAUDE.md's *The Flex Query* is the one to
  read. Short version: a statement generated on *D* covers *D−3 … D−1*, the account gets about one
  successful IBKR sync a day, so the margin is **two consecutive failed days** rather than ~90. Only
  the `<Trades>` rows are at risk — OpenPositions is period-independent, so holdings still arrive.
  **`find_stale_ibkr_sync` (7 days) is too slow to be the alarm for this**; watch `max(trade_date)`
  against the calendar instead.

- **The deploy guard covers every slot, installed 2026-08-04** (nine slots then, eight since the
  2026-08-08 IBKR move — `tests/test_deploy_guard_hours.py` keeps the three copies in step). `/root/auto-deploy.sh` is
  byte-identical to `ops/auto-deploy.sh` (verified by sha256), so the copy `test_deploy_guard_hours.py`
  checks is the copy cron executes. Installed by **atomic rename** rather than `install -m 755`,
  which matters: `install` truncates the destination in place, and replacing a *running* bash script
  makes the live shell read garbage from its current byte offset. Rename leaves an in-flight run on
  the old inode. Previous copy kept as `/root/auto-deploy.sh.bak-<date>`.

  What to expect the first time it fires: a `SKIP: within 10min of the HH:00 Europe/Berlin sync slot`
  line in `/root/auto-deploy.log`, and a push that lands ~10 minutes later than usual. That is the
  guard working, not a stuck deploy.

- **The `1001` problem is fixed — the Flex Query period is now `Last 30 Calendar Days`.** Confirmed
  by as clean an A/B as production allows: **20:00 error, 21:08 success, same token, same hour band,
  68 minutes apart, only the period changed.** 15:08 New York is mid-session, the window that had
  gone 0-for-8 that day. Statement shape went ~290 trade rows → 103 and ~107 cash transactions → 17.

  The cause was **the query growing from one section to six between 07-24 and 07-28** (Trades,
  CorporateActions, CashTransactions, then Deposits & Withdrawals and Transfers). Five of those scan
  the whole period; Open Positions, the only original section, does not — which is why years of
  YTD queries never provoked it. A *separate* change made it look worse than it was: the 13:00/20:00
  retry slots were added in `67e6a59` on 07-25, the same day `sync_runs` persistence landed, so new
  failing slots and first-ever visibility arrived together.

  Verified after the switch: all 71 YTD trades still on record month-by-month, `coverage_from` still
  2026-01-09, `taxlots_skipped: 0`, 979 lots. Nothing was lost.

  **Still worth confirming**: that 00:00 and 06:00 succeed on a normal night. And don't reason about
  statement cost from row counts — Open Positions is ~70% of the rows and ~0% of the scan work.
- **`find_stale_ibkr_sync` is now the thing that tells you a bounded window is drifting.** It warns
  after 7 days with no successful IBKR sync. Treat it as a prompt to download the statement from
  Client Portal and run `app/cli/ingest_flex_xml.py` — that path is idempotent, so re-ingesting a
  YTD export simply fills whatever the 30-day window missed. Real recovery, not a theoretical one,
  which is why a 30-day window is a comfortable choice rather than a tight one.
- **MCO and MRVL each forecast off only 2 samples.** Surfaced by the new `forecast_samples` field the
  day it shipped, and badged `n=2` in the dividends table. Not known-wrong — both are real payers with
  plausible schedules — but two samples is the exact shape that let SBI project a fake monthly cadence,
  so those projections deserve one look before being trusted. Check `manage_mappings list` for a
  `DIVIDENDS PREDATE MAPPING` flag (which would mean the rows came from an older ticker) rather than
  assuming either way.
- **`market_prices` gaps heal only at 08:00.** The 7-day jobs restore current value after a split
  purge; the full history comes back at the next 730-day `full_sync` — which, as of 2026-08-07, now
  actually runs daily. *Shipped 2026-08-07* in `docs/shipped-log.md` has why it had not.

## Known rough edges (accepted, not bugs)

- **FX drifts under 0.3% are accepted — owner decision, 2026-09-12.** This closes what used to
  be *Worth doing next* item 0: a security quoted in the base currency is valued through
  native→EUR→base with two independently rounded ECB quotes, ~0.12% high (`3.615 × 121.201101`
  served as 438.65 against 438.14), at the five valuation sites the timeline-equivalence test
  pins. The same round trip sits in the tax report's SELL-trade conversion (`tax_service`
  `_to_eur` + `base_fx.convert` where the summary uses `NativeToBase`), and `BaseFx`'s rate
  cache starts at the first tax lot rather than the first cash event (a pre-lot deposit uses a
  rate up to a week off). **None of the three is a task**; do not re-open them for anything
  under that threshold. If a figure ever drifts *past* 0.3%, the fix is the `NativeToBase`
  short-circuit applied at the valuation sites, and `test_timeline_equivalence.py` is the net.
- **The realized-P&L headline switches source all-or-nothing, unscoped by account** — found
  2026-09-12, dormant. `_realized_from_trades` uses `trades` for the whole book the moment
  *any* SELL exists there (the 3a importer writes them too), so closed lots of an account
  without `<Trades>` coverage would drop out. Today every IBKR sale is in `trades` (the
  holdings arrived in kind and were sold at IBKR), so nothing is wrong; `tax_service` already
  picks per account and per year, and the fix is to do the same here — a modelling change,
  not a one-liner.
- **A pillar-3a `LIQUIDATION` closes lots but books no realized P&L and no trade** — found
  2026-09-12, dormant (both 3a funds are accumulating). `finpension_ingest` computes the
  realized figure and discards it, writing an `INCOME` cash flow. Not fixed because adding a
  SELL trade beside the flow would double the Balance-oracle replay; the right shape needs a
  decision about how a liquidation moves cash. It becomes real on the first fund switch.
- **The Dividends KPI strip does not follow the year filter.** Its labels are absolute ("2026 so
  far", "Last 12 months") and the growth block is unwindowed by design — so selecting 2027 still
  shows this year's figures. Pinned by `test_growth_is_identical_whichever_year_is_selected`. This
  is the most likely thing to read as a bug when it isn't.
- **"Next 12 months" stays visible with the Forecast toggle off**, because that card is inherently a
  projection and hiding it would collapse the four-up grid. The per-year panel *does* respect the
  toggle, since it is the one surface that mixes measured and projected into one bar.
- **Accumulating ETFs correctly show no dividends** — DBPG, EMIM, IWDA, SXR8, VWCE, XAIX, XNAS.
  Verified, not assumed. **Don't "fix" their absence.**
- **Activity's Market Value delta and the chart's "Value change" are the same number**, shown twice
  on purpose: the card answers "how much did the portfolio move" at a glance, the chart header pairs
  it with Period Gain so the difference between the two is visible. Neither is a return.
- **`/api/portfolio/activity` shows dividends net, with estimates badged.** A `yfinance_estimate` row
  is a gross guess with no withholding and reads *Dividend · est.* This entry used to say the era
  splice deliberately does **not** apply to the ledger; that stopped being true on 2026-08-05, when
  not splicing turned out to list every dividend twice and overstate income 72%. It splices like every
  other reader now — pre-boundary estimates survive and stay badged.
- **`/api/portfolio/attribution` excludes-and-counts a line that did not exist at the window start.**
  A spun-off security has no start value to attribute against, so for a window opening before the
  spinoff it is dropped from both sides and reported as `unpriced_holdings: 1` — visible on the ALL
  range. Doing it properly means combining parent and child, which is a larger change than the
  valuation floor shipped on 2026-08-07. The timeline, the summary and the Steuerwert all handle it.
- **A price inside the session is an intraday value, not a close, and that is now normal.** Five of
  the seven market-data slots run mid-session, so the newest row is provisional until the market
  shuts; it is re-fetched at every slot for three days and settles on its own. Only a wrong value
  **older** than three days is a bug.
- **A benchmark's newest point can lag the portfolio's** if nobody opened the chart that day. The
  scheduled warm-up deliberately skips the provisional refresh — eight warm tickers × seven slots
  would multiply its burst for a value no one read — and the chart's own lazy fetch refreshes the
  benchmark actually selected. Deliberate Yahoo-budget trade, not an oversight.

## Watch after the next deploy

- **The projection-pending tail is live on `ec35713` and verified against the API
  (2026-09-19, 12:35 Berlin).** VT's September payment is on the calendar dated its own ex-date,
  `pending`, `ex_date`, at the amount the pre-ship A/B predicted; `next_pay_date` still reads the
  next *future* payment; no non-`pending` entry is dated on or before today; and no security
  carries two entries on one date.

  Verified before shipping by an A/B against one production snapshot, one base currency, old code
  and new in
  separate processes, across all six shapes `get_dividend_breakdown` serves (default, no-forecast,
  each of three years, 24m): **every key except `upcoming` is byte-identical**, no existing
  `upcoming` entry is removed or altered, and `upcoming` gains exactly one entry — VT, dated its
  own ex-date, `gross_estimate`, `ex_date`, `pending`. The no-forecast view gains nothing, as it
  must.

  **Still to see: the handoff on real data.** VT's Yahoo row was due at the 16:08 sync on the
  19th. When it lands, the projection stops being generated at all (the cadence steps from the last
  recorded ex-date) and the estimate tail takes the entry over — so VT should stay on the calendar,
  once, with the amount moving from the projected one to the recorded one. Two entries on 09-18, or
  none, is the thing to catch. A browser pass is worth it too, though nothing in the frontend
  changed: a pending row from this tail renders exactly like the five that were already there,
  which is itself the claim.

- **The estimated-net factor is live on `bb2bb48` and verified (2026-09-19, 11:25 Berlin).**
  Every gross-sized figure falls by exactly 0.85 — `forward_yield.annual_eur`,
  `growth.next_12m_eur`, `total_forecast_net_eur` and each `pending` calendar amount — while
  **every realized figure is byte-identical**: `total_net_eur`, `growth.ttm` / `ytd`, the annual
  actuals, all 31 monthly actual totals and all 32 `ttm_series` measured points. Provenance stays
  `gross_estimate` rather than flipping to `net`, and a second read returns the first read's
  numbers, so the factor cannot compound.

  **Verified by an A/B against one database snapshot, not by comparing two live reads — and that
  distinction is the lesson.** The first attempt compared production before and after the deploy
  and reported realized income moving by 7%, which would have meant the factor had escaped into
  the income ledger. It had not: the base currency was switched from EUR to CHF between the two
  reads, so every figure moved by FX as well. **Any check phrased as "this number should become
  that number" is unsound here** — `app_settings.base_currency` changes under you, and CLAUDE.md
  says so in its first paragraph. Hold the currency fixed by comparing one payload against
  another built from the same snapshot, or compare ratios inside a single payload.

- **Pay-date-dated dividend projections are live on `c791fd9` and verified against the API
  (2026-09-19, 10:45 Berlin).** All six securities that were blind now appear: the five with no
  measured lag read `ex_date` and `pending`, NVDA reads `measured_lag` dated three weeks past its
  ex-date. Eleven rows carry a lag, 7–29 days, matching what was measured before the change. Both
  invariants hold — no elapsed month carries a projection, no non-pending entry is dated on or
  before today — and the December-window-equals-calendar-year identity is exact for all three
  years. The migration (`v5e2a9b6c0d1`, additive: one empty table) applied cleanly.

  **Still to see in a browser**: the *payment pending* badge on a real row, the `ex …` note beside
  a shifted date, and the calendar at 390 px — that row gained two inline elements and is the one
  place this change is visible. Everything checked so far was the API.

  **And once the Flex section is ticked** (see *Needs a human*), which nothing has exercised yet:
  the next successful 18:00 Berlin `full_sync` should report a non-zero `dividend_accruals_seen`,
  `pay_date_source` should flip to `accrual` for the held payers, and an accrual should vanish
  within a sync of its cash posting.

- **The range-aware Dividends growth figure is built and unverified on production.** CMGR (and
  CAGR at twelve months of span or more) between the first and last rolling window on screen,
  from `lib/dividendPace.ts`; the server contributes only `ttm_coverage_start`. Verified locally
  against the five live `d4640d4` payloads replayed through a browser, and every range matched:
  All time +15.3%/mo & +451%/yr, 24m +21.3% & +918%, 2025 +28.5% (no CAGR, 7-month span), 2026
  +18.3% with forecast and +14.5% without, 2027 +6.6%, 2027 with forecast off renders nothing.
  **Check on production**: those figures against the real API, that `ttm_coverage_start` is
  `2025-05` in every range, and that the `†` shows on All time / 24m / 2025 and not on 2026 / 2027.
  **And in a browser**: whether the All-time figure is worth keeping at all once seen in context —
  if the funding ramp makes it noise rather than information, dropping CAGR on coverage-limited
  ranges is a two-line change.

- **The stacked, forecast-aware Dividends TTM is live on `170df7c` and verified against the API
  (2026-09-18, 20:55 Berlin).** Both identities hold on production: the December window equals
  that calendar year's annual row to the cent (2025 21.83, 2026 147.85, 2027 309.96 — all three
  exact), and All time's rolling series reaches 2027-12 against the monthly chart's 2026-12, which
  is the separate-array design doing its job. Per-symbol maps reconcile to their scalars within a
  cent or two on wide windows — independent rounding, worst case 0.03 on 309.96 (0.01%), written
  up in `docs/dividends.md`; not a defect and not worth chasing.
  **Still to see in a browser on production**: the solid/dashed split on a real book, the legend
  in TTM mode, the toggle dropping the open windows, and the tab at 390 px. Local verification
  used synthetic fixtures and made no Yahoo or Flex requests.

- **`/api/performance/*` is live on `ec3178a` and verified read-only (2026-09-13, 12:05
  Berlin).** All three routes 200; on the 1Y and 3M windows and on every calendar year the legs
  sum to `end − start` to the cent; `sum(by_sector)` and `sum(by_country)` equal `total_pnl_eur`
  (14 and 53 rows); the per-security attribution total equals the segments total, so the two
  surfaces agree. `cash_adjustment_eur` is non-null on 2026 windows and 0 for 2025 (measurement
  began 08-25). Three things seen that are correct and worth knowing: **2025 carries 7 unsplit
  holdings** (in-kind-transferred lots whose open dates predate the cached FX history, so their
  gain is carried whole, not guessed); **Unknown is the second-largest sector and country**
  because the allocation sync has classified only part of the direct book — run
  `POST /api/allocation/sync` to shrink it; and **every one of the 16 sold securities has a
  post-sale price** (the market-data pass still prices them), so *Rose after sale* reads 12 of 16.
  **Still to see in a browser**: the tab at 390 px, the rolling-risk card asking for 2Y/ALL on
  a 1Y range, beta only with a benchmark selected, and the reordered tab strip (`4eb3d3b`).

- **Sibling-class pricing for the 3a EM fund is live and verified (2026-09-12, 15:08
  Berlin).** Deployed `ce0035a` at 13:22, activated with `manage_mappings set CH1529078078
  FUND 0P0000S0OE.SW --sibling` (33 carried rows dropped, the 09-01 NAV kept as anchor), and
  the 15:00 market-data slot wrote the first `sibling_scaled` rows: 09-07, 09-09, 09-10 —
  the 7-day window's weekdays for which the sibling has a close (no 09-08 bar on Yahoo, 09-11
  not yet published; mutual-fund NAVs lag a day). The position reads `price_source: sibling`,
  `market_price 122.358` = 121.201 × (182.56 ÷ 180.83), +0.95% since the anchor — not the
  carried 121.20, not the wrong-class ~182. Run status `success`, no refusal. **One thing left
  to see once**: the 18:00 `full_sync`'s 730-day pass backfills 09-02 … 09-04 (the intraday
  slots only look 7 days back) — not checked on 09-13; the price rows are only readable over ssh,
  and this session had no production-read permission. In the browser the row carries a "sibling NAV" badge. If the
  sibling ever stops quoting, the 7-day "price feed looks broken" warning covers it. The same
  run also verified this morning's benchmark fix: `benchmark_result.benchmarks_total: 8`,
  `rate_limited: false`.

- **The 2026-09-12 bug sweep is live on `e6e7698` (11:22 Berlin) and the API checks passed**
  — timeline tail `cash_source: mixed` matching the summary, SK Hynix and every other forecast
  row `gross_estimate`, non-ASCII key → 401, current-year tax report → 200 (details in *Shipped
  2026-09-12*). Still to observe:
  - **The 18:00 Berlin `full_sync` is the first live SendRequest through `send_flex_request` —
    not yet exercised: the 09-12 slot skipped (`already_generated_today`), so watch 2026-09-13**
    (one GET, `(10, 60)` timeouts, ibflex's parser). Expect `ibkr_result.status: success` with
    a reference code logged as before. If it reads `error` with a message naming `SendRequest`
    or `BadResponseError`, compare the request against `ibflex.client.submit_request`'s
    (`params={"v": "3", "t", "q"}`, `user-agent: Java`) — the GET is meant to be identical —
    and do **not** retry by hand; the 00:00 slot is the recovery.
  - `benchmarks_total: 8` and `rate_limited: false` **seen on the 15:00 run** (the 13:00 run
    predated the deploy). **Seen on the 09-12 18:00 `full_sync`**: four stale baskets (GRID,
    IQQ, QQQM, SOXQ) refreshed to 09-10, `identifiers_pending: 25` — the bound held — and the
    following runs carry an empty `warnings[]`.
  - The dividends cooldown (429 on a second `POST /api/dividends/sync` inside five minutes) is
    **closed by the owner on 2026-09-12**: dividends update as expected.
  - **The finpension importer is rehearsed on a copy of production, 2026-09-12 12:00 Berlin.**
    Against the 09:40 UTC auto-deploy snapshot and the real 09-06 export: dry run parses 5 rows;
    a real re-import succeeds with the guard's baseline read from production's own `sync_runs`
    row (`rows: 5`), 2 trades / 3 deposits / 2 lots / 35 price rows (the World fund is
    Yahoo-priced, so only the EM fund is carried); with the World fund's 09-01 row forced to a
    `yahoo_finance` bar the re-import still succeeds and the Yahoo row survives untouched — the
    exact IntegrityError shape, gone; a `sync_runs` row pretending the previous import parsed 6
    rows makes the 5-row file refuse with "had 6", and `--force` still applies. Snapshot deleted
    afterwards. The next routine monthly upload needs nothing special; do it before the carried
    NAV runs out (last NAV 2026-09-01, the day-40 warning fires ~10-11).
  - Frontend, in a browser: on a range starting before inception (none of the buttons reach it
    on this account today — the general case is younger accounts and backfills), Max/Current
    Drawdown and the hero `DeltaChip` show *unmeasurable* rather than `0`; the value chart shows
    the "partly IBKR's own figure" cash caveat it was dropping; the Look-through tab shows an
    alert naming any unvaluable position above its KPIs; the watchlist's Analyst sort puts
    `strong_buy` first descending; EPS figures carry no `$`.

- **The WAL fix is live (`b6bbe6f`, 18:50 UTC) and its own deploy lost nothing** — the WAL
  was checkpointed by hand right before the push and the newest `sync_runs` id was 378 on
  both sides of the deploy. `/root/backup-db.sh` was refreshed from the repo at 18:52 UTC
  (auto-deploy and the daily cron prefer that copy) and a manual backup logged
  `WAL checkpoint (busy, frames, done): (0, 0, 0)` before its snapshot. Still to observe
  once: the *next* deploy's `/root/auto-deploy.log` should carry the same line between the
  image build and `Stopping` — the first deploy to run the new `deploy.sh` end to end.

- **The dependency bump (2026-09-08) is a major Starlette jump: 0.35 → 1.6, with FastAPI
  0.109 → 0.141, pydantic 2.5 → 2.13, httpx 0.26 → 0.28, pytest-asyncio 0.23 → 1.4.** The
  suite passed 1,443/1,443 on the new stack, and the one thing that broke was test code —
  FastAPI 0.141 keeps included routers as lazy nodes in `app.routes`, so the auth-coverage
  walk now uses `fastapi.routing.iter_route_contexts`. Watch `/health` for `write_auth_enabled:
  true` and one `POST` without a key answering 401 after the deploy; the middleware chain is
  where a Starlette major would bite first.

- **`deploy.sh` changed, so the deploy that ships it runs the OLD copy once** (CLAUDE.md,
  *Deployment*): expect the usual full-build outage one more time, and the new build-first
  order from the next push on. After that push, `ls /root/IBKR_investment_tracker/frontend`
  should show `dist` and no `dist.next` / `dist.old`.

- **Migration `u4d1f8a5b9c0` drops `benchmark_timeline_cache` on the first start.** Nothing
  reads it; a benchmark chart on any range should render exactly as before, ALL included.

- **The first scheduled-path refresh ran by hand on production right after `75ccc3a` landed**
  (recorded as a `manual_etf_basket` run): six baskets refreshed — GRID, QQQM, IQQ, QTUM, SOXQ,
  XAIX — 109 CINS/SEDOL identifiers and 25 ISINs resolved, and **one failure that was a real
  parser bug**: BlackRock dates EMIM's file `07/Sept/2026`, a four-letter month `%b` does not
  accept, so `parse_ishares` refused a good basket and the 3a EM fund stayed on the August one
  — exactly the isolation the design promised (previous basket kept, one warning, job status
  untouched). Fixed in the follow-up commit; the next 18:00 run, or a by-hand re-run, should
  refresh EMIM and leave the market-data `warnings[]` empty for the first time since late
  August. Read `lookthrough_result` on the 18:00 run in `/api/scheduler/history` to confirm.

- **`kept, next run:` is finally readable, and it says `kept` for all nine jobs.** This entry asked
  for exactly that line and it could not be checked before 2026-08-04 for a dull reason: it is
  logged at INFO, and `settings.log_level` configured nothing, so it never reached the container log
  at all. With logging fixed, the rebuild at 19:21 UTC shows every one of the nine jobs `kept` — not
  `rescheduling` — which is `_add_or_keep` finding an identically-triggered job already in the
  store. **That confirms the store genuinely persists across a container recreate**, which until now
  rested on inspecting the sqlite file rather than on the code's own account of what it did.

  **Still unobserved: misfire recovery end to end.** `kept` proves the stored job and its run time
  survived; it does not prove APScheduler *runs* one that was missed. That needs a deploy landing
  within `MISFIRE_GRACE_SECONDS` of a Berlin slot, and none has — though with nine slots instead of
  five it is now much likelier to happen by itself.

  Read a `false` on `scheduler_jobstore_persistent` as: the store fell back to memory again, so a
  deploy overlapping a slot still loses that sync. `/api/scheduler/status` cannot tell you — it
  lists every job either way, which is how this went unnoticed for two days.

- **Container log timestamps are UTC, the sync slots are Berlin.** `%(asctime)s` uses the
  container's local time and `python:3.11-slim` sets no `TZ`, so a line reading `19:21` is `21:21`
  Berlin. Same convention as every stored timestamp (see CLAUDE.md's naive-UTC paragraph), but it is
  a two-hour trap when you are matching a log line against a slot — in the direction that makes an
  on-time sync look early.

## Worth doing next

0. **Measure the withholding rate instead of assuming 15%.** `DEFAULT_DIVIDEND_NET_FACTOR = 0.85`
   is one global constant applied to every gross-sized forecast, and it is the US/Dutch treaty
   rate — German (26.375%), Korean and Taiwanese payers withhold materially more, so their
   projections still read high. The honest figure is already in the database:
   `withholding_tax_eur / gross_amount_eur` on the IBKR rows. It cannot be measured *per security*
   where it is needed — a security with IBKR payments already takes the `net` branch and never
   touches the factor — but it can be measured **per country**, off the ISIN prefix the DA-1
   report already groups by, and applied to an unpaid holding in the same jurisdiction. That is
   the same shape as the ex→pay lag: derive it from matched history, report the sample count, and
   fall back to the global constant when a country has none. Until then the caption says "assumed
   withholding", which is true and is the reason this is a *next* rather than a defect.

0. **Brinson allocation / selection attribution against the benchmark — needs data first.**
   The Analytics tab (2026-09-13) shows each sector's *weight at start* beside its *share of
   gain*, which is the portfolio-only half of the question. The other half — "did I beat the
   index by being overweight the right sectors or by picking the right names inside them" —
   needs the benchmark's per-sector weights and per-sector returns over the window. The
   benchmarks are indices with one price series each and no basket, so both are new upstream
   data: either an index ETF basket (e.g. SPY/URTH from an issuer file, which the basket
   fetchers already know how to store) plus one price series per sector ETF, or a factor
   provider. Budget it against rule 1 before building. Design is in
   `docs/performance-analytics.md`, *What is deliberately not here*.

0. **Mount the database's directory, not the file — the WAL-on-deploy data loss is
   mitigated, not fixed.** Found 2026-09-08 while verifying the basket refresh: a run's
   commits vanished in the next deploy. `./portfolio.db:/app/portfolio.db` bind-mounts a
   FILE, so SQLite's `-wal`/`-shm` sidecars live in the container layer and go with
   `docker compose down`; every deploy discarded the commits since the last auto-checkpoint
   (~4 MB), and every host-side backup lacked the same tail. Three checkpoints now cover
   the deploy path (app shutdown, `deploy.sh` before `down`, `backup-db.sh` before the
   copy), but a killed container never reaches its shutdown hook and the deploy that
   ships a `deploy.sh` change runs the old copy — so the sidecars have to land on the host.
   The job store already does this (`scheduler-data/`). Plan, in one deploy with the
   container down: `mkdir backend/data && mv backend/portfolio.db backend/data/`, compose
   `- ./data:/app/data`, `DATABASE_URL=sqlite+aiosqlite:///./data/portfolio.db` in the host
   `.env` (`up -d`, never `restart`), `deploy.sh`'s `touch portfolio.db` guard, `backup-db.sh`'s
   `DB` default, the `sqlite3.connect` snippets in CLAUDE.md, and `/root/backup-db.sh` on the
   VPS, which is a *copy* of the repo script and does not update itself (auto-deploy prefers
   it when present). Checkpoint by hand before the `mv`. Do it with the owner present.

0. **Stamp the attempt when Yahoo has no analyst rating or no fundamentals for a security**
   — found 2026-09-12, medium-low. `AnalystRatingService.sync_stale_ratings` and the
   fundamentals sibling write nothing when Yahoo answers "no data", so every ETF in the book
   is "missing a rating" forever and re-queued on every `sync-stale` pass: a real Yahoo
   request plus the 2–4 s pacing each, for an answer already known. Only the explicit routes
   reach it (no scheduled caller, no frontend auto-trigger), so it costs budget only when
   someone clicks — which is why it was left out of the sweep's fix list. The pattern is
   `AllocationService`'s `allocation_last_updated` stamp on the *attempt*
   (`allocation_service.py`, "leaving it null on failure meant a security Yahoo has no
   `.info` for was re-fetched on every sync forever"): write a row with zero counts and
   `last_updated`, which `AnalystRating.consensus` already reads as "No Rating".

0. **Decompose the World ex CH tranche from the fund that tracks its actual index.** It is
   ~1.9% of the book sitting in `uncovered_fund`, and the donor exists: **iShares World ex
   Switzerland Equity Index Fund (CH), `CH0244028970`**, MSCI Developed World ex
   Switzerland, CHF, BlackRock product id **279894**. Two things make it more than a
   one-line `basket_proxy_isin`: it is a Swiss institutional index fund rather than a UCITS
   ETF, so holdings come from a product-page `.ajax?fileType=xls` rather than the varnish
   JSON `parse_ishares` reads, and its `portfolio_id` is **not** the product id (the IQQ
   coincidence does not generalise — read it from the sitemap and confirm by row count).
   Deliberately not proxied to IWDA meanwhile: that would put Nestlé, Roche and Novartis
   into the look-through at ~2.5% of the position, which is a *fabricated* holding rather
   than the understatement every other proxy here errs toward.

Rough priority. The auto-deploy install moved to *Needs a human* — it is the last deploy step.

1. **Decide whether a trade should still count as an external flow, now that cash is tracked.**
   The 2026-08-26 cash work deliberately stopped at the totals and the charts: XIRR, Modified Dietz,
   the monthly heatmap, drawdown, Sharpe, Sortino and beta all still read `market_value_eur` and
   still treat a purchase or a sale as money entering or leaving the measured pot. That was correct
   before — holdings *were* the pot — and it is now a modelling choice rather than the only option.

   With cash inside the pot the textbook definition applies: **only deposits and withdrawals are
   external flows**, and a trade is an internal transfer that nets to zero. That makes time-weighted
   return exact instead of Modified-Dietz-approximate, removes the flow-day exclusions beta needs
   (`MIN_PAIRED_RETURNS` currently drops every day the portfolio traded), and makes the figures
   describe the account rather than its invested sleeve.

   It is not free, and the trade-off is why it was not bundled in. Returns would start including the
   **drag of idle cash**, which is a different question from "how did my investments do" — with
   12,229 CHF undeployed that is ~18% of the book earning nothing. Both are legitimate; showing one
   silently in place of the other is not. The likely answer is to serve both and label them, which
   is a design decision rather than a refactor.

   Whatever is chosen, `externalFlow` and `isMeasurable` in `portfolioKpis.ts` are where it lands,
   `dailyReturnSeries` has five consumers, and `betaAndCorrelation` derives its own returns
   separately — so all of them move together or the tab disagrees with itself.

2. **SEC N-PORT as the generic fallback for a future fund with no issuer route.** Items 1 and 2 here
   were the SMH adapter and an N-PORT adapter for SOXQ/GRID/QTUM; **all four now have issuer feeds**
   (2026-08-16), which are T-1 rather than 75–136 days old, so N-PORT is no longer needed for
   anything held. It is worth keeping as the escape hatch for the next US-registered fund bought
   without an issuer route — and **structurally unreachable for Irish and Luxembourg UCITS**, which
   are not SEC registrants, so it can never be the answer for VWCE. The research is expensive to
   redo and two points are silent-corruption traps:
   - **Key on ISIN, never ticker.** `company_tickers_mf.json` maps `SMH → CIK 1137360` (VanEck's *US*
     fund) and `XAIX → CIK 1503123` (a US namesake of the Xtrackers UCITS we already fetch) — a ticker
     lookup silently returns the wrong vehicle for **two of our twelve funds**. Verified:
     SOXQ `(1378872, S000072470)`, GRID `(1364608, S000026919)`, QTUM `(1540305, S000062478)`.
   - **`data.sec.gov/submissions/CIK*.json` is series-blind** (First Trust ETF II: 976 NPORT-P
     entries, zero `S000…` strings, 22 accessions filed in one day). Use
     `efts.sec.gov/LATEST/search-index?q=%22S000026919%22&forms=NPORT-P`, then
     `/Archives/edgar/data/{cik}/{accession}/primary_doc.xml` (49–141 kB). `/cgi-bin/` is
     robots-disallowed; `/Archives/` is allowed. A descriptive User-Agent is **mandatory** —
     omitting it is a 403 plus a ~10-minute IP block.
   - **Assert `formData/genInfo/seriesId`** before using a row — a sibling series is a silently
     100%-wrong basket.
   - **As-of is `repPdDate`, not `repPdEnd`** (the latter is the fiscal year end), and fiscal
     quarters are not calendar quarters.
   - **QTUM's `pctVal` sums to 104.61%** (sec-lending collateral). Filter to `assetCat ∈ {EC, EP}`
     with an `<isin>`, then renormalise, or every weight inflates ~4.6%.
   - Arrives **75–136 days old**, so it needs its own `ADAPTER_STALE_DAYS` entry rather than a raised
     default.

3. **Commercial holdings APIs are not viable free — checked 2026-08-16, do not re-shop.** The
   holdings array is the paywalled field at every vendor: FMP's free Basic is 250 calls/day but
   holdings are Ultimate-tier and US exchanges only; API Ninjas models UCITS domicile correctly but
   gates `holdings` behind premium; EODHD's Fundamentals feed costs 10 calls against a 20/day quota
   and is the $59.99 tier, not the $19.99 one; Intrinio has genuine global coverage at enterprise
   pricing. **Never wire up FMP's "ETF Holder" endpoint** — it returns the institutional investors
   who own shares *of* the ETF, the reverse direction, and it returns plausible-looking garbage that
   passes a smoke test.

4. **Look-through coverage keeps itself current since 2026-09-08 — what remains is the measured
   sector/geography project.** The detector shipped 2026-08-17; the refresh shipped 2026-09-08 as
   the last step of the 18:00 `full_sync`, built on the detector's own verdict
   (`etf_basket_refresh.stale_basket_verdicts`), with the CLIs' refusals intact and a bounded
   identity pass behind it. Identities still have no detector of their own; `unresolved_value_eur`
   is the figure that shows what the evening pass has not yet reached.

   Then, once every held fund has a basket: `etf_holdings.sector` and `.country` are the raw material
   for replacing `etf_mappings.py`'s hand-estimated sector/geography blocks with measured ones.
   `sector` is now normalised through `sector_taxonomy.py` and served for **grouping** the
   look-through treemap, which is half the work already done; `country` is still unserved and needs a
   country-to-region map with its own test — `countryOfRisk` is a country and the charts bucket
   regions — so it is a real project rather than a flag. The precondition is written into
   `etf_mappings.py`'s docstring, and the reason not to serve two sector *totals* at once is in
   CLAUDE.md.

5. **Fold `PRE_OWNERSHIP_HISTORY_YEARS` pruning into a scheduled job — reassess before building.**
   `prune_empty_dividends.py` is a manual CLI and the ingest window already prevents new junk, so
   there is very little left for a scheduled run to find. Investigating this on 2026-07-31 turned up
   a **defect in the CLI rather than a case for automating it** (below), which is a fair warning
   about automating a deleter over financial rows: the value is small and the downside is silent.
   If it is built, it must reuse the CLI's predicate rather than re-deriving one.

6. **Project the benchmark's baseline the way the portfolio's is projected — now one quantity,
   not two lookalikes.** Since 2026-08-26 the benchmark invests the *same* `money_in_legs` the
   chart draws, so this stopped being "two similar series" and became one series projected two
   ways: the benchmark's baseline wobbles with FX (53,308-53,725 across that week) while the
   portfolio's sits flat at 53,330 — under half a percent either way, not the 3,000 an early
   verification claimed by double-applying `_apply_base_currency`, which the service already
   calls before returning. Still invisible
   on the chart (only the benchmark's value line is drawn) and it does not reach beta, which
   excludes flow days — which is why it is here rather than in that change. Originally found while
   fixing Beta (above) and left alone as out of scope. `_apply_base_currency` converts the running
   cost basis at each point's date; `_calculate_timeline_swept` converts each lot's cost at its own
   `open_date`. Same tax lots, two conversion rules — the *dominant failure mode* in CLAUDE.md, in
   the form where both copies keep working and just stop agreeing. It already cost the Beta card
   outright, and it leaves the two cost lines on the chart drifting apart with FX. The awkward part
   is that the timeline cache is EUR-only on purpose (so switching base currency never invalidates
   it), and a per-lot conversion cannot be derived from the cached aggregate — the cost *events* have
   to be re-projected at read time. Do not "fix" it by making the portfolio convert per-date instead:
   that direction breaks the contributions identity, which depends on each leg converting at its own
   date. The window-anchored series (2026-09-07) inherits it — its `cost_basis_eur` is the seed
   plus the contributions since, converted at each point's date — and still does not draw it.

7. **CLAUDE.md restructured and STATUS.md's log moved out — done 2026-09-08 (night); one
   prune left.** CLAUDE.md is ~270 lines (~7k tokens, from ~92k): the two rules, the conventions
   that hold everywhere, deployment and local-dev essentials, and an index. Every other section
   moved *verbatim* into `docs/<topic>.md`; the 35 *Shipped* / *Ran on production* sections moved
   to `docs/shipped-log.md`. What remains: this file's own header is still a stack of "Before
   that:" recap paragraphs (~200 lines) — each should move to the shipped log once its
   verification is done, leaving one paragraph for the latest change. `docs/claude-md-analysis.md`
   is the measurement that sized all of this.

8. **Doc and hygiene drift the audit turned up, none of it shipped yet.** `README.md` is the
   public front page and still says "all values normalized to EUR", lists five tabs of nine and
   none of Tax, Dividends, Activity, Look-through, cash, Pillar 3a or write auth. `.env.example`
   lacks `LOG_LEVEL`, `LOOKTHROUGH_CONTACT_EMAIL` and `SCHEDULER_JOBSTORE_URL`. The `ResizeObserver`
   stub is copied into seven test files and belongs in a vitest `setupFiles`. ESLint is red (17,
   seven of them the react-refresh export rule) and neither it nor ruff runs in CI; ruff flags one
   bare `except` in `currency_service` and two dead assignments (`gross` in `activity_service`,
   `price_repo` in `routers/market_data.py`). `alembic check` fails on naming drift only — the
   `ticker_mappings` unique constraint exists unnamed, `corporate_actions` indexes are named
   `ix_corp_actions_*`, and `index=True` sits on primary keys — so it cannot guard the next
   forgotten migration until one reconciliation pass makes it green.

9. **Three duplicate helpers the AST lens found, each a copy that still agrees.** `_to_eur` in
   the dividend and tax services (identical four-line bodies, each docstring citing the other;
   CLAUDE.md records syncing them twice — extract to `CurrencyService`). Three CLI copies of
   "resolve a security by symbol and exchange, refuse ambiguity" in `import_prices`,
   `purge_dividend_estimates` and `manage_mappings`. The inner `_fetch` closures in the fundamentals
   and watchlist services, which pull the same yfinance sub-endpoints and differ only in that one
   logs each failure and the other swallows it. Also `BaseFx.convert` returns the EUR amount when no
   EUR→base rate exists at all (reachable only on a fresh database when Frankfurter also fails), a
   stand-in that claims a value.

## Local development traps

Each of these cost real time at least once.

- **A fresh worktree needs its own dependencies.** The global Python installation has an older
  FastAPI that cannot collect `test_api_hardening.py` (`iter_route_contexts` import error).
  Use a Python 3.12 `backend/venv` with `backend/requirements.txt`; use placeholder Flex credentials
  and `SCHEDULER_ENABLED=false` for offline tests.

- **A service-level benchmark test can reach Yahoo.** `calculate_benchmark_value_over_time`
  calls `_ensure_prices_available` itself, which fetches whenever the fixture leaves a gap it
  considers missing — a leading gap at the range start, or a trailing day inside
  `PROVISIONAL_PRICE_DAYS` of today — and its `except Exception` swallows a raiser, so the smoke
  test's `yf.Ticker` stub alone is *silent* here. This cost exactly one real `^GDAXI` request on
  2026-09-07 from a fixture whose index started three days after the window. Stub both fetch
  steps for the module and assert the raiser was never reached on teardown; the `_offline`
  fixture in `test_benchmark_window_anchor.py` is the pattern.
- **`SCHEDULER_ENABLED=false` in `backend/.env` for any local run.** Otherwise starting uvicorn arms
  the eight daily Europe/Berlin jobs against the live Flex token, Yahoo and, at 18:00, the
  issuer sites. Defaults to `True` so
  production is unaffected.
- **Check which port Vite actually took.** If 5173 is occupied it moves to 5174 and says so once. A
  stray dev server on 5173 configured against production means you are reading prod data and issuing
  requests to the live site — including `/api/dividends/summary`, which can enqueue a Yahoo sync.
- **Use a snapshot of the production DB, not the local `backend/portfolio.db`** — the latter predates
  trades, cash flows and the IBKR dividend era, so it exercises none of the interesting shapes.
  `sqlite3 .backup` on the VPS, copy down, point `DATABASE_URL` at it, **delete it afterwards** (it
  is real account data; `*.db` is gitignored but it should not linger).
- **The base currency is whatever the user last picked** (`/api/settings`, EUR/CHF/USD). Every money
  figure moves with it, so never compare a number across sessions without checking it.
- **Don't push within ~10 minutes of a Berlin sync slot** — eight of them, on the hour at
  00/08/11/13/15/18/20/22 (06:00 retired 2026-08-08). An overlapping deploy used to lose that sync
  outright; since 2026-09-08 `deploy.sh` builds before it stops anything, so the outage is the
  seconds of `down`/`up` rather than the whole build, and the persistent job store recovers a slot
  missed by under 30 minutes. `ops/finish-deploy.*` checks the window for you.
- **`curl 127.0.0.1:<vite port>` fails while the browser works.** Vite binds `localhost`, which
  resolves to `::1` first on this machine, so the IPv4 literal gets connection-refused and looks like
  a dead dev server. Use `http://localhost:<port>`.
- **`pkill` is not installed** (Git Bash has no procps). `pkill -f uvicorn` prints
  "command not found" and exits non-zero — easy to miss inside a `&&` chain — so the old server keeps
  the port and the new one dies on bind while `--strictPort`'s error scrolls past in a log file. The
  result is a dev server quietly answering from the *previous* config, which cost real time here: a
  browser pass appeared to show an empty ledger when it was reading the stale local DB. Kill by PID:
  `netstat -ano | grep ':8000 ' | grep LISTENING` then `taskkill //F //PID <pid>` (double slashes —
  MSYS eats single ones).
- **A dev server against a prod snapshot must run on port 5173.** `frontend/.env` points
  `VITE_API_URL` at `localhost:8000`, so the browser calls the backend cross-origin and only the
  ports in `CORS_ORIGINS` work. Any other port fails every request with a CORS error and looks like
  a backend outage.
- **Every position in the local `backend/portfolio.db` has `market_price: null` and a market value of
  0.00.** So the currency-exposure card reports *no priced positions* and the rebalance panel shows 29
  unpriced rows — both correct, and both easy to mistake for a broken feature. Anything that depends on
  a valued portfolio can only be browser-verified against a production snapshot.
- **`ResizeObserver` is stubbed inline in seven test files now.** Consolidating it is a real
  follow-up; until then copy the block from `RebalanceCard.test.tsx`, and remember a jsdom test that
  renders anything through `DataTable` needs it because `ScrollableTable` measures overflow.
- **uvicorn can die mid-Playwright-run with `OSError: [WinError 64] The specified network name is no
  longer available`** — a Windows asyncio-proactor reaction to an abruptly closed connection, not a code
  fault. The e2e script then reports `ERR_CONNECTION_REFUSED` and a shrunken panel, which reads exactly
  like a regression in whatever you just wrote. **Confirm `/health` still answers before believing an
  e2e failure.**
- **A test helper with a default parameter swallows an explicitly-passed `undefined`**, so
  `renderCard(undefined)` renders the default fixture and any "backend down" assertion silently tests the
  loaded state instead. `CurrencyExposureCard.test.tsx` keeps a separate `renderUnloaded()` for this.
- **A jsdom test that renders `ScrollableTable` needs a `ResizeObserver` stub**, and a component test
  that renders anything using `useBaseCurrency`/`useCurrencySymbol` needs a `QueryClientProvider`
  *above* `CurrencyProvider` — the provider reads the base currency through TanStack Query, so
  wrapping in `CurrencyProvider` alone throws `No QueryClient set`. Neither is a component defect:
  `ResizeObserver` has been in every browser since 2020, so guarding production code for jsdom's gap
  would be wrong. `RebalanceCard.test.tsx` has both patterns to copy.
- **A test that starts a scheduler drops a `scheduler_jobs.db` wherever it runs.** `tests/conftest.py`
  blanks `scheduler_jobstore_url` for the whole suite, so an in-memory store is the default; a test
  that wants persistence points it at `tmp_path` itself.
- **`sqlite:////tmp/x.db` in Git Bash lands in `C:\tmp`, not the shell's `/tmp`.** The SQLAlchemy URL
  is read by Python, which does not apply the MSYS path translation, so a stray file goes somewhere
  `ls /tmp` will not show it.
- **`TZ=Europe/Berlin date` silently returns UTC in Git Bash.** It does not error and it does not
  warn — `TZ=America/New_York` prints the same time — so any script reasoning about the Berlin sync
  slots from the shell clock is two hours out in summer, in the direction that permits a collision.
  Use Python's `zoneinfo` (`ops/finish-deploy.sh` has the helper).
- **Renaming the working-copy directory breaks `backend/venv`, and the documented test command is the
  one thing that hides it.** `activate`/`activate.bat` hardcode `VIRTUAL_ENV` and every `.exe`
  console-script shim embeds the interpreter's absolute path, so after a rename
  `venv\Scripts\activate && uvicorn ...` and a bare `alembic upgrade head` fail while
  `./venv/Scripts/python.exe -m pytest` keeps passing — python.exe resolves its own prefix, the shims
  do not. Recreate rather than patch, and **`pip freeze` first**: `requirements.txt` floats
  `yfinance`, `lxml` and `pyxirr`, so reinstalling from it quietly drifts the local env (the frozen
  set here was `yfinance==1.1.0`, i.e. exactly the documented floor). Then
  `python -m venv venv --clear` and install the freeze. A plain `python -m venv venv` regenerates
  `activate` and leaves the shims broken, which is the worst of the three states.

## Recent sessions (last 5)

One line each, newest first. **Drop the oldest rather than growing this list** — `git log` holds the
detail; this exists so the next session knows what just moved without reading it. Distinct from the
*Shipped* write-ups in `docs/shipped-log.md`, which record what shipped and what was verified: these
lines are permanent, so don't "tidy up" the overlap by deleting the wrong one.

- **2026-09-19 (the projection that died on its own date)** — "VT went ex yesterday and the
  September payment is missing entirely". It was, and not for the reason suspected: the pending
  tail added hours earlier was working correctly for five other securities, but it rescues
  dividends **yfinance has recorded**, and yfinance publishes the row the day after the ex-date.
  So a projection is now kept past its own date too. Two lessons. **Confirm the mechanism, do not
  accept a plausible one** — "ex-date fallbacks are not retained" was nearly right and would have
  produced a fix for a bug that did not exist; the API showed four ex-date fallbacks retained
  correctly on the same screen. And **a bound written as a side effect is not a bound**: the
  expiry falls out of the projection's start date, so it is pinned against the constant instead,
  and the same look found `_open_accruals` silently ageing out accruals its own docstring promised
  to keep.

- **2026-09-19 (net-factor follow-up)** — merged Codex's shared 0.85 factor for gross-derived
  forecasts and unpaid calendar estimates, cherry-picked onto main so the restyle in the tree
  stayed untouched. Two lessons, both about checking rather than coding. **A base currency that
  changes under you invalidates every before/after comparison**: the first post-deploy check
  read realized income as having fallen 7% and it had not — EUR became CHF between the two
  reads. Hold the currency fixed with an A/B on one snapshot. And **a constant can be right in
  direction and still wrong in detail**: 0.85 is the US/Dutch treaty rate applied to a book that
  also holds German, Korean and Taiwanese payers, and the measured rate is already in the
  database — recorded as *Worth doing next* rather than waved through.

- **2026-09-19 (later)** — "verify whether forecasts use ex-dividend date, payment date, or
  another date". They used the ex-date, everywhere, under field names that said pay date — and the
  investigation turned that labelling question into a live defect: six held securities had gone ex
  and were in no figure at all, because the projection dies on its own date and the era splice
  drops the estimate recording the same payment. Projections are dated on an expected pay date now
  (announced accrual > measured lag > ex-date, each named on the wire), and a gone-ex-unpaid
  dividend is `pending` on the calendar and in no total. Three lessons. **A field name is a
  claim** — `next_pay_date` carrying an ex-date was the whole bug, visible in the schema for
  months. **Measure before designing**: reading production turned "the lag is probably stable"
  into 7–29 days, stable per security, every payment paired — and revealed that half the payers
  have nothing to measure, which is what made the accrual ingest worth building rather than
  optional. And **a hand-kept map inside a guard is a hole in the guard**: the Flex attribute
  coverage test was parametrized over a dict someone has to remember to extend, so a new extractor
  was not unchecked but invisibly unchecked. Live and API-verified; the Flex section needs a tick.

- **2026-09-19** — rewrote yesterday's dividend growth pace after the owner called it unusable:
  it was unwindowed by design and so ignored the range selector entirely. Now CMGR/CAGR between
  the first and last window on screen, computed client-side from the array that already defines
  which windows those are, with the server-side pair deleted. The lesson is about the decision, not
  the code: **a figure shown beside a filter must answer that filter** — "computed over the full
  history" is right for the KPI tiles because they are labelled as such, and wrong for anything
  sitting under a range dropdown. Not deployed.

- **2026-09-18** — copied CLAUDE.md to AGENTS.md; shipped dividend Monthly/TTM and Last 24 months
  (`c0ba465`, live); then rebuilt TTM at the owner's request as a forecast-aware stacked column
  chart on its own `ttm_series`, trimmed of uncoverable months and working on a future year.
  Lessons, each a wrong number avoided: **a rolling window is already a total, so ranking by the
  sum of windows ranks by payment timing** (January lands in twelve windows, December in one);
  **gate an extension on the projection existing, not on the flag asking for one**, or the flag
  manufactures a decline — the `next_12m_vs_ttm_pct: -100.0` shape again; and **end a series at
  the horizon, not at the last data point in it**, or two ranges disagree about which months
  exist. Simulating the change against production first produced the two identities it is now
  tested on. Shipped as `170df7c`/`7ba36b8` and verified live. Then, same day, a **growth pace**
  on that series — geometric average monthly growth over six months, plus the annualized rate,
  in a strip under the KPI tiles, with the Forecast toggle picking between elapsed and projected
  anchors. Its lesson: **an `est.` badge must come from the data, not the toggle that selected
  it** — with the forecast requested and nothing projected, both bases are the measured one. Not
  deployed.
