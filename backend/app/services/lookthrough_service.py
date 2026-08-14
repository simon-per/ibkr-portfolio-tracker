"""
Company-level exposure: direct holdings plus every held fund decomposed into its
constituents.

48% of this account sits in twelve ETFs, so "how much do I own of Nvidia" was previously
unanswerable — and even the *direct* side was fragmented, because one company routinely
occupies several rows of the Positions table (GOOGL + ABEA + GOOG; ASML's NY registry line
beside its Amsterdam ordinary). `company_identity` folds those; this module weights them.

**Positions come from `PortfolioService.get_positions_breakdown()` and nowhere else.** That
method already projects every value into the configured base currency through `BaseFx`, so
this service performs no FX of its own and cannot disagree with the headline total — the
same reason `allocation_service` reads it rather than re-deriving.

**The five value buckets form a partition that must close exactly:**

    direct_equity + looked_through_equity + fund_residual + nested_fund + uncovered_fund
      == total_market_value

`tests/test_lookthrough_partition.py` pins it over buckets discovered from the response, so
a bucket added later is covered without anyone remembering to extend a list. It is one
assertion, and it catches a dropped holding, a double-counted constituent and an accidental
renormalisation alike.

**Two things in here are deliberately NOT done, because doing them would fabricate a
number:**

- *No renormalisation onto covered value.* Every percentage is a share of the whole
  portfolio. With six funds undecomposed, roughly a fifth of the book has no company
  attribution, so every row is *understated* — and rescaling to make the visible rows sum
  to 100 would convert a stated gap into a confident lie. `coverage_pct` is the figure to
  read first, and the frontend renders it above the table rather than in a footnote.
- *No cost basis and no gain.* Splitting a fund's cost across its constituents needs the
  basket as it stood on each purchase date, which nothing stores. A market-value view is
  honest; a cost column here would be invented.

**On duplicates.** A company held directly *and* inside three funds is the entire point, so
those contributions are summed and reported split (`direct_value_eur` /
`via_funds_value_eur`). A constituent appearing twice within *one* basket is also summed —
`import_prices.py`'s last-row-wins rule for a repeated date would silently drop weight here.
"""
import logging
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.etf_mappings import is_known_etf_isin, symbol_for_fund_isin
from app.etf_sources import ISSUER_OVERRIDES, source_for_fund_isin
from app.repositories.etf_basket_repository import EtfBasketRepository
from app.repositories.isin_identity_repository import IsinIdentityRepository
from app.services.company_identity import IdentityMember, company_groups

logger = logging.getLogger(__name__)

HUNDRED = Decimal("100")

# The equity classes a constituent row must declare to count as a company. A WHITELIST,
# never a blacklist — the same reasoning as `CashFlowRepository.get_deposits()` selecting
# DEPOSITWITHDRAW by name so no new transfer-ish type can leak in. A blacklist would admit
# the next value an issuer invents ('Rights', 'Warrant', 'Preferred') as a company.
EQUITY_ASSET_CLASSES = frozenset({"equity"})

# Below this, a basket is treated as *absent* rather than as a fund holding mostly cash.
# A residual is the right answer for EMIM's real 98.04; at Σ=3 — a half-parsed file — "this
# fund is 97% cash" is a plausible figure, which is the dangerous kind.
MIN_BASKET_COVERAGE_PCT = Decimal("80")

# Vanguard publishes month-end and lags ~6 weeks, which is normal and must not read as a
# fault; past this a basket is served but badged, because today's fund value times an old
# basket shows exposure to a company that has left the index.
BASKET_STALE_DAYS = 45

# The API will not return more rows than this however large a `limit` is asked for.
COMPANY_LIMIT_MAX = 200


