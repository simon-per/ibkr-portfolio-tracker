"""
Look-through Schemas
Pydantic models for the company-level exposure endpoint.

Every `*_eur` field carries a value in the selected `base_currency`, as everywhere else
in this API — the suffix is historical.

**A `response_model` is a filter, so an undeclared key is dropped from the wire in
silence.** That is not a hypothetical here: `/api/dividends/summary` shipped a model
declaring five of ten keys and deleted the provenance fields the footnote read from. Every
honesty field the service computes is therefore declared below, and
`tests/test_lookthrough_partition.py` compares the service's key set against the model's
in *both* directions.

**There is deliberately no cost-basis or gain column anywhere in this response.** A
fund's cost cannot be split across its constituents without knowing the basket on each
purchase date, which nothing stores — so any per-company cost would be fabricated rather
than merely approximate. The market-value view is the honest one; do not "complete" it.
"""
from pydantic import BaseModel, Field
from typing import List, Optional


class LookthroughFundContribution(BaseModel):
    """One fund's contribution to one company."""
    symbol: Optional[str] = Field(None, description="Mapped fund symbol, for labelling only")
    fund_isin: str
    value_eur: float
    weight_pct: float = Field(..., description="The company's weight inside that fund")

    class Config:
        from_attributes = True


class LookthroughCompanyRow(BaseModel):
    """One company, folded across every listing and share class that reaches it."""
    company_key: str = Field(
        ...,
        description=(
            "Stable id for the row. Chosen from the group's identifiers "
            "(LEI > shareClassFIGI > lowest ISIN) *after* grouping, never used to group."
        ),
    )
    key_type: str = Field(
        ..., description="lei | share_class_figi | isin | unidentified"
    )
    name: str
    sector: str = Field(
        ...,
        description=(
            "One of `sector_taxonomy.CANONICAL_SECTORS`, or 'Unknown'. Resolved per company by "
            "value-weighted vote across its listings and the funds holding it, preferring the "
            "issuer basket over Yahoo. Used to GROUP the companies on this response and nothing "
            "else — there is deliberately no portfolio-level sector rollup here, because that "
            "would be a second answer beside the Allocation tab's, computed over a different "
            "denominator (this one covers only what could be decomposed)."
        ),
    )
    value_eur: float
    pct_of_portfolio: float = Field(
        ...,
        description=(
            "Share of the WHOLE portfolio, never of the decomposed subset. Renormalising "
            "onto covered value would inflate every row by the undecomposed share and is "
            "the same refusal rebalance.ts makes about targets — read `coverage_pct` "
            "beside this."
        ),
    )
    direct_value_eur: float = Field(
        ..., description="Value held as a direct position in this company"
    )
    via_funds_value_eur: float = Field(
        ..., description="Value held through funds. direct + via_funds == value_eur"
    )
    isins: List[str] = Field(default_factory=list)
    listings: List[str] = Field(
        default_factory=list,
        description="Directly-held listings folded into this row, as SYMBOL@EXCHANGE",
    )
    via_funds: List[LookthroughFundContribution] = Field(default_factory=list)
    partially_resolved: bool = Field(
        ...,
        description=(
            "At least one member of this row carries neither an LEI nor a "
            "shareClassFIGI, so it could only be folded on its own ISIN — an unseen "
            "sibling holding a DIFFERENT ISIN would not have joined, and the row may "
            "still be understated. Per row rather than a global count, because "
            "'3 unresolved' says nothing about which company is short."
        ),
    )
    identity_conflicts: List[str] = Field(
        default_factory=list,
        description=(
            "Set when more than one LEI was folded into this row — correct for a "
            "dual-listed company like Rio Tinto, and exactly what a wrong ISSUER_OVERRIDES "
            "entry also looks like, so it is reported rather than silently preferred."
        ),
    )

    class Config:
        from_attributes = True


class LookthroughFundCoverage(BaseModel):
    """What is known about one held fund's basket — including that nothing is."""
    symbol: Optional[str] = None
    fund_isin: str
    market_value_eur: float
    status: str = Field(
        ...,
        description=(
            "looked_through | no_basket | excluded | implausible | unvaluable. Anything "
            "but the first means this fund's companies are absent from the table, and "
            "its value sits in uncovered_fund_eur."
        ),
    )
    reason: Optional[str] = Field(
        None, description="Why, verbatim, when the status is not looked_through"
    )
    basket_as_of: Optional[str] = Field(
        None,
        description=(
            "The ISSUER's own as-of date, never when we fetched it. Vanguard publishes "
            "monthly and lags ~6 weeks, so today's fund value times a six-week-old basket "
            "shows exposure to a company that has left the index."
        ),
    )
    stale: bool = Field(
        False, description="basket_as_of is older than BASKET_STALE_DAYS"
    )
    constituents: int = 0
    equity_weight_pct: Optional[float] = Field(
        None,
        description=(
            "Σ of the equity rows actually stored. Genuinely below 100 for real funds "
            "(EMIM measures 98.04) because issuer files carry cash, FX and futures rows."
        ),
    )
    residual_eur: float = Field(
        0.0, description="This fund's value not attributed to any company"
    )
    asset_class_available: bool = Field(
        True,
        description=(
            "False when the issuer file carries no asset-class column (Xtrackers/DWS), so "
            "the equity whitelist could not be applied and the residual is 100 − Σ(all rows)."
        ),
    )
    source: Optional[str] = None
    proxy_for_symbol: Optional[str] = Field(
        None,
        description=(
            "Set when this fund publishes no basket and another fund's was used instead, "
            "naming that fund. Its companies are approximate — see the warning, which "
            "carries the reason the substitution is defensible and which way it errs. "
            "`status` stays looked_through because the value IS attributed; the proxy is "
            "a statement about provenance, not about the partition."
        ),
    )
    unweighted_constituents: int = Field(
        0,
        description=(
            "Rows the issuer published at 0.00%. For a broad fund this explains the whole "
            "residual and it is rounding, not cash: Vanguard prints weights to 2dp, so "
            "8,007 of VT's 10,032 holdings carry no weight and its file sums to 91.86%."
        ),
    )

    class Config:
        from_attributes = True


