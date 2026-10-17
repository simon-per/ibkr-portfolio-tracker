"""
A foreign amount in EUR, or None — the one rule for every path that can skip a row.

Four copies of this lived in three services: `TaxService._to_eur`, `DividendService._to_eur`,
the inner `_to_eur` of `finpension_ingest`, and the inline conversion in
`DividendService.compute_dividend_income`. They had drifted twice already. Each fixed the
`return amount  # fallback: store unconverted` defect separately (the tax copy on
2026-07-30, the dividend copy later, the inline one later still — fixing a helper is not
fixing the file), the finpension copy swallowed the error without a word, and a missing
currency meant **EUR** in two of them and **USD** in the inline one. A foreign figure
stored or reported as EUR is the failure all four exist to prevent, so the rule is written
once:

- no amount is zero in any currency;
- EUR passes through;
- a missing currency is unconvertible — never a guess at which one it was;
- a failed rate is None, with a warning naming the currency and the date.

What a None means for the row is the caller's: the tax report skips it, dividend ingest
leaves it uncomputed so the next pass retries it, the finpension import refuses the file.
`tests/test_eur_conversion_family.py` fails a module under `app/` that defines its own
`_to_eur` or guesses a missing currency.
"""
import logging
from datetime import date
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


async def to_eur_or_none(
    currency_service, amount: Optional[Decimal], currency: Optional[str], on_date: date,
) -> Optional[Decimal]:
    """`amount` in EUR at `on_date`'s rate, or None when it cannot be converted honestly.

    `currency_service` is anything with `convert_to_eur(amount=, from_currency=,
    target_date=)` — the real `CurrencyService`, or a test's fake.
    """
    if not amount:
        return Decimal("0")
    if not currency:
        logger.warning(
            f"An amount dated {on_date} has no currency recorded; leaving it unconverted "
            f"rather than guessing one"
        )
        return None
    if currency == "EUR":
        return amount
    try:
        return await currency_service.convert_to_eur(
            amount=amount, from_currency=currency, target_date=on_date
        )
    except Exception as e:
        logger.warning(
            f"No {currency}->EUR rate for {on_date} ({e}); leaving the amount unconverted "
            f"rather than reporting {currency} as EUR"
        )
        return None
