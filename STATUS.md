# Working state

**Last updated: 2026-08-17 (night).** Latest: **a bug sweep found four more members of the
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

## Picking this up cold — the 2026-08-08 handoff

Check `/health`'s commit against `git rev-parse origin/main` before assuming a symptom is unfixed.

Four threads are genuinely open. In descending order of what they cost:

1. **The 18:00 Berlin IBKR slot is proven, and this thread is closing** → *Watching*, first entry.
   Nine ET days measured on 2026-08-17: 12:00 ET is **7 of 9**, and the one miss was recovered by
   the 00:00 Berlin slot the same ET day. The old "mid-session is fatal" reading was confounded —
   13:00 and 20:00 Berlin also ran *after* an earlier slot had spent the day's generation.
2. **The `full_sync` decoupling is deployed but still unobserved** → *Watching*, `full_sync` entry.
   It only shows on a run where IBKR refuses, which the guard now makes rarer.
3. **The Flex window is 3 days**, which is a two-day margin → *Watching*, Flex period entry, and
   CLAUDE.md's *The Flex Query* for the arithmetic.
4. **Two credentials are knowingly unrotated, both by owner decision** → *Needs a human*. Neither is
   a task, neither may be re-litigated, and both transcript exposures of `API_ADMIN_TOKEN` were
   likewise accepted. **There is no open security item.**

The durable half of these findings is in **CLAUDE.md**, not here: the once-per-day rule, the guard
that now enforces it, the `whenGenerated`-is-Eastern rule and why 18:00 Berlin was chosen are all
under *Sync schedule* / *The Flex Query*. This file carries only what is perishable about them.

## Needs a human

- **The Look-through CLI runs are DONE as of 2026-08-17 and will need redoing as baskets age.**
  Ran on production that evening: 10 baskets fetched (0 failed), 9 imported, identity resolution
  completed. Coverage 95.16%, `unresolved_value_eur` 516 CHF. Nothing is scheduled, so this is a
  recurring chore, not a one-off — the six daily feeds badge `†` after 7 days and Vanguard after 75
  (`ADAPTER_STALE_DAYS`). `find_stale_etf_baskets()` is still the missing piece; until it exists the
  badge in the fund table is the only signal. None of it touches Yahoo or IBKR, so it is safe at any
  hour:

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
  commit against `origin/main`. Background in the *Shipped 2026-08-07* section below.

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
  actually runs daily. See the section below for why it had not.

## Shipped 2026-08-17 (night) — four more incomplete valuations, and the basket alarm

A bug sweep using this repo's own lenses: *what would this metric's stand-in value claim?*, *which
other code publishes the same name?*, and an AST sweep for functions defined in more than one module.
Four defects, all one family — **a figure built from an incomplete valuation, presented as
complete.** CLAUDE.md records that family being closed for the timeline, `/summary`,
`/attribution`, `dailyReturnSeries`, `betaAndCorrelation` and `computeModifiedDietzReturn`; these are
the members missed each time.

Ranked by *plausibility*, not size, which is the rule that ranks them correctly:

1. **Current Drawdown claimed the portfolio never fell, in green, over a range it could not
   measure.** `maxDrawdownPct` returned `0` when `dailyReturnSeries` yielded nothing, and
   `RiskMetricsCards` read that zero as licence to print *"Never below its opening value"* — with a
   green tone, because the current drawdown was `0` too. Reachable when every point in the range is
   unmeasurable: a multi-day market-data stall on a 7D range, or MTD in the first days of a month
   over a freshly bought holding with no price — the same reachability that got Sharpe fixed on
   08-05. Both drawdowns are `number | null` now, plus a `sampleDays` count in
   `betaAndCorrelation`'s shape, and a measured zero still says "never fell" because that statement
   is true.
2. **XIRR discarded `unpriced_holdings`.** `calculate_xirr` values both window endpoints with
   `_calculate_daily_value` — which returns the count — and read only the value. The tax-lot
   purchases are unconditional flows while the endpoint valuation omits the unpriceable holdings, so
   **Annual Return (XIRR)** understated, and so did the **Calmar** built on it. Reported rather than
   excluded (dropping the security would leave a cost with no matching value, unlike
   `/attribution` where exclusion is right), via a `last_xirr_unpriced` latch — the documented
   precedent, because one router and six assertions unpack that 5-tuple. `AnnualizedReturnResponse`
   declares the field with the "a response_model is a filter" note its sibling already carries, and
   `test_api_smoke.py` pins it non-zero on the fixture's unpriced TSMC.
3. **Win Rate counted every unpriced holding as a losing position.** `get_positions_breakdown`
   values an unvaluable holding at 0.00, so its `gain_loss_eur` is `−cost` — it left the numerator
   and stayed in the denominator. Live rather than theoretical: `unpriced_holdings` read **3** on
   2026-08-07 in the gap before the new ETFs were priced, ≈7.7 pp on this book, and the card's own
   footnote stated it as fact ("36 of 39 profitable"). Now `winRate()` in `portfolioKpis.ts` beside
   the two concentration figures that already refused this condition, excluding from both sides and
   naming the count, `null` when nothing is priced. It uses the **two-clause** predicate
   (`market_price === null || market_value_eur <= 0`) cited from `rebalance.ts`, because a missing FX
   rate leaves the price populated and zeroes the value.
4. **`computeModifiedDietzReturn` fell through to `0`** when its denominator was not positive, while
   already returning `null` for its two other undefined cases. Defensive rather than observed — it
   needs outflows exceeding the opening valuation — but a `0.00%` cell reads as a quiet month.

Plus the improvement this file has listed as *Worth doing next* item 3: **`find_stale_etf_baskets()`**,
hung off the market-data job beside its four siblings (that slot succeeds while Flex is refusing).
Held funds only; the basket a fund actually reads comes from
`LookthroughService._alias_proxied_baskets` rather than a second copy of the proxy rule, and the
threshold from the existing per-adapter `stale_after_days`. **Nothing unclearable warns** — a fund
excluded by design, or one whose only route is a hand download with nothing to borrow, stays silent,
because a warning that can never clear is the always-present-Flex-banner pathology. But "has a route"
follows the proxy: VWCE's own adapter is `manual` while VT is fetchable, so a missing VT *is*
actionable. That last rule was a real gap in the first draft, caught by writing the test.

**One extraction came out of writing the fix, not out of reading the code.** `winRate` needed
"can this position be valued?", which was already inline in `rebalance.ts` *and*
`currencyExposure.ts` — so the fix was about to become the third copy of the predicate. It is
`positionValuation.ts` now, with a family test that names any `lib/` module testing the columns
itself. What makes it more than tidiness: those two copies had already needed correcting
**together** on 2026-08-05, when the one-clause `market_price === null` form turned out to miss the
FX case — the drift had happened once already and only stayed in lockstep by luck. The mutation
check is the sharp part: reintroducing a local copy leaves `rebalance.test.ts` **green** and only
the family test fails, which is what "a correct copy is still a copy" looks like in practice.

Verified: backend **1183 passed** (was 1168), frontend **485** (was 466), `tsc -b` and
`npm run build` clean. **Every fix was mutation-checked** — revert it, watch the new test fail — for
the eleven mutants across both halves, because this repo has twice had a test pass against the bug it
was written for. A twelfth check pins the *wiring*: an AST walk over `sync_market_data` asserting all
five detectors are actually called, since a detector nobody calls is the same silence as no detector.

`e2e/ledger.mjs` is fixed but **not run** — it needs a production DB snapshot, and none was pulled.
See *Known rough edges*.

## Shipped 2026-08-17 (late) — DBPG decomposes through VOO

Owner's instruction: use VOO's S&P 500 basket for DBPG, overwriting whatever DBPG itself
publishes; the position is being replaced with IQQ later anyway. So DBPG is no longer `excluded`
and **every held fund now decomposes**.

**Half the objection is answered and half is not, which is why the two were recorded separately.**
VOO's basket fixes "the disclosed names are collateral, not the index". It does nothing about the
**2x leverage**: DBPG is decomposed at its market value, so its real exposure to each company is
about double what the table shows. Scaling it is not available — the five buckets must sum to the
portfolio to the cent — so `leverage` now drives a `warnings[]` line instead, and clearing that
field would delete the second disqualifier silently.

`replication` stays `synthetic`, which makes the proxy **beat** a stored basket rather than merely
fill a gap: importing DBPG's collateral file can no longer un-proxy it. Pinned as a family rule
(`test_a_synthetic_fund_is_never_decomposed_from_its_own_basket`) so a second swap fund cannot
arrive without it.

**VOO is declared in both fund tables but is NOT held** — a basket donor only. Its `ETF_ALLOCATIONS`
blocks are byte-identical to SXR8's and DBPG's, which were already identical to each other: three
S&P 500 trackers, one set of numbers, change one and change all three. ISIN `US9229083632` verified
against OpenFIGI rather than recalled.

**Fetched and imported on production the same evening**: 503 rows, as-of 2026-07-31, 100% ISIN
coverage, weights summing to 99.94%. **Coverage went 95.16% -> 98.97% and `uncovered_fund_eur` is
now 0.00 — every held fund decomposes.** Both pages declared 503 and 503 arrived, so VT's torn-read
hazard does not reach VOO in practice: 2 page boundaries against 21, and none of VOO's rows carry a
0.00% weight.

## Ran on production 2026-08-17 — the runbook, and what it caught

**Identity resolution completed** (`manual_identity_resolve`, success): 1,998 ISINs resolved — 893
LEI, 1,988 shareClassFIGI — plus **108 fund constituent rows folded by CINS/SEDOL**, which is
GRID's and QTUM's issuer identifiers becoming companies. `unresolved_value_eur` fell from
**4,281 → 516 CHF** (0.7% of the book); coverage 95.16%.

Two things about the run worth keeping. It **commits once at the very end**
(`resolve_identities.py:165`), so an interrupted run loses everything including the `*_checked_at`
stamps — do not deploy while one is in flight. And **`pgrep` is not installed in the container**, so
a `pgrep -f … | wc -l` liveness check counts the *error line* and always returns 1; use the
`sync_runs` row to tell whether it finished.

**Coverage is 95.14% and DBPG is the only fund not looked through.** Fetched all 10 baskets
(0 failed) and imported 9; SMH, SOXQ, GRID and QTUM stored for the first time. Identity
resolution is running detached in the container (~37 min, GLEIF-bound) — **do not deploy until
it finishes**, because `docker compose down` kills it.

**VT's import refused, and the guard earned its keep.** Vanguard serves that endpoint from a
cluster holding different snapshots: the 21-page walk came back 13 pages declaring 10,055
holdings and 8 declaring 10,032, twice in a row. Because ~8,000 of VT's rows have a 0.00%
weight and no stable order between snapshots, the assembled set carried **9,114 distinct
holdings in 10,032 rows** against 10,025 in the stored basket — ~900 companies would have
disappeared from ~11% of the book, with weights summing to a plausible 91.60%.

Nothing is lost: the refusal keeps the 2026-06-30 basket, and VT publishes month-end so there
is no urgency. `parse_vanguard_us` now refuses a size disagreement as **its own named fault**
rather than reporting "a page is missing", which sends the operator hunting something that was
never missing. Committed, **not pushed** — see below.

**`OPENFIGI_API_KEY` went on the VPS later the same evening** and the app reads it. It was never
blocking — the run above needed it for 105 non-ISIN identifiers, ~11 keyless requests, while GLEIF's
one-request-per-ISIN is the whole 37 minutes.

## Shipped 2026-08-17 — a borrowed basket, and an 8% shortfall that was never cash

Two asks from the owner, plus the answer to a question that turned out to be a documentation gap.

**VWCE borrows VT's basket.** Declared in `etf_sources.py` as `basket_proxy_isin`, aliased at read
time so nothing downstream has a proxy branch and there is one stored basket with two readers —
a copy would drift the moment VT is refetched. Measured on a production snapshot: coverage
**78.6% → 87.02%**, partition closes exactly, VWCE reports 10,032 constituents with `proxy_for_symbol
= VT`. Once the four new baskets are imported it should land around **95%**, the rest being DBPG
(3.8%, excluded) and per-fund residuals.

It is an approximation and says so on every surface: an amber *Via VT* badge instead of a green
*Decomposed*, a `warnings[]` line carrying the declared reason verbatim, and the Coverage card
**staying amber** while any fund is proxied. That last one is the point — with VWCE resolved the
unresolved list empties, so the old rule would have turned the card green over a fund decomposed
from someone else's file. Unlike a percentage threshold this badge can clear: import a real basket
and the proxy is never consulted again.

Five guards in `test_etf_source_registry.py` (no reason, self-proxy, chain, excluded target,
`manual` target) and five in `test_lookthrough_partition.py`, including that a real basket wins and
that the partition still closes.

**Why VT's equity weight reads 91.86% — it is rounding, and now it says so.** Asked as "it should be
nearly 100% stocks", and it is. Vanguard publishes weights to 2dp, the smallest non-zero weight in
the file is `0.01`, and **8,007 of VT's 10,032 rows are printed at exactly 0.00%** — that tail *is*
the missing 8.14%. Nothing was truncated or misparsed (`stored_rows == source_rows == 10032`,
`skipped_rows = 0`, identifier coverage 100%). EMIM is the same shape at 2,279 of 4,042. The fund
table now shows `N rows at 0%` under the percentage, because an unexplained 8% next to a fund's
value reads as an uninvested cash balance — plausible, and wrong.

**`OPENFIGI_API_KEY` is set locally and verified** against the live API with an 11-job batch (which
a keyless request refuses). Still needs adding on the VPS — *Needs a human*.

One incidental fix: the new count used a bare `toLocaleString()`, which renders 8,007 as "8.007"
under a German runtime. Pinned to `en-US` like `formatCurrency` and `formatPercent`.

## Shipped 2026-08-16 (evening) — sector clustering, and four funds that were never unreachable

Two follow-ups on the charts: cluster the treemap **by sector** rather than by how a company is
held, and "take a look at the missing data too" — 5 funds worth 17.3% of the book publishing no
basket, and 9,930 constituent ISINs carrying no identifier.

