# The second account — Swiss pillar 3a (finpension)

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## A second account — Swiss Pillar 3a (finpension)

`securities.account`, `trades.account`, `cash_flows.account`, `cash_balances.account`, all
`String(16)` defaulting to `'ibkr'`; `app/accounts.py` is the vocabulary. Ingested by
`app/cli/import_finpension_csv.py` from a transaction export the owner downloads
periodically. **One portfolio, one set of tables** — the same charts, totals, allocation,
look-through, contributions and returns. No second tab, deliberately: a second tab is a
second positions table, a second value chart and a second allocation breakdown, which is
three more places for this file's opening warning to happen.

The tables were always broker-agnostic in *shape* and IBKR-only in *fact*, and the gap
between those two is where the whole feature lives.

### Three destructive paths, and why scoping them is the precondition

`reconcile_taxlots` read **every** open tax lot, unioned those security ids with the
statement's own, and DELETEd. An IBKR Flex statement cannot mention a Swiss pension fund —
there is no section it could appear in — so the first sync after a 3a lot existed would
delete it, and Phase D would then see the whole position as sold and book a **fictitious
disposal** into realized P&L, XIRR's flow terms and that day's `external_flow_eur`.

The empty-statement wipe guard had the mirror problem, and its quiet half is the dangerous
one: it compared incoming lots against every open lot in the database, so an IBKR book that
had genuinely emptied would be **masked** by a 3a holding and the guard would stay silent on
exactly the statement it exists to catch. `restamp_unsourced_closed_lots` is the third — it
matches a closed lot to a SELL on **quantity alone**, so it could stamp a 3a lot with an
unrelated IBKR sale date.

`TaxLotRepository.get_open_taxlots(account=None)` defaults to *all accounts* on purpose:
`/api/sync/status` counts what the database holds and should keep blending. It is
`reconcile_taxlots` that must pass `IBKR`. `tests/test_account_isolation.py` pins the three
paths plus an AST rule that catches the fourth, which is the one nobody will remember.

**Everything else blends, and that is the feature.** `_calculate_daily_value`,
`_calculate_timeline_swept`, `get_positions_breakdown`, `calculate_xirr`,
`get_performance_attribution`, `_contribution_inputs`, `AllocationService`,
`LookthroughService`, `ActivityService` and `BenchmarkService` all read both accounts. 3a
deposits are money in, so the benchmark invests them too — the comparison is "what if I had
put my savings in the index", and 3a savings are savings.

### Identity, and the upsert that would have corrupted it

- `conid` is **NULL** for a 3a security. There is no honest IBKR contract id, and a
  fabricated one is a label the row has not earned — the SBI lesson in a new place. Still
  UNIQUE, which SQLite applies per non-NULL value.
- **`SecurityRepository.upsert` refuses a NULL conid**, and this is the sharpest trap in the
  schema change. SQLAlchemy renders `conid == None` as `IS NULL`, so it does not merely fail
  to find a new security — it *matches an arbitrary conid-less one* and overwrites it with
  another fund's name, ISIN and currency, or raises `MultipleResultsFound` once there are
  two. It would have looked perfect on the first import and corrupted the second.
  `upsert_by_isin_exchange` is the non-IBKR path.
- `trades.conid` stays **NOT NULL** and carries the ISIN. It is a `String(32)` that is only
  ever matched, never parsed (`get_by_conid_in_range` is never satisfied by a `CH…` string,
  and `persist_transactions` filters through `.isdigit()`), so weakening a contract every
  IBKR row honours to avoid writing a value we have is a bad trade.
- `exchange` is the sentinel `'FUND'`. Non-null is load-bearing: `_get_yahoo_ticker` returns
  the bare symbol and **never consults `ticker_mappings`** when `exchange` is falsy.
- `symbol` is the **ISIN**. finpension publishes no ticker, and `ticker_mappings` keys on
  `(symbol, exchange)`, so the symbol must never change.
- `ib_key` is `fp:{account}:{sha256(...)[:16]}`, hashing the row's fields **and its running
  `Balance`**. Position-sensitive on purpose: a backdated restatement changes every
  downstream key, which makes "history was restated" countable rather than silent.

