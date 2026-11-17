"""
OpenDART's periodic reports as facts the one normaliser reads (docs/company-financials.md,
*South Korea*). Pure.

A report is (business year, report code): `11013` the first quarter, `11012` the half year,
`11014` the third quarter, `11011` the annual business report. `fnlttSinglAcntAll` gives
each consolidated statement line with its `account_id` and amounts but **no dates**: they
are the report's, by the statement:

- **balance sheet** (`BS`): `thstrm_amount` at the period end, `frmtrm_amount` at the prior
  fiscal year's end (and `bfefrmtrm_amount` a year before that in an annual report);
- **income statement** (`IS`, `CIS`): in an interim report `thstrm_amount` is the quarter
  and `thstrm_add_amount` the year to date, `frmtrm_q_amount` and `frmtrm_add_amount` the
  same a year before; in an annual report the three fiscal years;
- **cash flow statement** (`CF`): `thstrm_amount` the year to date (the fiscal year in an
  annual report), `frmtrm_amount` the same a year before.

So each fact gets real durations, and the normaliser's year-to-date ladder derives the fourth
quarter (the annual figure less nine months) exactly as for a 10-K. The statement of changes
in equity is not read. **A December fiscal year only** (`acc_mt` 12): OpenDART names a
non-calendar year by a business year this cannot date without a guess, so such a company is
refused whole (`UnsupportedYear`).

Lines: `ifrs-full_*` under the IFRS concept, OpenDART's own `dart_OperatingIncomeLoss` (the
operating profit K-IFRS 1001 requires every Korean filer to present) under
`ProfitLossFromOperatingActivities` only where the report tags no IFRS operating profit, any
other `dart_*` line not read; a line OpenDART marks non-standard (`-표준계정코드 미사용-`, no
standard concept) is skipped and counted.
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.services.company_financials_normaliser import (
    REGULATOR_ANNUAL,
    REGULATOR_INTERIM,
)
from app.services.regulator_facts import CONCEPT_KIND, one_value_each, unit_for

Q1, HALF, Q3, ANNUAL = "11013", "11012", "11014", "11011"
#: In the order a year files them; months into the fiscal year each covers.
REPORTS: Tuple[Tuple[str, int], ...] = ((Q1, 3), (HALF, 6), (Q3, 9), (ANNUAL, 12))
MONTHS = dict(REPORTS)

IFRS_PREFIX = "ifrs-full_"
#: OpenDART's own lines read as an IFRS concept, only where the report has no such IFRS line.
DART_TWINS = {"dart_OperatingIncomeLoss": "ProfitLossFromOperatingActivities"}

FLOW_STATEMENTS = ("IS", "CIS")
CASH_FLOW = "CF"
BALANCE_SHEET = "BS"


class UnsupportedYear(Exception):
    """A fiscal year not ending in December: the report's periods cannot be dated."""


@dataclass
class Converted:
    facts: List[Dict[str, Any]] = field(default_factory=list)
    #: Lines with no standard concept (OpenDART's `-표준계정코드 미사용-`), skipped.
    non_standard: int = 0
    #: IFRS concept -> the line's own id (`ifrs-full_Revenue`, `dart_OperatingIncomeLoss`).
    tagged: Dict[str, str] = field(default_factory=dict)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def period_end(year: int, report: str, fy_end_month: int = 12) -> date:
    """The report's period end: the end of its quarter in a December year."""
    if fy_end_month != 12:
        raise UnsupportedYear(f"fiscal year ends in month {fy_end_month}")
    return _month_end(year, MONTHS[report])


def due(year: int, report: str, today: date, after_days: int) -> bool:
    """Whether the report's period ended at least `after_days` ago."""
    return today >= period_end(year, report) + timedelta(days=after_days)


def filed_on(receipt: str) -> Optional[date]:
    """OpenDART's receipt number begins with the filing date (`20250311001085`)."""
    try:
        return date(int(receipt[:4]), int(receipt[4:6]), int(receipt[6:8]))
    except (TypeError, ValueError):
        return None