**The missing-data half turned out to be a research failure, not a data gap.** `etf_sources.py`
recorded SOXQ, GRID and QTUM as unreachable single-page apps and SMH as needing a hand download.
All four have keyless, login-free routes, and each old note was wrong in its own way:

| fund | what the note said | what is actually there |
|---|---|---|
| SOXQ | "Invesco serves a single-page app" | true of the *product page*; the API behind it is keyless JSON, keyed by the fund's own CUSIP — derivable from its ISIN, so nothing to discover |
| GRID | "tickers but no ISINs, so a scrape could not fold" | there is a CUSIP column beside the tickers. The *fold* concern was right for a different reason — see below |
| QTUM | "a WordPress table carrying CUSIPs but no ISINs" | the table is on `/qtum-full-holdings/`, not `/qtum/`; and the identifiers are not all CUSIPs |
| SMH | needed a hand download | an XLSX with a real ISIN on **every** row — the best-identified feed of the seven |

**10 of 12 funds gained an automated fetch route.** VWCE has none (Vanguard Europe publishes
complete holdings by email on request) and DBPG stays excluded by design — VWCE decomposes anyway
as of the following day, by borrowing VT's basket.

**The trap that would have been silent, and the reason this took a live API check.** Three of the
four publish nine-character identifiers, and most of them are not CUSIPs:

- **77 of GRID's 128 rows are CINS** — the same numbering space extended to foreign issuers, marked
  by a leading letter — including its three largest holdings (Eaton, Schneider, Johnson Controls).
  `US` + a CINS produces a **check-digit-valid ISIN that belongs to nothing**, so a bulk prefix
  would have fabricated an identifier for 60% of the fund, each one passing every validity test.
- **20 of QTUM's 89 rows are SEDOLs** in a column headed "CUSIP".
- **`ID_CUSIP` with a CINS returns zero rows from OpenFIGI** — no error, just nothing. The plan for
  this session said to resolve CINS as `ID_CUSIP`; it would have resolved nothing and reported
  success. The idType is `ID_CINS`, verified with one live request before any code was written.
- **OpenFIGI's mapping endpoint returns no ISIN at all**, only FIGIs. So resolution writes
  `etf_holdings.constituent_share_class_figi`, which `IdentityMember` already unions on — Eaton's
  CINS and its ISIN both give `BBG001S5QZ45`, so the fold works without inventing an identifier.

**Sector clustering.** `sector_taxonomy.py` normalises four vocabularies into the eleven names the
Allocation tab already shows; a company's sector is a value-weighted majority of its contributing
rows, tie-broken on the name so the answer cannot depend on arrival order. Precedence was measured
rather than assumed: BlackRock's baskets classify 97 of the 103 top ISINs, Yahoo 27, and where both
answer they differ only by taxonomy. This narrows `etf_basket.py`'s "sector is deliberately
unserved" refusal rather than breaking it — grouping only, no rollup anywhere in the response, so
nothing states a portfolio-level sector figure.

**The old sector palette failed validation outright**, which is why one is now shared by both tabs:
`#3b82f6` Technology against `#8b5cf6` Communications measures **ΔE 1.3 deuteran** — the two largest
groups here, indistinguishable. Brute-forcing all 256 subsets of the categorical order found only
**four** hues that clear the all-pairs CVD pairlist simultaneously, so four sectors get a hue and the
rest fold into *Other sectors*. `Unknown` also took a positional colour and moved when the chart
reordered; it has a fixed grey now.

**What to check on prod, in order of how quietly it would be wrong:**

- run the CLI steps in *Needs a human* — **baskets first, identities second**. The four funds were
  8.1 pp of the book unattributed at the 08-14 snapshot, so coverage should move from ~78.6% to
  **just under 87%**, less each fund's own residual. Anything much below that means an import
  refused; anything above it means something is being renormalised, which nothing here may do.
- **TSMC must appear once.** SOXQ and SMH are the first baskets to carry the TSM ADR, so the
  `ISSUER_OVERRIDES` entry fires for the first time outside a test.
- GRID's and QTUM's companies should carry `key_type: share_class_figi` after step 2. If they read
  `unidentified`, `OPENFIGI_API_KEY` or the CINS pass is not doing its job.
- the treemap's sector legend, in **both themes**. Four hues plus a grey; each fill carries its own
  ink because dark `--viz-sector-2` measures 2.94:1 against white.

Verified: backend **1112 passed** (+27 adapters, +58 identifiers, +13 resolution), frontend 464,
`tsc -b` and `npm run build` clean, and all four adapters run against the real downloaded files —
SOXQ 33 rows/100.00%, GRID 128/100.00%, QTUM 89/100.04%, SMH 26/100.02%, each imported into a local
DB through the same CLI production uses. **The browser suite was not re-run** — nothing rendered
changed since the morning's run beyond the treemap's fills, which `sectorColors.test.ts` covers.

## Shipped 2026-08-16 — the Look-through tab's two charts

Asked for "some charts, maybe a treemap or a pie chart". Shipped the treemap; **did not ship a
pie**, and the reason is worth keeping: the company ranking is ~50 rows over three orders of
magnitude, which is the distribution angles are worst at. The part-to-whole that a ring *could*
have served is the coverage split, and even there three long-named segments read better as a
horizontal stacked bar at 390px.

- **`Where the value sits`** — one bar: held directly / through funds / not attributed, with each
  segment's value and a sentence saying what it is.
- **`Company exposure`** — a treemap above the existing table, sharing its Top 25/50/100 control.
  Tiles are coloured by how the company is held, and clicking one opens the drill-down that was
  already there. Companies reachable only through a fund are the point of the colour.

What to check on prod once it deploys, in order of how quietly it would be wrong:

- the treemap's grey `Not attributed to a company` tile is **present and large** (~21% of the book
  at today's coverage). If it is missing, the tiles have been renormalised and every company tile
  is overstated — with no axis to give it away.
- the two legends do not share a phrase. The bar says *Held directly*; the treemap says
  *Direct only*. They mean different things and a company held both ways is in both charts at once.
- both themes. The `--viz-*` palette is the first here that is stepped separately for light and
  dark; every other chart hardcodes one set. Verified locally that the two resolve to different
  hexes, which eyeballing the screenshots could not settle.

Verified before pushing: frontend **448 passed** (17 new), `tsc -b` and `npm run build` clean, and
the browser suite against a locally seeded DB — `mobile` 50/50 at 390x844, `a11y` 17/17,
`sweep` 18/18, `csp` 4/4, `chunks` 37/37, `errors` 18/18. The look-through chunk went 3.5 kB → 5.6 kB
gzipped; Recharts was already eager on the Performance tab, so nothing extra is downloaded.

Backend untouched — the endpoint already carried every field both charts read.

## Shipped 2026-08-14 — the Look-through tab: one company, one row

**Asked for a "seethrough" view: how much is actually invested in each company, with the ETFs broken
into their single stocks and share classes like GOOG/GOOGL/ABEA combined — "not by string match, but
a smarter way".** The smarter way is two keyless identifier services, and the interesting part is
that they are complementary rather than redundant.

**What it fixes on the direct side alone**, before any ETF is opened. One company occupies several
rows of Positions in three different shapes:

| shown as | folds on |
|---|---|
| `GOOGL@NASDAQ` + `ABEA@IBIS` | one ISIN — no provider needed at all |
| + `GOOG@NASDAQ` | the **LEI** (a different ISIN, share class C) |
| `ASML@NASDAQ` + `ASML@AEB` | the **LEI** (`USN070592100` NY registry vs `NL0010273215` Amsterdam) |

**Measured on this account's own 25 held ISINs:** OpenFIGI resolved 25/25, GLEIF 20/25 — it has no
ISIN record at all for TSMC, Samsung, SK Hynix, Credo or Marvell, and OpenFIGI covers every one.
GLEIF is what folds *share classes*; OpenFIGI's `shareClassFIGI` is what folds *venues* and is the
only tier that answers for the Asian ordinaries. Neither alone would do.

**Grouping is a union over every identifier, not a precedence chain.** `key = lei or figi or isin`
re-creates the bug the feature exists to fix: when one of a company's ISINs resolves an LEI and its
sibling does not, the two are keyed at different depths and split into two rows *while the response
reports nothing wrong*. Pinned by a test that shuffles the input 25 times.

**Verified end to end against a snapshot of production** (fetched, imported, resolved, deleted
afterwards). The two real bugs it found are worth knowing, because both were invisible on tidy
fixtures:

- **The partition missed by a cent.** Five independently rounded buckets summed to 70,842.40 against
  a total of 70,842.39 — the "never derive a total from rounded rows" rule, broken by my own
  verification. The residual is now published as the rounded remainder. **The first test written for
  it passed against the bug**; a mutation check showed it could not reach the condition, and the
  engineered version now fails 3 of 6 cases when the fix is removed.
- **XNAS produced company rows called *US DOLLAR* and *NASDAQ 100 E-MINI SEP26*.** Xtrackers
  publishes no asset-class column, so cash and futures rows counted as companies. It *does* mark them
  by identifier (`_CURRENCYUSD`, `___ADI34XYM5`), which the adapter now reads. Unidentified company
  groups went 21 → 8.

Two more shapes real files taught us, both now pinned: EMIM ships **five negative cash rows** (KRW
−0.10 and friends — ordinary overdrawn balances), so the negative-weight refusal is scoped to
*invested* rows rather than costing a 4,042-row basket; and XNAS ships a real ISIN with a **blank
name**, so the label falls back to the identifier.

**Coverage, and what it costs to read the page wrongly.** 78.6% of the book is attributed to
companies. The remaining 21.1% is six funds with no basket (VWCE 6,510, SMH 2,391, GRID 1,523,
SOXQ 1,456, QTUM 386) plus DBPG 2,703 excluded outright — it is a *synthetic 2× leveraged* swap ETF
whose published basket is substitute collateral (Mastercard 6.6%, Altria 5.7%, Tesla 4.9% for an
S&P 500 product). Every company figure is therefore an **understatement**, and nothing is rescaled
to disguise that: the coverage figure leads the panel as a `role="alert"` outside every collapsible.

Also worth knowing: VT's weights sum to **91.86%**, and that is *rounding*, not cash — Vanguard
publishes `percentWeight` to 2dp and thousands of its 10,032 holdings round to 0.00.

**The bounded identity rule earns its keep, measured.** The union of six baskets is ~10,400 distinct
constituent ISINs, and resolving all of them would be hours of paced requests. Resolving the **480**
that reach 99.5% of cumulative look-through value took unresolved value from **13,219 to 2,113** —
18.7% of the book down to 3.0% — while the 9,930 ISINs left unresolved are worth about 0.2 each and
cannot move any published figure. Companies folded by LEI went 18 → 355 and by shareClassFIGI 5 →
147. GLEIF had no record for 142 of the 480, which is the same one-in-three gap the held ISINs
showed, so both providers remain load-bearing at constituent scale too. Top ten companies = 49.2%
of the portfolio.

Backend 845 → 976 (+131), frontend 416 → 431.

**The browser suite ran against the snapshot and caught a defect nothing else could.** `mobile.mjs`
reported the Look-through tab pushing the page **1,282px sideways at 390px** and named the element:
the fund table's `reason` column landed in `DataTable`'s phone card as
`<dd class="shrink-0 tabular-nums">`, which is exactly right for a figure and exactly wrong for a
sentence. Prose is now rendered as prose below the table, visible on both viewports — it explains why
a fifth of the portfolio is absent, so hiding it on a phone was not an option. jsdom loads no CSS, so
no unit test could have seen this.

Final suite: `mobile` 50/50, `a11y` 17/17, `sweep` 18/18, `axis` 8/8, `csp` 4/4, `chunks` 37/37
(the new lazy chunk is requested on click, served 200, and mounts clean), `errors` 18/18.
`ledger` is 5/7 for a reason that predates this work — see *Known rough edges*.

**Two defects in the coverage card, both found by an adversarial audit of code written the same day,
both fixed before the push.**

- **The green threshold was above the achievable ceiling.** `coverage_pct >= 95` could not be reached:
  DBPG is 3.8% excluded by design and no basket attributes 100% of its fund, so the practical maximum
  is **95.08–95.86%** — the card would have sat amber forever with under a point of margin, which is
  the always-present-Flex-banner pathology. It now tones on whether any fund is *unresolved*: green
  when every held fund is decomposed or deliberately excluded, amber naming the count that are not.
  Reachable, and it goes amber again the day a fund is bought.
- **A single global staleness threshold meant two different things.** `BASKET_STALE_DAYS = 45` badged
  Vanguard US permanently for publishing month-end with a ~6-week lag, exactly as documented, while
  giving Xtrackers and iShares — which republish *daily* — six weeks of silence. `ADAPTER_STALE_DAYS`
  is per-source now (7 / 7 / 75 / 45), so `†` means "the issuer has newer holdings we failed to fetch".
  The card also names ageing baskets, because staleness deliberately does not move `coverage_pct` and
  a hand-imported VWCE basket would otherwise keep claiming ~9 pp while describing last quarter's index.

Three new frontend tests pin the tone from both sides, and a mutation check confirms all three fail
against the old threshold — the first version of the rounding test earlier in this session passed
against its own bug, so that check is now habit.

**One factual error corrected in docs committed hours earlier:** `etf_sources.py` claimed Vanguard
Europe's robots.txt "disallows automated agents". It does not — every Vanguard EU domain is
`User-agent: * / Disallow:`, allow-all, with only model-training crawlers named. VWCE's blocker is
that **the data is not published**, not that we are forbidden to read it, and stating a policy barrier
that does not exist would stop the next person looking for the route that does.

Two e2e corrections worth knowing. The tab count was hardcoded in **three** scripts plus `lib.mjs`'s
`TABS`, and `csp.mjs` held it **twice** — I updated one and its sibling diagnostic then fired a false
failure against a good build. All three now read `TABS.length`, so a tenth tab cannot desynchronise
them. And raising `errors.mjs`'s floor from 10 to 11 was **wrong**: that count is scoped to the
*Performance* panel, so a new tab can never contribute to it. Reverted, with the new tab given its own
three assertions instead — including that it must not publish a coverage figure during an outage.