### The Balance oracle

finpension prints a running `Balance` on every row, and `parse_transaction_report` replays
its own bookings against it **per row**. That makes "a category was silently dropped"
**structurally impossible** rather than merely unlikely, which is what lets a vocabulary we
have only ever seen four members of be trusted at all. Per row and not only at the end,
because two errors that cancel are exactly what a final-only check misses.

The whole file is refused on any disagreement, and on any unrecognised `Category`. The
reference open-source parser for this format logs a warning and skips — precisely the
failure this codebase rejects, since a partial import is indistinguishable afterwards from a
complete one.

Two measured details that look like nits:

- **Cost basis comes from `Cash Flow`, never shares × price.** finpension rounds the cash
  flow to 6dp, so the product does not reproduce it: `3.615 × 121.201101 = 438.141980115`
  against a stated `438.141980`. Recomputing accumulates drift into the derived balance
  until the oracle fires on our own arithmetic instead of on a real defect.
- **A partial export refuses**, because the replay starts from zero and a 3a account does
  too. That matters because ingest **replaces wholesale** — sound only while the file is
  full history, and necessary because a content hash alone orphans a restated row's
  predecessor. A shrink guard mirrors the empty-statement one — and since 2026-09-12 it
  compares the file's row count with what the **previous run parsed** (read back from its
  `sync_runs` row, `details.rows`), not with what is stored: a row skipped for a missing FX
  rate is parsed and never stored, so after k skips a re-export missing up to k rows passed
  the old guard and the wholesale replace deleted the difference. With no run on record it
  falls back to the stored count.
- **Price rows are inserted with ON CONFLICT DO NOTHING, never a bare add.** The replace
  deletes only our own `finpension_*` rows, and for a fund pinned to Yahoo the market-data
  sync re-fetches the trailing `PROVISIONAL_PRICE_DAYS` even when cached — its upsert
  rewrites a statement row's `source` to `yahoo_finance`. Until 2026-09-12 the next upload
  re-added that date and hit the `(security_id, date)` unique constraint: an IntegrityError
  on flush in place of a refusal with a reason, two monthly uploads away for `CH0117044948`.
  An existing Yahoo bar wins; `prices_written` counts rows *submitted*, the same contract as
  `MarketPriceRepository.bulk_create`.

The full vocabulary is `Buy`, `Sell`, `Portfolio Transaction`, `Liquidation distribution`,
`Deposit`, `Transfer vested benefits`, `Flat-rate administrative fee`, `Flat-rate
administration fee` (both spellings are real — map both, never fuzzy-match), `Implementation
fees`, `Dividend`, `Dividend and Interest Distributions`, `Interests`. A trade's direction
comes from the **sign of the cash flow**, which is why `Portfolio Transaction` needs no
special case.

**Fees and income go to `cash_flows` (`FEE`, `INCOME`), never `dividend_payments`.** That is
an exclusion achieved by *not writing the row* rather than by a filter three readers have to
remember: the era splice, the dividend forecast and the DA-1 reclaim all stay IBKR-only for
free. It is also the correct Swiss treatment, and `get_deposits()`'s whitelist keeps them out
of contributions with no new logic. `Liquidation distribution` is booked and **warned about
rather than refused** — the export is full history, so refusing would block *every* future
upload permanently once such a row existed.

`realized_pnl` on a 3a SELL is **computed, never NULL**: `_realized_from_trades` prefers the
trades table wholesale once any SELL exists and reads `t.realized_pnl or 0`, so a NULL would
report the sale's gain as exactly zero in the blended headline.

### Tax — the one place separation is mandatory

3a capital is **not** part of the Steuerwert and 3a income is **not** taxable; both are taxed
on withdrawal at a separate reduced rate. Including either produces a wrong tax return.

The DA-1 case is the sharpest: the country bucket is keyed on `isin[:2]` and both Swisscanto
ISINs begin `CH`, so 3a income would land in the one bucket that is definitionally not a
foreign-withholding reclaim — a claim against a country that withheld nothing.

