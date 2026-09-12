# IBKR Flex, ingest, the scheduler and the API's protections

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## ⚠️ Two rules that must never be broken

### 1. Never call Yahoo Finance without explicit user permission
`yfinance` powers market prices, dividend estimates, fundamentals and benchmarks. Yahoo rate-limits
hard (~500-2,000 requests/hour, ~10-20 in a burst, IP-based). A full market-data sync is **50-150+
requests**. Symptoms of a limit: HTTP 429, HTTP 404 with `Expecting value: line 1 column 1 (char 0)`,
empty JSON, timeouts. Recovery: **stop immediately, wait 30-60 min.**

Protections in `market_data_service.py`: random 1-3s delay per request, 2-4s between securities, Chrome
User-Agent, rate-limit detection that aborts the run, and incremental caching (only missing dates).
`yfinance` must stay **>= 1.1.0**.

**Every service that loops against Yahoo must abandon the pass on a 429, and until
2026-08-05 only `market_data_service` did.** Six services import `yfinance`;
`fundamentals` (~5 endpoints per security, the most expensive), `analyst ratings`,
`watchlist`, `allocation`, `dividends` and the scheduler's benchmark warm-up all caught
the error, logged it, and asked the next one seconds later — the exact shape fixed for
market data, five times over. The predicate now lives once in
`app/services/yahoo_rate_limit.py` and `tests/test_yahoo_rate_limit_family.py` walks the
AST for any module importing `yfinance` without consulting it, so a seventh service is
caught without anyone remembering.

Two details are load-bearing. **The marker list stays narrow** (`429`, `too many
requests`, `rate limit`, `blocked`) and deliberately excludes the `Expecting value: line
1 column 1` and 404 symptoms named above: `_try_fetch_yahoo` uses the same verdict to
decide whether to keep trying ticker *variations*, so matching those would abort
auto-discovery for every ticker Yahoo simply does not know. And **allocation checks
before it stamps** — its failure path writes `allocation_last_updated` to bound retries
against securities with no `.info`, which is right for a real "no data" answer and badly
wrong for a rate limit, where it would mark every remaining security attempted and
suppress its sector and country for the whole staleness window.


The IBKR Flex sync (`POST /api/sync/ibkr`) is Flex-only and touches **no** Yahoo — it's always safe.

**Yahoo is not the only price provider, which this rule used to imply.** `market_data_service.py`
falls back to **Alpha Vantage** when Yahoo returns nothing for a security on NASDAQ/NYSE/ARCA/AMEX,
and `ALPHA_VANTAGE_API_KEY` is set in `backend/.env`, so the path is live rather than dormant. It
does not widen the rule — it only ever runs *inside* a market-data sync that already needs
permission, and never on the Flex path — but "no Yahoo" is not the same as "no network", and a
reader checking only for `yfinance` will miss it. Two things about it are load-bearing:

- **Its rows are tagged `source='alpha_vantage'`.** The caller hardcoded `'yahoo_finance'` for every
  row until 2026-08-01, so fallback prices claimed a provenance they did not have — in the column
  every pricing diagnosis reads first.
- **It refuses a non-USD security.** The endpoint quotes US listings in USD and its response carries
  no currency to read back, so unlike Yahoo there is nothing to verify a label against. Stamping the
  security's own currency onto a USD quote is exactly how SBI was carried 61% high; that path was
  fixed for Yahoo in July and not here. Don't "restore" the fallback for non-USD listings.

### 2. Never loop the IBKR Flex sync
IBKR allows **1 request/second and 10 requests/minute per token**, and one `ibflex.client.download()`
is *several* HTTP requests (request statement, then poll until ready, each with 3 internal tries). An
eager retry loop blows the cap and triggers **`Code=1025: Too many failed attempts`** — an undocumented
token lockout lasting **hours** (observed ~14h) that blocks all syncing. This has happened **three times,
every one self-inflicted** — twice by looping `download()`, once by re-requesting after a `1001` (below).

Retrying *during* a lockout can extend it. When locked: do nothing and let the schedule recover it.

Budgets in `ibkr_service.py`: `_FLEX_RETRY_DELAYS = [30]` (2 attempts, interactive path) and
`FLEX_RETRY_DELAYS_PATIENT = [120, 300, 600]` (scheduled jobs). `1025` fails fast with guidance; `1018`
always backs off >= 60s. Pinned by `tests/test_flex_retry_policy.py`.

**`1001` means the opposite thing at each step, and getting that wrong caused a third lockout
(2026-07-26).** While *retrieving*, it means "not ready" — keep polling the same reference. From
*SendRequest*, it means IBKR tried to generate and failed, which is exactly what `1025` counts:

```
Code=1001 ...; attempt 1/4, re-requesting in 120s   ->   Code=1025 Too many failed attempts
```

So `1001` is in `_RETRIEVE_PENDING_CODES` but deliberately **not** in `_REQUEST_RETRYABLE_CODES` — it
fails fast and the next scheduled job asks for a fresh statement. Giving up costs hours of freshness;
re-requesting costs a lockout of every sync. The codes still retryable at the
request step (`1009`/`1018`/`1019`/`1021`) all mean "throttled or busy, no generation job was created".

Genuine transport errors (`requests.RequestException`: DNS, reset, timeout) **are** retried — they never
reached IBKR, so they cost nothing against the token. Mid-poll they re-retrieve the same reference; before
a reference exists they re-issue SendRequest. A DNS blip used to kill a whole job.

### The two-step rule (why we don't use `client.download()`)

Flex is a two-step protocol: **`SendRequest`** asks IBKR to generate the statement and returns a
`ReferenceCode`; **`GetStatement`** retrieves it, answering "try again shortly" until it's ready. IBKR's
docs are explicit:

> "If statements are still being generated when you submit your request to retrieve them, you should
> **not re-initiate the Flex request**, but instead keep trying to **retrieve** the statement."

`ibflex.client.download()` polls correctly for `1009`/`1019`/`1018` but **not `1001`** — it raises, so any
outer retry calls `download()` again and starts a *brand-new generation job*. Do that a few times and
IBKR blocks the token: **`1025` counts failed *generations*, not request volume** (volume is `1018`,
which we never saw). The first two lockouts came from this; the third came from re-requesting on `1001`,
the same mistake one layer up.

So `IBKRService._download_statement()` drives the two steps itself using ibflex's public pieces
(`request_statement`, `submit_request`, `check_statement_response`, `STMT_URL`): **SendRequest once**,
then poll the *same* `ReferenceCode` for every `_RETRIEVE_PENDING_CODES` hit, bounded by a deadline
(120s interactive / 900s scheduled) rather than an attempt count. The outer loop only ever re-issues
SendRequest, and only for failures raised *before* a reference code exists — the one case where
re-initiating is unavoidable.