**What to check once deployed** — the feature ships with empty tables, so it needs two operational
runs before the tab says anything interesting. See *Needs a human*.

## Shipped 2026-08-08 — the Flex sync stopped asking for a statement IBKR had already made

**Reported as "the flexquery always errors out".** It never was: the query succeeds every single
day. What failed was everything we asked *after* the day's success — IBKR issues about one
generation per US-Eastern calendar day and refuses the rest with `Code=1001` at the SendRequest
step. Two of three scheduled slots plus every manual Sync press, daily, twelve days of twelve with
no counterexample. Each refusal is a failed *generation*, which is exactly what the `Code=1025`
token lockout counts, so the red rows were not merely cosmetic.

**The guard** (`app/services/flex_generation.py`) answers "has today's generation been spent?" from
`sync_runs`, in ET days. `sync_ibkr_data(force=False)` and `POST /api/sync/ibkr` both return
`skipped` / `already_generated_today` without touching the network when it has. Expected effect:
scheduled IBKR errors go from ~2/day to ~0, and the Sync button reports *Already up to date* with
the next available time instead of a red failure.

Two type sets, deliberately different, and getting either backwards is a real bug in the opposite
direction: `FLEX_API_SYNC_TYPES` **excludes** `ibkr_manual_xml` (an offline browser ingest spends no
generation — proven twice, 07-28 and 07-31, where an offline ingest was followed by a *successful*
API generation the same ET day), while `IBKR_SYNC_TYPES` **includes** it (it genuinely refreshes the
data, so it must quieten the staleness alarms).

**The schedule moved at the owner's request**, reaffirmed after the trade-off was put to them twice:

| | before | after |
|---|---|---|
| IBKR primary | 06:00 Berlin (00:00 ET) | **18:00 Berlin (12:00 ET)** |
| IBKR recovery | 08:00 + 00:00 Berlin | 00:00 Berlin only, guarded |
| Yahoo repricing | 8, 11, 13, 15, 18, 20, 22 | **unchanged** |
| 730-day deep pass | 08:00 | 18:00, with IBKR |

Yahoo coverage is byte-identical — only which job makes the 08:00 and 18:00 touches changed. The
`full_sync` job moved rather than a new IBKR job being added at 18:00, because two jobs on one hour
collide on `single_flight`; pairing them also keeps the property that a security the statement
creates is priced by the same run.

**The concern was stated and overruled, which is why it is written down rather than argued again:**
18:00 Berlin captures no additional trades (the window ends yesterday *in US Eastern* and rolls at
midnight ET, so 12:00 ET covers exactly what 00:00 ET does), it is mid-session where the historical
rate is ~1/14, and it takes attempts from three per ET day to two against a 3-day window whose whole
margin is two failed days. `test_every_ibkr_job_avoids_us_market_hours` became
`test_ibkr_jobs_run_at_the_declared_hours` — an explicit allowlist with the reasoning attached, so
drift is still caught but the exception is recorded rather than the rule silently dropped.

**Because of that, `find_flex_generation_gap` shipped with it**: 2 ET days without a successful IBKR
sync, run from the market-data job. `find_stale_ibkr_sync` at 7 days cannot see the failure it was
written for once the period is 3 days — it fires four days after the trades have gone from every
future statement.

Also: `trigger_sync_now` was raising `SyncBusy` → 429 on any `skipped` status, which had only ever
meant a pipeline collision; it now keys on `reason == "pipeline_busy"`, because a day with nothing
left to sync is finished, not busy. And the header's sync panel became `SyncStatusMessage` — three
outcomes now, and the middle one carries no counts, so the old unconditional
`Securities: {securities_synced}` would have rendered "Securities: undefined" under a green tick.
Extracting it is what made that branch testable at all.

Backend 828 → 845, frontend 407 → 416.

## Shipped 2026-08-07 (evening) — two "avg monthly" figures that were never the same quantity

**PUSHED.** Asked whether the contributions averages disagree. **They do not**, and that is worth
recording so it is not re-investigated: the strip's `/deployed` suffix is byte-identical to the figure
`MonthlyDeploymentCard` renders as *12M avg*, and `Σ monthly[].net_eur` holds against the cost basis to
**0.03%** — the per-date FX residual on closed lots under a CHF base, exactly as CLAUDE.md's identity
check predicts, not a dropped lot.

What differed was **labelling**: the strip's headline is money *in* over **all time** (≈2,026) and the
card's is capital *deployed* over **12 months** (≈2,836). Four different (window, metric) pairs, all
called an average per month, with the only distinction in a `title` attribute. The strip now reads
**"Avg Monthly in"** and the `/` suffix has a rendered legend — `CHF2026/2023` reads like one broken
number until you know it is two.

The 3M figure being ~2× the all-time one is real, not an artefact: 12,852 of the last six months'
14,491 in deposits arrived in the last three.

Overflow at 390px was reasoned rather than measured — the widened title is ~185px of muted text in an
already-wrapping flex row, well inside the ~358px card interior and narrower than existing items — so
`e2e/mobile.mjs` was **not** run for it. Worth one pass next time that suite runs against a local
stack. Frontend 403 → 407.

## Shipped 2026-08-07 (afternoon) — a spinoff's tax lots predate the instrument, and it blanked half the returns table

**PUSHED** as `2bace4d`; the deploy guard deferred it past the 20:00 Berlin slot, so it lands ~20:20.
Reported as "the monthly returns are not right": December 2025 through May
2026 blank, November 2025 daggered, and a collapsed summary reading `Aug: +1.5% · YTD: +3.1%`.

The client's arithmetic was faithful — replaying it against the live endpoint reproduced the screenshot
exactly, which is what pointed at the data. **`MBGL` (Mobility Global, spun out of `SPGI` 1-for-1 on
2026-06-30) has tax lots dated 2025-11-06 and 2025-12-29**, because IBKR carries the parent's holding
period over and reallocates 4.84% of its cost basis. Its Yahoo history starts 2026-06-26, at listing.
So `unpriced_holdings` was **1 on 166 consecutive days**, and `isMeasurable` correctly dropped every
one of them.

**Nothing looked wrong anywhere else**, which is why it survived: the stub is 0.2% of the book, so no
total moved. The only surface that showed it was the one figure that depends on *whole* days.

The fix floors each security's valuation start at the corporate action that created it
(`_load_position_start_dates`). The reasoning, the four load-bearing details and why the action set is
much narrower than `SPLIT_LIKE_ACTIONS` are in CLAUDE.md under *A spun-off line is not held before the
action that created it*. Also fixed: the collapsed card summary carried the figure with **no partial
marker at all**, so the dagger and its footnote both lived inside the body almost nobody opens.

**Verified by A/B against a snapshot of the production DB**, same data, floor off then on — every
period that was already fully measured is byte-identical, so nothing else moved:

| period | before | after |
|---|---|---|
| 2025-11 | −0.96%† (3 days) | −1.36% |
| 2025-12 … 2026-05 | — (blank) | −0.10%, +2.32%, −5.50%, −3.64%, +14.41%, +7.46% |
| 2026-06 | +3.86%† (5 days) | +3.96% |
| **YTD 2025** | +30.77%† (to 5 Nov) | **+25.22%** |
| **YTD 2026** | +3.50%† (6 weeks) | **+23.54%** |

Days with `unpriced_holdings > 0`: **166 → 0.**

**What to check once deployed:** the Monthly Returns card should show no `†` and no `–` inside the
holding period, and `/api/portfolio/value-over-time?start_date=2024-05-28` should report
`unpriced_holdings: 0` on every point. If a dagger returns, it is a *different* holding — hover the
cell, which now names the days the figure covers. Backend 818 → 828, frontend 399 → 403.

**Left deliberately alone:** `/api/portfolio/attribution` still excludes-and-counts MBGL for windows
that start before the spinoff. A line that did not exist at the window start has no start value to
attribute against, and doing it properly means combining parent and child — a larger change than this
one. It shows as `unpriced_holdings: 1` on long windows there, and that is honest rather than wrong.

## Shipped 2026-08-07 — IBKR generates one statement a day, and it was quietly starving the deep price pass

**DEPLOYED** as `2c75004` at 06:32 Berlin; `/health` reports it healthy with the scheduler armed and
the job store persistent, and the old gate is confirmed absent from the running container. Backend
814 → 818.

**What to check tomorrow morning**, since this change is only observable on a day IBKR refuses: the
08:00 `full_sync` should record `market_result` as a real object rather than `null` even when its
`ibkr_result` is an error, and `/api/scheduler/history` should show `status: error` alongside it —
the IBKR verdict must not be masked by the Yahoo half succeeding.

Asked "why does the full sync fail, is there an issue with IBKR and Yahoo?" The answer to the second
half is **no** — 32 of 32 `market_data_only` runs succeeded, 40 of 40 securities every time, and not
one rate-limit event in the whole recorded history. The answer to the first half was not the one in
CLAUDE.md.

**IBKR generates this statement about once per ET calendar day, and refuses every later attempt with
`Code=1001`.** Six days of six, with all three slots inside the safe overnight window:

| ET day | 00:00 ET (06:00 Berlin) | 02:00 ET (08:00 Berlin) | 18:00 ET (00:00 Berlin) |
|---|---|---|---|
| 08-01 | **success** | error | error |
| 08-02 | error | **success** | error |
| 08-03 | error | **success** | error |
| 08-04 → 08-06 | **success** | error | error |

Always the earliest attempt that works; everything after it refused. The only two-success ET day in
the entire history is 07-31, the day the query definition was edited.

**This subsumes the mid-session theory rather than refuting it, and that is the interesting part.**
The 07-31 evidence (08:00 at 4/5, 13:00 at 0/6, 20:00 at 1/8) fits *both* readings equally well,
because the mid-session slots were also the later ones — one dataset, two theories, no way to tell
them apart. What discriminates is 08-01 onward, where every slot is overnight and still only one
succeeds. The hour rule stands; it is just no longer the binding constraint. **Adding IBKR slots
does not add freshness, it adds failed generations** — which is exactly what `Code=1025` counts.

**The fix: `full_sync` no longer gates its market-data half on its IBKR half.** That gate turned the
730-day pass into the rarest job in the schedule: 06:00 takes the day's one generation, so 08:00
fails, so the deep backfill had not run since **2026-08-03**. Two independent providers were wired
together for no reason — Flex refusing a statement says nothing about Yahoo, and the securities
needing prices are the ones already in the database.

**What made it invisible is the part worth carrying forward.** The six 7-day slots run
unconditionally and keep *current* value fresh, so no screen looked wrong — only the two-year
history quietly stopped extending, and nothing reports the age of a backfill. It was legible solely
as `market_result: null` buried in `details` on runs already flagged `error` for an unrelated
reason. And a skipped step emits no warnings, so `find_stale_priced_securities` could not fire on
those mornings either: an unpriced holding was structurally undiscoverable on exactly the days IBKR
had refused.

`status` still reports the **IBKR** verdict, so a green Yahoo half cannot paper over a refused
statement. Four tests pin it from every side — prices on failure, still reports the failure, still
prices *after* IBKR rather than before, and market warnings now survive a failed IBKR half.

**Verified on production the same morning** (owner's explicit permission for the Yahoo call): a
manual 730-day pass covered **43 of 43 securities, 1,615 prices, no rate limiting**, and the three
new ETFs each pulled a full two-year history. `unpriced_holdings` 3 → **0**.

## Shipped 2026-08-04 (later) — the chart was reserving a fifth of itself for nothing

**The portfolio chart's Y axis ran to −20,000 on a phone and −10,000 on desktop while no series was
ever negative.** Reported as "−20k will not be reached anyway"; it turned out to be worse than a
cosmetic preference, because the data could not reach it *even in principle*.

Read off `/api/portfolio/value-over-time`: the three default series spanned **+1,122 … +65,025** —
the profit line's minimum is positive, and it has never been negative in the whole series. But the
padding is `minValue − range × 10%`, a share of the **whole** range, which the market-value line
dominates. 10% of 63.9k is 6.4k, so the padded minimum came out at **−5,268**, and `niceTicks`
rounds the minimum *out* to a step multiple — against the 20k step a 4-tick phone axis picks, that
floors to **−20,000**. A fifth of the plot height, permanently blank.

`axisFloor()` now bounds it, with the cap the user asked for and one rule they did not:

- **Nothing negative in the data → floor at zero.** This is the case that was actually wrong, and it
  removes the whole band rather than shrinking it.
- **Something negative → cap at −5k, unless a real value is lower**, in which case the value wins.
  The cap is deliberately *soft*: empty space is cosmetic, a loss clipped off the bottom of the
  chart is a wrong number. `axisFloor` can never return a value above `minValue`, and that property
  is pinned across a range of minima rather than at one point.

The floor is applied **after** rounding, not to the input, because rounding-outward is precisely
what has to be overridden — clamping the input still floors back to the same multiple. A test
reproduces the −20,000 with no floor passed, so the fix cannot end up measuring itself, and another
asserts existing callers are byte-identical when the argument is omitted. Frontend 308 → 316.

## Shipped 2026-08-04 (late) — three things found by looking at what a pass reports

Deployed as `0117d76` (chart axis) plus follow-ups. All verified against production:
`axis` 8/8, `a11y` 17/17, `sweep` 16/16, `mobile` 45/45; backend 684 → 689, frontend 316.

- **`settings.log_level` configured nothing.** It was read in exactly one place —
  `echo=settings.log_level == "DEBUG"` for SQLAlchemy — and no code ever called into the
  logging module, so the root logger kept its default and Python's *last-resort* handler
  emitted WARNING and above only. **Every `logger.info` in the app was discarded in
  production**, confirmed by reading the container log: uvicorn's access lines and alembic
  present, not one line from `app.*`. That is worse than a missing feature because the
  docs assume otherwise — CLAUDE.md tells you to grep the container log for a request id,
  and the scheduler's `removing retired job` / `kept, next run:` lines (the only direct
  evidence that pruning and misfire recovery work) are INFO. Part of why a job store that
  had never opened looked healthy for two days. Needs `force=True`: uvicorn installs
  handlers before the app is imported, and `basicConfig` is a no-op when one exists.
  Chatty providers (yfinance above all) are held at WARNING or the volume would triple.