`tax_exempt_accounts()` is resolved once and applied at four sites: the dividend/DA-1 select,
the realized-gains select, `holdings_snapshot_as_of` and its closed-lot fallback. Realized
gains filter on **`Trade.account`, not the joined security** — that join is an outer one
because `Trade.security_id` is nullable by design, so a Security-side clause would silently
drop every unlinked IBKR sale too. Exemption is a **prefix** test, so `--account pillar3a-2`
inherits it with no edit.

Two things the exclusion must not do. An excluded security is never recorded in
`last_snapshot_skipped` — that latch means *could not be valued*, and reporting a deliberate
omission through it would make the one figure that goes on a tax return look broken. And the
note states **why**, not merely that, or the next reader repairs it back.

What 3a *does* contribute is the year's **deposits**, reported as deductible from taxable
income, leading the JSON and the CSV because they explain what is absent below. Deposits
only: a `TRANSFER_IN` is capital deducted in an earlier year, and counting it overstates the
deduction — the direction that costs money at an audit. Absent entirely on an IBKR-only
database rather than a zeroed block, which would assert a 3a account holding nothing.

### Prices — `price_source`, and the oracle that validates a mapping

`securities.price_source` ∈ `yahoo` | `manual` | `sibling` is the Yahoo opt-out, **orthogonal to
`account`**: of the two funds here, one is reachable on Yahoo and one is not, in the same
account. It exists because there was previously **no way to leave a security alone** — the
loops select `Security` unfiltered, `ticker_mappings.is_active=False` does not stop a fetch,
and the variation loop can auto-save a *bare symbol* mapping onto an unrelated listing.
Honoured at seven call sites across five services plus the innermost `fetch_and_cache_prices`
(reachable directly), and enforced as a family rule by
`tests/test_yahoo_eligibility_family.py`.

A manual fund is priced from the statement NAVs, plus a **bounded business-day carry**:
`finpension_statement` for an observed NAV and `finpension_carry` between them, tagged apart
so a staleness detector can ask for the newest *observed* one. Bounded to
`CARRY_HORIZON_DAYS` rather than run to today, or a six-month-stale upload would value the
fund at a six-month-old NAV for ever with nothing saying so.