**Flex error codes:** `1001` statement not ready (transient, expected — poll, don't re-request),
`1003` not available (terminal), `1018` rate limit (1/sec, 10/min per token), `1019`/`1021` transient,
`1025` **undocumented** token lockout from repeated failures (fatal, never retry), `1012` token expired,
`1013` IP restriction, `1015` bad token. The official table stops at 1021 — 1025 appears nowhere in it.

---

## IBKR Flex Query integration

### The Flex Query (`App_OpenLots`, ID 1389408)

Required sections and the fields the parsers actually read:

| Section | Options | Key fields |
|---|---|---|
| **Open Positions** | **Lot** | `conid`, `symbol`, `isin`, `description`, `currency`, `listingExchange`, `position`, `costBasisPrice`, `costBasisMoney`, `openDateTime`, `reportDate` |
| **Trades** | **Execution** | `conid`, `symbol`, `tradeDate`, `buySell`, `quantity`, `tradePrice`, `proceeds`, `ibCommission`, `currency`, **`fifoPnlRealized`** (= "Realized P/L"), `transactionID` |
| **Cash Transactions** | Dividends, Payment in Lieu, **Withholding Tax**, **Deposits & Withdrawals** | `type`, `conid`, `symbol`, `settleDate`/`dateTime`, `amount`, `currency`, `transactionID` |
| **Corporate Actions** | Detail | `type`, `conid`, `symbol`, `dateTime`/`reportDate`, `quantity`, `value`, `proceeds`, `actionDescription`, `transactionID` |
| **Transfers** | — | `type`, `direction`, `date`/`reportDate`, `cashTransfer`, `positionAmount`, `symbol`, `conid`, `company`, `transactionID` |
| **Cash Report** *(optional)* | **Base Currency Summary** | `currency`, `toDate`, `endingCash`, `levelOfDetail` |
| **Equity Summary in Base** *(optional)* | — | `reportDate`, `cash`, `stock`, `total` |

**The two cash sections are the only optional ones**, and the only ones whose absence is a supported
steady state rather than a gap: without either, `CashService` derives the balance from the trade,
deposit and dividend ledgers and every surface badges it `derived`. Enabling one lets IBKR's own
figure take over, which is the only way the app can see broker interest, account fees and FX spread.

**They are different sections and not interchangeable.** `Cash Report` is per **currency** over the
whole statement period, so it yields one anchor dated `toDate` per sync; `Equity Summary in Base` is
per **day** in the account's base. Either works — `resolve_cash_balances` collapses both onto one
row-per-date shape and the daily series wins where both cover a date. Cash Report is the one to
reach for first: it appears in every Flex Query editor, and **Base Currency Summary** is the option
to tick, because a Currency Breakout has to be converted and summed and this account holds five
currencies. Ticking both options emits both shapes, and the summary row is preferred so the breakout
is not added to a total that already contains it. See *Cash*.

**Deposits & Withdrawals** feeds the contributions report; without it there is no record of external
money at all. **Transfers** exists only so an incoming broker transfer can be told apart from a deposit
— see the contributions section. Both are inert until parsed: `extract_cash_transactions` filters to the
three dividend types, so ticking them early cannot disturb anything.

**General config that matters:** Format **XML**; Period **Last N Calendar Days** (N is measured, not
assumed — see below);
Date `yyyyMMdd`, Time `HHmmss`, separator `;`. **Never use `dd/MM/yyyy`** — ibflex assumes US
`MM/dd/yyyy` for ambiguous formats and would silently swap month and day.

**The period was Year to Date until 2026-07-31 and must not go back** — that is what caused the
`Code=1001` failures (see *Sync schedule*). Trades/CashTransactions contain only rows *inside* the
period, so the window has to exceed the longest plausible run of failed syncs.

**The live period is `Last 30 Calendar Days`, and no constant in this repo is allowed to assert
that any more.** An earlier revision of this file recorded it as narrowed to N=3 on 2026-08-06,
"a deliberate choice by the account owner, reaffirmed after the trade-off was put to them" — and
on 2026-08-24 every measurement said 30: the portal download's own header
(`period="Last30CalendarDays"`), and `data_from`/`data_to` on three successful API syncs spanning
07-01…07-30, 07-16…08-14 and 07-23…08-21. Whether the portal edit was never applied or was set
back, the file was wrong and the code derived from it was wrong with it. **The period is a portal
setting; treat any number written down here as hearsay and read `data_to − data_from` off a
successful sync instead.**

That is now what the code does: `flex_generation.flex_window_days()` measures the span off the
last statement IBKR served, and `find_flex_generation_gap` warns at **N−1 ET days** rather than at
a hardcoded 2. Know what the window costs, whatever N turns out to be:

- A statement generated on day *D* covers **D−N … D−1**. So a trade on day *T* is reachable from a
  statement generated on *T+1 … T+N*, and **unreachable from T+N+1 onward**.
- The account gets about **one successful IBKR sync per day** (see the once-per-day rule under
  *Sync schedule*), so the margin is roughly **N consecutive failed days**. At N=30 that is weeks;
  at N=3 it was two, and two-day gaps have happened (08-02 and 08-03 both failed at the day's first
  attempt and were recovered by the second; 08-23 and 08-24 both failed outright).
- Only the `<Trades>` / `<CashTransactions>` rows are at risk. **OpenPositions is
  period-independent**, so the lots and holdings still arrive whatever the window — what would be
  lost is the execution record the Activity ledger, XIRR's flow terms and realized P&L read.
- Recovery is a browser download with a wider period ingested through `app/cli/ingest_flex_xml.py`,
  which is idempotent. That is the reason a short window is survivable at all.
- **Which alarm fires first depends on N, and both orders are correct.** At N=3,
  `find_flex_generation_gap` (N−1 = 2 ET days) leads and `find_stale_ibkr_sync` (7 days) is far too
  slow — the data is gone four days before it speaks. At N=30 the 7-day one leads, because an
  operational fault deserves attention weeks before the trades are actually at risk. They answer
  different questions: *the schedule is broken* versus *the trades are about to become
  unrecoverable*.

**Also note the window ends at the last completed *trading* day, not simply "yesterday".** A
statement generated Monday 2026-08-24 at 12:32 ET came back `toDate=20260821` — Friday, not
Sunday. Tuesday-to-Saturday generations do reach the previous day; a Monday one reaches back to
Friday. So a Monday purchase is first reachable on Tuesday, and "I bought today and it is not
there" is the expected answer on any day, not a fault.

Prior tax years would need a one-off period change, then setting back — but **for this account
there is nothing to reach**: the owner did not trade at IBKR before 2026. The holdings arrived by
in-kind transfer from Trading 212, Scalable Capital and Trade Republic in early 2026, so IBKR has no
2025 trades, dividends or cash transactions to generate whatever the period says. Widening the window
for a prior year is therefore only ever useful for a *later* account, not this one — see *Prior years
are permanently estimates here*. Ingestion is idempotent (upserts keyed on `ib_key`), so re-syncing is
safe — which is also the recovery if a bounded window ever *does* miss something: download a wider
statement from Client Portal and ingest it offline.

**The rolling window ends *yesterday*, so today's activity cannot be ingested today.** "Last N
Calendar Days" means the N days ending on the last *completed* statement day, not the N up to now.
So a same-day buy is absent from every section at once: no trade row, no lot, and no deposit for
the cash that funded it. This is the expected answer to "I bought today, update the data" —
**nothing needs forcing and nothing is broken**.

**And "yesterday" is measured in US Eastern, which decides whether a download is worth taking.**
`whenGenerated` is stamped in ET, not Berlin and not UTC — `20260806;122722` on a file downloaded
at 18:27 Berlin, `20260806;234042` on one downloaded at 05:40 Berlin the next morning. Both came
back `to=20260805`. The window rolls at **midnight ET**, so the honest rule is:

| generated | Berlin | reaches |
|---|---|---|
| 12:27 ET 08-06 | 18:27 08-06 | through 08-05 |
| 23:40 ET 08-06 | **05:40 08-07** | still only 08-05 |
| 00:00 ET 08-07 | 06:00 08-07 | **08-06** |

That middle row is the trap and it cost a morning on 2026-08-07: a statement downloaded at 05:40
Berlin *looks* like today's and is still yesterday's, because 05:40 Berlin is 23:40 the previous
day in New York. Twenty minutes later the 06:00 Berlin `ibkr_only_sync_job` — 00:00 ET — got the
missing day without anyone doing anything. **Check `toDate` in the file header before ingesting
a manual download**; the generation time tells you nothing on its own. Only a **custom date range**
ending today can reach the current day, which is a portal edit that has to be set back afterwards —
worth it for a prior-year backfill, not for waiting one night.

### Offline ingest — the escape hatch from a locked token

The Flex **Web Service** and the **download button** in Client Portal serve the same statement over
independent channels. Only the API path spends the token's request budget, so only it can trip `1025`.
So a statement saved from the browser can be ingested *during* a lockout — and since the browser download
uses whatever period you set, it is also the practical way to reach a prior tax year:

```bash
docker cp stmt.xml backend-portfolio-backend-1:/tmp/stmt.xml
docker exec backend-portfolio-backend-1 python -m app.cli.ingest_flex_xml /tmp/stmt.xml --dry-run
docker exec backend-portfolio-backend-1 python -m app.cli.ingest_flex_xml /tmp/stmt.xml
```

`app/cli/ingest_flex_xml.py` reuses `IBKRService.parse_flex_xml()` and
`sync_helper.ingest_flex_statement()` — the *same* functions `POST /api/sync/ibkr` and the scheduled jobs
use — so reconciliation order, the empty-statement wipe guard and the idempotent upserts all apply
identically. It records a `sync_runs` row with `sync_type='ibkr_manual_xml'`. Touches no network at all
(no Flex, no Yahoo). `--dry-run` reports counts without writing. Tests: `tests/test_manual_xml_ingest.py`.

There is deliberately **no upload endpoint**: `/api/` is proxied publicly, and a route that rewrites tax
lots is a far larger surface than a CLI run over ssh — write auth (below) narrows that surface but does
not change the judgement.

### Offline price import — the escape hatch from a wrong or missing feed

`app/cli/import_prices.py` is the price-side twin, for when Yahoo can't be called (rule 1) or can't
resolve a listing at all. It takes a JSON file of daily closes and writes them through the same
`MarketPriceRepository.bulk_create()` upsert the sync uses, so it is re-runnable and a later Yahoo
fetch overwrites cleanly. Records `sync_type='manual_prices'`. Touches no network.

```bash
docker cp prices.json backend-portfolio-backend-1:/tmp/prices.json
docker exec backend-portfolio-backend-1 python -m app.cli.import_prices /tmp/prices.json --dry-run
docker exec backend-portfolio-backend-1 python -m app.cli.import_prices /tmp/prices.json
```

```json
{"symbol": "SBI", "exchange": "TSE", "currency": "CAD", "source": "ibkr",
 "prices": [{"date": "2026-07-27", "close": 4.79}]}
```

**IBKR's Client Portal is the good source for this** (`get_price_history` via the MCP connector):
independent of Flex, so it spends no token budget and can't trip `1025`, and it quotes the listing's
own currency. That's how SBI was refilled — 2 years of daily CAD bars, `source='ibkr'`.

A **currency mismatch against the security refuses the whole file**, mirroring the guard on Yahoo
auto-discovery, because writing USD closes under a CAD security is the exact bug this CLI repairs.
A malformed row also rejects the whole file: a partially-applied series is indistinguishable
afterwards from a complete one. Ambiguous `symbol` (ASML is two rows) refuses rather than guessing —
pass `--security-id`. Tests: `tests/test_price_import_cli.py`.

Note the trade-off: once a date has a price, `get_missing_dates()` never re-fetches it, so an
imported window stays imported (visible in `market_prices.source`) until something deletes it.

### `_sanitize_flex_xml()` — why it exists

ibflex 0.15 (released 2021) converts **every** XML attribute onto a frozen dataclass and raises
`FlexParserError` on the first thing it can't handle, which **aborts the entire document** — so one
unrecognised field kills the whole sync, open positions included. IBKR has drifted well past it:

- **Unknown attribute names**, e.g. `subCategory` on `<Trade>` (modelled only on `SecurityInfo`).
- **Unknown enum values**, e.g. `type="Broker Fees"` — the query can enable 17 cash-transaction types
  but `enums.CashAction` models 10. Also `CorporateAction.type` (`Reorg`) and `notes`/`code` (`Code`).

`IBKRService._sanitize_flex_xml()` runs before `parser.parse()` and:
1. drops **any attribute ibflex's own `parser.parse_element_attr()` rejects** — that single call covers
   unknown names, bad enum values, unparseable dates/decimals and unknown currencies, and can't drift
   out of step with ibflex;
2. drops aggregate duplicate rows (`levelOfDetail` in ORDER / SYMBOL_SUMMARY / CLOSED_LOT / …) when
   real execution rows sit beside them, since IBKR gives each its own `transactionID` and ingesting
   both would **double-count trades and realized P&L** — but keeps them if they're all there is, so a
   populated section is never emptied.

It returns the original bytes untouched when nothing changed, never raises, and reports drops that
**can change ingested data** via `warnings[]` (surfaced in the sync response). **Don't patch attribute
names one by one** — it's generic.

**Only material drops are warnings, and that distinction is the point.** ibflex 0.15 cannot model most
of what IBKR now sends, so every statement drops ~27 attributes — `figi`, `serialNumber`, `weight`,
`subCategory`, `commodityType`, `Trade.notes`… — and until 2026-08-05 all of them went into
`warnings[]` as a single unreadable line, on every sync, for ever. A warning that is always present
and never actionable is worse than no warning: it is exactly what teaches the reader to skip the
banner that also carries a skipped tax lot or an unconvertible dividend, which is the whole reason
`warnings[]` exists.

So `INGESTED_ATTRS` maps each element to the attributes the extractors actually read, and a drop is
loud only when it hits one. Everything else lands on `IBKRService.last_schema_notes` → `flex_notes` →
the sync run's `flex_schema_notes` in `details`, where "did a *new* kind of thing start being
dropped?" stays answerable without a permanent banner.

The two failure directions are not symmetric, which decides how it is guarded. Listing a field we
don't read merely re-creates the noise; **omitting one we do read makes a real problem silent** — an
unparseable value on `CashTransaction.type` means `extract_cash_transactions` skips the row and a
dividend never arrives. So `tests/test_flex_attr_coverage.py` doesn't trust the map: it AST-walks each
extractor for attribute access, intersects with ibflex's own `__dataclass_fields__` (so `.append` and
local names can't masquerade as schema), and fails if the map has drifted behind. It caught a real
omission on its first run — `extract_transfers` reads `Transfer.date`, which a hand-written pass had
discarded as a Python builtin.

A latch (`last_schema_notes`) rather than a third return value only because eighteen call sites
unpack a 2-tuple; same shape as `MarketDataService.rate_limited`, and declared in `__init__` so a
service built via `__new__` in a test still has it.
`_fix_currency_codes()` still runs first so `RUS`→`RUB` is repaired rather than dropped.

Degradation is graceful: an unknown cash `type` becomes `None` and the row is skipped by
`extract_cash_transactions` (which only wants dividends/withholding); an unknown reorg `type` lands as
`'UNKNOWN'` with quantity and date intact.

Tests: `tests/test_flex_xml_sanitizer.py`, `tests/test_flex_ingestion_e2e.py`.

---

## Sync schedule (Europe/Berlin)

| Time | ET | Job | Touches Yahoo? |
|---|---|---|---|
| 08:00, 11:00, 13:00, 15:00, 20:00, 22:00 | | `market_data_only_sync_job` (7d) | yes |
| **18:00** | **12:00** | `full_sync_job` — IBKR + FX + 730d market data + dividends + look-through upkeep (issuer sites, OpenFIGI, GLEIF) | **yes** |
| 00:00 | 18:00 | `ibkr_only_sync_job` — IBKR + FX, **skips unless 18:00 failed** | no |

**Yahoo is repriced at seven hours — 8, 11, 13, 15, 18, 20, 22 — and that set has not
changed** since the 2026-08-04 widening. What moved on 2026-08-08 is only *which job* makes the
08:00 and 18:00 touches: the 730-day pass travelled with IBKR to 18:00, and 08:00 became a plain
7-day slot. Don't read the reshuffle as a change in pricing coverage.

**Only one IBKR slot can succeed per ET day, so the second one now asks first.**
`app/services/flex_generation.py` holds the rule: `last_generation_today()` reports whether a
successful Flex sync already landed in the current **US-Eastern** calendar day, and
`SchedulerService.sync_ibkr_data(force=False)` returns `status="skipped"`,
`reason="already_generated_today"` without touching the network when it has. So 00:00 Berlin is a
no-op on a normal day and a real recovery attempt on a day 18:00 failed — the case that saved the
data on 2026-08-02 and 08-03, when the day's first attempt failed and a later one succeeded.

Three details are load-bearing:

- **`ibkr_manual_xml` does not count as a generation**, and that is measured rather than assumed:
  the browser download and the Web Service are independent channels, so an offline ingest spends
  nothing. Twice in this account's history an offline ingest was followed by a *successful* API
  generation in the same ET day (07-28: 00:16 ET then 02:05 ET; 07-31: 00:07 ET then 02:07 ET).
  Counting it would suppress the day's real attempt on exactly the days someone had just recovered
  by hand. `FLEX_API_SYNC_TYPES` excludes it; `IBKR_SYNC_TYPES` — which answers the *different*
  question "was the data refreshed?" — includes it, and is defined as an extension of it so the two
  cannot drift into separate literals.
- **A failure does not spend the day either.** The guard keys on a *successful* run, so a day full
  of `1001`s leaves every later slot free to try.
- **The job's status becomes `skipped`, not `success`**, so `find_stale_ibkr_sync` (which counts
  successes) is untouched by it. `trigger_sync_now` had to be narrowed at the same time: it raised
  `SyncBusy` → 429 on any `skipped` status, which had only ever meant a pipeline collision, so it
  now keys on `reason == "pipeline_busy"`. A day with nothing left to sync is finished, not busy.