- **Docker had no log rotation**, which only became a problem once the above started
  emitting. The default `json-file` driver is unbounded and this VPS's disk also holds the
  database and its backups; now capped at 10 MB × 5.
- **`prices_fetched` counted rows in the window, not rows written** — 234 reported for
  ~80 real writes, via a second full-range SELECT per security whose result nobody could
  act on. The docstring already promised writes.

## Shipped 2026-08-04 (late) — the sixteen KPI cards are one component

*Worth doing next* item 0, done. `ui/KpiCard.tsx` replaces the hand-written card in
`PortfolioSummaryCards` (5), `PerformanceMetricsCards` (6) and `RiskMetricsCards` (5) — 526 lines of
component down to 357 plus a 111-line primitive, and more to the point one place to edit instead of
sixteen. `RiskMetricsCards.test.tsx`'s existing 13 assertions pass **unchanged** against the
primitive, which is what says the render was reproduced rather than reinterpreted.

Two judgements worth knowing:

- **`DividendKpiCards`' `Tile` was deliberately left alone**, though the old entry named it as a
  fourth copy. It is not the same card: `bg-card/50` rather than `bg-card`, an `text-xs` label, a
  `font-semibold` value, and a footer row whose `flex-wrap` and `min-h` exist because the
  month + MoM + YoY chips are wider than a 155px phone tile and pushed the page into horizontal
  scroll unwrapped. Folding it in would have merged two things that only look alike and changed the
  Dividends tab's appearance for no gain.
- **One visible change: `N/A` became `—`** on Annual Return and Calmar Ratio when those are null.
  Three of the four files already agreed an absent metric must not render as `0`, and then disagreed
  on how to say so — an em dash in `RiskMetrics`, `N/A` twice in `PerformanceMetrics`. Absence is now
  a single code path (`value={null}`), and it also **refuses to colour a dash**: a caller computing
  `tone` from a number it has not null-checked would otherwise paint the missing value green.

`KpiCard.test.tsx` carries the primitive's contract plus the backstop — a source scan for the value
class outside `ui/`, the `noRawTables` pattern from `tableFamily.test.tsx`. It uses
`import.meta.glob`, not `node:fs`: the frontend tsconfig is browser-targeted with no `@types/node`,
so fs type-checks under vitest and then **fails `npm run build`**, breaking the deploy rather than
the suite. Frontend 316 → 329.

## Shipped 2026-08-04 (late) — the portfolio's dividend rate — DEPLOYED and verified

Two cards on the Performance tab's risk row — *Dividend Yield* (projected next-12-month income over
market value) and *Yield on Cost* (the same income over cost basis) — in place of *Effective
Holdings*, which moved into the *Top 5 Weight* footnote so the metric survives without a card. The
row went 5-up to 6-up, matching the grid the row above already uses. Per-security `Fwd yield` column
on the Dividends tab is the audit of the headline. Backend 689 → 699, frontend 329 → 342.

**The premise was that Yahoo already gives us per-security yields; it does not, and that is now
written down in CLAUDE.md.** No dividend field exists anywhere in the backend, `fundamental_metrics`
is on-demand only, and adding a Yahoo yield would have meant a migration plus a fundamentals pass
before the cards showed anything — and a second annual-dividend-rate implementation beside the
forecast. Everything needed was already in one service call.

Three things a review caught that had already been written and were wrong:

- **Hoisting the positions fetch to build `growth` in one place was a latent 500.** It is allowed to
  degrade to "yields omitted" only because it is the *last* DB access in the method: a DBAPI error
  leaves the session needing a rollback, so the `earliest_open` query after it would have raised
  `PendingRollbackError` and taken the whole endpoint down. Reverted, with a comment saying why the
  uglier ordering is the correct one.
- **`0.00%` was reachable and was a lie.** The smoke fixture's only forecaster is its unpriced TSMC
  row, so the priced holdings projected nothing and the yield came out a confident zero meaning "the
  interesting holding is missing". The object is `None` whenever the numerator is zero.
- **Gating the row's `isLoading` on the dividend query hid four already-computed metrics** behind a
  third request. The cards carry their own three states instead.

**Verified on production**, in the browser as well as on the wire: both cards render, `pct` and
`on_cost_pct` each reproduce exactly when hand-divided against `/api/portfolio/summary`,
`unpriced_holdings` is 0, `basis` is `mixed`, and `mobile.mjs` passes 45/45 at 390px with the row's
longest footnote.

**The check worth repeating** if either card ever looks wrong:
`annual_eur ÷ summary.total_market_value_eur × 100` must equal `pct`, and `on_cost_pct ÷ pct` must
equal market value ÷ cost basis. The denominators come from a different code path than the *Market
Value* card's; they were byte-identical when this shipped, and if they drift the two disagree in a
way a user can see. Beware comparing a `pct` to an `annual_eur` fetched minutes apart — the window
rolls with `as_of`, so the total moves by a cent or two across a date boundary. That is the rolling
figure working, not a rounding bug.

## Shipped 2026-08-05 — the permanent sync warning, and a yield on cost that punished buying more

Two reported issues, both real, and neither where it looked.

**1. The 27-attribute warning on every sync.** The sanitizer was working exactly as designed — ibflex
0.15 cannot model `figi`, `serialNumber`, `weight`, `subCategory`, `Trade.notes` and the rest, and
dropping them is what stops one schema addition aborting the whole document. The defect was that all
of it went into `warnings[]`, so a healthy sync carried a permanent unreadable banner. **A warning
that is always present and never actionable trains the reader to skip the banner** — the same banner
that carries a skipped tax lot or an unconvertible dividend, which is the only reason it exists.

Drops are now classified by consequence: loud when the attribute is one the extractors read
(`INGESTED_ATTRS`), recorded in the run's `details` as `flex_schema_notes` otherwise. All 27 on this
account are cosmetic, so the banner should be **empty** after the next sync.

The guard matters more than the fix, because the two directions are not symmetric — a spurious entry
is merely noisy, a missing one makes a real problem silent. `tests/test_flex_attr_coverage.py`
AST-walks the extractors and intersects with ibflex's own dataclass fields rather than trusting the
map, **and caught a genuine omission on its first run**: `extract_transfers` reads `Transfer.date`,
which the hand-written map had discarded as a Python builtin.

**2. Yield on cost fell when you added to a holding.** Asked about sell-and-rebuy; the same defect was
already live on **nine of fifteen rows**. It divided income *already received* by *current* cost, and
those describe different positions once the size changes: MCO read 0.35% against a real forward rate
of 0.84%, SPGI 0.53% against 0.93%. Unbadged, too — the `†` partial marker was only ever on the
trailing yield column. It also disagreed with the Performance card, which has always been
forward-over-cost: one name, two definitions, two screens.

Now forward-over-cost everywhere, so the gap against the yield beside it is appreciation and nothing
else. Sell-and-rebuy at a higher price still lowers it, which is the honest answer — more capital
committed for the same income — but it now equals exactly the new cost's rate rather than a blend.

Backend 705 → 723.

**Yield on cost is verified on production.** All 17 rows carrying both figures satisfy
`yield_on_cost_pct ÷ forward_yield_pct == market value ÷ cost` to within 2dp rounding, and the
understated rows recovered as predicted: MCO 0.33 → 0.79, SPGI 0.46 → 0.80, MA 0.25 → 0.62,
MRVL 0.12 → 0.28. Four securities that had *no* yield on cost now have one, because they carry a
projection but no trailing income yet.

**The empty banner is verified against the deployed build**, without spending an IBKR request. The
08:00 run after the deploy returned a routine `Code=1001` and correctly declined to re-request, so
waiting on a real statement was not an option and retrying is precisely what trips `1025`. Instead
`IBKRService.parse_flex_xml` — the entry point every sync and the offline CLI both use — was run
inside the production container against a statement carrying this account's own drift:

| input | `flex_warnings` (the banner) | `flex_notes` |
|---|---|---|
| 38 unmodelled attributes across `<Trade>` / `<CashTransaction>` | **empty** | 1 compact line |
| `CashTransaction.type="Broker Fees"` (a field the ingest reads) | 1 warning, *data may be affected* | not filed as harmless |

Pure — no network, no DB, no token, nothing written. The last hop is confirmed present in the running
container (`sync_helper.py:176` copies `flex_notes` → `flex_schema_notes`) and is driven end to end by
`tests/test_manual_xml_ingest.py`, which goes through the same `parse_flex_xml` +
`ingest_flex_statement` pair the scheduled job does.

**Confirmed against a real statement on 2026-08-07** — the one thing the constructed document could
not settle. An `ibkr_manual_xml` ingest of a genuine Client Portal download returned `flex_warnings`
**empty** and filed all **26** unmodelled attributes (`figi`, `serialNumber`, `subCategory`,
`Trade.rtn`, `initialInvestment`, …) into `details.flex_schema_notes` as one compact line. The
account's real drift is the drift we modelled, and the permanent banner is gone.

Note `/api/scheduler/history` names the field **`type`**, not `sync_type` — reading the wrong key
makes every run look untyped, which briefly looked like a second bug and was not one.

## Shipped 2026-08-05 (later still) — the Activity ledger showed every dividend twice

**Found by reading the screen and disbelieving a number** — "why are there 2026 dividends marked *est.*
when the transfer happened in January?" The labels were the symptom. The defect: the same dividend was
listed **twice**, once as the yfinance estimate under its ex-date and once as the IBKR actual under its
pay date a fortnight later. `GOOGL est. 06-08` beside `GOOGL 06-15`, `SPGI est. 05-29` beside `06-10`.

`ActivityService._dividends` was the only reader not applying `_splice_by_era`. Measured before the fix,
era boundary 2026-02-18: **31 duplicate rows, 47 of 113 CHF — dividend income overstated 72%.**

Nothing the app *computes* was wrong. The breakdown, the summary card, XIRR and the tax report all
splice, which is exactly why this survived: the only wrong surface was the one that merely displays.

Two things worth carrying forward:

- **The boundary cannot come from the window.** `_splice_by_era` derives `min(ibkr_dates)` from the rows
  handed to it — right for readers that splice the whole history, wrong for the ledger, which windows
  first. So `DividendRepository.earliest_ibkr_payment_date()` now exists, mirroring
  `CashFlowRepository.earliest_flow_date()`. `_splice_by_era(get_between(...))` is the obvious-looking
  form and is the bug; a test fails if anyone writes it.
- **Partial alignment is the nastiest form of the duplication failure.** The docstring said zero rows
  are excluded "on the same test the two dividend readers use" — singular. It was written *with* the
  readers open and copied one of their two rules, so it reads as deliberate rather than forgotten.

**Expect the ledger to change visibly**: 31 fewer dividend rows and a dividend total falling from ~113
to ~66 CHF. That is the correction, not data loss. Pre-boundary estimates (29 rows before 2026-02-18)
are still there and still badged — dropping those is the mirror-image bug, which once blanked every
pre-IBKR month from the dividend card.

Also: **yield on cost is now a column in the Positions table**, from the breakdown the Dashboard already
fetches, so it costs no request. 19 of 36 rows show a figure and 17 show a dash — a holding that
distributes nothing has no rate, and every accumulating ETF reads that way correctly. Sortable, with
absent sorting below any real yield in the default descending order. `detailLimit` went 4 → 5 so the new
detail row does not push Weight behind the phone's "Show all" disclosure. `PositionsList` gained the
test file it never had, and the latent `useMemo` dep omission in its sort (`totalMarketValue` was
already missing) is fixed — adding a case that reads a separately-fetched map would have made it bite.

Backend 723 → 726, frontend 343 → 352.

## Shipped 2026-08-06 — the /loop audit batch, pushed after being held

**Pushed and deployed on 2026-08-06 evening.** They were batched rather than shipped one at a time
because `deploy.sh` does a full `down` + `build --no-cache`, so each push costs ~90s of downtime and a
10-minute audit loop pushing every pass would have taken the dashboard down ~9 minutes an hour. None
was wrong on current data, which is what made holding them safe.

The batch rebased onto one remote docs commit, conflicting only in this file's header. **Nothing in
the sixteen threads below is outstanding** — they are kept as the record of what moved and why.

**Checked on production 2026-08-06 straight after the deploy** — the first two are confirmed, the
rest are what to look at next:

- ✅ **The Activity tab lost about half its dividend rows** (Thread 14, compounded by Thread 13's
  boundary-duplicate match): **87 → 44**. That is the correction, not data loss. The check worth
  keeping is the *relationship* rather than a franc total, which goes stale silently: no row with
  `source == 'yfinance_estimate'` may be dated on or after the era boundary. Latest surviving estimate
  reads 2026-01-09 against a boundary of 2026-02-18.
- ✅ **`unpriced_holdings` is on the summary, the timeline and attribution**, and currently reports
  **0** — so the headline total covers the whole book. A yellow notice above the KPI cards is what a
  non-zero looks like.
- **Yield on cost should rise on nine of fifteen rows** (it was dividing received income by current
  cost). Not yet eyeballed.
- **Sharpe, Top 5 Weight and RSI now refuse rather than substituting a plausible number** — expect
  dashes where a `0.00` or a green `0.0%` used to sit on short ranges.

**Two were a matched pair** (`adb992c` adds the completeness signal, `557f82d` acts on it), so they
had to ship together; they did.

### Thread 1 — a zero standing in for "unknown"

The codebase's most repeated bug, found three more times. The refinement worth keeping is **what the
stand-in value would claim**: a `0` volatility looks broken and gets noticed, a `0.00` Sharpe looks like
an answer, and a `0.0%` concentration looks like a *good* answer. **Severity tracks plausibility, not
magnitude** — which is why the concentration one sat in plain sight beside two cards already fixed for
the identical flaw.

