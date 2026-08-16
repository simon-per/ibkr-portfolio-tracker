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
from app.services.etf_basket_parsers import counts_as_invested
from app.services.sector_taxonomy import UNKNOWN, normalise as normalise_sector

logger = logging.getLogger(__name__)

HUNDRED = Decimal("100")

# Below this, a basket is treated as *absent* rather than as a fund holding mostly cash.
# A residual is the right answer for EMIM's real 98.04; at Σ=3 — a half-parsed file — "this
# fund is 97% cash" is a plausible figure, which is the dangerous kind.
MIN_BASKET_COVERAGE_PCT = Decimal("80")

# Past this a basket is served but badged, because today's fund value times an old basket shows
# exposure to a company that has left the index. Default for an adapter not named below.
BASKET_STALE_DAYS = 45

# **Per adapter, because otherwise the badge means two different things.** "Stale" is only useful
# if it says *the issuer has newer data we failed to fetch* — not *this feed is inherently slow*.
# Xtrackers and iShares republish daily, so a week-old basket there really is a missed fetch and
# worth acting on. Vanguard US publishes month-end and lags ~6 weeks by design, so a single global
# 45-day rule would badge it permanently for doing exactly what it says it does — and a badge that
# can never clear is the pathology the always-present Flex banner already taught this codebase.
# A future quarterly source (SEC N-PORT arrives 75-136 days old) would need its own entry for the
# same reason; do not "fix" that by raising the global default.
ADAPTER_STALE_DAYS = {
    "dws": 7,
    "blackrock": 7,
    "vanguard_us": 75,
    "manual": 45,
}


def stale_after_days(adapter: Optional[str]) -> int:
    """How old this adapter's basket may get before the age is worth reporting."""
    return ADAPTER_STALE_DAYS.get((adapter or "").strip().lower(), BASKET_STALE_DAYS)

# The API will not return more rows than this however large a `limit` is asked for.
COMPANY_LIMIT_MAX = 200