**The hours live in one place** — `IBKR_ONLY_HOURS` / `FULL_SYNC_HOUR` / `MARKET_DATA_HOURS`, and
`ALL_SYNC_HOURS` over them — because three other files carry a copy: `ops/auto-deploy.sh` defers a
deploy that would land in a slot, and both `ops/finish-deploy.*` twins warn a human before pushing.
All three had drifted by 2026-08-04, the finish-deploy pair for four days, in the direction that
*permits* a collision: they warned about the retired 13:00/20:00 and never mentioned the live
00:00/06:00. `tests/test_deploy_guard_hours.py` reads all three against `ALL_SYNC_HOURS` and
`test_the_registered_slots_are_exactly_the_declared_ones` checks the triggers come from it, so the
chain is closed end to end. Whole hours only — the guards reason in hours, so a half-hour slot
could not be expressed on their side and would run unprotected.

The IBKR-only job exists because a transient `Code=1001` at 08:00 used to cost a full day of
freshness. It deliberately **skips** market data and yfinance dividends — see rule 1. Pinned by
`tests/test_scheduler_jobs.py`. Status: `GET /api/scheduler/status`.

**Market data was repriced three times a day until 2026-08-04 and now runs seven times, five of
them mid-session.** Before, a value read mid-morning could be seven hours old, and Xetra's *close*
was never captured at all: the job named "after EU close" ran at 15:00, 2.5 hours before the 17:30
close. The worst gap inside either session is now ~2.5h. **This is only correct because a recent
close is re-fetched** (`PROVISIONAL_PRICE_DAYS`, below) — without it an earlier slot freezes an
*earlier* price and makes the number worse rather than fresher, so the two changes cannot be
separated. Coverage is pinned as a property (`≤3h between slots, plus one after each close`) rather
than as a list of hours, so re-timing a slot stays free while dropping back to two a day does not.