- `5f824c6` **Sharpe returned 0** below the minimum sample. Reachable in one click: MTD in the first days
  of a month leaves 2–3 daily returns, and the card drew a green `0.00` captioned *Risk-adjusted return*
  beside a dashed Volatility and Sortino. Its clamp test had also been passing vacuously off the same
  early return.
- `244baa8` **Top 5 Weight drew a green `0.0%`** when nothing was priced — the tone ladder calls anything
  under 50% good news.
- `03a3a48` **`days_held_in_ttm` measured time since first purchase**, not time held, so a sell-and-rebuy
  with a gap reported full coverage and the partial-yield badge never fired.

### Thread 2 — an incomplete sum presented as a complete one

`portfolio_service` values an unpriced holding at 0.00 while its cost still counts, so every total built
that way understates. `find_stale_priced_securities` guarded only the current snapshot.

- `adb992c` **the timeline** — measured on production: at +14 days past the last cached price the total
  read a plausible **+15.3%**, at +15 days **−100%**. That is what a stalled market-data sync looks like:
  a smooth decay to zero, not a gap. Each point now carries `unpriced_holdings`.
- `557f82d` **and it poisoned every risk metric**, not just the line — a complete→incomplete pair is a
  −100% daily return, and `dailyReturnSeries` feeds drawdown, volatility, Sharpe, Sortino. `beta` needed
  the guard separately.
- `eb21c9e` **the headline Market Value**, which is the SBI incident restated: 446.93 CHF once left that
  figure with only a sync warning to catch it.
- `b26f75f` **a missing FX rate slipped past the client's unpriced guard** as a 0% weight, so drift
  advised buying the whole target. It survived because its comment justified the narrow predicate with a
  *false* fact — that a fully-sold holding reaches the client, which `is_open == True` prevents.

### Thread 3 — a fix justified by a false reading of the code it copied

- **`sync_stale_fundamentals` could never bootstrap a security** (newest in the batch). It pre-filtered on
  `get_stale_metrics`, which selects rows that already **exist**, and bailed when that came back
  empty — while the union that would have caught a security with no row sat one call *below* the
  guard. So whenever every existing row was fresh, a newly-bought security never acquired
  fundamentals through `POST /api/fundamentals/sync-stale` at all.

  **`sync_stale_ratings` was fixed for this exact shape and cited this method as the sibling that
  "already unions the two sets".** So did CLAUDE.md's duplicated-logic table, and so did the ratings
  bootstrap test's opening paragraph. All three were describing the *inner* function while the entry
  point pre-filtered. Three places asserting a fix that was not there is what kept it alive; all three
  are corrected. **When citing a sibling as correct, read its entry point.**

  Masked on production today only because all 40 fundamentals rows are stale, so the guard happens to
  pass. One fundamentals run would have hidden the next new security indefinitely.

### Thread 4 — a latent 100× money error

- `976e15b` **a pence quote stored as pounds.** Yahoo reports London in `GBp`; the code `.upper()`'d it to
  `GBP` and left the amount alone. Worse than the factor: normalising the label **defeats the currency
  guard** rather than tripping it, since the normalised code matches the security's own. Latent — this
  account holds no GBP security and its one London line is a USD ETF — but three LSE codes already map
  to `.L`.

### Thread 5 — a failure rendered as an absence

- `bc3cbae` **twelve metrics vanished on a backend error instead of saying so.**
  `PerformanceMetricsCards` and `RiskMetricsCards` returned `null` whenever `metrics` was null, and
  Dashboard's memos return null when their query fails — so an outage did not produce an error state
  on those two rows, it produced **nothing**, and the twelve metrics were simply not on the page. A
  row that disappears is worse than one that fails visibly: a stated failure invites a retry, a
  missing row reads as a feature that was never built. `PortfolioSummaryCards`, in the same folder,
  had the correct branch all along.
  **`e2e/errors.mjs` is the proof and also shows how it hid:** its count of panels reporting the
  failure went **8 → 10**, so the two surfaces it existed to cover had never been in its own tally —
  under `hits >= 4`, a floor far enough below the real count that it could not fail. Now `>= 10`.
  The `null` return survives for the genuine no-data-yet case, pinned by its own test, or the fix
  would turn every empty portfolio into a reported outage.

### Thread 6 — a chart that summed to less than it claimed

- **Two of the three allocation charts dropped a holding whose category is unknown.**
  `get_portfolio_allocation` buckets each position three times; asset type used
  `security.asset_type or 'Unknown'` while sector and geography used `if security.sector:` /
  `if security.country:` and simply skipped it. So those two summed to under 100% while
  `AllocationTab` printed every slice as *"% of portfolio"* — and the treemap sizes by area, so it
  renormalised and still drew a full rectangle. The picture looked complete; only the printed
  percentages were short.
  **The trigger is routine, not theoretical.** `sync_helper` never writes `sector` or `country`, so
  every IBKR-ingested security starts with both NULL while `asset_type` has a `"Stock"` column
  default — a newly bought holding appeared in the asset-type chart and in neither of the others.
  Only `sync_allocation_data` fills them and **nothing schedules it** (it needs Yahoo), so the gap
  lasted until someone ran it by hand.
  Both now use `or 'Unknown'`, the convention three lines above them, and `AllocationTab` already had
  a grey colour defined for that bucket. Five tests, written against the **family** — *every*
  breakdown the endpoint returns must sum to 100% — so a fourth chart is held to the rule without
  anyone remembering to add a case.
  **Checked against production before fixing:** all three currently sum to 100.00/100.01, so nothing
  on screen is wrong today. This is hardening against the next purchase, not a live correction.

### Thread 7 — the consumer missed when its siblings were guarded

- **`computeModifiedDietzReturn` had no unpriced-day guard**, so the Monthly Returns heatmap and its
  **YTD column** were still exposed to the stalled-feed failure the risk row had just been protected
  from. Modified Dietz reads only the two endpoint market values and the flows between them, so an
  incomplete endpoint is not a small error but the whole answer: a stale month end reads as a loss, a
  stale start as a gain, and past the backend's 14-day lookback the period prints **-100%**. YTD is the
  worst case — it ends on *today*, exactly the day a stalled feed breaks.
  It now **trims** leading and trailing unmeasurable points rather than refusing the period. That is
  exact, not approximate: Dietz never reads an interior market value, so the result over the kept days
  is a true return for those days, and discarding a whole month over one stale day would lose more than
  it protects. `partial` rides on the result, the cell is badged `†`, and a footnote explains it —
  a caveat living only in a `title` attribute does not exist on a phone.
  **The lesson is the miss, not the fix.** "I guarded the consumers of `unpriced_holdings`" was true
  and incomplete on the same day: two were found by reading the risk row, and the third only turned up
  by listing every importer of the timeline type. Grep the type, not the screen.

### Thread 8 — two numbers under one name, on one screen

- **`MonthlyDeploymentCard` recomputed the 12-month deployment average** that
  `ContributionsStrip` — a few hundred pixels above it on the same tab — already renders from the
  server's `avg_deployed_per_month_eur`. On live data they read **2,546 and 2,530**: close enough
  that neither looks wrong, far enough apart to be visibly different once rounded.
  The client's version was wrong twice over, both times upward. `monthly` is built from a dict keyed
  only by months that had activity, so a quiet month is simply absent: `slice(-12)` takes the last
  twelve *rows* (which can span more than twelve months) and then divides by that row count rather
  than by the months covered. The server divides by the window's elapsed months, clamped to available
  history, and reports the clamp via `partial`.
  The card now reads the server's figure and names the shorter window when `partial` is set, so a
  four-month-old portfolio stops claiming a twelve-month average. Five tests, including one that
  seeds a six-month gap in `monthly` to prove the rows can no longer influence it.
  **Not currently wrong on this account** — 27 monthly rows from 2024-05 to 2026-07 with no calendar
  gaps, so only the rolling-vs-calendar boundary separated the two figures.

### Thread 9 — a sweep that mostly confirmed things, and two unpinned invariants

This pass found **no live miscalculation**. What it did find were two rules the codebase already
states, applied incompletely — both now pinned.

- **`Dashboard` trusted the *shape* of its stored benchmark selection.** `JSON.parse` was wrapped in
  a `try/catch`, which covers malformed JSON but not well-formed JSON of the wrong shape: `42` and
  `{"a":1}` both parse cleanly and were handed back as `string[]`. `selectedBenchmarks.map(...)`
  feeds `useQueries`, so a non-array throws inside the **root** component rather than a tab — and
  because the value is re-read on every mount, reloading cannot recover it. Clearing site data would
  be the only way out. `RebalanceCard.readTargets` already draws this line for one tab; this is the
  same reader with a larger blast radius. Six tests, including the property that matters: whatever is
  stored, the result must be mappable.
- **`MAX_RANGE_DAYS` was a constant written in two languages with nothing holding them together.**
  The ALL button clamps to `365 * 5` so it never asks for a span the router rejects with a 400, and
  the clamp lands *on* the boundary. Tighten the server and ALL starts 400ing for anyone with enough
  history, with both suites still green. `tests/test_range_limit_agreement.py` reads both files and
  pins them equal — same shape as `breakpoints.test.ts` and `test_deploy_guard_hours.py`.

**Verified correct and not worth re-chasing** (each looked like a defect and was not):
`allocation_service` has no `BaseFx` but inherits the projection from `get_positions_breakdown` —
its total matches `/summary` to the cent under CHF. `ActivityService`'s `counts_as_money_in` uses the
same `DEPOSITWITHDRAW` whitelist constant as `get_deposits()`. `cash_flows.amount_eur` is NOT NULL and
ingest skips rather than storing a null, so the ledger's conversion cannot meet a `None`. Benchmark
timeline points survive market holidays through the 14-day carry-forward (523 points for ~522 business
days), and its cost-basis line tracks the portfolio's to within a sign-changing FX residual, so no lots
are being dropped. The tax report and the dividend breakdown agree to the cent for 2026 (48.14 CHF),
so both apply the era splice. The 0.01 between the dividend chart's month totals and its per-symbol
stack is 2dp rounding across twelve rows, not a gap.

### Thread 10 — rule 1 was enforced in one service out of six

The largest finding of the loop, and it sits on the project's most important rule.

- **Five Yahoo loops kept asking after a 429.** `market_data_service` latches and
  abandons the pass; `fundamentals` (~5 endpoints per security), `analyst ratings`,
  `watchlist`, `allocation`, `dividends` and the scheduler's benchmark warm-up all
  caught the error, logged it, and hit the same IP again seconds later — the exact shape
  fixed for market data on 2026-08-04, five times over. Continuing is what turns a short
  block into a long one.
  Extracted to `app/services/yahoo_rate_limit.py`; `tests/test_yahoo_rate_limit_family.py`
  walks the **AST** for any module importing `yfinance` without consulting it, so a
  seventh service is caught automatically. **Allocation needed the most care**: its
  failure path stamps `allocation_last_updated` to bound retries, so a rate limit would
  have marked every remaining security attempted and suppressed its sector and country
  for the full staleness window — the check runs before the stamp.
- **A pre-existing crash the new tests exposed: two failures in one pass killed it.**
  The handlers call `db.rollback()`, which expires **every** object in the session, so
  the next iteration's `security.symbol` became a lazy refresh — and in async SQLAlchemy
  that raises `MissingGreenlet`. In fundamentals and ratings that read sits *outside*
  the try, so it propagated out of the sync entirely. One security Yahoo has no data for
  is completely ordinary, which made this reachable on any pass with two of them. Each
  loop now reloads through an awaited `db.get`.
- **`_to_eur`'s third site.** After the tax copy and then this file's own copy were both
  fixed to return `None` on FX failure, `compute_dividend_income` still carried the
  original `gross_eur = gross_amount  # fallback: store unconverted`, a few dozen lines
  below the helper it never called. Worse than the other two: it is an **ingest** path,
  so the foreign figure is *persisted* into `gross_amount_eur`/`net_amount_eur` and then
  read by the Dividends tab, the forecast, the forward yield and the tax report's DA-1
  income. The row is now left uncomputed — `shares_held IS NULL` is the sentinel the
  prune CLI already refuses to delete, so it retries once a rate exists.
  **Latent, not live**: TSMC reads 0.80% forward yield on production, so its TWD rows
  converted correctly. `WARM_CURRENCIES` keeps TWD fresh and pre-ownership history is
  skipped, which is what has kept it out of reach.

### Thread 11 — the completeness gap on the one chart that names each security

- **Performance attribution counted an unvaluable holding at zero.** `get_eur_value` returned `0.0`
  when the price *or* the FX rate was missing, and `value_change = end_mv - start_mv`, so a still-held
  position whose feed went stale read as **`-start_value`** — the same shape the disposal term fixed
  for sales, reached by the other route and never covered. An unvaluable *start* fabricates a gain the
  same size.
  This is the worst surface for it: one bar per security, so the fabricated number is the largest bar
  on the chart under the security's own name — not buried in a total. It also inflated every other
  security's `weight_percent` (the denominator was missing the zeroed holding's value) and shifted
  `contribution_percent` through a moved `total_pnl`.
  Unvaluable securities are now excluded from both sides and reported as `unpriced_holdings`, the same
  name and signal as the timeline and the summary. A lot held at *neither* endpoint never reaches the
  helper, so a fully-sold position keeps its legitimate zero — that is what makes exclusion safe.
  **The notice had to move out of the collapsible.** The card is collapsed by default and its
  collapsed summary shows `total_pnl_eur`, the figure the notice qualifies; a caveat you must expand a
  card to see is as good as absent.
  Also: `PerformanceAttributionResponse` had to declare the field or the `response_model` would have
  filtered it off the wire — the trap `test_dividend_summary_contract.py` exists for.
  The smoke fixture's unpriced TSMC makes the new assertion the **non-zero** case rather than one that
  would pass on any book.

### Thread 12 — the wealth-tax base could omit a holding and not say so