def amount(value: Any) -> Optional[float]:
    """A statement amount (`"1,234"`, `"-5"`), or None for a blank or a dash."""
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in ("", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _spans(
    year: int, report: str, statement: str
) -> List[Tuple[str, Optional[date], date]]:
    """(amount field, start, end) a statement's line carries in this report."""
    months = MONTHS[report]
    end = period_end(year, report)
    prior_end = _month_end(year - 1, months)
    if statement == BALANCE_SHEET:
        out = [
            ("thstrm_amount", None, end),
            ("frmtrm_amount", None, date(year - 1, 12, 31)),
        ]
        if report == ANNUAL:
            out.append(("bfefrmtrm_amount", None, date(year - 2, 12, 31)))
        return out
    if report == ANNUAL:
        return [
            ("thstrm_amount", date(year, 1, 1), end),
            ("frmtrm_amount", date(year - 1, 1, 1), prior_end),
            ("bfefrmtrm_amount", date(year - 2, 1, 1), date(year - 2, 12, 31)),
        ]
    ytd_start, prior_ytd_start = date(year, 1, 1), date(year - 1, 1, 1)
    if statement == CASH_FLOW:
        return [
            ("thstrm_amount", ytd_start, end),
            ("frmtrm_amount", prior_ytd_start, prior_end),
        ]
    quarter_start = date(year, months - 2, 1)
    prior_quarter_start = date(year - 1, months - 2, 1)
    return [
        ("thstrm_amount", quarter_start, end),
        ("thstrm_add_amount", ytd_start, end),
        ("frmtrm_q_amount", prior_quarter_start, prior_end),
        ("frmtrm_add_amount", prior_ytd_start, prior_end),
    ]


def convert(
    rows: List[Dict[str, Any]], year: int, report: str, receipt: str
) -> Converted:
    """One report's statement lines as facts (`regulator_facts`)."""
    out = Converted()
    filed = filed_on(receipt)
    if filed is None:
        return out
    form = REGULATOR_ANNUAL if report == ANNUAL else REGULATOR_INTERIM
    ifrs_tagged = {
        (r.get("account_id") or "")[len(IFRS_PREFIX) :]
        for r in rows
        if (r.get("account_id") or "").startswith(IFRS_PREFIX)
    }
    facts: List[Dict[str, Any]] = []
    for row in rows:
        statement = row.get("sj_div")
        if statement not in (BALANCE_SHEET, CASH_FLOW, *FLOW_STATEMENTS):
            continue
        account = (row.get("account_id") or "").strip()
        if account.startswith(IFRS_PREFIX):
            concept = account[len(IFRS_PREFIX) :]
        elif account in DART_TWINS:
            concept = DART_TWINS[account]
            if concept in ifrs_tagged:
                continue
        elif account.startswith("dart_"):
            continue
        else:
            out.non_standard += 1
            continue
        currency = (row.get("currency") or "").strip().upper()
        unit = unit_for(concept, currency) if len(currency) == 3 else None
        if unit is None:
            continue
        kind_is_instant = statement == BALANCE_SHEET
        for field_name, start, end in _spans(year, report, statement):
            value = amount(row.get(field_name))
            if value is None:
                continue
            facts.append(
                {
                    "concept": concept,
                    "unit": unit,
                    "start": None if kind_is_instant else start.isoformat(),
                    "end": end.isoformat(),
                    "val": value,
                    "accn": receipt,
                    "filed": filed.isoformat(),
                    "form": form,
                }
            )
        out.tagged.setdefault(concept, account)
    out.facts = one_value_each(facts)
    return out


__all__ = [
    "ANNUAL",
    "CONCEPT_KIND",
    "Converted",
    "DART_TWINS",
    "HALF",
    "Q1",
    "Q3",
    "REPORTS",
    "UnsupportedYear",
    "amount",
    "convert",
    "due",
    "filed_on",
    "period_end",
]