Cost, since rule 1 makes this the question: **one Yahoo request per security per pass either way** —
the refresh only widens a range on a request already being made. 40 securities × 7 passes ≈ 280
requests/day at ≤48 in any hour, against a documented ~500–2,000/hour, and the ~7.5s per-security
pacing keeps a pass at ~8 requests/minute, under the ~10–20 burst tolerance. Slots are ≥2h apart, so
two passes never share an hour. The scheduled *benchmark* warm-up deliberately does **not** refresh —
it loops all eight warm benchmarks 1-2s apart, so doing it seven times a day would multiply that
burst for a value nobody read; the chart's own lazy fetch refreshes the one being viewed.

**A rate limit now abandons the rest of the pass.** This file already credited
`market_data_service.py` with "rate-limit detection that aborts the run" and it only ever aborted the
ticker *variations* for the security in hand — the caller logged a failure and moved on to the next of
40, asking the same IP again seconds later. `MarketDataService.rate_limited` latches on the first 429
and `sync_market_data` breaks, reporting `rate_limited: true` plus a `warnings[]` line. What was
already written stays written and the next slot resumes, since the dates it never reached are simply
still missing. That mattered little at three passes a day.

**Every scheduled Yahoo step reports the abandonment, and every step result reaches
`_collect_warnings`.** Found 2026-09-12: the dividend fetch and the benchmark warm-up both latched
the flag and broke correctly, then returned dicts with no `rate_limited` and no `warnings` — and
neither `div_result` nor `bench_result` was passed to `_collect_warnings`, so a pass Yahoo killed at
5 of 40 was recorded as `status: success` and was byte-identical in the API to a complete one. The
dividend step also reported `securities_processed` as "everything not skipped" rather than what it
asked about. Both now carry `rate_limited`, the standard "do not retry manually" warning, and (for
the warm-up) `benchmarks_total` beside `benchmarks_synced`; `sync_dividends()` hoists its two
children's warnings, which is also how `compute_dividend_income`'s FX-skipped line first became
visible. Rule: a step that can stop early says so on its own result, and a job passes *every* step
result to `_collect_warnings` — the silent shape is the default one, since a dict without the key
looks finished. Tests: `tests/test_dividend_sync_rate_limit.py`, the "every Yahoo step reports" block
in `tests/test_scheduler_jobs.py`.

Related, same day: the Alpha Vantage fallback in `fetch_and_cache_prices` fired on `not prices_data`,
and a 429 comes back from the Yahoo fetcher as the same empty list as "Yahoo has never heard of this
ticker" — so a rate limit spent one of the free tier's few daily calls and rewrote that security's
`source` column. It now also requires `not self.rate_limited`.

**Every IBKR attempt must sit outside US market hours, and this is measured rather than assumed.**
IBKR builds a Year-to-Date statement from *finalised* daily data, so `SendRequest` succeeds overnight
and fails mid-session — the failure surfaces as `Code=1001` **at the request step**, which is the
fatal-fast kind, not the "keep polling" kind. This account's own `sync_runs`, read on 2026-07-31:

| Berlin | ET | ok/total | |
|---|---|---|---|
| 00:00 | 18:00 | 1/1 | after the US close |
| 06:00 | 00:00 | 2/2 | |
| 08:00 | 02:00 | 4/5 | |
| 09:00 | 03:00 | 1/1 | |
| 13:00 | 07:00 | **0/6** | pre-market |
| 20:00 | 14:00 | **1/8** | mid-session |

Overnight 8/9; afternoon and evening 1/15. The retries used to sit at **13:00 and 20:00**, where they
were not merely weak but **negative**: every failure is a failed *generation*, and failed generations
are exactly what `Code=1025` counts. Two jobs whose purpose was protecting freshness were spending
lockout budget twice a day to recover nothing. They moved to 00:00 and 06:00 on 2026-07-31.

That argued for keeping every IBKR slot inside roughly **22:00–09:00 Berlin**, and it held until
2026-08-08, when the account owner moved the primary slot to 18:00 Berlin (12:00 ET) — see
*The 18:00 Berlin slot* below. `test_ibkr_jobs_run_at_the_declared_hours` now checks an explicit
allowlist rather than that range, so an hour still cannot drift unnoticed; it simply records a
deliberate exception instead of a rule the schedule no longer follows.

**But the hour is the weaker constraint. IBKR generates this statement about once per ET
calendar day, and every attempt after the day's success is refused with `Code=1001`.** Read
off `sync_runs` on 2026-08-08, with all three slots sitting inside the safe overnight window:

| ET day | 00:00 ET (06:00 Berlin) | 02:00 ET (08:00 Berlin) | 12–13 ET (manual) | 18:00 ET (00:00 Berlin) |
|---|---|---|---|---|
| 08-01 | **success** | error | | error |
| 08-02 | error | **success** | error | error |
| 08-03 | error | **success** | error | error |
| 08-04 | **success** | error | | error |
| 08-05 | **success** | error | | error |
| 08-06 | **success** | error | error | error |
| 08-07 | **success** | error | | error |
| 08-08 | **success** | error | | |