- **`holdings_snapshot_as_of` dropped an unvaluable lot silently**, and the tax report summed
  whatever it got. Omitting is the right arithmetic — CLAUDE.md says so, and counting the holding at
  zero would be worse — but the report already treated a snapshot that **raised** as a stated failure
  (`holdings_snapshot_total: None` plus a warning) while a snapshot that quietly returned fewer rows
  produced a plausible number that reads as the complete book. Loud on total failure, mute on partial
  failure, which is backwards: this is the same asymmetry that made the value timeline's `+15.3%` more
  dangerous than its `-100%`, on the one figure in the app that goes on a tax return.
  `last_snapshot_skipped` is a per-run latch naming the dropped securities; the report turns it into a
  `warnings[]` line — the surface `TaxTab` renders as a banner and `to_csv` writes as a WARNINGS block
  — and still serves the figure, because a partial base is the best available and must not be confused
  with the `None` that means no base at all.
  **The tests caught a bug in the fix**: the latch was not reset on the early-return path, so an empty
  snapshot would have inherited a previous date's skip list and reported the wrong date's
  completeness.
  **And `test_api_smoke` was pinning the bug** — it asserted `tax["warnings"] == []` on a fixture
  whose Steuerwert genuinely omits its unpriced TSMC. That expectation now asserts the warning is
  present and names the security; the clean-report direction moved to a fixture that is actually
  clean.

### Thread 13 — the era splice leaked one dividend per security, at the boundary

- **13.7% of 2026's dividend income was double-counted**, on every reader that splices.
  `_splice_by_era` keeps estimates strictly before the first IBKR payment — but the two sources file
  the *same* payment under different dates (yfinance by ex-date, IBKR by pay-date), so the first IBKR
  payment's own estimate sits before the boundary and survives beside the IBKR row it duplicates.
  Found by reading the Activity ledger, boundary 2026-02-18, ASML held on two exchanges:
  `02-09` + `02-10` estimates next to two `02-18` IBKR rows. **Four rows for two dividends.**
  Measured: 2026 read **48.14 CHF**, of which **5.80** is the duplicate pair → **42.34 CHF** correct.
  It moved the breakdown, the summary card, XIRR's dividend inflows, the tax report's DA-1 income and
  the ledger simultaneously, which is why nothing disagreed and nothing caught it.
  Now matched per security, nearest-first, **one-to-one**, bounded by `EX_TO_PAY_MAX_LAG_DAYS` (30).
  One-to-one is what makes the window safe for a monthly payer whose cycle is shorter than it.
  **The width errs toward keeping**: 45 was tried first and deleted a genuine dividend 45 days out —
  too wide removes real income from a filing aid, too narrow leaves a visible, already-badged
  duplicate, and understating taxable income is the worse failure.
  The account's one genuine pre-boundary estimate (MA, 40 days before the boundary) is preserved.

### Thread 14 — the fix from Thread 13 did not reach the ledger