class LookthroughService:
    """Aggregates direct holdings and fund constituents into company-level exposure."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_lookthrough(self, limit: int = 50) -> Dict:
        from app.services.portfolio_service import PortfolioService

        limit = max(1, min(int(limit), COMPANY_LIMIT_MAX))
        today = date.today()

        portfolio_service = PortfolioService(self.db)
        positions = await portfolio_service.get_positions_breakdown()
        base_currency = await portfolio_service.get_base_currency()

        warnings: List[str] = []
        valuable, unvaluable = self._split_by_valuability(positions)

        if not valuable:
            # An empty portfolio is not a failure and must not claim to be one — the same
            # distinction PortfolioSummaryCards draws between "no data yet" and "the
            # backend didn't answer".
            return self._empty(today, base_currency, unvaluable, warnings)

        total = sum((p["_mv"] for p in valuable), Decimal("0"))

        direct_positions = [p for p in valuable if not is_known_etf_isin(p["isin"])]
        fund_positions = [p for p in valuable if is_known_etf_isin(p["isin"])]

        basket_repo = EtfBasketRepository(self.db)
        held_fund_isins = sorted({p["isin"].strip().upper() for p in fund_positions})
        baskets = await basket_repo.get_baskets(held_fund_isins)
        holdings = await basket_repo.get_holdings(
            [i for i in held_fund_isins if i in baskets]
        )

        # `contributions` maps a member ref to what that member is worth and where it came
        # from. Kept beside the identity members rather than inside them so
        # `company_identity` stays a pure function of identifiers and knows nothing about
        # positions.
        members: List[IdentityMember] = []
        contributions: Dict[str, Dict] = {}

        direct_equity = Decimal("0")
        looked_through = Decimal("0")
        residual_total = Decimal("0")
        nested_total = Decimal("0")
        uncovered_total = Decimal("0")

        for pos in direct_positions:
            ref = f"sec:{pos['security_id']}"
            direct_equity += pos["_mv"]
            contributions[ref] = {
                "kind": "direct",
                "value": pos["_mv"],
                "listing": f"{pos['symbol']}@{pos['exchange'] or '?'}",
            }
            members.append(
                IdentityMember(
                    ref=ref,
                    isin=pos["isin"],
                    fallback_name=pos.get("description") or pos["symbol"],
                    weight=pos["_mv"],
                )
            )

        fund_coverage: List[Dict] = []
        for pos in fund_positions:
            isin = pos["isin"].strip().upper()
            value = pos["_mv"]
            status, reason = self._fund_status(isin, baskets.get(isin))

            entry = {
                "symbol": symbol_for_fund_isin(isin) or pos["symbol"],
                "fund_isin": isin,
                "market_value_eur": value,
                "status": status,
                "reason": reason,
                "basket_as_of": None,
                "stale": False,
                "constituents": 0,
                "equity_weight_pct": None,
                "residual_eur": Decimal("0"),
                "asset_class_available": True,
                "source": None,
            }

            if status != "looked_through":
                uncovered_total += value
                fund_coverage.append(entry)
                continue

            basket = baskets[isin]
            rows = holdings.get(isin, [])
            entry.update(
                basket_as_of=basket.as_of_date.isoformat(),
                stale=(today - basket.as_of_date).days > BASKET_STALE_DAYS,
                constituents=len(rows),
                equity_weight_pct=Decimal(basket.equity_weight_pct),
                asset_class_available=bool(basket.asset_class_available),
                source=basket.source,
            )
            if entry["stale"]:
                warnings.append(
                    f"{entry['symbol']}'s basket is dated {basket.as_of_date.isoformat()}, "
                    f"more than {BASKET_STALE_DAYS} days old — its companies may have "
                    f"changed."
                )
            if not basket.as_of_is_issuer_stated:
                warnings.append(
                    f"{entry['symbol']}'s issuer publishes no as-of date, so "
                    f"{basket.as_of_date.isoformat()} is when it was fetched — the basket "
                    f"itself may be older."
                )

            company_pct, nested_pct = self._split_weights(rows, basket)
            fund_company_value = value * company_pct / HUNDRED
            fund_nested_value = value * nested_pct / HUNDRED
            fund_residual = value - fund_company_value - fund_nested_value

            looked_through += fund_company_value
            nested_total += fund_nested_value
            residual_total += fund_residual
            entry["residual_eur"] = fund_residual
            fund_coverage.append(entry)

            for row in rows:
                if not self._counts_as_company(row, basket):
                    continue
                row_value = value * Decimal(row.weight_pct) / HUNDRED
                ref = f"{isin}#{row.line_no}"
                contributions[ref] = {
                    "kind": "fund",
                    "value": row_value,
                    "fund_isin": isin,
                    "fund_symbol": entry["symbol"],
                    "weight_pct": Decimal(row.weight_pct),
                }
                members.append(
                    IdentityMember(
                        ref=ref,
                        isin=row.constituent_isin,
                        fallback_name=row.constituent_name,
                        weight=row_value,
                    )
                )

        members = await self._attach_identities(members)
        groups = company_groups(members, ISSUER_OVERRIDES)
        rows_out, aggregates = self._build_rows(groups, contributions, total)

        shown = rows_out[:limit]
        shown_value = sum((r["value_eur"] for r in shown), Decimal("0"))
        companies_value = sum((r["value_eur"] for r in rows_out), Decimal("0"))
        covered = direct_equity + looked_through

        warnings.extend(
            self._coverage_warnings(fund_coverage, uncovered_total, total, unvaluable)
        )

        basket_dates = [
            e["basket_as_of"] for e in fund_coverage if e["basket_as_of"]
        ]

        return {
            "as_of": today.isoformat(),
            "base_currency": base_currency,
            "total_market_value_eur": self._q(total),
            "direct_equity_eur": self._q(direct_equity),
            "looked_through_equity_eur": self._q(looked_through),
            "fund_residual_eur": self._q(residual_total),
            "nested_fund_eur": self._q(nested_total),
            "uncovered_fund_eur": self._q(uncovered_total),
            "coverage_pct": self._pct(covered, total),
            "companies": [self._present_row(r, total) for r in shown],
            "companies_shown": len(shown),
            "company_count_total": len(rows_out),
            "shown_value_eur": self._q(shown_value),
            "other_companies_eur": self._q(companies_value - shown_value),
            "other_companies_count": len(rows_out) - len(shown),
            "funds": [self._present_fund(e) for e in
                      sorted(fund_coverage, key=lambda e: -e["market_value_eur"])],
            "oldest_basket_as_of": min(basket_dates) if basket_dates else None,
            "unvaluable_positions": len(unvaluable),
            "unvaluable_symbols": [p["symbol"] for p in unvaluable],
            "identity": aggregates,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ classification

    @staticmethod
    def _split_by_valuability(positions: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """
        Partition positions into ones that can be valued and ones that cannot.

        The server-side test is `market_value_eur > 0` alone, exactly as
        `dividend_service`'s forward-yield filter does it — cited rather than reinvented so
        this does not become another copy of the same predicate. Both ways of failing to
        value a holding (no cached price, and no FX rate for its price currency) zero the
        value here, which is why the two-clause form `rebalance.ts` needs does not apply:
        the client cannot distinguish them, the server does not have to.

        An unvaluable *fund* is the sharpest silent failure this feature has. It does not
        render a visible zero — it renders as the absence of its companies from rows that
        still say "% of portfolio" — so it is excluded from both sides and named.
        """
        valuable, unvaluable = [], []
        for pos in positions:
            mv = Decimal(str(pos.get("market_value_eur") or 0))
            enriched = dict(pos, _mv=mv)
            (valuable if mv > 0 else unvaluable).append(enriched)
        return valuable, unvaluable

    @staticmethod
    def _fund_status(isin: str, basket) -> Tuple[str, Optional[str]]:
        """Whether this fund can be looked through, and the reason when it cannot."""
        source = source_for_fund_isin(isin)
        if source and not source.look_through_eligible:
            return "excluded", source.exclude_reason
        if basket is None:
            if source and source.adapter == "manual":
                return "no_basket", (
                    "No machine-readable holdings file is published for this fund; its "
                    "basket has to be downloaded and imported by hand."
                )
            if source is None:
                return "no_basket", "No holdings source has been declared for this fund."
            return "no_basket", "No basket has been fetched for this fund yet."
        if Decimal(basket.equity_weight_pct) < MIN_BASKET_COVERAGE_PCT:
            return "implausible", (
                f"The stored basket accounts for only "
                f"{Decimal(basket.equity_weight_pct):.2f}% of the fund, which is too "
                f"little to attribute — treated as no basket rather than as a fund "
                f"holding mostly cash."
            )
        return "looked_through", None

    @staticmethod
    def _is_nested_fund(row) -> bool:
        """
        Is this constituent itself a fund?

        Live risk rather than theory: iShares baskets carry the BlackRock ICS liquidity
        fund as a line item, which would otherwise render as a top-50 "company". v1 does
        not recurse — that needs a depth cap and a visited set, because a feeder fund can
        hold its own share class — so these land in a named bucket instead.

        The asset-class substring catches the funds our own table has never heard of,
        without a speculative list of every label an issuer might use.
        """
        if row.constituent_isin and is_known_etf_isin(row.constituent_isin):
            return True
        return "fund" in (row.asset_class or "").strip().lower()

    @classmethod
    def _counts_as_company(cls, row, basket) -> bool:
        """A row contributes to a company only if it is equity and is not itself a fund."""
        if cls._is_nested_fund(row):
            return False
        if not basket.asset_class_available:
            # The issuer publishes no asset class (Xtrackers), so the whitelist cannot be
            # applied. Every row counts and the API says the filter did not run — never
            # inferred from "it has an ISIN", which a bond or a money-market line also has.
            return True
        return (row.asset_class or "").strip().lower() in EQUITY_ASSET_CLASSES

    @classmethod
    def _split_weights(cls, rows, basket) -> Tuple[Decimal, Decimal]:
        """(company weight %, nested-fund weight %) over a basket's rows."""
        company = Decimal("0")
        nested = Decimal("0")
        for row in rows:
            weight = Decimal(row.weight_pct)
            if cls._is_nested_fund(row):
                nested += weight
            elif cls._counts_as_company(row, basket):
                company += weight
        return company, nested

    # ---------------------------------------------------------------------- identities

    async def _attach_identities(
        self, members: List[IdentityMember]
    ) -> List[IdentityMember]:
        """Fill each member's LEI / shareClassFIGI / names from the identity cache."""
        repo = IsinIdentityRepository(self.db)
        known = await repo.get_map([m.isin for m in members if m.isin])
        out = []
        for member in members:
            row = known.get((member.isin or "").strip().upper())
            if row is None:
                out.append(member)
                continue
            out.append(
                IdentityMember(
                    ref=member.ref,
                    isin=member.isin,
                    share_class_figi=row.share_class_figi,
                    lei=row.lei,
                    legal_name=row.issuer_name,
                    figi_name=row.figi_name,
                    fallback_name=member.fallback_name,
                    weight=member.weight,
                )
            )
        return out

    # ------------------------------------------------------------------------- assembly

    def _build_rows(
        self, groups, contributions: Dict[str, Dict], total: Decimal
    ) -> Tuple[List[Dict], Dict]:
        """Turn identity groups into company rows, and count what identified them."""
        rows: List[Dict] = []
        by_key_type = {"lei": 0, "share_class_figi": 0, "isin": 0, "unidentified": 0}
        partially = 0
        unresolved_isins = set()
        unresolved_value = Decimal("0")

        for group in groups:
            direct = Decimal("0")
            via_funds: Dict[str, Dict] = {}
            listings: List[str] = []
            value = Decimal("0")

            for member in group.members:
                contribution = contributions.get(member.ref)
                if contribution is None:
                    continue
                value += contribution["value"]
                if contribution["kind"] == "direct":
                    direct += contribution["value"]
                    listings.append(contribution["listing"])
                else:
                    fund = via_funds.setdefault(
                        contribution["fund_isin"],
                        {
                            "symbol": contribution["fund_symbol"],
                            "fund_isin": contribution["fund_isin"],
                            "value_eur": Decimal("0"),
                            "weight_pct": Decimal("0"),
                        },
                    )
                    fund["value_eur"] += contribution["value"]
                    fund["weight_pct"] += contribution["weight_pct"]

                if not member.lei and not member.share_class_figi and member.isin:
                    unresolved_isins.add(member.isin.strip().upper())
                    unresolved_value += contribution["value"]

            by_key_type[group.key_type] = by_key_type.get(group.key_type, 0) + 1
            if group.partially_resolved:
                partially += 1

            rows.append({
                "company_key": group.key,
                "key_type": group.key_type,
                "name": group.name,
                "value_eur": value,
                "direct_value_eur": direct,
                "via_funds_value_eur": value - direct,
                "isins": group.isins,
                "listings": sorted(listings),
                "via_funds": sorted(
                    via_funds.values(), key=lambda f: -f["value_eur"]
                ),
                "partially_resolved": group.partially_resolved,
                "identity_conflicts": group.override_conflicts,
            })

        # Descending value, then key ascending. The tie-break is not cosmetic: thousands of
        # constituent rows sit at effectively zero, and without a total order the tail
        # membership would shuffle between requests and a company would appear and vanish.
        rows.sort(key=lambda r: (-r["value_eur"], r["company_key"]))

        aggregates = {
            "resolved_by_lei": by_key_type.get("lei", 0),
            "resolved_by_share_class_figi": by_key_type.get("share_class_figi", 0),
            "resolved_by_isin": by_key_type.get("isin", 0),
            "unidentified_groups": by_key_type.get("unidentified", 0),
            "partially_resolved_groups": partially,
            "unresolved_isins": len(unresolved_isins),
            "unresolved_value_eur": self._q(unresolved_value),
        }
        return rows, aggregates

    def _coverage_warnings(
        self,
        fund_coverage: List[Dict],
        uncovered: Decimal,
        total: Decimal,
        unvaluable: List[Dict],
    ) -> List[str]:
        """State the gaps in prose, so a partial answer cannot read as a complete one."""
        out: List[str] = []
        if uncovered > 0 and total > 0:
            missing = [
                e for e in fund_coverage if e["status"] != "looked_through"
            ]
            names = ", ".join(
                f"{e['symbol']}" for e in sorted(
                    missing, key=lambda e: -e["market_value_eur"]
                )
            )
            out.append(
                f"{self._pct(uncovered, total):.1f}% of the portfolio sits in funds whose "
                f"constituents are not known ({names}), so every company figure below "
                f"excludes whatever those funds hold."
            )
        if unvaluable:
            out.append(
                f"{len(unvaluable)} holding(s) could not be valued and are excluded "
                f"entirely: {', '.join(p['symbol'] for p in unvaluable)}."
            )
        return out

    # -------------------------------------------------------------------- presentation

    @staticmethod
    def _q(value: Decimal) -> float:
        """Round for the wire. Totals are always summed BEFORE this, never after."""
        return float(round(value, 2))

    @staticmethod
    def _pct(part: Decimal, whole: Decimal) -> float:
        return float(round(part / whole * HUNDRED, 2)) if whole else 0.0

    def _present_row(self, row: Dict, total: Decimal) -> Dict:
        return {
            **row,
            "value_eur": self._q(row["value_eur"]),
            "direct_value_eur": self._q(row["direct_value_eur"]),
            "via_funds_value_eur": self._q(row["via_funds_value_eur"]),
            "pct_of_portfolio": self._pct(row["value_eur"], total),
            "via_funds": [
                {
                    "symbol": f["symbol"],
                    "fund_isin": f["fund_isin"],
                    "value_eur": self._q(f["value_eur"]),
                    "weight_pct": float(round(f["weight_pct"], 4)),
                }
                for f in row["via_funds"]
            ],
        }

    def _present_fund(self, entry: Dict) -> Dict:
        return {
            **entry,
            "market_value_eur": self._q(entry["market_value_eur"]),
            "residual_eur": self._q(entry["residual_eur"]),
            "equity_weight_pct": (
                float(round(entry["equity_weight_pct"], 4))
                if entry["equity_weight_pct"] is not None
                else None
            ),
        }

    def _empty(
        self, today: date, base_currency: str, unvaluable: List[Dict], warnings: List[str]
    ) -> Dict:
        return {
            "as_of": today.isoformat(),
            "base_currency": base_currency,
            "total_market_value_eur": 0.0,
            "direct_equity_eur": 0.0,
            "looked_through_equity_eur": 0.0,
            "fund_residual_eur": 0.0,
            "nested_fund_eur": 0.0,
            "uncovered_fund_eur": 0.0,
            "coverage_pct": 0.0,
            "companies": [],
            "companies_shown": 0,
            "company_count_total": 0,
            "shown_value_eur": 0.0,
            "other_companies_eur": 0.0,
            "other_companies_count": 0,
            "funds": [],
            "oldest_basket_as_of": None,
            "unvaluable_positions": len(unvaluable),
            "unvaluable_symbols": [p["symbol"] for p in unvaluable],
            "identity": {
                "resolved_by_lei": 0,
                "resolved_by_share_class_figi": 0,
                "resolved_by_isin": 0,
                "unidentified_groups": 0,
                "partially_resolved_groups": 0,
                "unresolved_isins": 0,
                "unresolved_value_eur": 0.0,
            },
            "warnings": warnings,
        }
