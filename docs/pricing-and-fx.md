# Ticker mapping, prices and FX

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Ticker mapping & currency

**Ticker mapping** (`market_data_service.py`) — three tiers: custom `ticker_mappings` row → exchange
suffix → try variations (`.DE`, `.F`, `.L`, bare), then auto-save what worked.
`EXCHANGE_SUFFIXES`: `XETRA`/`IBIS2`→`.DE`, `LSEETF`→`.L`, `AEB`→`.AS`, `KRX`→`.KS`, `TWSE`→`.TW`;
`TSE` is Tokyo (`.T`) **except** for CAD listings, where IBKR means Toronto (`.TO`). Add new exchanges
here when a security has no prices, and verify a ticker in a browser first.

**One resolver, two ways in** (issue #32). `yahoo_ticker_for(security, mapping)` is the pure first two
tiers. `_get_yahoo_ticker` reads the one mapping fresh and calls it; every Yahoo loop goes through
this. `yahoo_tickers_for(securities)` reads every active mapping once
(`TickerMappingRepository.active_mappings`, keyed `(symbol, exchange)`) and calls it per security;
it is for readers that resolve the whole security table, today the watchlist index. There is **no
memo inside `_get_yahoo_ticker`**, because the price sync auto-saves a mapping mid-pass and the next
lookup must see it. `tests/test_ticker_resolution_agrees.py` pins the bulk form, the single form and
the watchlist index against each other.

**A wrong auto-discovered mapping is sticky and silent** — the failure that put `SBI@TSE` (Toronto, CAD)
on a US fund for months. The last variation tried is the **bare symbol**, which readily matches an
unrelated US listing of the same ticker; it fetched cleanly, so it was auto-saved — and because tier 1
(`ticker_mappings`) is consulted *first*, that row then shadowed the `.TO` suffix logic even after the
logic was fixed. `_get_currency_from_ticker()` compounded it: with no suffix to read it fell back to
"use the security's currency", stamping USD prices **CAD**, so nothing downstream could see the
mismatch. Result: the position was carried 61% high (7.70 vs 4.78 CAD).

Two guards now: prices carry **the currency Yahoo reports** (read from the history metadata already in
the response — no extra request), and a variation whose currency disagrees with the security's is
**rejected, not adopted, and not saved**. Tests: `tests/test_market_data_service.py`.

**A minor-unit quote defeats that second guard rather than tripping it, so the amount is scaled too.**
Yahoo reports London equities in `GBp` (pence), Johannesburg in `ZAc`, Tel Aviv in `ILA`. The code used
to `.upper()` those into the major code and store the number unchanged — a £5.12 close persisted as
512.4 GBP, **100× high**. And because the normalised code then *matches* the security's own currency,
the disagreement check passes and nothing downstream can see it: the SBI failure with its one safeguard
removed. `MINOR_UNIT_CURRENCIES` now maps each to `(major_code, divisor)`, the label and the amount move
together, and a `TICKER_CURRENCY_OVERRIDES` hit is **never** scaled — an override means someone read the
listing, so the reported code is not evidence.

Explicit map rather than inferring from letter case: `GBp`/`ZAc` are mixed-case but `ILA` is not, so
"not all-uppercase means minor unit" would silently miss Tel Aviv. Extend it like `EXCHANGE_SUFFIXES`.

**One helper decides the scale for every Yahoo amount: `app/services/yahoo_minor_units.py`** (GitHub
issue #35). It owns both maps, `minor_unit_scale(ticker, reported_currency)` (the divisor, or 1 for a
major code, a missing report or an override) and `reported_currency_of(yf_ticker)` (the code from the
cached `_history_metadata` — never the public `history_metadata` property, which re-requests). The
decision used to live in `MarketDataService` and only the price path asked it, so Yahoo's dividends for
the same listing — also quoted in pence — were stored 100× by both dividend readers
([dividends.md](dividends.md)). Its readers: the price path (`_try_fetch_yahoo`), the chart-dividend
capture (the scale rides in `chart_actions`), the dividend pass (`yahoo_dividend_rows(..., scale=)`,
required) and `market_cap`. `tests/test_yahoo_minor_unit_family.py` walks `app/` for any module that
imports yfinance and reads a frame's `Close`/`Dividends` or a ticker's `.dividends`, and requires it to
call `minor_unit_scale`; the benchmark (hand-pinned index currencies; a constant scale cancels in the
simulation), `manage_mappings` (writes no amount; an unscaled level check refuses) and the watchlist
(closes feed only RSI) are exempt with their reasons written there. It also fails on a second copy of
either map or of the metadata read.

Latent today, not live — this account holds no GBP security, and its one London line is `SMH@LSEETF`,
a USD ETF pinned `manual` to `SMH.L` (which is also why bare-ticker auto-discovery never grabbed the
NASDAQ SMH). The trap opens the day a UK equity or a GBP-line ETF is bought.

### Managing mappings — `app/cli/manage_mappings.py`

This table decides where every price comes from and was the last one still edited by hand-written SQL
over ssh, where a typo mis-prices a position and looks like a real price. Use the CLI instead:

```bash
docker exec backend-portfolio-backend-1 python -m app.cli.manage_mappings list
docker exec backend-portfolio-backend-1 python -m app.cli.manage_mappings set 2330 TWSE 2330.TW
docker exec backend-portfolio-backend-1 python -m app.cli.manage_mappings disable SBI TSE --purge-prices
# Another share class of the same fund, for a tranche Yahoo does not quote (docs/pillar3a.md, Prices):
docker exec backend-portfolio-backend-1 python -m app.cli.manage_mappings set CH1529078078 FUND 0P0000S0OE.SW --sibling --notes "NT class of the same fund; NMT tranche held is not quoted"
```

- **`list`** prints the security's currency beside the one its Yahoo ticker implies. A disagreement
  between those columns *is* the SBI bug, so it's flagged explicitly rather than left to be noticed in
  the portfolio total.
- **`set`** stamps `source='manual'` and **refuses** a ticker whose suffix contradicts the security
  (`SBI.L` → GBP under a CAD security). It reuses `_get_currency_from_ticker()`, so the CLI and the
  fetch path can't drift apart. A *bare* ticker implies no currency and so can't be refused — for a
  foreign listing it warns loudly instead, since that's the exact SBI shape. Storing a mapping before
  the security exists is allowed on purpose: that's how you pin one ahead of the statement.
- **`disable`** sets `is_active=False` rather than deleting (`get_mapping()` already filters on it), so
  the row stops being consulted while the record of what was tried survives. **`--purge-prices`** is the
  whole recovery in one step: drop the prices **and the yfinance dividend estimates** the bad mapping
  produced, and let a scheduled job refill them — incremental caching fetches only missing dates, so
  it costs **no ad-hoc Yahoo call**. A sold security's purged span refills too: the 18:00 repair
  probe forgives only a trailing run of a few weekdays at an old close date (issue #33,
  [flex-and-sync.md](flex-and-sync.md), *Sync schedule*), never a whole purged span. IBKR dividend rows are never touched. It purged prices only
  until 2026-07-30, which is how SBI's poisoned estimates outlived its mapping fix.
- `--dry-run` on both mutating commands; every edit records a `sync_runs` row (`manual_mapping`),
  because a mapping change being invisible is why SBI went unnoticed for months.
- **`set` and `disable` end the security's price back-off** (`clear_price_backoff`, 2026-10-17).
  A security every provider came back empty for is skipped for up to 30 days
  ([flex-and-sync.md](flex-and-sync.md), *Sync schedule*); empty answers under the old ticker say
  nothing about the new one, so the next pass asks again.
- **An ambiguous `SYMBOL EXCHANGE` is refused** (2026-10-17). `securities` is unique on
  (isin, exchange), not (symbol, exchange), so an ISIN change can leave two rows on one ticker; a
  mapping applies to both, and `.first()` checked the currency of whichever SQLite returned first
  and let `--purge-prices` delete one row's prices on a guess. `set` and `disable` now refuse,
  naming each id, and `list` flags `AMBIGUOUS: N securities`. The lookup is the one every CLI uses
  (`app/cli/security_lookup.py`; `import_prices` and `purge_dividend_estimates` take `--security-id`
  to settle it), and `tests/test_cli_security_lookup.py` fails a CLI that queries `Security.symbol`
  itself.

Tests: `tests/test_mapping_cli.py`, `tests/test_cli_security_lookup.py`.

**Currency** — Frankfurter at `https://api.frankfurter.dev/v1` (`.app` now 301-redirects here).
Batch-fetches date ranges (one call per ~30 days) and carries the last known rate forward across
weekends/holidays.

**One carry bound, counted from the published rate, on both FX paths** (2026-10-18, issue #20).
`app/services/fx_policy.py` owns `FX_MAX_CARRY_DAYS` (**14**) and the `+carried` tag. A rate may
stand in for a date at most 14 days after the date it was *published* for — on
`CurrencyService.get_exchange_rate` (activity, cash, tax, dividends, the sync), on `fx_preload`
(timeline, benchmark, crypto history) and on the cache-only reader `fx_policy.newest_published_rate`
(the dividend forecast and accruals, market caps). The verdict for a date is a function of the
published rows alone, whichever path asks first:

- *Why one value:* the live path carried 30 days and **persisted** its copy, the preload path
  filled 14 and read those copies, so a date 15–30 days past the newest rate was unpriced on the
  value chart until someone opened the ledger, and priced after — `unpriced_holdings` depended on
  read order. `PortfolioService` and `BenchmarkService` restated 14 as literal defaults besides.
- *Why 14, not 30:* it is the bound the read path always had, and the price path's
  (`PRICE_LOOKBACK_DAYS`), so a holding leaves the chart at the same age whether its price or its
  rate stopped. Every gap the ECB itself leaves is a few days (a weekend, Easter, the Christmas
  closing days), and a held currency's outage warns after three missed publication days
  (`find_stale_exchange_rates`), well before a refusal. Past it a rate stamped onto a persisted
  `cost_basis_eur` or `amount_eur` is a mis-valuation that never self-corrects, while a refused lot
  is skipped with a warning and heals when the providers return.
- *Why from the published rate:* a carried copy is a cached verdict, never an anchor
  (`fx_policy.published_rows()`, used by `fx_policy._get_most_recent_rate`, `_published_span` and
  `preload_eur_rates`). Measured from the newest row of any source, each day's copy restarted the
  clock, so daily carries chained past any bound; and a reader filling from a copy accepted a rate
  up to twice the bound old. A copy already stored for the requested date is re-derived from its
  anchor on every read, without a fetch: refreshed to the anchor's current value (so it no longer
  keeps a stand-in's value after the quote replaced it), or **deleted** once the anchor is past
  the bound — which is how copies stored under the 30-day rule, or chained, age out. That costs
  one indexed query per read of a carried date.
- *Why one cache-only reader* (issue #56): a GET that must never reach a provider cannot call
  `get_exchange_rate`, so it read `_get_most_recent_rate` — the newest published rate of **any**
  age. `DividendService._latest_fx_to_eur` (accrual ingest, forecast) and the forecast's
  IBKR-per-share back-conversion called it bare, so a currency silent for 90 days was refused by
  the ledger and still converted accruals and the forecast; `market_cap` restated the bound beside
  its own call. Now `newest_published_rate(session, from, to, as_of)` applies the one comparison
  (`within_carry_bound`, which `_carry_forward` uses too), and `_get_most_recent_rate` lives in
  `fx_policy`, called only there and by `CurrencyService._carry_forward`. A refused currency is
  absent: its accrual is skipped and counted in the ingest's warning, its forecast amounts are
  unsized, its market cap is shown in the listing's own currency.

Tests: `tests/test_fx_carry_bound_family.py` (read-order invariance at bound−1 and bound+1, no
chaining, no preload fill from a copy, the cache-only readers' verdict equal to the ledger's at
the bound, past it and at 90 days; and AST checks that no other module defines the bound, an FX
function restates it as a literal default, or any module but `fx_policy` and `currency_service`
names `_get_most_recent_rate`).

**Every Frankfurter request is EUR-based, and every pair is derived from it** (2026-10-17, issue
#2). `_fetch_ecb_range` asks `/{start}..{end}?from=EUR&to=A,B,…` — the ECB's own figures — and
stores each pair in the direction readers match exactly: `X -> EUR` as the inverse, `EUR -> CHF`
as quoted, any other pair as `table[to] / table[from]`, rounded to the column's 8 decimals.
`_batch_fetch_rates` is a one-pair call of it, so the warm-up and the on-demand path cannot store
different numbers for one row. It is also more exact than what it replaced: asked with `X` as the
base, v1 rounds to **five decimals**, so KRW->EUR came back as `0.00067` against the ECB's
1/1499.01 = 0.00066711 (JPY `0.00562`, IDR `0.00005`). Rows stored before then keep the rounded
figure — a published row is never rewritten — so a weak currency's series steps once at the
switch-over (KRW ~0.4%, JPY ~0.06%); a backfill that overwrote them would be a separate,
deliberate job.

**The warm-up asks only for what is missing, in one request** (`warm_rates` →
`_refresh_ecb_pairs`). For each ECB pair — every warmed currency to EUR, plus the `EUR -> CHF/USD`
rows the base-currency switch reads (`extra_pairs`) — it takes the newest **published** row (a
fallback row or a carried copy does not count: it is what a fetch must still replace). A pair whose
newest row is the ECB's last publication day (`_last_ecb_publication_day`: weekdays, not before
16:00 UTC on the day itself, not on a TARGET closing day) is current; if every pair
is, **no request is made**. Otherwise one request spans from the oldest stale pair's newest day —
re-asked on purpose, so a reply is never empty while the next day is not out yet and a pair missing
from it still means "nothing for this currency" — to the target date. A pair with no published row,
or whose history does not reach the window's start within `WARM_REACH_SLACK_DAYS` (7), gets the
whole `days_back` window: that is how the crypto book's long backfill still happens. A
failed request sends every warmed currency to the fallback as before (`frankfurter_failed`, which
names a failed extra pair as `EUR->CHF`). Before this, each slot sent one 7- or 30-day request per
ECB currency plus two for CHF/USD, nearly all re-downloading stored rows. Tests:
`tests/test_fx_incremental_warm.py`.

**The TARGET calendar includes Easter** (2026-10-18, issue #40). `is_target_closing_day` adds Good
Friday and Easter Monday, from `easter_sunday` (the anonymous Gregorian computus), to the fixed
dates in `ECB_FIXED_CLOSING_DAYS`; `_last_ecb_publication_day`, and so the warm-up and
`stale_pairs`, read it. Before, Good Friday counted as published from 16:00 UTC through the
weekend and Easter Monday until Tuesday 16:00 UTC, so **every slot in between** — 18 of the 26
stock slots over Easter 2027, not "once on each of the two days" as this file said — asked for a
day that never came. Cost only: the reply re-carried Thursday, nothing failed and no figure moved.

**One code Frankfurter rejects costs only its own pair** (2026-10-18, issue #40). Riding one
request, a 4xx for the batch used to send every pair to the fallback. Now `_fetch_ecb_range`
retries a rejected multi-code request **once per code**; a code rejected on its own is remembered
until the UTC day ends (`_frankfurter_rejected`) and left out of every later request, and only its
pairs come back unserved (`frankfurter_failed`) and go to the fallback. So a drifted code costs one
batch rejection plus one request per code on the day's first slot, and 0 or 1 request at every
slot after — on the warm-up and the on-demand `_batch_fetch_rates` alike. A 5xx, a 408/429 or a
transport error is an outage and fails the whole request with no per-code retry, and an outage
midway through the retries stops them. `frankfurter_requests` in the summary counts what went out.
The memo is in-process, like `benchmark_service._last_upstream_attempt` (one uvicorn worker): a
restart re-learns a rejection once, which bounds the same way with no table to migrate.
**Unverified:** what Frankfurter answers for a `to=` code it does not carry. The tests assume a 404
(what the deleted `_fetch_from_api` assumed); a 200 that omits the code is a per-pair miss and was
already handled. Check once by hand and record the answer here.

**BGN stays in `SUPPORTED_CURRENCIES`** although Bulgaria adopted the euro on 2026-01-01 and the
ECB publishes no BGN rate after 2025: its history before then is an ECB series only Frankfurter can
supply for an old lot (the fallback is latest-only), and for a later date either answer above
costs only BGN's pair. Tests: `tests/test_fx_warm_rejections_and_easter.py`.

**A failed warm-up is recorded at every run and warned about only once it is stale** (2026-10-18,
issue #17). `sync_exchange_rates` keeps `frankfurter_failed` and `fallback_available` from
`warm_rates`, so each job's `details.fx_result` names a currency neither provider served — but one
run's failure is not a warning, because the next slot re-asks and usually heals it. The warning is
`find_stale_exchange_rates`, run inside `sync_exchange_rates` and so collected by all three jobs: a
currency with an **open lot** (`security_repository.held()`), plus the **stored** base currency's
`EUR -> base` pair, whose newest *published* rate (`_published_span`: a fallback or carried row
does not reset the age) has missed `FX_STALE_PUBLICATION_DAYS` (3) ECB publication days, counted
with `_last_ecb_publication_day` so a weekend or TARGET holiday is not an outage
(`CurrencyService.stale_pairs`). A pair Frankfurter cannot serve (TWD) is judged on its fallback
rows, its only publication. Currencies warmed on spec — `WARM_CURRENCIES`, the market-cap quote
currencies — never warn: no position depends on them. Database only, and quiet again by itself
once rates return. Until then a held currency failing both providers was invisible until
carry-forward refused and `reconcile_taxlots` started skipping lots. Tests:
`tests/test_fx_staleness_warning.py`.

**Frankfurter v2 is not used, and is not a drop-in.** Checked 2026-10-17: `/v2/rates` serves daily
**history** for TWD, CNH, AED and many other non-ECB currencies (`/v2/currencies` lists ~160), so
it could replace the latest-only fallback for `WARM_CURRENCIES`. But its default answer blends
providers — KRW on 2026-10-15 was 1504.36 there against the ECB's 1508.48 — so moving the ECB
currencies would need `providers=ecb` pinned, the non-ECB ones a chosen provider, and the reply
shape changes to a flat `[{date, base, quote, rate}]` array. A deliberate change, not a URL swap.

Frankfurter republishes the **ECB reference rates**, so its list is fixed at the ECB's 30 + EUR and will
never include TWD, RUB, QAR or SAR. That is not cosmetic: an unconvertible currency makes
`reconcile_taxlots()` skip the lot, so the holding vanishes from the portfolio *and* the tax report
(counted in `taxlots_skipped`, reported in `warnings[]`). Buying TSMC on TWSE hit exactly this.

So `CurrencyService` falls back to `https://open.er-api.com/v6/latest/EUR`, tagging rows
`source='er-api-latest'`. It is EUR-based (`rate = rates[to] / rates[from]`) and **latest-only — there
is no free historical endpoint**, so it is used only within `FALLBACK_MAX_AGE_DAYS` (7) of today and
refuses older dates rather than backdating a current rate onto an old tax lot. Never raises;
`get_exchange_rate()` owns that decision.

`get_exchange_rate()` resolves in this order, and the order is the design:
**cache → Frankfurter → carry-forward → fallback → carry-forward → raise.**

- Carry-forward comes *before* the fallback because Friday's real ECB rate beats today's
  approximation over a weekend. It copies the newest *published* row within `FX_MAX_CARRY_DAYS`
  and preserves that row's source tag, plus `+carried`.
- **A stand-in gives way to the published quote.** A fallback row or a carried copy fills a date
  before the ECB has published it; when a later Frankfurter range fetch returns that date (the
  warm-up, at every slot, spans from the pair's newest *published* day, so every stand-in after it
  is covered), `_store_published_rate` overwrites the stand-in. Any
  other row — a hand-loaded rate, an earlier quote — is left alone. Until 2026-10-01 nothing was
  ever replaced: a fallback CHF→EUR of 1.05918 stored for 09-28 (ECB: 1.0566) was carried onto
  09-30 and stayed, and a CHF 4,500 withdrawal read CHF 4,517.50. Rows carried before the tag
  existed say `frankfurter`, count as published, and are never replaced. Amounts
  already persisted at a stand-in rate (`cash_flows.amount_eur`, a lot's `cost_basis_eur`) move
  only when a sync re-writes them.
- The fallback now also covers currencies that *are* in `SUPPORTED_CURRENCIES` when Frankfurter
  answers with nothing. That demotes the hardcoded list from load-bearing to advisory: a provider
  outage or the ECB list drifting degrades the rate instead of erasing a position (a raise here makes
  `reconcile_taxlots()` skip the lot, so the holding disappears from the portfolio *and* the tax
  report). It is never reached while Frankfurter is answering — no silent provider switching.

**`WARM_CURRENCIES`** (`TWD, CNH, AED, SAR, QAR, KWD, RUB, CLP, COP`) is warmed daily by
`sync_exchange_rates` alongside the currencies actually held, via `warm_rates()`. Only currencies
Frankfurter *cannot* serve are listed, because those are the ones whose history can only accumulate
forward — an ECB rate is retrievable at any time, so warming it would be waste. The whole set costs
**one** request (`_fetch_fallback_table()` returns all ~166 at once), which is what makes daily
affordable. Without it, TWD only got a rate on days a lot happened to need one, so missing the 7-day
window once meant the lot was skipped forever. Extending the list is one edit and no extra request.

**Market caps** (since 2026-10-17). The Fundamentals tab and the Watchlist show a cap in the base
currency through **one** converter, `app/services/market_cap.market_cap_to_base`. It works through
EUR on the newest cached rate only (`fx_policy.newest_published_rate`) and refuses a stale rate or a
minor-unit quote, in which case the
page shows the listing's own figure with its code. `tests/test_market_cap_family.py` holds both
surfaces to it. The rates it needs come from `sync_exchange_rates`, which also warms the **quoted
currencies** (`quoted_currencies`): every fundamentals row's and watchlist item's `data_currency`,
plus the owner's safety set `MARKET_CAP_CURRENCIES` (JPY, CNY, HKD, KRW, TWD, GBP). They join the
normal pass at every slot: the warm-up costs one request for all ECB currencies and none once they
are current, so the `QUOTED_REFRESH_DAYS` throttle that briefly held them to every third day is
gone.

Tests: `tests/test_currency_fallback.py`, `tests/test_fundamentals_market_cap.py`.

**A base-currency switch the app cannot convert into is refused** (2026-10-17). `BaseFx.convert`
hands back the EUR amount when it holds no EUR→base rate at all, so a switch accepted while
Frankfurter was down printed every figure in the app as EUR under a CHF or USD label. `PUT
/api/settings/base-currency` now validates the code (`normalize_base_currency`), backfills, asks the
read path's own loader (`load_base_fx(..., backfill=False)`) whether it would find a rate, and only
then writes the setting; with none it answers 503 and the base stays where it was (the header says
*Currency change failed — still showing the previous currency*). One stored rate is enough: `BaseFx`
carries the nearest one forward and back, so the worst case is a stale rate, never a mislabelled
figure.

**And a stored base with no rate behind it is not used** (same day). The one way left into the
mislabel was a database whose base was chosen before the refusal existed, or whose rates are gone.
`AppSettingsRepository.get_base_currency()` — which every reader asks: the portfolio, look-through,
fundamentals and crypto services and `/api/settings` — now answers the stored base only while an
EUR→base rate exists, and EUR otherwise; `get_stored_base_currency()` keeps the choice, and
`/api/settings` serves it as `base_currency_unavailable` while it cannot be used, which the header
turns into a one-line warning. So `BaseFx.convert`'s EUR fallback can no longer meet a non-EUR
label: a base is in use only with a rate to convert by. The scheduler refreshes EUR→CHF and EUR→USD
daily, so on a running install the check passes and costs one indexed lookup per request.
Tests: `tests/test_base_currency_switch.py`.

---