- **`ActivityService._dividends` reimplemented the boundary rule inline**, for a good reason: it
  windows before splicing and so needs the whole-table boundary. That copy was correct the day it
  was written (Thread 13's predecessor) and silently wrong two days later, the moment the shared
  helper gained its duplicate match — every other reader stopped showing the ASML pair and the
  ledger kept showing it. **A copy of a rule stays correct only until the rule changes**, which is
  this codebase's oldest lesson, relearned here on a two-day-old copy of my own.
  `_splice_by_era` now takes an explicit `boundary`, so the windowing caller is a real caller
  instead of a copy, and the ledger widens its fetch by `EX_TO_PAY_MAX_LAG_DAYS` on both sides and
  narrows back afterwards — the IBKR row that pairs with a windowed estimate can fall outside the
  window even when the estimate does not, so asking for 1–15 February would otherwise resurrect it.
  `test_era_splice_boundary.py` now **fails any service that reads dividend rows without reaching
  the helper**, which is the guard that would have caught this class both times.

### Thread 15 — the other two rule-copies in the same file

- **`ActivityService` also reimplemented `_net_eur` and `_is_income`.** Both agreed with the helpers
  to the digit, which is why an earlier pass of this loop looked at them and moved on. That judgement
  was wrong for the reason Thread 14 demonstrated on a two-day-old copy: **agreement is what a copy
  looks like right until the rule moves.** `_net_eur`'s own docstring says "every consumer must" use
  it. Both now call the helpers, and the structural guard covers all three rules — no service may read
  dividend rows without reaching `_splice_by_era`, and none may decide the net-vs-gross fallback
  locally.

**Verified and not drifted** (checked rather than assumed, since CLAUDE.md asserts it):
`lib/dividendGrowth.ts` still matches `DividendService._pct` and the annual-row loop on all four
copied rules — adjacency, the zero-base refusal, 1-decimal rounding, and `yoy_vs_partial` reading the
*previous* row's flag only when adjacent. It remains the one duplicate that has survived, because both
ends write their reasoning down.

### Thread 16 — a stand-in that claimed a maximum rather than a zero

- **`_compute_rsi` returned 100 for a series that never moved.** `avg_loss == 0` was treated as one
  case when it is two: gains with no losses is a real, maximal RSI, while *nothing moving* leaves RSI
  undefined. Returning 100 claims the strongest overbought reading the scale has.
  The cost is concrete, because `_compute_buy_score` reads it: `rsi = 100` scores **0 of 10** on
  technical timing while `rsi = None` scores a neutral **5**. The fabricated value was ten points
  worse than admitting the metric could not be measured — and the neutral branch already existed,
  which makes this a substitution rather than a missing case.
  Reachable on a halted or delisted listing, a fixed-NAV fund or a very illiquid one, and the
  watchlist is where arbitrary tickers get added. Every previous instance of this lens found a zero
  standing in for unknown; this one is a **maximum**, so grepping for a suspicious `0` would not have
  turned it up.
  Seven tests, including a guard-on-the-guard: if the `rsi is None` branch ever stopped being the
  midpoint, refusing would stop being better than guessing and the fix would go inert.

**Verified correct on the same path:** `pct_from_52w_high` and `pct_from_ma200` only compute when
their divisor is present and positive, so an absent one falls through to its own neutral branch. RSI
was the only indicator substituting a confident extreme.

**Expect after deploy:** 2026 dividend income drops ~5.80 CHF across the Dividends tab, the
Performance card, the tax report and the ledger. That is the correction, not data loss.

**After they deploy, check:** the chart and hero row show no yellow notice (nothing is unpriced today);
`summary.unpriced_holdings == 0` and equals the last timeline point's; Sharpe and Top 5 Weight still show
numbers on a normal range and dashes on MTD early in a month; and `days_held_in_ttm` is unchanged for all
twenty rows carrying it, since every holding is continuously held.

## Known rough edges (accepted, not bugs)

- **`e2e/ledger.mjs` is FIXED but has not been run — it needs a production DB snapshot.**
  It was 2 of 7 red from 2026-08-14, and the check had aged rather than the app: its two transfer
  assertions read the rendered Activity panel, the account's only transfer is the in-kind arrival of
  **2026-01-21**, and while the default `1Y` window still reaches it the window now holds ~175 rows
  — so the transfers, being the oldest, fell past the first page of 100. The API was always correct
  (`/api/portfolio/activity?limit=400` returns all 22 `TRANSFER_IN` rows, badged).

  Fixed 2026-08-17 (night) the way this entry prescribed: it clicks the **Cash** event-type filter
  before asserting anything about transfers, so it stays stable as trades accumulate (47 cash rows
  against a `PAGE_SIZE` of 100). The trade-shaped assertions run on the unfiltered panel first, and a
  narrowing check sits between them so a renamed button cannot silently restore the old behaviour —
  8 checks now, not 7.

  **Unverified**, and that is the honest state: running it needs a `sqlite3 .backup` snapshot of
  production on this machine, which was deliberately not done. Nothing about the change is
  data-dependent beyond the button's accessible name (`Cash`, from `KIND_LABELS`), but nobody has
  watched it go green. Run it next time a snapshot is down for another reason.
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

## Shipped 2026-08-04 (latest) — Beta was structurally unreachable under a non-EUR base

**The Beta card had never once shown a number on production, and could not have.** It read
*Needs 20 flow-free days (9 so far)* — and 9 was not a thin window, it was an artefact.

`betaAndCorrelation` disqualified a day if *either* series saw a flow, inferring the benchmark's
from its cost-basis step because a benchmark point carries no `external_flow_eur`. That inference is
sound in EUR and false in CHF: **the backend projects the two cost-basis lines into the base currency
by different rules.** `_calculate_timeline_swept` converts each lot's cost at its own `open_date`, so
the portfolio's line is flat on a day nothing traded; `BenchmarkService._apply_base_currency`
converts the *running total* at each point's date, so the benchmark's line moves whenever EUR/CHF
did. Every such day was thrown away.

Measured on production over the 1Y window, replaying the real series through both rules:

| | flow-free pairs |
|---|---|
| portfolio `external_flow_eur` == 0 | **147** of 261 |
| benchmark cost-basis step == 0, in EUR (the cache) | **147** — the same days |
| benchmark cost-basis step == 0, after the CHF projection | **9** |

The 147/147 agreement is the point: both series are built from one set of tax lots, so the benchmark's
line carries no information the portfolio's does not, and consulting it only re-measured the exchange
rate. The fix is one line — test the portfolio's `external_flow_eur`, plus its own cost-basis step for
the one flow that field cannot see (a disposal whose proceeds netted to zero). Sample days go 9 → 147.

Predicted from the cached timelines before shipping: **β ≈ 1.04 / r ≈ 0.73 vs S&P 500**,
**β ≈ 0.82 / r ≈ 0.83 vs NASDAQ**, **β ≈ 0.54 / r ≈ 0.37 vs FTSE 100** — the ordering a growth-heavy
global book should produce. **Measured in the browser on production after deploy: β 1.03, r 0.74 vs
S&P 500**, which is the prediction landing within a hundredth and the strongest evidence the rule now
measures flow rather than the exchange rate.

`frontend/src/lib/portfolioKpis.ts` + its test. The old test *drops a day the benchmark saw a flow on*
encoded the removed rule and is replaced by both halves of the new one. Frontend suite 343 green.

**Committed and deployed 2026-08-05** after a full-diff review that re-derived the claim from the two
backend projection sites (`benchmark_service.py` converts a running total at each point's date;
`portfolio_service.py` converts each lot at its own `open_date`). Authored in a parallel session — the
review happened because a working tree carrying an unrecognised change is reviewed before it ships,
not because anything looked wrong with it.

- **The backend inconsistency behind it is NOT fixed**, deliberately (out of the requested scope). It
  is also visible on the chart: under a non-EUR base the benchmark's cost-basis line drifts from the
  portfolio's by pure FX — order of ~0.3% over the past year, so small and easy to miss. Fixing it
  means converting the benchmark's cost events per lot `open_date`, which the EUR-only timeline cache
  cannot do post-hoc. See *Worth doing next*.

## Shipped 2026-08-04 — DEPLOYED; a follow-up fix is committed but unpushed

**Market data now reprices seven times a day (08/11/13/15/18/20/22 Berlin) instead of three, and the
reason it took more than a cron edit is that adding slots alone would have made the numbers worse.**

`get_missing_dates()` returned only dates with **no row at all**, so whichever job wrote a date first
owned it forever. Read off `market_prices.created_at` on production, not inferred:

- every European close was its **15:00 Berlin mid-session price** — Xetra and Euronext run to 17:30,
  and the job named "after EU close" ran 2.5 hours before it;
- Korea's alternated between mid-session and final depending on whether the 08:00 or the 15:00 job
  happened to land the row first;
- the 22:00 job wrote US closes within ~40s of the bell and nothing ever restated them.

So an *earlier* slot would have frozen an *earlier* price. `PROVISIONAL_PRICE_DAYS` (3) re-fetches a
recent weekday even when cached and the existing upsert restates it — no extra Yahoo requests, just a
wider range on one already being made. CLAUDE.md has the durable rules (*Sync schedule*, and the new
paragraph beside the holiday rule); what follows is only what it does not say.

### Measured on real data, 2026-08-04 19:07-19:12 Berlin

One full market-data pass, **user-authorised** under rule 1 (the only thing that makes a live Yahoo
call permissible), run with the new code against a `.backup` snapshot of the production DB **from a
local machine, not the VPS** — Yahoo's limit is IP-based, so this spent this machine's budget and
could not have cost the server its evening slots. Snapshot deleted afterwards.

Timing chosen to make the bug visible: 19:07 Berlin is 90 minutes after Xetra closed, so the stored
row *had* to be wrong, and mid-US-session, so US names *had* to gain a price.

**40/40 securities, `status: success`, no errors, no rate limit, ~40 requests over ~5 minutes
(~8/min).** And the frozen prices were wrong by real amounts — each of these had been stored as its
15:00 Berlin mid-session value and was restated to the settled close:

| | was (15:00) | settled close | error |
|---|---|---|---|
| XAIX@IBIS2 | 201.25 | 205.50 | **+2.11%** |
| ABEA@IBIS (Alphabet, Frankfurt) | 320.25 | 326.15 | **+1.84%** |
| SMH@LSEETF | 106.14 | 107.82 | +1.58% |
| XNAS@IBIS2 | 58.58 | 59.46 | +1.50% |
| SXR8@IBIS2 (S&P 500) | 713.12 | 719.96 | +0.96% |
| EMIM / IWDA @AEB | | | +0.84% / +0.74% |
| AMZ@IBIS / ASML@AEB | | | +0.23% / +0.12% |

So the European sleeve was understated by up to ~2% every day, and the daily chart kept it
permanently. **22 US securities gained a price for today that they would not otherwise have had until
22:00** — prod's 15:00 Berlin job runs at 13:00 UTC, before the 13:30 UTC US open, so no row existed
at all. Three rows were correctly left alone: Korea and Taiwan close before 15:00 Berlin, so their
stored values were already settled, and the refresh re-read them and got the same number — the "does
not thrash an already-settled price" half working.

One reporting wart noticed, pre-existing and not touched: the pass reported `prices_fetched: 234`,
but that is `sync_security_prices` returning **rows in the window**, not rows written (~80). The field
has always over-reported; it will simply look larger now that a pass always writes something.

### Deployed and confirmed on production, 19:21 UTC

`5093be5` live. Nine jobs registered with the declared hours; the retired
`market_sync_eu_close` / `market_sync_us_close` were **pruned** from the persistent store, so nothing
runs twice. `scheduler_jobstore_persistent` and `write_auth_enabled` both still true.

**A pass was then run on production by hand at the user's request** (19:35 Berlin) and corrected the
live rows exactly as the snapshot predicted: nine European closes restated (XAIX +2.11%, ABEA +1.84%,
SMH +1.58%, XNAS +1.50%, SXR8 +0.96%) and **24 US rows created where there had been none** — half the
book by cost basis had no price for the day, because the old 15:00 Berlin slot fires at 13:00 UTC and
the US opens at 13:30. `status: success`, no rate limit, no errors.

It was run through `SchedulerService.sync_market_data` inside the container, **not** through
`POST /api/market-data/sync`, and that distinction turned out to matter — see below.

**Two things the live run exposed, both fixed the same evening (unpushed):**

- **`POST /api/market-data/sync` had its own copy of the securities loop and therefore no rate-limit
  breaker.** The breaker went into the scheduler's copy only, leaving the *public* route asking Yahoo
  for another ~38 securities after a 429. Now both delegate to
  `MarketDataService.sync_securities`. Two tests: the sweep stops, and a source check that neither
  caller has re-grown its own loop. **The AST lens in CLAUDE.md could not have found this** — the two
  copies shared no function name, and "router-to-service pairs are noise" argues for skipping it;
  what identified it was asking which paths reach the same *upstream*. That reasoning is now in
  CLAUDE.md beside the lens.
- **`created_at` is not bumped by a restatement.** `bulk_create` updates only the columns supplied,
  and the price dicts carry no `created_at` — right for the name, but it is the column that *proved*
  the freeze and it is useless for confirming the fix. A troubleshooting row added earlier the same
  day said to check it; that advice was wrong and is corrected. Verify by the price changing.

**Checked, and clean.** Every market-data pass since the deploy reports `rate_limited: false`,
`status: success`, 40/40 securities and zero warnings — including the 20:00 Berlin slot
(`2026-08-04T18:05:31Z`), the first real run on the new schedule. So 2.8× the Yahoo traffic from the
VPS's own IP is not provoking a limit, which was the open question.

**Still to check:** that the 11:00 slot settles Korea's close. The 08:00 run catches KRX mid-session
and 11:00 is the first pass after Seoul shuts, but the deploy landed at 19:21 so no 11:00 slot has
run yet. Compare a KRX row's value across the 08:00 and 11:00 passes — **not** its `created_at`,
which is not bumped by a restatement.

Also landed, both found while sizing the traffic increase rather than sought:

- **A Yahoo 429 no longer keeps asking.** This repo's guide credited `market_data_service.py` with
  "rate-limit detection that aborts the run"; it aborted only the ticker *variations* for the
  security in hand, so the caller logged a failure and moved on to the next of 40. Harmless at three
  passes a day, not at seven. `MarketDataService.rate_limited` latches and `sync_market_data` breaks
  with a warning.
- **Both `ops/finish-deploy.*` twins had the wrong slot hours for four days** — written with
  13:00/20:00 on the very day those were retired for 00:00/06:00, so the interactive guard would
  report "clear of every sync slot" at 05:58 Berlin. Only `auto-deploy.sh` was under test; all three
  copies are now read by `tests/test_deploy_guard_hours.py` against `ALL_SYNC_HOURS`, and the
  scheduler-side check no longer regexes literal `hour=` digits out of the source (which would have
  silently ignored any slot registered from a loop or at a half-hour). The twins also gained the
  midnight wraparound they needed from the moment 00:00 became a slot.
- Stale hour lists corrected in `app/main.py`'s startup log (now built from the constant) and
  `config.py`'s comment.

Suites: backend **664 → 682**, all offline. Frontend untouched.

## Shipped 2026-07-31 — DEPLOYED and verified

Live at 19:31 Berlin. Suites: backend 357 → 462, frontend 45 → 91, `tsc -b` and `npm run build`
clean. **Write auth is ON in production** — verified from outside the host: a write with no key and
a write with a wrong key both 401, reads still 200. All five scheduler jobs re-registered after the
rebuild — **which was read at the time as the persistent job store working, and was not**: the
in-memory fallback re-registers exactly the same five, and the store had never opened at all (see
*Recent sessions*, 08-01). **The guarded `auto-deploy.sh` is
installed** on the VPS at 20:11 Berlin (5140 bytes, `-rwxr-xr-x`, byte-identical to `ops/`), so
deploys now defer rather than landing inside a sync slot.

Two things about that deploy worth knowing, both cost time on the day:

- **`commit` reads `unknown` on this one deploy, and that is expected.** `deploy.sh` pulls the repo
  *itself* (line 13), so the copy already executing is the one from before the pull — bash does not
  reload a running script. Any deploy that changes `deploy.sh` therefore runs the **old** logic once,
  and the `GIT_COMMIT` export it gained is missing for exactly that run. The next deploy stamps the
  real sha. The finish-deploy scripts now accept `unknown` + the `write_auth_enabled` marker as
  proof the new build is live, instead of hanging 15 minutes over a cosmetic stamp.
- **`docker compose restart` does not reload `env_file`.** Compose reads it when it *creates* a
  container; `restart` reuses the existing one with its original environment. So `API_ADMIN_TOKEN`
  landed in `.env` and was silently ignored — `write_auth_enabled` stayed `false` while everything
  reported success. **`docker compose up -d`** is required. Both scripts now use it *and* re-check
  `/health` afterwards rather than assuming, because the failure is invisible: a site whose write
  API is still wide open looks exactly like one that is locked down.

**The bundle is now code-split**, which changed the shape of a deploy for users. It was one 891 kB /
264 kB-gzipped chunk; it is now four eager files (app 52 kB gz, react 57, charts 119, query 15) plus
one per deferred tab. Two separate wins: the seven non-default tabs no longer load at first paint,
and — the bigger one, given the VPS redeploys within 10 minutes of any push — **the chunk that
re-hashes on every deploy fell from 264 kB gzipped to 52 kB**, because vendor code now sits in files
that only change when a dependency does. nginx already serves `/assets/` `immutable` for a year, so
that caching is real rather than theoretical. Recharts stays eager on purpose: three components on
the *default* Performance tab use it, so deferring it would only move the wait.

`ui/LazyTabPanel.tsx` exists because splitting introduced a failure the eager imports could not
have. Chunks are content-hashed and the VPS redeploys constantly, so a browser holding the page
across a deploy requests a filename that no longer exists — unhandled, that rejection reaches the
app-level boundary in `App.tsx` and blanks the whole dashboard, which is strictly worse than before
the split. The panel-scoped boundary recognises the wording Vite/webpack/Safari each use for it,
says a new version shipped, and offers the reload that fixes it (`index.html` is `no-cache`, so a
reload genuinely resolves it). A non-chunk error still shows its real message — mislabelling a
genuine bug as a deploy race would have users reloading forever.

Verified in a real browser against the built output under the production CSP, since chunk boundaries
are a property of the build that no unit test can observe: 4 chunks at first paint, none of the
seven deferred ones; each tab fetching its own chunk on click, all 200; every panel mounting; zero
CSP violations.

**Verified against a production DB snapshot and in a real browser** (Playwright — now committed as
`e2e/`), not just through the test client. That is worth stating because it found three defects the whole green
suite did not:

- **Trades were converted wrong.** `trades.proceeds`, `trades.realized_pnl` and
  `corporate_actions.proceeds` are stored in the trade's **own** currency — there is no `_eur` column
  on either, unlike `cash_flows.amount_eur` and `dividend_payments.*_eur`. The ledger applied only
  the EUR→base factor, so a CAD 30.27 realized gain read as CHF 27.85 instead of CHF 17.15 and the
  ledger's realized total sat 6.8% away from `/api/portfolio/summary`. Both now agree to the cent.
- **67 BUY rows showed `CHF 0.00` realized.** IBKR sends `fifoPnlRealized=0` on every buy; rendering
  it asserts a realized result where there is none.
- **Fractional share counts rounded to `0`** (and a fractional sell to `-0`). This account trades
  0.5 SOXQ, 0.3 MU, 0.1 CSU routinely, so it was most rows, not an edge case.

Confirmed on the snapshot: 194 events in the default window; **22 in-kind transfer rows badged
*Transfer · not money in*** and 26 deposits + 1 withdrawal counted, matching the DB exactly; the
ledger's deposit total equals `/api/portfolio/contributions`'s `deposits_eur` to the cent (two
independent code paths). All eight tabs render with zero console errors. With the backend stopped,
eleven surfaces report the failure explicitly and none falls back to an empty-data message.
Keyboard: arrow/Home/End across the tab strip, Enter on all four collapsible headers, 9 headers
carrying `aria-sort`.

What landed, and why each was worth doing:

- **`_ttm_growth_from_quarterly` was duplicated and divergent**, so one security could report
  different earnings growth on `/api/fundamentals/portfolio` than on `/api/watchlist`. Now
  `app/services/ttm_growth.py`, with the 5–7-quarter tier the fundamentals copy lacked.
- **Every chart date boundary went through `toISOString()` on a local date**, so YTD/MTD started a
  day early in any positive-UTC-offset zone. `lib/dateRanges.ts` is local-calendar throughout.
- **Inception is read from the data**, not hardcoded twice (`2024-05-28` for ALL, `2024` for the tax
  year picker) — see *Needs a human*.
- **Four cards and every sortable column in Fundamentals/Watchlist were mouse-only**, and the tab
  strip had no ARIA at all. Shared `CollapsibleCardHeader` / `SortableTh`, full WAI-ARIA tabs, and
  21 jsdom tests pinning it.
- **Four more surfaces let a backend failure read as empty data.** That class is now closed.
- **The write API had no authorization anywhere** — off by default, see *Needs a human*. Alongside:
  a per-IP rate limit, `X-Request-ID` on every response with a redacting 500 handler, and `/health`
  reporting version/commit/scheduler/auth, rendered in a new footer.
- **An Activity tab.** `trades`, `corporate_actions`, `cash_flows` and `dividend_payments` were all
  ingested and depended on with no read surface at all. Cash rows carry `counts_as_money_in`, so the
  transfer audit CLAUDE.md prescribes is a UI action rather than an ssh command.
- **A deploy landing in a Berlin slot no longer loses that sync** — persistent APScheduler job store,
  which is *Worth doing next* item 9 from yesterday.
- **`prune_empty_dividends` was deleting the forecast's cadence basis.** Found while assessing
  whether to automate it — the answer turned out to be "fix it first". The CLI deleted any computed
  row carrying no income, on the stated grounds that it "deletes only rows the readers already
  ignore". That stopped being true when the forecast was changed to infer cadence from the **raw**
  history: a pre-ownership yfinance row is income-free *and* load-bearing, and dropping it is what
  the "only 15 of 36 payers project" bug looked like. Running the documented cleanup would have
  quietly reverted that fix for every recently-bought payer. Prune is now bounded by the ingest
  window it should always have mirrored — a row goes only if it is older than the history
  `sync_dividend_data` deliberately retains — and a security with no lots is left alone entirely,
  matching ingest's own refusal to guess a cutoff. The existing test fixture had masked it by
  holding 10 shares with no tax lot, which cannot happen in real data.
- **The browser checks are in the repo** as `e2e/`, a package deliberately separate from `frontend/`:
  `deploy.sh` runs `npm ci` inside `frontend/` on every `--no-cache` rebuild and Playwright's
  postinstall pulls ~150 MB of Chromium, which would tax a deploy that runs every 10 minutes. Nothing
  in the deploy path touches `e2e/`. Six scripts with a table of preconditions in its README —
  `a11y` (14 checks), `sweep` (16), `csp` (4), `chunks` (33), plus `errors` (backend deliberately
  down) and `ledger` (needs a prod snapshot). Screenshots are gitignored: real account data, public
  repo.

  Committing them surfaced one flaw. **`csp.mjs` used to run against the dev server, where it could
  not have been meaningful:** Vite injects an inline `<script type="module">` for react-refresh and
  `script-src 'self'` blocks it, so the app never boots and the script reports a violation that
  cannot exist in production. It now targets `vite preview`. The *conclusion* was never wrong — the
  CSP had already been verified against the real build (see *Watch after the first deploy*), and
  re-running it there passes 4/4 — but the reusable script was measuring Vite's HMR transport.

## Shipped 2026-08-03 — mobile layout — deployed and verified on production

Live as `c81d883`. The app is built to 390x844 now, and the checks below were re-run against
production rather than only against the local stack — the local DB has null prices, so it cannot
exercise the shapes that actually break.

`e2e/mobile.mjs` is the new check and the reason to trust the rest: 45/45, zero horizontal overflow
on all eight tabs. It went 36/45 on its first run against the then-current tree, naming
`div.flex.items-center.gap-2 w=493` — the nine chart range buttons in a 324px card, 204px of document
overflow. Everything else was verified at both viewports: unit 268 → 308, `a11y` 17/17, `sweep`
16/16, `errors` 15/15, `csp` 4/4, `chunks` 33/33 (so the code-split lazy panels still defer).

Every table below `sm` is a card list — symbol and description left, headline figure and delta right,
the rest as label/value pairs behind a disclosure — rendered from the **same** `Column[]` as the
desktop table. See CLAUDE.md *The mobile layout* for the rules; what follows is only what that does
not say.

**Two bugs found that were not about width at all:**

- The **Performance tab was permanently untappable on a phone**. `TabsList` is `justify-center`, and
  a centred flex row wider than its scroller overflows on both sides with no way to scroll left.
  Invisible to every check that opens at 1440px.
- **`mobile.mjs`'s own sticky assertion was vacuous** as first written — a non-sticky strip scrolls
  off the top and reports a large *negative* offset, which `<= 1` accepts.

**Worth knowing before the deploy:**

- **One deliberate desktop change**: `PortfolioValueChart`'s X axis no longer labels only the 1st of
  a month, so ticks will not land on month firsts. The old rule defeated recharts' own thinning
  (which drops ticks by *index*), leaving up to 24 full-length labels with empty strings between them.
- **Cell padding is unified** at `px-2`/`px-3` via `density`, so a few desktop tables shift by a few
  pixels of column gap. Deliberate; eyeball it if it looks off.
- **KPI cards are two-up below `md`** with Market Value as a full-width hero, and their values are
  `text-lg sm:text-2xl`. A 2-up cell has a 141px interior and `text-2xl` renders a seven-figure
  currency string at ~182px.

## Watch after the next deploy

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

## The overnight batch — pushed and live

The autonomous loop of 2026-07-31 into 08-01 (`/loop 10m`, ~22 iterations) was pushed on request and
auto-deployed at **07:32 UTC**. `/health` reports the sha; `git log` is the record of what changed,
and **each commit message carries its own reasoning**. What follows is only what the log does not
give you. The durable rules are already in CLAUDE.md (*Client-side analytics*, *The dominant failure
mode*, the naive-UTC paragraph in *Database schema*, the Alpha Vantage note under rule 1).

**Verifying that deploy is what found the job-store bug above** — the one item in this file that had
been marked "watch after the next deploy" and was, until someone actually looked, believed fixed.

### Wants Simon's judgement

- **SOXQ's geographic split in `app/etf_mappings.py` is my estimate** (US 80 / Taiwan 10 / Netherlands
  8 / Korea 2), skewed more US than SMH's because the PHLX SOX index only takes US-listed names. The
  sector (100% Technology) is unambiguous; the geography is approximate like the rest of that file.
