"""
Parsing a finpension transaction export.

Pure: no DB, no network, no clock. Everything here is a function of the file's bytes,
which is what lets the two refusals below be tested exhaustively without a fixture
database — the same split `import_prices.parse_payload` makes, for the same reason.

**Whole-file refusal, never a skipped row.** A partially applied import is
indistinguishable afterwards from a complete one: the holdings look plausible, the
cash balance is merely a bit off, and nothing says which. The reference open-source
parser for this format logs a warning and skips unknown categories, which is exactly
the failure this codebase rejects.

**The Balance oracle is what makes that guarantee structural rather than aspirational.**
finpension prints a running `Balance` on every row, so replaying our own bookings and
comparing to it, *row by row*, means a category we booked with the wrong sign — or one
a filter three revisions later quietly drops — cannot pass. It is checked per row and
not only at the end, because two errors that cancel are precisely what a final-only
check misses. This is the same shape as `Σ monthly[].net_eur == cost basis` and the
look-through partition identity, and it is the reason a vocabulary we have only ever
seen four members of can be trusted at all.

Two measured details that look like nits and are not:

- **Cost basis comes from `Cash Flow`, never from shares × price.** finpension rounds
  the cash flow to 6dp, so the product does not reproduce it: `3.615 × 121.201101` is
  `438.141980115` against a stated `438.141980`. Recomputing accumulates drift into
  the derived balance until the oracle fires on our own arithmetic instead of on a
  real defect.
- **`Asset Price in CHF` is already converted**, and every cash flow is in the account
  currency. `Asset Currency` and `Currency Rate` are informational; a non-CHF fund
  still settles in CHF. So the whole ledger is booked in CHF and the FX question never
  arises here — it is asked once, later, when CHF is converted to EUR at each row's
  own date.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple


class FinpensionParseError(Exception):
    """A problem with the file — reported, never partially applied."""


#: The account currency. Every `Cash Flow` and `Balance` is in it, and
#: `Asset Price in CHF` says so in its own name.
ACCOUNT_CURRENCY = "CHF"

#: The header, exactly. Matched rather than searched: a changed export format must
#: refuse loudly, not be guessed at column by column.
EXPECTED_HEADER: Tuple[str, ...] = (
    "Date", "Category", "Asset Name", "ISIN", "Number of Shares",
    "Asset Currency", "Currency Rate", "Asset Price in CHF", "Cash Flow", "Balance",
)

# --- What a row does to the ledger ------------------------------------------------
DEPOSIT = "DEPOSIT"            # external money in — a contribution
TRANSFER_IN = "TRANSFER_IN"    # vested benefits moved from elsewhere — NOT a contribution
TRADE = "TRADE"                # buy or sell, direction taken from the sign of the cash flow
FEE = "FEE"                    # account and implementation fees
INCOME = "INCOME"              # dividend, interest, liquidation proceeds
LIQUIDATION = "LIQUIDATION"    # income, plus the position ceases to exist

#: The full published vocabulary, not the four categories this account has so far
#: produced. An unlisted one refuses the whole file, which is what makes the map a
#: decision rather than an accident.
#:
#: Both spellings of the flat-rate fee are real and both are listed. Do not "tidy" one
#: away and do not normalise by fuzzy match — an exact map is what makes a *third*
#: spelling refuse loudly instead of being silently absorbed.
CATEGORY_KINDS: Dict[str, str] = {
    "Deposit": DEPOSIT,
    "Transfer vested benefits": TRANSFER_IN,
    "Buy": TRADE,
    "Sell": TRADE,
    "Portfolio Transaction": TRADE,
    "Flat-rate administrative fee": FEE,
    "Flat-rate administration fee": FEE,
    "Implementation fees": FEE,
    "Dividend": INCOME,
    "Dividend and Interest Distributions": INCOME,
    "Interests": INCOME,
    "Liquidation distribution": LIQUIDATION,
}


@dataclass(frozen=True)
class FinpensionRow:
    """One ledger row, normalised. `line_no` is the file line, for error messages."""
    line_no: int
    row_date: date
    category: str
    kind: str
    asset_name: Optional[str]
    isin: Optional[str]
    shares: Optional[Decimal]
    asset_currency: Optional[str]
    price_chf: Optional[Decimal]
    cash_flow: Decimal
    balance: Decimal

    @property
    def is_buy(self) -> bool:
        return self.kind == TRADE and self.cash_flow < 0

    @property
    def is_sell(self) -> bool:
        return self.kind == TRADE and self.cash_flow > 0


@dataclass(frozen=True)
class FinpensionReport:
    """A parsed export, guaranteed internally consistent by the Balance oracle."""
    rows: Tuple[FinpensionRow, ...]
    #: ISIN -> the name finpension last used for it, for `securities.description`.
    assets: Dict[str, str]
    #: Anything a human should look at. Never a reason to skip a row.
    warnings: Tuple[str, ...]

    @property
    def first_date(self) -> date:
        return self.rows[0].row_date

    @property
    def last_date(self) -> date:
        return self.rows[-1].row_date

    @property
    def final_balance(self) -> Decimal:
        return self.rows[-1].balance


def _decimal(raw: str, field: str, line_no: int) -> Optional[Decimal]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        raise FinpensionParseError(
            f"line {line_no}: {field} is not a number ({raw!r})"
        ) from None


def _required(value: Optional[Decimal], field: str, category: str, line_no: int) -> Decimal:
    if value is None:
        raise FinpensionParseError(
            f"line {line_no}: a {category!r} row needs {field}, and this one has none"
        )
    return value


def parse_transaction_report(text: str) -> FinpensionReport:
    """
    Parse and validate a finpension transaction export.

    Raises `FinpensionParseError` for anything at all — the file is applied whole or
    not at all. See the module docstring for why that is not merely tidiness.
    """
    # utf-8-sig at the caller; strip a BOM here too so a str from either route works.
    reader = csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";")
    try:
        header = tuple(h.strip() for h in next(reader))
    except StopIteration:
        raise FinpensionParseError("The file is empty.") from None

    if header != EXPECTED_HEADER:
        raise FinpensionParseError(
            "Unexpected header. finpension's export format appears to have changed, "
            "and guessing which column is which is how a cost basis silently becomes "
            "a share count.\n"
            f"  expected: {';'.join(EXPECTED_HEADER)}\n"
            f"  found:    {';'.join(header)}"
        )

    rows: List[FinpensionRow] = []
    assets: Dict[str, str] = {}
    warnings: List[str] = []
    running = Decimal("0")

    for line_no, raw in enumerate(reader, start=2):
        if not any(field.strip() for field in raw):
            continue  # trailing blank line
        if len(raw) != len(EXPECTED_HEADER):
            raise FinpensionParseError(
                f"line {line_no}: expected {len(EXPECTED_HEADER)} fields, found {len(raw)}"
            )

        (d, category, asset_name, isin, shares_s, asset_ccy,
         _rate, price_s, cash_s, balance_s) = (f.strip() for f in raw)

        try:
            row_date = date.fromisoformat(d)
        except ValueError:
            raise FinpensionParseError(
                f"line {line_no}: {d!r} is not an ISO date"
            ) from None

        kind = CATEGORY_KINDS.get(category)
        if kind is None:
            raise FinpensionParseError(
                f"line {line_no}: unknown Category {category!r}. Refusing the whole "
                f"file rather than skipping the row — a category booked as nothing "
                f"moves the cash balance off finpension's own and leaves a holding "
                f"unexplained, and afterwards a partial import looks exactly like a "
                f"complete one. Add it to CATEGORY_KINDS with a booking rule.\n"
                f"  known: {', '.join(sorted(CATEGORY_KINDS))}"
            )

        cash_flow = _decimal(cash_s, "Cash Flow", line_no)
        if cash_flow is None:
            raise FinpensionParseError(f"line {line_no}: Cash Flow is empty")
        balance = _decimal(balance_s, "Balance", line_no)
        if balance is None:
            raise FinpensionParseError(f"line {line_no}: Balance is empty")

        shares = _decimal(shares_s, "Number of Shares", line_no)
        price_chf = _decimal(price_s, "Asset Price in CHF", line_no)

        if kind == TRADE:
            if not isin:
                raise FinpensionParseError(
                    f"line {line_no}: a {category!r} row has no ISIN, so there is no "
                    f"security to attribute it to"
                )
            shares = _required(shares, "Number of Shares", category, line_no)
            price_chf = _required(price_chf, "Asset Price in CHF", category, line_no)
            if shares <= 0:
                raise FinpensionParseError(
                    f"line {line_no}: Number of Shares is {shares}. finpension states a "
                    f"positive quantity and puts the direction in the sign of Cash Flow."
                )
            if price_chf <= 0:
                raise FinpensionParseError(
                    f"line {line_no}: Asset Price in CHF is {price_chf}"
                )
            if cash_flow == 0:
                raise FinpensionParseError(
                    f"line {line_no}: a {category!r} row moves no cash, so its "
                    f"direction is undecidable"
                )
            assets[isin] = asset_name or isin

        if kind == LIQUIDATION:
            # Never yet seen on this account. Booked rather than refused, and the
            # reasoning matters: the export is full history, so refusing would block
            # *every* future upload permanently once such a row exists, which is a
            # worse failure than an approximation that says so. The cash is exact; it
            # is the position side that needs a human.
            if isin:
                assets[isin] = asset_name or isin
            warnings.append(
                f"line {line_no}: a 'Liquidation distribution' of {cash_flow} CHF"
                + (f" for {isin}" if isin else " naming no ISIN")
                + ". The cash is booked exactly. Any remaining lots are closed at this "
                  "date with these proceeds, which is right for a full liquidation and "
                  "wrong for a partial one — check the position."
            )

        if kind in (DEPOSIT, TRANSFER_IN) and cash_flow <= 0:
            raise FinpensionParseError(
                f"line {line_no}: a {category!r} row moves {cash_flow}, which is not "
                f"money arriving"
            )
        if kind == FEE and cash_flow > 0:
            raise FinpensionParseError(
                f"line {line_no}: a {category!r} row of {cash_flow} adds cash"
            )

        # --- The Balance oracle, per row -----------------------------------------
        running += cash_flow
        if running != balance:
            raise FinpensionParseError(
                f"line {line_no}: replaying the ledger gives a balance of {running} "
                f"but the file says {balance} (off by {running - balance}).\n"
                f"Refusing the whole file. Either the export is a partial range — this "
                f"replay starts from zero, because a 3a account does — or a row is not "
                f"being booked for its full Cash Flow. This check is what makes a "
                f"silently dropped category impossible rather than merely unlikely."
            )

        rows.append(FinpensionRow(
            line_no=line_no,
            row_date=row_date,
            category=category,
            kind=kind,
            asset_name=asset_name or None,
            isin=isin or None,
            shares=shares,
            asset_currency=asset_ccy or None,
            price_chf=price_chf,
            cash_flow=cash_flow,
            balance=balance,
        ))

    if not rows:
        raise FinpensionParseError("The file has a header but no transactions.")

    dates = [r.row_date for r in rows]
    if dates != sorted(dates):
        raise FinpensionParseError(
            "Rows are not in date order. The running Balance is only meaningful in the "
            "order finpension wrote it, so re-sorting here would validate an ordering "
            "the file does not assert."
        )

    return FinpensionReport(
        rows=tuple(rows), assets=dict(assets), warnings=tuple(warnings),
    )