Exactly one success per day, twelve days of twelve, always the earliest attempt that works and
everything after it refused. The one two-success ET day in the whole history is 07-31 — the
day the query definition was edited, which appears to reset it, and the reason both the guard
and the manual endpoint keep a `force` escape hatch.

Note the manual column: those are Sync-button presses, and they are why this read as "the Flex
query always errors out" from the UI. **The code enforces the rule now** — see the guard under
*Sync schedule* above.

**This subsumes the mid-session reading rather than contradicting it, and that is the part
worth understanding.** The 07-31 table above is equally well explained by "the later slots had
already spent the day's generation": 08:00 went 4/5 while 13:00 and 20:00 — both *after* it —
went 0/6 and 1/8. Two theories, one dataset, because the mid-session slots were also the
later ones. What discriminates is 08-01 onward, where every slot is overnight and only one
still succeeds. So the hour rule stays as evidence (a midday slot is bad for both reasons), but
**adding IBKR slots does not add freshness** — it adds failed generations, which is exactly what
`Code=1025` counts. The account has two slots and can use one.

The concrete consequence used to be that **the earliest job starves the later ones**, which is
why `full_sync`'s market-data half must not be gated on its IBKR half. Since the guard, the later
ones skip instead of failing, but the gating rule stands for its own reasons.

### The 18:00 Berlin slot — chosen against the evidence, and since vindicated by it

The primary IBKR slot moved from 06:00 to **18:00 Berlin (12:00 ET)** on 2026-08-08 at the account
owner's request, reaffirmed after the trade-off was put to them twice. It was recorded here as a
decision made against the measurements, so that a run of `1001`s would not read as a drift.

**Nine ET days later it is working, and the "mid-session is fatal" reading does not survive them.**
Read off `sync_runs` on 2026-08-17:

| ET day | 12:00 ET (18:00 Berlin) | 18:00 ET (00:00 Berlin) |
|---|---|---|
| 08-09 … 08-15 | **success, seven for seven** | skipped — the day's generation was already spent |
| 08-16 | error | **success** — the second slot recovered it |
| 08-17 | error | pending at the time of writing |

So 12:00 ET is **7 of 9**, against the 1-of-8 the retired 20:00 Berlin slot managed and the 0-of-6 of
13:00 Berlin. Those two were never simply "mid-session": they also ran *after* an earlier slot had
already taken the day's generation, which is the confound named above. With one primary slot in that
band the band turns out to be fine. **Do not "restore" 06:00 Berlin on the strength of the old
table** — and equally, do not read this as licence to add slots, because the once-per-day rule is
unchanged and every extra attempt is a failed generation that `Code=1025` counts.

Two properties of the choice are unchanged and still worth knowing:

- **It captures no additional trades.** The window ends yesterday *measured in US Eastern* and rolls
  at midnight ET, not at generation time — so 12:00 ET and 00:00 ET on the same day both cover
  D−N…D−1. Identical statement, ~12 hours later. Reaching *today's* trades needs a custom date range
  set by hand in the portal, which is not a schedule change.
- **There are two attempts per ET day, not three.** Against a 3-day window that is a margin of two
  consecutive failed days; against the 30-day period actually in force it is weeks. 08-16 spent one
  of those attempts and recovered on the second slot, which is the design working rather than a
  warning.

What makes it survivable is that the guard reduces the cost of a doomed attempt to zero, and that
`find_flex_generation_gap` (below) alarms in time to act. **If both slots start failing on the same
ET day repeatedly, moving the primary back to 06:00 Berlin — the instant the window rolls — is still
the fix.** A single failed 12:00 ET is not that signal; it is Sunday's shape too.

**`find_flex_generation_gap` warns after **N−1** ET days with no successful IBKR sync, where N is
the window `flex_window_days()` measures off the last statement IBKR served.** It exists because
`find_stale_ibkr_sync` at 7 days *cannot see the failure it was written for* under a narrow period:
at N=3 trades fall out after two missed days, four days before the 7-day alarm says a word, and
warning at 7 about a 3-day window is warning after the loss. It counts in **ET days** rather than
elapsed hours — a failure at 23:00 ET and one at 01:00 ET the next day are two missed generations
two hours apart — and it runs from the **market-data** job for the same reason its sibling does:
those slots succeed while Flex is refusing.

**The threshold used to be the hardcoded 2 that N=3 implies, and on 2026-08-24 that was a false
alarm with four weeks of margin in hand.** The live query was `Last 30 Calendar Days` (see *The
Flex Query*), so an ordinary two-day gap produced a banner telling the reader trades were "about to
become unreachable" when ~28 days of slack remained. A false alarm that names a data-loss risk is
worse than no alarm, because the next true one reads identically. Two things follow, and both are
now enforced rather than written down: the threshold is derived from the measured span, and the
message **states the window it measured** (`reaches back 30 calendar days`, or
`window length is unknown, assuming 3` when nothing is on record) so a wrong reading is visible
instead of inferred. `FLEX_GENERATION_GAP_WARN_DAYS_FLOOR` (2) keeps a pathologically narrow period
off a hair trigger — IBKR issues nothing at the weekend, so a Friday success is two ET days old by
Sunday through no fault of anything.

**`full_sync` runs its 730-day market-data pass whether or not IBKR succeeded, and gating it
was a silent outage (fixed 2026-08-07).** `_full_sync_job_locked` used to wrap step 3 in
`if ibkr_result["status"] == "success"`, which reads as prudent — no new securities, no new
prices — and is wrong twice. It confuses two independent providers: Flex refusing to generate a
statement says nothing about Yahoo, and the securities needing prices are the ones already in
the database. And combined with the once-per-day rule above it made the deep pass the **rarest**
job in the schedule rather than a daily one: the 06:00 slot takes the day's generation, so 08:00
fails, so the 730-day backfill did not run between 2026-08-03 and the fix.

**What made it invisible is the thing to remember.** The six 7-day `market_data_only` slots run
unconditionally and keep *current* value fresh, so nothing on any screen looked wrong — only the
two-year history quietly stopped extending, and no surface reports the age of a backfill. The
failure was legible solely as `market_result: null` inside `details` on runs whose top-level
`status` was already `error` for an unrelated reason. A skipped step also reports **no
warnings**, so `find_stale_priced_securities` could not fire on those mornings either: an
unpriced holding discovered by the 08:00 pass was structurally unreachable exactly when IBKR
had refused.

Cost of decoupling: one Yahoo request per security on the mornings it now runs where it used to
skip — the span narrowing in `fetch_and_cache_prices` starts the range a few days before
`min(missing)`, so an already-backfilled security re-requests almost nothing, and only a newly
bought one pulls its full history. `status` still reports the **IBKR** verdict, so a green Yahoo
half cannot paper over a refused statement and `find_stale_ibkr_sync` still counts correctly.
Pinned from all four sides in `tests/test_scheduler_jobs.py` — it prices on failure, it still
reports the failure, it still prices *after* IBKR rather than before, and market warnings now
survive a failed IBKR half.

**Market data reprices at 13:00 and 20:00 and that is not those slots coming back.** The prohibition
is specific to Flex: Yahoo has no statement to generate and no `Code=1025` budget to spend, so a
mid-session market-data request is ordinary while a mid-session *IBKR* request is self-harm. The test
above keys on `IBKR_JOB_IDS`, not on the hour, for exactly this reason.

**Read "we suddenly get constant 1001s" as *we added slots that never worked*, not as a regression.**
The 13:00/20:00 jobs were introduced in `67e6a59` on **2026-07-25** — the same day `sync_runs`
persistence landed, so the oldest record we have (20:00:11 Berlin, a failure) *is* the first retry
ever attempted. Before that only the 08:00 `full_sync` ran, and its success rate has not moved (4/5
that week). Two changes arriving together — new failing slots and, for the first time, a record of
every attempt — read as IBKR getting worse. It had not.

That accounts for the *volume* of failures. It does **not** account for why `1001` started at all,
and the answer to that is the query itself:

| | Flex Query contents |
|---|---|
| before 2026-07-24 | **Open Positions only** |
| `6cccdab` 07-24 | + Trades, CorporateActions, CashTransactions |
| `86960aa` 07-28 | + Deposits & Withdrawals, Transfers |

**One section became six in four days, and five of them scan the whole YTD period.** Open Positions
does not — it is an as-of snapshot, which is why it never provoked this. The 08:00 `1001`s that
motivated adding the retries in the first place began the day *after* the first expansion. So the
chain is: sections added → `1001` appears → retries added at hours that can never work → `1001`
everywhere.

**Do not reason about statement cost from row counts.** Open Positions is ~70% of the *rows* (979
lots) and ~0% of the *scan work*. An earlier revision of this file used the row share to argue that
shortening the period would barely help; that was measuring the wrong quantity.

One hypothesis this does kill: **"failures accumulate into a throttle."** The autocorrelation is
*inverted* — the next attempt succeeds 5/15 after a failure and 0/5 after a success. That is
schedule position, not contagion: 08:00 follows a failed 20:00, and 13:00 follows a successful
08:00. A success 4½ hours after a failure (2026-07-26 00:30) rules out a cooling-off period.

**The period is `Last 30 Calendar Days` as of 2026-07-31, and that is what fixed it.** The evidence
is a clean A/B: a 20:00 failure and a 21:08 success 68 minutes apart, same token, same hour band —
15:08 New York, mid-session, where the day had gone 0-for-8. Statement shape dropped from ~290 trade
rows to 103 and ~107 cash transactions to 17.

It is safe because `reconcile_taxlots` reads trades from the *database*, ingestion is idempotent and
additive, `widen_cash_flows_covered_from` only moves the boundary earlier (a 30-day `from_date` is
ignored, so January coverage stands), and Open Positions is period-independent. Verified after the
switch: all 71 YTD trades still on record, `coverage_from` still 2026-01-09, 979 lots, 0 skipped.

**Do not restore the YTD period to "be safe".** That reintroduces the failure. What it bought — a
statement that re-delivers the whole year every time — is available on demand instead: a browser
download ingested through `app/cli/ingest_flex_xml.py` is idempotent, so it simply fills whatever a
bounded window missed. `find_stale_ibkr_sync` exists to tell you when to do that.

**One pipeline at a time (`app/single_flight.py`).** `/api/` is public — and was unauthenticated when
this was written; `app/auth.py` (below) can now gate the writes, but throttling and authorization are
different jobs and this one is still needed. Nothing stopped concurrent or rapid-fire triggers: APScheduler's `max_instances=1` only fences jobs *it*
dispatches, and `POST /api/scheduler/trigger` ran `full_sync_job()` as a bare coroutine outside the job
store entirely — so a stranger could overlap the 08:00 run or spam Flex requests toward a `1025`
lockout. Everything that can reach IBKR or Yahoo shares the `sync-pipeline` gate; two pipelines racing
is the failure mode regardless of which endpoint started them. Scheduled jobs enter with **no cooldown**
and, on collision, record a `status="skipped"` run rather than running concurrently (the next slot
recovers freshness). The public routes add cooldowns (ibkr 120s; market-data / trigger / fundamentals /
ratings / allocation / watchlist / dividends 300s) and answer **429 with `Retry-After`**. In-process by design —
single uvicorn worker, and the check-and-set has no `await` between test and set. A backgrounded route
(fundamentals `/sync`) checks `is_running()` in the handler and holds the lock inside the task, so the
gate spans the actual work rather than the enqueue. **`POST /api/watchlist` (add) is gated too** (60s
cooldown — a single-ticker fetch, lighter than the 300s bulk sync): it fires a per-add `force=True`
yfinance fetch and was the one Yahoo-triggering route the rollout missed, so rapid-fire adds of
distinct tickers were an unthrottled fetch storm. The add itself stays *outside* the gate — busy or
cooling, the row is created with `last_synced` null and the next sync fills it in, rather than a
running 08:00 job turning a bookkeeping action into a 429. Tests: `tests/test_single_flight.py`.

**`GET /api/portfolio/benchmark` is gated at the *fetch*, not the handler** (added 2026-07-30 — it was
the last route with no gate at all; `portfolio.py` never imported `single_flight`). It lazy-fetches
Yahoo and tiles Frankfurter on a cache miss, so looping the 8 keys in `BENCHMARKS` over the 5-year span
the route allows could run beside the 08:00 `full_sync`. It must **not** wrap the handler:
`sync_benchmark_prices()` only refreshes benchmarks that *already have rows*, so this route bootstraps
the warm set — a cache-only GET leaves a first-time selection empty forever, and gating the read would
429 the chart every morning. So the gate sits inside `_ensure_prices_available` /
`_ensure_fx_rates_available` around the network call, and `SyncBusy` serves what is cached.

Two consequences worth keeping straight. **Entering the gate bumps the shared last-start clock every
other route's cooldown reads**, which is why the gate wraps only the actual fetch — a warm chart load
must not 429 a manual IBKR sync. And **the gate cannot stop a sequential loop**, so each ticker and
currency carries its own `UPSTREAM_RETRY_COOLDOWN_SECONDS` (300) attempt memo: trailing weekdays the
provider has no bar for stay missing *by design*, so the range end is otherwise re-requested on every
request forever — the same shape the holiday rule fixed for `market_prices`. Keyed **per upstream
target**, because warming eight distinct benchmarks is legitimate and re-hitting one is not.
`reset_upstream_throttle()` exists for tests, since the memo is process-lifetime state.

`_ensure_fx_rates_available` also asks what is missing before tiling. `_batch_fetch_rates` issues its
request **unconditionally** (it dedups per row, *after* the response), so a five-year chart load cost
~60 provider requests every time regardless of the cache. It now uses the same holiday-aware
missing-days rule as the price path, extracted to `_missing_business_days()` so the two can't drift.

`POST /api/fundamentals/sync` finally carries the 300s this file already claimed for it. `is_running()`
fences only *overlapping* runs, so a poller that waited for each pass to end ran them back to back
indefinitely at ~5 Yahoo calls per security per pass. `cooldown_remaining()` lets a BackgroundTasks
handler answer 429 honestly instead of replying `"started"` to a run the background half then drops.
`POST /api/dividends/sync` had exactly the same shape — a module flag that fences only overlapping
runs, a gate entered with no cooldown — and was the one bulk Yahoo route the 2026-07 rollout left
out; it mirrors the fundamentals route since 2026-09-12 (`SYNC_COOLDOWN_SECONDS = 300`). The
`GET /summary` auto-trigger was already throttled (6h); the unthrottled path was the explicit one.

**Errors are redacted before they are stored or served (`app/redact.py`).** Flex sends the token as a
`t=` URL parameter and `requests` transport errors stringify with the full URL, so a plain `str(e)` from
a failed SendRequest carries it — and those went verbatim into `sync_runs.message`, which the public
`/api/scheduler/status` and `/history` re-serve forever. Production really did leak it (found and
scrubbed 2026-07-28; **rotate the token if this ever recurs** — that instruction is about a *new* leak, which would mean the redaction below had failed. The 07-28 exposure itself the owner decided on 2026-08-17 to accept rather than rotate; STATUS.md records why, and it is not to be re-raised). `SyncRunRepository.record()` redacts on
write and `to_dict()` again on read, so rows written before the fix or restored from a backup can't leak
either; the routers redact their `HTTPException` details. The `q=` query id stays readable — public in
these docs and useless alone. Tests: `tests/test_secret_redaction.py`.

**A price that never arrives is otherwise silent.** `portfolio_service` values a position with no price
at **0.00** and moves on, so deleting SBI's poisoned prices took 446.93 CHF off the total with nothing
reporting it. `find_stale_priced_securities()` now runs after every market-data sync and warns when a
security **with open lots** has no cached price at all, or none newer than `STALE_PRICE_DAYS` (5 —
enough to absorb a weekend plus a holiday). Closed-out holdings are excluded: they legitimately stop
getting prices, and warning on them would be permanent noise.