**A `sibling` fund is priced from another share class of the same fund** (since 2026-09-12,
the owner's call). The bounded carry traded the wrong things for the EM tranche: its share
count never changes between uploads, an EM index drifts a few percent a month, and the
position is ~0.6% of the book — so going *unpriced* after 45 business days removed 464 EUR
from the total to avoid a stale-price error of perhaps 15. Yahoo does not quote the tranche
held (`CH1529078078`, the NMT class), but it quotes the same fund's NT class
(`0P0000S0OE.SW`, CHF, daily) at a level ~49% higher — the very thing the NAV check refuses
as a *direct* price, and exactly what a returns anchor wants:

    price(t) = NAV(anchor) × close_sibling(t) / close_sibling(anchor)

with the anchor the newest `finpension_statement` row. `MarketDataService.sync_sibling_prices`
does it inside the ordinary market-data pass (so it honours the rate-limit latch and the
provisional re-fetch): explicit mapping only, never a suffix guess; refuses whole on a
sibling in another currency (FX would ride into the ratio), on no NAV to anchor to, or on no
sibling close within `SIBLING_ANCHOR_LOOKBACK_DAYS` before the anchor date; never derives
before the first NAV; never writes over a statement row. Rows are tagged
`sibling_scaled`. **No carry** for such a fund — the sibling supplies every day — and no
horizon, so an upload is needed only for the *transactions* it brings.

Two guards keep it honest. The importer **deletes the derived rows on every upload** (they
were scaled to the previous newest NAV; the next sync re-derives from the new one) and
**checks the tracking first**: for each new transaction NAV that has a derived row on the
same day, a gap past `NAV_TOLERANCE_PCT` is a warning naming the size — the sibling stopped
moving like the fund, or the mapping points at the wrong class — while the import still
lands, because re-anchoring is the repair. And `manage_mappings set … --sibling` is the only
way in: with one NAV on record it anchors the level; with two or more it verifies the
sibling's *moves* against the provider's NAV ratios at the same bound before saving, and
refuses a security Yahoo already quotes directly. `yahoo_eligible()` still says **no** for a
sibling-priced security — the sibling's dividend history and `.info` are not this fund's, so
only the price loop consults `is_sibling_priced`. The positions table badges the row
"sibling NAV" (and a manual one "statement NAV"), since a derived figure carries its word
next to it. Tests: `tests/test_sibling_pricing.py`, the sibling blocks in
`test_finpension_import.py` and `test_mapping_cli.py`.

**`manage_mappings set` verifies a candidate against those published NAVs and refuses on a
disagreement**, which is the strongest oracle in this codebase: a NAV observed by the party
that sold you the shares. Measured, not hypothetical — asked to price the EM holding, the
obvious candidate `0P0000S0OE.SW` is a *perfect* name match ("Swisscanto (CH) Index Equity
Fund Emerging Markets"), is the fund's **NT** tranche against the **NMT** one held, and
quotes **+49%**. That is the SBI failure at the same order of magnitude, and only the NAV
check separates it from the correct mapping.

`NAV_TOLERANCE_PCT` is 2 and both bounds are measured: the *correct* ticker for the World ex
CH holding (`0P0000S0OD.SW`) read 446.87 / 448.32 / 448.75 across surrounding days against a
stated 448.5281 — 0.6% spread, 0.05% at the nearest day — while the wrong share class sat 49%
away. Nothing lands between them. A verified mapping is also **the only thing that flips
`price_source` to `yahoo`**: saving the row alone is inert, since `sync_securities` skips a
manual security.

### Cash — locked, and never differenced against IBKR's level

`balance_events` gathers events **per account** and splices each against that account's own
measured levels before merging. Not tidiness: `_apply_measured` turns a level into
`level − derived_running`, so an IBKR level differenced against a total carrying 3a money
emits a correction that silently subtracts the 3a balance on every measured day and puts it
back on every day between. The line would sawtooth and every individual point would look
plausible. **Latent rather than live** — `cash_balances` is empty until the Cash Report
section is enabled in the Flex portal.

It iterates the union of accounts-with-events and accounts-with-levels, because a Flex query
can carry Cash Report while `<Trades>` is still off.

`cash_source` gains **`mixed`**: `ibkr` means *read from the broker*, and a 3a balance can
never be one, so badging a half-derived total `ibkr` claims a provenance for money IBKR never
saw — the overclaim `derived_source()` was split out to avoid, by a new route.

### Allocation and look-through

Both ISINs are declared in `ETF_ALLOCATIONS` **and** `FUND_SOURCES` (the registry test pins
both directions). Undeclared, a fund takes the `asset_type` column default of `"Stock"` and
is drawn as a company at a plausible weight while `uncovered_fund_eur` still reads 0.00 —
the IQQ failure.

The EM tranche borrows **EMIM's** basket: MSCI EM IMI against its MSCI EM, the VWCE/VT shape,
erring low because the wider index spreads weight thinner.

The World ex CH tranche is **deliberately un-proxied**, and this is the interesting half.
IWDA's basket (MSCI World, *with* Switzerland) would put Nestlé, Roche and Novartis into the
look-through at ~2.5% of the position — three companies the fund exists specifically not to
hold — while understating every company it does. That is a **mixed** direction, where every
other proxy here errs low and says so, and a fabricated holding is a different kind of wrong
from an understated one. It stays an honest `uncovered_fund`. The exact-index donor exists
and is the follow-up: **iShares World ex Switzerland Equity Index Fund (CH), `CH0244028970`**,
product id 279894 — a Swiss institutional fund, so its holdings come from a product-page
`.ajax?fileType=xls` rather than the varnish JSON `parse_ishares` reads.

Tests: `tests/test_account_isolation.py`, `tests/test_finpension_parse.py`,
`tests/test_finpension_import.py`, `tests/test_tax_pillar3a_exclusion.py`,
`tests/test_yahoo_eligibility_family.py`, plus the cash and mapping cases in
`tests/test_cash_balance.py` and `tests/test_mapping_cli.py`.

---