# How far down the constituent ranking identity resolution is worth taking, as a share of
# cumulative look-through value. A share rather than an amount because the base currency is
# user-switchable, and a fixed floor would silently change the resolved set when the user flips
# a display toggle. The cap bounds one run's duration — GLEIF is one request per ISIN at ~1/s.
IDENTITY_COVERAGE_TARGET_PCT = Decimal("99.5")
IDENTITY_MAX_ISINS = 500


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

        # `securities.sector` is the only sector a directly-held company has, and it reaches this
        # service nowhere else: `get_positions_breakdown()` builds its dicts from the ORM rows but
        # never copies the column. Widening that helper would change `/api/portfolio/positions` and
        # the `Position` contract for two consumers that do not want it, so the lookup is local.
        direct_sectors = await self._direct_sectors(direct_positions)

        for pos in direct_positions:
            ref = f"sec:{pos['security_id']}"
            direct_equity += pos["_mv"]
            contributions[ref] = {
                "kind": "direct",
                "value": pos["_mv"],
                "listing": f"{pos['symbol']}@{pos['exchange'] or '?'}",
                "sector": direct_sectors.get(pos["security_id"], UNKNOWN),
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
            age_days = (today - basket.as_of_date).days
            stale_limit = stale_after_days(basket.adapter)
            entry.update(
                basket_as_of=basket.as_of_date.isoformat(),
                stale=age_days > stale_limit,
                constituents=len(rows),
                equity_weight_pct=Decimal(basket.equity_weight_pct),
                asset_class_available=bool(basket.asset_class_available),
                source=basket.source,
            )
            if entry["stale"]:
                warnings.append(
                    f"{entry['symbol']}'s basket is dated {basket.as_of_date.isoformat()}, "
                    f"{age_days} days old against a {stale_limit}-day expectation for its "
                    f"source — its issuer has newer holdings that were not fetched, so its "
                    f"share of every company figure describes an older index."
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
                    # Already loaded — `get_holdings()` selects whole entities, so reading this
                    # costs no query and no column list to keep in step.
                    "sector": normalise_sector(row.sector),
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
            # The residual is published as the ROUNDED remainder, not as its own rounded sum.
            # Σ of five independently-rounded buckets is not the rounded total — on the real
            # book the two differed by a cent — and a partition that fails to close by a cent
            # is still a partition that fails to close, both for the test that pins it and for
            # anyone adding the columns up on screen. The residual is the right bucket to carry
            # it because "whatever is left of a decomposed fund" is literally its definition.
            "fund_residual_eur": self._residual_remainder(
                total, direct_equity, looked_through, nested_total, uncovered_total
            ),
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

    async def material_constituent_isins(
        self,
        coverage_target_pct: Decimal = IDENTITY_COVERAGE_TARGET_PCT,
        cap: int = IDENTITY_MAX_ISINS,
    ) -> List[str]:
        """
        Which constituent ISINs are worth spending a provider request on, largest first.

        **The naive form of this rule is circular** — it wants each ISIN's *company* value to
        decide whether to resolve its identity, and the identity is what builds companies. It is
        broken by ordering: aggregate by raw constituent ISIN, which needs no identity at all,
        then resolve the head of that ranking.

        The threshold is a share of cumulative value rather than an amount, because the base
        currency is user-switchable and a fixed floor would change the resolved set when a
        display toggle is flipped. `cap` then bounds one run's duration: VT alone contributes
        10,032 rows for under 2% of the book, and its tail rows are worth fractions of a cent
        and can never move a published figure.

        Directly-held ISINs are NOT included — those are resolved unconditionally elsewhere,
        being where all the user-visible folding lives.
        """
        from app.services.portfolio_service import PortfolioService

        positions = await PortfolioService(self.db).get_positions_breakdown()
        valuable, _ = self._split_by_valuability(positions)
        funds = [p for p in valuable if is_known_etf_isin(p["isin"])]
        if not funds:
            return []

        repo = EtfBasketRepository(self.db)
        held = sorted({p["isin"].strip().upper() for p in funds})
        baskets = await repo.get_baskets(held)
        holdings = await repo.get_holdings(list(baskets))

        by_isin: Dict[str, Decimal] = {}
        for pos in funds:
            isin = pos["isin"].strip().upper()
            basket = baskets.get(isin)
            if basket is None or self._fund_status(isin, basket)[0] != "looked_through":
                continue
            for row in holdings.get(isin, []):
                # The same predicates the read path uses, so this ranks exactly the rows that
                # can become company rows — not merely everything in the file.
                if not row.constituent_isin or not self._counts_as_company(row, basket):
                    continue
                key = row.constituent_isin.strip().upper()
                by_isin[key] = by_isin.get(key, Decimal("0")) + (
                    pos["_mv"] * Decimal(row.weight_pct) / HUNDRED
                )

        ranked = sorted(by_isin.items(), key=lambda kv: (-kv[1], kv[0]))
        grand_total = sum((v for _, v in ranked), Decimal("0"))
        if grand_total <= 0:
            return []

        out: List[str] = []
        running = Decimal("0")
        ceiling = grand_total * coverage_target_pct / HUNDRED
        for isin, value in ranked:
            if len(out) >= cap or running >= ceiling:
                break
            out.append(isin)
            running += value
        return out

    async def _direct_sectors(self, positions: List[Dict]) -> Dict[int, str]:
        """
        Canonical sector per directly-held security id, from `securities.sector`.

        Expect gaps and do not read them as breakage: only `POST /api/allocation/sync` writes that
        column and nothing schedules it, so an IBKR-ingested security carries NULL until someone
        runs it by hand. Measured on this account, 27 of 39 held securities have one — but the
        issuer baskets answer 97 of the 103 ISINs behind the top companies, so this is the *weaker*
        of the two sources and exists to cover holdings that appear in no fund at all.
        """
        if not positions:
            return {}
        from sqlalchemy import select
        from app.models.security import Security

        ids = [p["security_id"] for p in positions if p.get("security_id") is not None]
        if not ids:
            return {}
        result = await self.db.execute(
            select(Security.id, Security.sector).where(Security.id.in_(ids))
        )
        return {row[0]: normalise_sector(row[1]) for row in result.all()}

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

    @staticmethod
    def _is_invested(row, basket) -> bool:
        """
        Is this row a holding in a security at all, rather than cash or a derivative?

        Delegates to `etf_basket_parsers.counts_as_invested`, which is the same predicate the
        import used to compute `etf_baskets.equity_weight_pct`. Sharing it is what makes the
        stored figure and the value derived here from the same rows agree — a second copy is
        precisely how a stored total and a recomputed one come to disagree.
        """
        return counts_as_invested(row.asset_class, bool(basket.asset_class_available))

    @classmethod
    def _counts_as_company(cls, row, basket) -> bool:
        """A row contributes to a company only if it is invested and is not itself a fund."""
        return cls._is_invested(row, basket) and not cls._is_nested_fund(row)

    @classmethod
    def _split_weights(cls, rows, basket) -> Tuple[Decimal, Decimal]:
        """
        (company weight %, nested-fund weight %) over a basket's rows.

        The invested filter runs FIRST and the company/nested split second, so the two sum to
        exactly `equity_weight_pct` and the residual is whatever is left. Splitting before
        filtering would let a cash-classified row reach the nested bucket and make the three
        parts stop adding up to the fund.
        """
        company = Decimal("0")
        nested = Decimal("0")
        for row in rows:
            if not cls._is_invested(row, basket):
                continue
            weight = Decimal(row.weight_pct)
            if cls._is_nested_fund(row):
                nested += weight
            else:
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
            sector_votes: Dict[str, Decimal] = {}

            for member in group.members:
                contribution = contributions.get(member.ref)
                if contribution is None:
                    continue
                value += contribution["value"]
                # A company folded across several listings and funds can be classified more than
                # once, and the sources genuinely differ (a fund may carry a stale classification,
                # or none at all). Weight the vote by value so the largest contributor decides,
                # which is the rule `display_name()` already uses to pick the row's name — the two
                # must agree or a row could show one issuer's name beside another's sector.
                sector = contribution.get("sector") or UNKNOWN
                if sector != UNKNOWN:
                    sector_votes[sector] = (
                        sector_votes.get(sector, Decimal("0")) + contribution["value"]
                    )
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

            # Ties break on the sector name so the answer cannot depend on dict insertion order,
            # which depends on member order, which the caller is free to shuffle.
            sector = (
                max(sector_votes.items(), key=lambda kv: (kv[1], kv[0]))[0]
                if sector_votes
                else UNKNOWN
            )

            rows.append({
                "company_key": group.key,
                "key_type": group.key_type,
                "name": group.name,
                "sector": sector,
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

    @classmethod
    def _residual_remainder(
        cls,
        total: Decimal,
        direct: Decimal,
        looked_through: Decimal,
        nested: Decimal,
        uncovered: Decimal,
    ) -> float:
        """
        The residual bucket, derived so the five published figures close exactly.

        Rounds the other four first and subtracts them from the rounded total, so the identity
        holds in the numbers a caller actually receives rather than only in the Decimals behind
        them. The correction is at most a couple of cents; it can make the residual very
        slightly negative when an issuer's weights sum a hair above 100 (IWDA publishes
        100.01), which is honest — that fund really does attribute marginally more than its own
        value.
        """
        return float(
            round(total, 2)
            - round(direct, 2)
            - round(looked_through, 2)
            - round(nested, 2)
            - round(uncovered, 2)
        )

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