**The timeline had the same silence, and it renders as a loss rather than a gap.** `find_stale_priced_securities`
guards the *current* snapshot; `/api/portfolio/value-over-time` drops an unpriced holding from
`market_value_eur` while its cost stays in `cost_basis_eur`, so the point understates by that holding's
whole value. Measured against production by querying dates past the last cached price:

| date | market value | gain/loss % | |
|---|---|---|---|
| today | 64,944 | +33.7 | correct |
| +7d | 64,944 | +33.7 | carried forward inside `PRICE_LOOKBACK_DAYS` (14) |
| **+14d** | **56,009** | **+15.3** | **partial — some securities still resolve, some are zero** |
| +15d | 0.00 | **−100.0** | every holding out of lookback: a fabricated wipeout |

**The partial row is the dangerous one**, by the same rule as the zero-for-unknown cards: `+15.3%` looks
like an answer, `−100%` looks like a bug. And this is precisely what a **stalled price feed** looks like
— a smooth decay to zero rather than a missing line — which is the failure mode where nobody is watching
the sync warnings either.

So each point now carries **`unpriced_holdings`**; anything above 0 means the valuation is incomplete. It
was previously only a `logger.warning`, which at up to ~29k lines for a 730-day window over 40 securities
is noise rather than a signal. Counted in **both** `_calculate_timeline_swept` and
`_calculate_daily_value`, and `test_timeline_equivalence.py` pins them equal — note the point query walks
tax *lots* while the swept one walks *securities*, so it counts a **set of ids**; incrementing would
report 110 for a holding split across 110 lots and break that equivalence. The field is declared on
`PortfolioValuePoint`, without which the `response_model` would have dropped it silently.

**`/api/portfolio/attribution` carries the field too, and it was the worst place it was
missing.** `get_eur_value` returned `0.0` when either the price or the FX rate was absent, so a
still-held position whose feed went stale read as **`-start_value`** — the exact shape the disposal
term was added to fix for *sales*, arriving by the other route and never covered. The mirror case (an
unvaluable start) fabricates a gain of the same size. This endpoint renders one bar per security, so
the fabricated figure is not buried in an aggregate: it is the largest bar on the chart, under the
security's own name. Two knock-ons made it worse — `weight_percent` divides by a `total_end_mv` the
zeroed holding is missing from, inflating every *other* security's weight, and `contribution_percent`
divides by a `total_pnl` the phantom loss moved.

Unvaluable securities are now **excluded from both sides** and counted, as the forward yield already
did. What makes exclusion safe is that a lot held at *neither* endpoint never reaches the valuation
helper, so a fully-sold position keeps its legitimate zero and is never confused with an unpriced one.
The notice sits **outside** the collapsible body: this card is collapsed by default and its collapsed
summary shows `total_pnl_eur`, the very figure the notice qualifies — a caveat you must expand a card
to reach is as good as absent, the same rule that put the dividend basis in a footnote rather than a
tooltip.

**`MonthlyReturnsHeatmap` was the same shape and was fixed on 2026-08-07**, which is what makes this
a rule rather than one card's detail. The table inside badged every trimmed figure `†` and explained
the dagger in a footnote — both *inside* the collapsed body — while the collapsed summary rendered
`Aug: +1.5% · YTD: +3.1%` with no marker at all. So the qualifier was present on the surface almost
nobody opens and absent from the one everybody reads. The summary now carries the dagger **and spells
the legend out inline** (`† part of the period only`), because the footnote that would explain it is
not rendered yet; and `cellTitle` names the days the figure actually covers, since on a trimmed figure
the wrong thing is the label, and "part of the period" alone cannot distinguish a lost day from a lost
half-year.

**The client acts on it in three places, and the metric ones matter more than the chart.** A pair spanning
a complete day and an incomplete one manufactures a move that never happened — 64,944 then 0 is **−100%
in a single day** — and `dailyReturnSeries` feeds *everything*: max drawdown, current drawdown,
volatility, Sharpe, Sortino. So a stalled feed did not merely bend the line, it moved the whole risk row
to match, with each number looking individually reasonable. `isMeasurable()` now disqualifies a pair with
either end incomplete, exactly as a flow disqualifies a day for beta: excluding it costs a point and
biases nothing, while keeping it invents one. `betaAndCorrelation` needs the guard **separately**,
because it derives its own returns rather than going through `dailyReturnSeries`.

**`computeModifiedDietzReturn` is the third, and it was missed when the other two were written** —
which is the part worth remembering, because "I guarded the consumers" was true and incomplete on the
same day. It behaves differently from them by necessity: Modified Dietz reads only the two **endpoint**
market values and the flows between, so an incomplete endpoint is not a small error but the entire
answer, while an incomplete *interior* day cannot affect it at all. Dropping a whole month over one
stale day would also lose far more than it protects. So it **trims** leading and trailing unmeasurable
points instead of refusing, which is exact rather than approximate — a true Dietz return over the days
it kept — and sets `partial` so `MonthlyReturnsHeatmap` can badge the cell `†` with a footnote. The
**YTD column is the one that matters**: it ends on *today*, precisely the day a stalled feed breaks.

The chart still *plots* those days — a hole in the line would be its own kind of lie — so
`PortfolioValueChart` renders a `role="alert"` naming how many days and how many holdings, and saying the
dip is missing price data rather than a loss.

**Guarding the consumers was necessary and not sufficient, and 2026-08-17 found the rest.** Excluding
an unmeasurable day is only half the answer: the *result* of excluding every day still has to be
distinguishable from a measured one, and three surfaces could not do it.

- **`maxDrawdownPct` and `drawdownDetail` return `null`** when `dailyReturnSeries` yields nothing,
  because `RiskMetricsCards` reads a zero max as licence to print *"Never below its opening value"* —
  in prose, and in green, since the current drawdown is zero too. So the guard above turned a
  fabricated `−100%` into a fabricated *reassurance*, which is worse: the loud version invites doubt.
  A measured zero still says "never fell", and `sampleDays` rides along in `betaAndCorrelation`'s
  shape so a thin window can declare itself.
- **`calculate_xirr` latches `last_xirr_unpriced`** and `/annualized-return` declares it. XIRR values
  both endpoints through `_calculate_daily_value` and had been reading only `market_value_eur`, so a
  holding it could not value left the terminal inflow while its purchases stayed in the flow list —
  the **Annual Return (XIRR)** card and the **Calmar** derived from it both understated. It
  **reports** rather than excluding, unlike `/attribution`: the lot purchases come from `taxlots` and
  are unconditional, so dropping the security would leave a cost with no matching value.
- **`winRate()` excludes an unvaluable holding from both sides** and returns `null` when nothing is
  priced. It had counted one as a *loser* — an unpriced position is valued at 0.00, so its
  `gain_loss_eur` is `−cost` — leaving the numerator while staying in the denominator, with the
  card's footnote stating it as fact ("36 of 39 profitable"). It lives in `portfolioKpis.ts` beside
  the two concentration figures that already refused this condition, and uses the **two-clause**
  predicate `market_price === null || market_value_eur <= 0` cited from `rebalance.ts`, because a
  missing FX rate leaves the price populated and zeroes the value.

- **`/api/allocation/portfolio` excludes an unvaluable holding and names it**
  (`unpriced_holdings` / `unpriced_symbols`), where it used to carry it at a 0% weight. This is
  the quietest member of the family and the last one found: every slice is labelled
  "% of portfolio", the three breakdowns sum to **exactly 100** whether the holding is in or
  out, and so the failure renders as an *absence* from a picture that looks complete. Note what
  that did to the test suite — `test_an_unpriced_holding_does_not_break_the_percentages` pinned
  the sums for months and was structurally blind to it, because here summing to 100 is what
  being wrong looks like. When a completeness bug can satisfy the assertion you already have,
  the assertion is measuring the wrong thing.