class LookthroughIdentityCoverage(BaseModel):
    """How much of the answer rests on which identifier."""
    resolved_by_lei: int = 0
    resolved_by_share_class_figi: int = 0
    resolved_by_isin: int = 0
    unidentified_groups: int = 0
    partially_resolved_groups: int = 0
    unresolved_isins: int = Field(
        0, description="ISINs with no LEI and no shareClassFIGI on record"
    )
    unresolved_value_eur: float = Field(
        0.0,
        description=(
            "Value sitting in those ISINs, which is the figure that matters. 14,000 "
            "unresolved ISINs worth 0.3% is fine and 3 worth 8% is not — a bare count "
            "reads as either at random."
        ),
    )

    class Config:
        from_attributes = True


class LookthroughResponse(BaseModel):
    """
    Company-level exposure: direct holdings plus every fund decomposed.

    The five value buckets form a PARTITION that must close exactly:

        direct_equity_eur + looked_through_equity_eur + fund_residual_eur
          + nested_fund_eur + uncovered_fund_eur  ==  total_market_value_eur

    That identity is the one assertion catching a dropped holding, a double-counted
    constituent and an accidental renormalisation at once, and it is pinned over buckets
    discovered from this response rather than a hardcoded list.
    """
    as_of: str
    base_currency: str = "EUR"
    total_market_value_eur: float = Field(
        ...,
        description=(
            "Σ over valuable positions — the same figure /api/portfolio/summary reports, "
            "from the same helper, so the two cannot disagree."
        ),
    )

    direct_equity_eur: float
    looked_through_equity_eur: float
    fund_residual_eur: float = Field(
        ...,
        description=(
            "The part of a decomposed fund not attributed to any company. Cash, FX and "
            "derivative rows — and also weight lost to rounding in the issuer's own file, "
            "which is the larger cause for a broad fund: Vanguard publishes percentWeight to "
            "2dp, so several thousand of VT's 10,032 holdings round to 0.00 and its weights "
            "sum to ~92%. Deliberately NOT renormalised away, since that would invent the "
            "attribution rather than report the gap."
        ),
    )
    nested_fund_eur: float = Field(
        ...,
        description=(
            "Value in constituents that are themselves funds. Not recursed into: iShares "
            "baskets carry the BlackRock ICS liquidity fund as a line item, which would "
            "otherwise render as a top-50 'company'."
        ),
    )
    uncovered_fund_eur: float = Field(
        ..., description="Funds with no usable basket. Named in `funds`, never dropped."
    )
    coverage_pct: float = Field(
        ...,
        description=(
            "Share of the portfolio actually attributed to companies. THE figure to read "
            "before any row: at 79%, 'Nvidia 6.1%' omits the Nvidia inside every "
            "undecomposed fund, and a plausible understatement invites no doubt."
        ),
    )

    companies: List[LookthroughCompanyRow] = Field(default_factory=list)
    companies_shown: int = 0
    company_count_total: int = 0
    shown_value_eur: float = 0.0
    other_companies_eur: float = Field(
        0.0,
        description=(
            "The truncated tail as a VALUE, not just a count — without it the visible "
            "percentages sum to well under 100 under a '% of portfolio' header."
        ),
    )
    other_companies_count: int = 0

    funds: List[LookthroughFundCoverage] = Field(default_factory=list)
    oldest_basket_as_of: Optional[str] = None
    unvaluable_positions: int = Field(
        0,
        description=(
            "Positions excluded from both sides because they could not be valued. An "
            "unpriced fund does not render a visible zero — it renders as the ABSENCE of "
            "its companies from rows that still say '% of portfolio'."
        ),
    )
    unvaluable_symbols: List[str] = Field(default_factory=list)
    identity: LookthroughIdentityCoverage = Field(
        default_factory=LookthroughIdentityCoverage
    )
    warnings: List[str] = Field(
        default_factory=list,
        description=(
            "Rides on a SUCCESSFUL response, so it is structurally invisible unless "
            "rendered — the frontend shows it above the table, outside any collapsible."
        ),
    )

    class Config:
        from_attributes = True