- **Allocation targets are stored in localStorage, not the database.** Chosen because `/api/` is public
  and auth-gated, so a route storing portfolio intent is more surface than the feature earns — but
  targets do not follow you to another browser. Reversible: the lib takes a plain map.
- **`safe_float` now rounds to 4 decimals on both endpoints** (previously only the watchlist). ≤5e-5 on
  any metric, and it makes Fundamentals and Watchlist agree, but it does change displayed digits.
- **`securities_without_data` now counts missing *data* rather than a missing timestamp.** Needed,
  because the timestamp had to start recording failed attempts to bound retries — but it changes what
  the Allocation tab's banner counts.

### Verified and **not** bugs — recorded so nobody re-chases them

`ActivityTab`'s `amount_base ?? 0` (unreachable — `BaseFx.convert` never returns `None`);
`DividendKpiCards`' `prev_net_eur ?? 0` (over-permissive TS type only);
`PortfolioValuePoint.external_flow_eur`'s `0.0` model default (the service supplies it on every row);
the first timeline row's flow (already fixed by the pre-window seeding loop); the success-path
`SyncRunRepository.record()` outside the `try` (identical in **all five** CLIs, so deliberate);
`expire_on_commit=False` making post-commit reads safe; `security.asset_type` and `asset_category`
both being real columns; `_add_to_category` merging by symbol (which is what correctly combines a
dual-listed ASML); `lib/monthlyReturns.ts` already routing through `externalFlow`; a currency switch's
unfiltered `invalidateQueries()` (~20 refetches against a 120/min limit, and it cannot reach Yahoo
because the benchmark cache stays EUR); `AnalystRating.consensus` already answering "No Rating" on five
zeros; and `market_price_repository.bulk_create` genuinely updating `source` on conflict.

**`benchmark_service.calculate_benchmark_value_over_time()` was audited line by line and is correct** —
it feeds the new beta metric and re-reading 200 lines is expensive, so: close events exclude **on** the
close date; pre-window events fold in without a seeding loop; share and cost events are appended *and
skipped* together, so a zero value can never be emitted against a live cost basis; and
`_apply_base_currency` converts all four money fields. One behaviour to know: when shares are held but
a price or FX rate is missing, that day is **omitted** rather than zeroed — `betaAndCorrelation` skips
such a pair by design and the date self-heals.

### State

Suites **backend 664 / frontend 268**, `tsc -b` and `npm run build` clean. `e2e/`: `a11y` 17/17,
`sweep` 16/16, `errors` 15/15, `chunks` 33/33, `csp` 4/4 — everything except `ledger`, which needs a
production snapshot.

The deploy is verified beyond `/health` returning 200: the five served asset hashes match a local
`npm run build` byte for byte, and the read endpoints answer 200 across portfolio, allocation,
contributions, dividends, activity and tax. `/api/portfolio/benchmark` and `/api/dividends/summary`
were deliberately **not** called — both can reach Yahoo on a cache miss.


## Worth doing next

Rough priority. The auto-deploy install moved to *Needs a human* — it is the last deploy step.

1. **SEC N-PORT as the generic fallback for a future fund with no issuer route.** Items 1 and 2 here
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

2. **Commercial holdings APIs are not viable free — checked 2026-08-16, do not re-shop.** The
   holdings array is the paywalled field at every vendor: FMP's free Basic is 250 calls/day but
   holdings are Ultimate-tier and US exchanges only; API Ninjas models UCITS domicile correctly but
   gates `holdings` behind premium; EODHD's Fundamentals feed costs 10 calls against a 20/day quota
   and is the $59.99 tier, not the $19.99 one; Intrinio has genuine global coverage at enterprise
   pricing. **Never wire up FMP's "ETF Holder" endpoint** — it returns the institutional investors
   who own shares *of* the ETF, the reverse direction, and it returns plausible-looking garbage that
   passes a smoke test.

3. **Make look-through coverage keep itself current — half done.** `find_stale_etf_baskets()`
   shipped 2026-08-17 (night), so a stale basket now *warns* instead of only badging the tab. What
   remains is the automatic half: a staleness-guarded refresh on the existing 18:00 `full_sync`
   rather than a new slot, because a new hour has to be threaded through `ALL_SYNC_HOURS` and the
   three deploy-guard copies `test_deploy_guard_hours.py` keeps in step. The read path must stay pure
   DB either way, and the refresh has to reuse the detector's verdict rather than re-deriving
   "which basket is stale" — that predicate now exists in exactly one place and should stay there.
   Identities are still hand-run and have no detector at all; `unresolved_value_eur` is the figure
   that shows them drifting.

   Then, once every held fund has a basket: `etf_holdings.sector` and `.country` are the raw material
   for replacing `etf_mappings.py`'s hand-estimated sector/geography blocks with measured ones.
   `sector` is now normalised through `sector_taxonomy.py` and served for **grouping** the
   look-through treemap, which is half the work already done; `country` is still unserved and needs a
   country-to-region map with its own test — `countryOfRisk` is a country and the charts bucket
   regions — so it is a real project rather than a flag. The precondition is written into
   `etf_mappings.py`'s docstring, and the reason not to serve two sector *totals* at once is in
   CLAUDE.md.

4. **Fold `PRE_OWNERSHIP_HISTORY_YEARS` pruning into a scheduled job — reassess before building.**
   `prune_empty_dividends.py` is a manual CLI and the ingest window already prevents new junk, so
   there is very little left for a scheduled run to find. Investigating this on 2026-07-31 turned up
   a **defect in the CLI rather than a case for automating it** (below), which is a fair warning
   about automating a deleter over financial rows: the value is small and the downside is silent.
   If it is built, it must reuse the CLI's predicate rather than re-deriving one.

5. **Project the benchmark's cost-basis line the way the portfolio's is projected.** Found while
   fixing Beta (above) and left alone as out of scope. `_apply_base_currency` converts the running
   cost basis at each point's date; `_calculate_timeline_swept` converts each lot's cost at its own
   `open_date`. Same tax lots, two conversion rules — the *dominant failure mode* in CLAUDE.md, in
   the form where both copies keep working and just stop agreeing. It already cost the Beta card
   outright, and it leaves the two cost lines on the chart drifting apart with FX. The awkward part
   is that the timeline cache is EUR-only on purpose (so switching base currency never invalidates
   it), and a per-lot conversion cannot be derived from the cached aggregate — the cost *events* have
   to be re-projected at read time. Do not "fix" it by making the portfolio convert per-date instead:
   that direction breaks the contributions identity, which depends on each leg converting at its own
   date.

## Local development traps

Each of these cost real time at least once.

- **`SCHEDULER_ENABLED=false` in `backend/.env` for any local run.** Otherwise starting uvicorn arms
  the nine daily Europe/Berlin jobs against the live Flex token and Yahoo. Defaults to `True` so
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
- **Don't push within ~10 minutes of a Berlin sync slot** — now nine of them, on the hour at
  00/06/08/11/13/15/18/20/22. Auto-deploy rebuilds in ~90 s, so an overlapping deploy used to lose
  that sync outright. The persistent job store recovers it if the gap is under 30 minutes, but a slow
  `--no-cache` rebuild can exceed that. `ops/finish-deploy.*` checks this for you and is now correct
  — both twins had been warning about the retired 13:00/20:00 and missing the live 00:00/06:00.
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
- **`ResizeObserver` is stubbed inline in three test files now.** Consolidating it is a real
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
*Shipped* section above, which is actionable (what to check on prod, and what has already been
confirmed) and gets deleted once nothing in it is outstanding: these lines are permanent, so don't
"tidy up" the overlap by deleting the wrong one.

- **2026-08-17 (night)** — "find bugs and improvements and implement them". Four defects, and all four
  turned out to be the *same* family this file has now closed seven times: a figure built from an
  incomplete valuation, served as a measurement. The lens that found them is the one CLAUDE.md
  already states — **ask what a metric's stand-in value would claim** — and its corollary is the real
  lesson: *severity tracks plausibility, not magnitude.* The nastiest was not the largest error but a
  green `0.00%` drawdown captioned "Never below its opening value", which a stalled price feed
  produces on demand. Two process notes: **the family lens beats the instance lens** (XIRR and Win
  Rate were both reachable by asking "who else reads an incomplete valuation?", and neither shares a
  function name with anything), and **writing the test found the design bug** — the basket detector's
  first draft keyed "is this fixable?" on the held fund's own adapter, which silently skipped VWCE,
  the one case that actually is.

- **2026-08-17** — "assume VT's values for VWCE", plus "why does VT only have a ratio of 91.86%, it
  should be nearly 100% stocks". The second was the interesting one: it *is* nearly 100% stocks, and
  the shortfall is **rounding published as fact** — 8,007 of VT's 10,032 rows are printed at 0.00%
  because Vanguard rounds to 2dp, so the tail that makes up 8.14% of the fund is recorded as nothing.
  Nobody had looked, and the fund table said only "91.86%", which reads as cash. Lesson: **a figure
  that survives because it is nearly right is still unexplained** — the rounding was in CLAUDE.md as
  "thousands of holdings round to 0.00" and had never been counted or put on screen. The proxy ask
  crossed a prohibition this file had written down; it was implemented as an approximation that
  *declares itself* (amber badge, warning carrying the reason, Coverage card refusing to go green)
  rather than either refusing the instruction or quietly obeying it. Later the same day: DBPG got the
  same treatment via VOO, which answers its collateral disqualifier and **not** its 2x leverage — so
  the leverage is stated in `warnings[]` rather than scaled, because scaling a bucket would break the
  partition. Coverage finished at **98.97% with no undecomposed fund**. Two operational lessons worth
  more than the features: **a guard that fires is not a bug** (VT's import refused a torn paginated
  read that would have dropped ~900 companies from 11% of the book while the weights still looked
  plausible), and **never dump bytes from a file holding secrets** — an `od -c` newline check leaked
  the tail of `API_ADMIN_TOKEN` into the transcript.

- **2026-08-16 (evening)** — "cluster by sector, and look at the missing data too". The missing data
  was a **research failure, not a gap**: all four funds recorded as unreachable single-page apps had
  keyless routes, and each old note was wrong differently — the SPA was only the product page, the
  "no ISINs" fund had a CUSIP column, the table was on another path. Lesson: *fetch it once before
  writing down that it cannot be fetched.* Then the trap underneath — the CUSIP column is not all
  CUSIPs (77 CINS, 20 SEDOLs), and `US` + a CINS is a **check-digit-valid ISIN belonging to
  nothing**, so the naive conversion fabricates identifiers that pass every validity test. One live
  OpenFIGI request also killed the plan's own instruction: `ID_CUSIP` on a CINS returns zero rows
  with no error, and the endpoint returns no ISIN at all. Check the contract before coding against
  a remembered one.

- **2026-08-16** — "some charts for the seethrough, maybe a treemap or a Kreischart". The useful part
  was declining half the request: a pie cannot render a 50-row distribution spanning three orders of
  magnitude, so the treemap took the companies and a stacked bar took the coverage split. Two things
  only came out of *looking at the render* — SVG text cannot be measured before layout, so the
  label-fit estimate has to budget for shouted IBKR names or they cross their own tile edges; and the
  first draft gave the bar and the treemap the same word for two different quantities forty pixels
  apart. Also a local-DB lesson: `insert or ignore` silently swallowed 2,635 NOT NULL violations and
  the script cheerfully reported inserting them.

- **2026-08-14** — asked for a "seethrough" view breaking ETFs into single stocks, with GOOG/GOOGL/ABEA
  combined "not by string match, but a smarter way". Two lenses paid off. **First: measure the
  premise before designing on it.** Web research produced confident answers that were wrong for this
  account — four fund ISINs (GRID, SOXQ, QTUM, and the US VanEck SMH instead of the UCITS line
  actually held) — while running the two identifier APIs against our *own* ISINs settled the design
  in one pass: GLEIF and OpenFIGI are **complementary**, GLEIF having no record at all for TSMC,
  Samsung, SK Hynix, Credo or Marvell. **Second: the obvious key ladder was the bug.**
  `lei or figi or isin` splits a company whose ISINs resolve to different depths and reports nothing
  wrong, so grouping is a union. And two defects showed up only on real data — a partition missing by
  a cent from summing rounded buckets, and *US DOLLAR* rendered as a company because Xtrackers ships
  no asset-class column. The first test written for the cent bug **passed against it**; a mutation
  check is what exposed that.
*(The 2026-08-08 Flex-generation entry was dropped here to keep this list at five. Its lesson is
durable and lives in CLAUDE.md — the once-per-day ET generation rule and why the 18:00 Berlin slot
was chosen — rather than in a perishable log line.)*