- **`PositionsList` shows a dash for value, gain and weight, and names the holdings above the
  table.** The last member found, and the one the family should have started with: this is the
  screen a reader opens to find out *which* holding, and it was the one publishing the fabricated
  figure. An unvaluable position has `market_value_eur` 0.00, so `gain_loss_percent` is exactly
  **−100.00** — rendered in red, weighted `0.00%`, with no marker of any kind. Three rows of KPI
  cards directly above already said *"N unpriced, not judged"*, so the app **named the condition
  and then contradicted it forty pixels below**. Weight is `null` rather than `0` for the same
  reason `concentrationPct` is (a `0.00%` claims the holding is a negligible part of the book),
  and unpriced rows leave the weight denominator so it covers the same set as the rows allowed a
  weight. It reads `isUnpriced` from `positionValuation.ts` rather than testing
  `market_price === null`, because a missing FX rate is the second route in and does not clear
  the price.

The lens that found all four, and the one to reuse: **ask which other code reads an incomplete
valuation**, not which code shares a name — none of them shares a function name with anything.

**Absent means complete**, deliberately: the field only exists from 2026-08-05, so reading `undefined` as
unmeasurable would put a permanent warning on every chart served by an older backend. Same choice
`externalFlow` makes about its own optional field.

**`/api/portfolio/summary` carries the same field, and that is the more important one.**
`total_market_value_eur` is a sum over the holdings the backend could price — the *headline* figure, on
the hero card — and an unpriced holding leaves it while its cost stays in `total_cost_basis_eur`, so the
total and both gain figures understate. This is the SBI incident restated: deleting one security's
poisoned prices took 446.93 CHF off exactly this number and the only thing that said so was a sync
warning nobody has to read. `PortfolioSummaryCards` now renders a `role="alert"` above the row.

It comes off `_calculate_daily_value`, the same helper the timeline uses, so the headline and every chart
point agree about their own completeness instead of each deciding — pinned by `test_api_smoke.py`, which
asserts the summary's count equals the last timeline point's.

**Do not recompute this on the client from `market_price === null`.** The backend fails to value a
holding for *two* reasons — no price **or** no FX rate — so a client-side count under-reports, and
`currencyExposure.ts`'s own `unpricedCount` is a different (narrower) question about quote currency.

**A sync that never *succeeds* is silent in the same way.** Individually a failed IBKR run is
unremarkable — `1001` is routine and the schedule shrugs it off — so the thing worth alarming on is
the **absence of a success**, not any single failure. `find_stale_ibkr_sync()` warns after
`IBKR_SYNC_STALE_DAYS` (7) with no successful run of an `IBKR_SYNC_TYPES` sync. Three details carry
the weight:

- It runs from the **market-data** job, not an IBKR one. Market data succeeds while Flex is refusing,
  so the warning still reaches `warnings[]`; hanging it off the IBKR job would silence it in exactly
  the outage it exists to report.
- **`ibkr_manual_xml` counts as a success.** Ingesting a browser download genuinely refreshes the
  data, so the documented escape hatch from a token lockout must reset the clock — otherwise the
  alarm blares through the correct recovery and trains the reader to ignore it.
- An **empty** history is quiet (fresh install), but *attempts with no success ever* warns. Those are
  different states and collapsing them would either cry wolf on day one or hide a broken token.

This is what makes **shortening the Flex Query period** safe. Under Year-to-Date a gap costs only
freshness, because every statement re-delivers the year; under a bounded window trades that fall out
of it before a sync succeeds are gone from every future statement. Seven days against three IBKR
attempts a day is ~21 consecutive failures, so it cannot fire over a `1025` lockout (~14h).

`_collect_warnings()` hoists each step's warnings to the top of the job's result, because `_record_run`
reads `result["warnings"]` and a job's own dict never had that key — so warnings were being buried in
`details` and never rendered as warnings.

**The job store is persistent, and three details make it actually work.** APScheduler runs in-process, so
a `docker compose down` overlapping a Berlin slot used to drop that slot outright — which is what
happened to the 2026-07-30 08:00 `full_sync`. A `SQLAlchemyJobStore` (`settings.scheduler_jobstore_url`,
a **separate** sqlite file: the store is synchronous SQLAlchemy while the app is aiosqlite/WAL) plus
`coalesce=True` and `MISFIRE_GRACE_SECONDS` (1800) runs the missed job on startup instead.

- **The registered targets are module-level functions** (`full_sync_job_entry` and friends), not the
  bound methods they were. A persistent store serializes each job, and pickling `self.full_sync_job`
  drags the live `AsyncIOScheduler` in with it.
- **`_add_or_keep()` exists because `add_job(replace_existing=True)` recomputes `next_run_time` from
  now** — it would overwrite the missed timestamp on the way in and make the persistence pointless. An
  identically-triggered job is left alone; comparing `str(trigger)` is what lets a genuine schedule
  change still replace one. Both directions are pinned in `tests/test_scheduler_jobs.py`.
- **docker-compose mounts the store's *parent directory*, never the `.db` file.** Docker creates a
  missing bind-mount source **as a directory**, so `./scheduler_jobs.db:/app/scheduler_jobs.db` — the
  mount from 2026-07-30 to 08-01 — could only ever produce an empty directory at the database path
  and `sqlite3.OperationalError: unable to open database file` on every boot. sqlite creates the file
  but never its parent, which is why the URL lives inside `scheduler-data/` and
  `ensure_jobstore_parent()` runs first. Pinned structurally by
  `tests/test_scheduler_jobstore_path.py`, which reads the compose file — no runtime assertion can.

Thirty minutes is chosen from both ends: long enough for a `build --no-cache` rebuild, short enough
that a real outage doesn't dump four stale slots onto a cold container.

**The failure mode to fear here is that the fallback looks exactly like success.** When the store
won't open, `start()` degrades to an in-memory scheduler — deliberately, because an exception out of
the lifespan handler costs the whole site while losing misfire recovery costs one late sync. But the
fallback **re-registers every job**, so `/api/scheduler/status` reports a fully-armed scheduler
either way, and the sole symptom is one `logger.error` nobody had reason to read. It was inert for
two days behind exactly that appearance, while STATUS.md recorded it as verified working. `/health`
therefore reports **`scheduler_jobstore_persistent`**: if it is `false`, a deploy overlapping a
Berlin slot still loses that sync, whatever `/api/scheduler/status` says.

**`/api/` protections that are not `single_flight`.** Three middlewares in `app/main.py`, innermost
last, so a rejection from any of them still carries a correlation id:

- **`app/auth.py`** gates every `POST/PUT/PATCH/DELETE` under `/api/` on `settings.api_admin_token`
  (`X-API-Key`, or `Authorization: Bearer`, compared with `secrets.compare_digest`). It is
  **middleware, not a per-route dependency**, deliberately: every router takes only `Depends(get_db)`,
  so a dependency would have to be added to ~14 routes and remembered on every route added later —
  keying on the HTTP method means a new `POST` is covered the moment it exists, and
  `test_every_mutating_route_is_covered_without_being_annotated` walks the live route table to prove
  it. **Empty token = disabled**, so shipping it could not 401 the running site; startup warns loudly
  while it is off, the same treatment `SCHEDULER_ENABLED` gets. Reads stay open because the frontend
  has no login and gating them would black out the UI.
- **`app/rate_limit.py`** is a fixed-window per-client counter (`RATE_LIMIT_PER_MINUTE`, 0 disables).
  `single_flight` fences the sync *pipelines*; nothing bounded the expensive anonymous reads. Keyed on
  the first `X-Forwarded-For` entry, since nginx makes `request.client.host` always loopback — forging
  it only splits the forger's own bucket. `/health` is exempt: the deploy script polls it.
- **`app/observability.py`** stamps `X-Request-ID` (reusing a plausible inbound one so it correlates
  across a proxy) and installs the handler for unhandled exceptions — the one path where `str(e)`
  still reached the client unredacted. The body is a fixed string plus the id; the log line goes
  through `redact_secrets`.

`tests/conftest.py` neutralises the limiter's process-lifetime window state and the job-store path for
the whole suite, or one module's traffic would 429 another's. Tests: `tests/test_api_hardening.py`.

---
