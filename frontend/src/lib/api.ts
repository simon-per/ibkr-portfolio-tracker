/**
 * API Client for IBKR Portfolio Analyzer Backend
 */

const API_BASE_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export interface AppSettings {
  base_currency: string;
  supported_currencies: string[];
}

export interface PortfolioValuePoint {
  date: string;
  // NOTE: *_eur fields carry values in the selected base_currency (see base_currency).
  cost_basis_eur: number;
  market_value_eur: number;
  gain_loss_eur: number;
  gain_loss_percent: number;
  /**
   * Money entering (+) or leaving (−) the holdings that day: purchases at cost,
   * sales at market proceeds. Optional so older payloads still parse.
   */
  external_flow_eur?: number;
  /**
   * Held securities whose value could not be resolved that day. Above 0 means
   * `market_value_eur` is an **incomplete** sum while `cost_basis_eur` is not, so the
   * gain and percent understate by those holdings' whole value.
   *
   * A stalled market-data sync produces a smooth decay to zero rather than a gap: at 14
   * days past the last cached price some securities still resolve and the total reads a
   * plausible +15%, and at 15 days every one drops out and it reads −100%.
   *
   * Optional because a backend older than 2026-08-05 does not send it; absent is read as
   * complete, which is the only backward-compatible reading.
   */
  unpriced_holdings?: number;
  base_currency?: string;
}

export interface PortfolioSummary {
  // NOTE: *_eur fields carry values in the selected base_currency (see base_currency).
  total_cost_basis_eur: number;
  total_market_value_eur: number;
  total_gain_loss_eur: number;
  total_gain_loss_percent: number;
  num_positions: number;
  /**
   * Held securities whose value could not be resolved today. Above 0 means
   * `total_market_value_eur` is an **incomplete** sum while `total_cost_basis_eur` is
   * not, so the gain and percent understate by those holdings' whole value.
   *
   * Do not compute this on the client from `market_price === null`: the backend also
   * fails to value a holding when its FX rate is missing, so a client-side count
   * under-reports. Optional because a backend older than 2026-08-05 does not send it.
   */
  unpriced_holdings?: number;
  date?: string;
  base_currency?: string;
  total_realized_gain_loss_eur: number;
  total_realized_proceeds_eur: number;
  total_realized_cost_basis_eur: number;
  num_closed_positions: number;
}

export interface TaxLotInfo {
  open_date: string;
  quantity: number;
  cost_basis: number;
  cost_basis_eur: number;
}

export type MoneyInMethod = 'deposits' | 'spliced' | 'deployed';

export interface ContributionWindow {
  label: 'all' | '12m' | '6m' | '3m';
  months: number;          // divisor used, clamped to available history
  partial: boolean;        // history shorter than the nominal window
  // NOTE: *_eur fields carry values in the selected base_currency.
  // The answer. Spliced at the deposit-coverage boundary — lot cost basis before it,
  // real deposits after — so a position rotation cannot inflate it.
  money_in_eur: number;
  avg_money_in_per_month_eur: number;
  money_in_method: MoneyInMethod;
  deposits_eur: number;    // the post-boundary part, for the tooltip
  // Secondary: capital put to work. Exceeds money_in once positions are rotated.
  deployed_eur: number;
  avg_deployed_per_month_eur: number;
  net_eur: number;         // deployed minus released, tooltip only
}

export interface ContributionMonthlyItem {
  month: string;           // "YYYY-MM"
  deployed_eur: number;    // cost basis of lots opened in the month
  net_eur: number;         // deployed minus released in the month
}

export interface ContributionsResponse {
  windows: ContributionWindow[];
  monthly: ContributionMonthlyItem[];
  first_contribution_date: string | null;
  coverage_from: string | null;    // the splice point: deposits are complete from here
  deposits_from: string | null;    // the first actual deposit row
  transfer_in_date: string | null; // the incoming broker transfer that explains it
  base_currency: string;
}

export interface AnalystRating {
  strong_buy: number;
  buy: number;
  hold: number;
  sell: number;
  strong_sell: number;
  total_ratings: number;
  consensus: string;
  last_updated: string;
}

export interface Position {
  security_id: number;
  symbol: string;
  description: string;
  isin: string;
  currency: string;
  exchange: string | null;
  quantity: number;
  cost_basis_eur: number;
  market_value_eur: number;
  market_price: number | null;
  gain_loss_eur: number;
  gain_loss_percent: number;
  taxlots: TaxLotInfo[];
  analyst_rating: AnalystRating | null;
}

export interface AnnualizedReturnResponse {
  method: string;
  annualized_return_pct: number | null;
  start_date: string;
  end_date: string;
  num_cash_flows: number;
  /**
   * Held securities the backend could not value at one of the window's two endpoints.
   * Above 0 means this return was measured against a partial book: the tax-lot purchases
   * are unconditional cash flows while the endpoint valuation omits those holdings, so
   * the figure understates (or overstates, when the *start* is the short one).
   *
   * Optional for the same reason the timeline's field is: absent means an older backend,
   * read as complete, rather than a permanent warning on every chart.
   */
  unpriced_holdings?: number;
}

export interface SchedulerJob {
  id: string;
  name: string;
  next_run_time: string | null;
  trigger: string;
}

export interface SchedulerStatus {
  status: string;
  jobs: SchedulerJob[];
  last_sync: {
    type: string;
    timestamp: string;
    status: string;
    /** Populated on failures — e.g. the IBKR Code=1025 lockout explanation. */
    message?: string | null;
    /** Flex XML schema drift the sanitizer worked around, skipped currencies, etc. */
    warnings?: string[] | null;
    details?: Record<string, unknown> | null;
  } | null;
  message?: string;
}

export interface SyncRun {
  type: string;
  status: string;
  timestamp: string | null;
  message?: string | null;
  warnings?: string[] | null;
  details?: Record<string, unknown> | null;
}

export interface SyncHistory {
  count: number;
  runs: SyncRun[];
}

export interface BenchmarkInfo {
  key: string;
  name: string;
  ticker: string;
  currency: string;
}

export interface BenchmarkValuePoint {
  date: string;
  benchmark_value_eur: number;
  cost_basis_eur: number;
  gain_loss_eur: number;
  gain_loss_percent: number;
}

export interface BenchmarkResponse {
  benchmark_name: string;
  benchmark_ticker: string;
  data: BenchmarkValuePoint[];
}

export interface SecurityAttribution {
  security_id: number;
  symbol: string;
  description: string;
  start_market_value_eur: number;
  end_market_value_eur: number;
  new_investment_eur: number;
  value_change_eur: number;
  pnl_contribution_eur: number;
  contribution_percent: number;
  weight_percent: number;
}

export interface PerformanceAttributionResponse {
  start_date: string;
  end_date: string;
  total_pnl_eur: number;
  /**
   * Securities left out because they could not be valued at an endpoint — no cached
   * price, or a price whose currency has no FX rate. Above 0 means `total_pnl_eur` and
   * every bar's `contribution_percent` cover less than the whole book.
   *
   * Optional because the field only exists from 2026-08-05: absent means complete, the
   * same choice the timeline's `unpriced_holdings` and `externalFlow` both make.
   * Reading `undefined` as incomplete would badge every chart served by an older
   * backend.
   */
  unpriced_holdings?: number;
  attributions: SecurityAttribution[];
}

export interface AllocationPosition {
  symbol: string;
  description: string;
  weight: number;
  market_value_eur: number;
  is_etf_contribution: boolean;
}

export interface AllocationCategory {
  percentage: number;
  market_value_eur: number;
  positions: AllocationPosition[];
}

export interface PortfolioAllocationResponse {
  sector_allocation: Record<string, AllocationCategory>;
  geographic_allocation: Record<string, AllocationCategory>;
  asset_type_allocation: Record<string, AllocationCategory>;
  total_market_value_eur: number;
}

/**
 * Look-through exposure. Names match the Pydantic models exactly, which is what lets
 * `tests/test_api_contract_drift.py` pair them with no hand-maintained list — so a field
 * declared here that the backend stops sending fails the backend suite rather than arriving
 * as a silent `undefined`.
 *
 * All `*_eur` fields are in `base_currency`, as everywhere else in this client.
 */
export interface LookthroughFundContribution {
  symbol: string | null;
  fund_isin: string;
  value_eur: number;
  /** The company's weight inside that fund. */
  weight_pct: number;
}

export interface LookthroughCompanyRow {
  company_key: string;
  key_type: 'lei' | 'share_class_figi' | 'isin' | 'unidentified';
  name: string;
  /**
   * A canonical sector, or 'Unknown'. Groups the treemap and nothing else — there is no
   * portfolio-level sector total on this response, deliberately: that would be a second answer
   * beside the Allocation tab's, over a different denominator.
   */
  sector: string;
  value_eur: number;
  /** Share of the WHOLE portfolio — never renormalised onto the decomposed subset. */
  pct_of_portfolio: number;
  direct_value_eur: number;
  via_funds_value_eur: number;
  isins: string[];
  listings: string[];
  via_funds: LookthroughFundContribution[];
  /** A member of this row was folded on its ISIN alone, so a sibling ISIN could be missing. */
  partially_resolved: boolean;
  identity_conflicts: string[];
}

export interface LookthroughFundCoverage {
  symbol: string | null;
  fund_isin: string;
  market_value_eur: number;
  status: 'looked_through' | 'no_basket' | 'excluded' | 'implausible';
  reason: string | null;
  basket_as_of: string | null;
  stale: boolean;
  constituents: number;
  equity_weight_pct: number | null;
  residual_eur: number;
  asset_class_available: boolean;
  source: string | null;
  /** Set when this fund publishes no basket and another fund's stood in — that fund's symbol. */
  proxy_for_symbol: string | null;
  /** Rows the issuer published at 0.00%. For a broad fund this IS the residual, and it is rounding rather than cash. */
  unweighted_constituents: number;
}

export interface LookthroughIdentityCoverage {
  resolved_by_lei: number;
  resolved_by_share_class_figi: number;
  resolved_by_isin: number;
  unidentified_groups: number;
  partially_resolved_groups: number;
  unresolved_isins: number;
  unresolved_value_eur: number;
}

export interface LookthroughResponse {
  as_of: string;
  base_currency: string;
  total_market_value_eur: number;
  /** These five buckets partition total_market_value_eur exactly. */
  direct_equity_eur: number;
  looked_through_equity_eur: number;
  fund_residual_eur: number;
  nested_fund_eur: number;
  uncovered_fund_eur: number;
  /** Share of the portfolio attributed to companies. Read this before any row. */
  coverage_pct: number;
  companies: LookthroughCompanyRow[];
  companies_shown: number;
  company_count_total: number;
  shown_value_eur: number;
  other_companies_eur: number;
  other_companies_count: number;
  funds: LookthroughFundCoverage[];
  oldest_basket_as_of: string | null;
  unvaluable_positions: number;
  unvaluable_symbols: string[];
  identity: LookthroughIdentityCoverage;
  warnings: string[];
}

export interface SyncResponse {
  message: string;
  // 'skipped' means IBKR had already generated today's statement, so nothing was
  // fetched and nothing is wrong — it issues about one per US-Eastern day. The counts
  // are absent on that branch, which is why they are optional: rendering
  // `Securities: {securities_synced}` unconditionally printed "undefined".
  status?: 'success' | 'skipped';
  reason?: string;
  last_success_at?: string;
  next_attempt_after?: string;
  securities_synced?: number;
  taxlots_synced?: number;
  warnings?: string[];
}

export interface FundamentalMetrics {
  security_id: number;
  symbol: string;
  description: string;
  exchange: string | null;
  currency: string;
  quote_type: string | null;
  trailing_pe: number | null;
  forward_pe: number | null;
  peg_ratio: number | null;
  price_to_sales: number | null;
  price_to_book: number | null;
  revenue_growth: number | null;
  earnings_growth: number | null;
  fwd_revenue_growth: number | null;
  fwd_eps_growth: number | null;
  profit_margins: number | null;
  gross_margins: number | null;
  operating_margins: number | null;
  market_cap: number | null;
  number_of_analysts: number | null;
  target_mean_price: number | null;
  target_high_price: number | null;
  target_low_price: number | null;
  data_currency: string | null;
  last_updated: string | null;
}

export interface EarningsCalendarItem {
  security_id: number;
  symbol: string;
  description: string;
  earnings_date: string;
  eps_estimate: number | null;
}

export interface EarningsHistoryItem {
  security_id: number;
  symbol: string;
  description: string;
  earnings_date: string;
  eps_estimate: number | null;
  reported_eps: number | null;
  surprise_percent: number | null;
  beat_or_miss: string | null;
}

export interface FundamentalsStatus {
  total_securities: number;
  securities_with_data: number;
  securities_without_data: number;
  stale_metrics: number;
  total_earnings_events: number;
  oldest_update: string | null;
  newest_update: string | null;
}

export interface FundamentalsSyncResponse {
  status: string;
  securities_processed: number;
  metrics_updated: number;
  earnings_updated: number;
  errors: number;
  message: string;
}

export interface WatchlistItem {
  id: number;
  yahoo_ticker: string;
  symbol: string | null;
  company_name: string | null;
  notes: string | null;
  target_price: number | null;
  current_price: number | null;
  currency: string | null;
  trailing_pe: number | null;
  forward_pe: number | null;
  peg_ratio: number | null;
  ev_to_ebitda: number | null;
  revenue_growth: number | null;
  earnings_growth: number | null;
  fwd_revenue_growth: number | null;
  fwd_eps_growth: number | null;
  profit_margins: number | null;
  market_cap: number | null;
  analyst_target: number | null;
  analyst_rating: string | null;
  analyst_count: number | null;
  week52_high: number | null;
  week52_low: number | null;
  pct_from_52w_high: number | null;
  ma200: number | null;
  ma50: number | null;
  pct_from_ma200: number | null;
  rsi14: number | null;
  buy_score: number | null;
  data_currency: string | null;
  last_synced: string | null;
  created_at: string | null;
}

export interface DividendMonthlyItem {
  month: string;
  amount_eur: number;
}

export interface DividendSummaryResponse {
  /** NET of withholding, era-spliced. The `_eur` suffix is legacy — these are base currency. */
  monthly: DividendMonthlyItem[];
  ytd_eur: number;
  /** NET (back-compat key for total_net_eur). */
  total_eur: number;
  total_net_eur: number;
  total_gross_eur: number;
  total_withholding_eur: number;
  /** 'mixed' is the boundary era: estimates before `ibkr_from`, actuals from there on. */
  source: 'ibkr' | 'mixed' | 'yfinance_estimate';
  ibkr_from: string | null;
  base_currency: string;
  last_updated: string | null;
  sync_in_progress: boolean;
}

export interface DividendMonthBar {
  month: string;                    // "YYYY-MM"
  /**
   * Growth of the REALIZED figure only, vs the previous month and the same month
   * a year earlier. null when the base is zero (constant for a quarterly payer)
   * or when the month holds only forecast.
   */
  mom_pct?: number | null;
  yoy_pct?: number | null;
  // NOTE: values carry the selected base_currency.
  actual: Record<string, number>;   // symbol -> net received
  forecast: Record<string, number>; // symbol -> projected net (cadence-based)
  actual_total_eur: number;
  forecast_total_eur: number;
}

export interface DividendSecurityRow {
  security_id: number;
  symbol: string;
  /** Set when the same ticker is listed on two venues (identity is isin + exchange). */
  exchange: string | null;
  description: string;
  payouts: number;
  gross_eur: number;
  withholding_eur: number;
  net_eur: number;
  forecast_payouts: number;
  forecast_net_eur: number;
  trailing_yield_pct: number | null;
  /**
   * The forward counterpart: this security's projection for the NEXT 12 MONTHS over its
   * current market value. Weight these by market value and they average to
   * `DividendForwardYield.pct` — which is what makes that headline auditable.
   *
   * Unwindowed, unlike `forecast_net_eur` above, so it does not change with the
   * selected year even though which rows appear does. A row can therefore show a zero
   * forecast beside a real yield when its next payment falls past the window.
   */
  forward_yield_pct: number | null;
  /**
   * True when the position wasn't held for the whole trailing year, so the yield
   * divides a partial year's income by a full position value and reads low.
   * Deliberately not annualized — that would invent income.
   */
  trailing_yield_partial: boolean;
  days_held_in_ttm: number | null;
  /**
   * Projected next-12-month income over what the position cost — the same numerator as
   * `forward_yield_pct`, so the gap between them is the holding's appreciation, and the
   * same definition the Performance tab's *Yield on Cost* card uses.
   *
   * Not trailing income over current cost: those describe different positions once the
   * size changes inside the window, which understated nine of fifteen rows.
   */
  yield_on_cost_pct: number | null;
  /** Share of the window's total (actual + forecast). */
  share_pct: number | null;
  next_pay_date: string | null;
  source: 'ibkr' | 'estimate' | 'mixed' | null; // null = forecast-only row
  /** 'net' = sized from dividends received; 'gross_estimate' = withholding not deducted. */
  forecast_basis: 'net' | 'gross_estimate' | null;
  /** How many dated payments defined the schedule. 2 is a guess with a schedule attached. */
  forecast_samples: number | null;
  /** Median gap the cadence settled on, in days. */
  forecast_cadence_days: number | null;
}

/** A figure beside the comparable it is measured against. */
export interface DividendDelta {
  net_eur: number;
  prev_net_eur: number | null;
  /** null whenever the base is zero — undefined, not large. Render a dash. */
  pct: number | null;
}

export interface DividendAnnualRow {
  year: number;
  net_eur: number;
  forecast_net_eur: number;
  /** net + forecast — what the bar length shows, and what yoy_pct compares. */
  total_eur: number;
  yoy_pct: number | null;
  /** This row or its comparable contains projection, so the chip is forward-looking. */
  yoy_includes_forecast: boolean;
  /** The prior year did not cover a full year, so the percentage overstates growth. */
  yoy_vs_partial: boolean;
  /** This year's realized figure covers less than the whole calendar year. */
  partial: boolean;
}

export interface DividendLatestMonth {
  month: string;
  net_eur: number;
  mom_pct: number | null;
  yoy_pct: number | null;
}

/**
 * Growth of dividend income, computed over the FULL history regardless of the
 * year filter — with a year selected the response carries no prior-year months,
 * so none of this could be derived client-side.
 */
export interface DividendGrowth {
  /** Trailing 12 months vs the 12 before. The headline: raw MoM swings ±90% on cadence. */
  ttm: DividendDelta;
  /** The two windows straddle the estimate/actual boundary — badge, don't equate. */
  ttm_crosses_era: boolean;
  /** Jan 1 → today vs Jan 1 → the same day last year. Strictly like-for-like. */
  ytd: DividendDelta;
  avg_month: DividendDelta;
  next_12m_eur: number;
  /** Forecast against measured — mark it as projected. */
  next_12m_vs_ttm_pct: number | null;
  annual: DividendAnnualRow[];
  latest_month: DividendLatestMonth | null;
}

/** One projected payment, dated — the dividend calendar. */
export interface DividendUpcomingPayment {
  date: string;
  security_id: number;
  symbol: string;
  net_eur: number;
  basis: 'net' | 'gross_estimate' | null;
}

/**
 * What the portfolio as held today will pay over the next twelve months, as a rate.
 *
 * A sibling of {@link DividendGrowth} rather than part of it: growth comes from the
 * unwindowed payment *history*, and a figure that moves with a market price is neither
 * growth nor history.
 *
 * The whole object is absent — never zeroed — when no projection ran, nothing held is
 * priced, or the priced holdings project nothing. `paying_holdings: 0` beside a null
 * percentage would read as "nothing in this portfolio pays a dividend", and the worst
 * case of that is when the one security that *does* pay is the unpriced one.
 */
export interface DividendForwardYield {
  /** Projected net income over the next 365 days, priced holdings only. */
  annual_eur: number;
  /** annual_eur over the market value of those holdings. */
  pct: number;
  /**
   * Same income over what they cost. Higher than `pct` on an appreciated book, and the
   * gap between the two is exactly that appreciation — which holds only because both
   * denominators cover the same securities.
   */
  on_cost_pct: number | null;
  /** An accumulating ETF counts as priced and not as paying. That is the right answer. */
  paying_holdings: number;
  priced_holdings: number;
  /** Held but unpriced, so excluded from both sides. Worth saying when non-zero. */
  unpriced_holdings: number;
  /** How much of `annual_eur` deducts no withholding, so the caveat can be quantified. */
  gross_estimate_eur: number;
  basis: 'net' | 'mixed' | 'gross_estimate';
}

export interface DividendBreakdownResponse {
  years: number[];
  year: number | null;              // null = all time
  months: DividendMonthBar[];
  securities: DividendSecurityRow[];
  total_net_eur: number;
  total_forecast_net_eur: number;
  /** Era-splice boundary: estimates strictly before, IBKR actuals from here. */
  ibkr_from: string | null;
  base_currency: string;
  /** Optional so an older backend payload still parses. */
  growth?: DividendGrowth | null;
  /** null = no projection, nothing priced, or nothing projecting. Never a zeroed object. */
  forward_yield?: DividendForwardYield | null;
  upcoming?: DividendUpcomingPayment[];
}

export interface TaxDividendRow {
  symbol: string | null;
  description: string | null;
  isin: string | null;
  pay_date: string;
  gross: number;
  withholding: number;
  net: number;
}

export interface TaxCountryRow {
  /** ISIN prefix, e.g. "US", "CH", "DE". "??" when the ISIN is unknown. */
  country: string;
  positions: number;
  gross: number;
  withholding: number;
  net: number;
}

export interface TaxRealizedRow {
  symbol: string | null;
  trade_date: string;
  quantity: number | null;
  proceeds: number;
  cost_basis: number;
  gain_loss: number;
}

export interface TaxHoldingRow {
  symbol: string | null;
  quantity: number | null;
  market_value: number;
  cost_basis: number;
}

export interface TaxReport {
  year: number;
  base_currency: string;
  dividend_source: 'ibkr' | 'yfinance_estimate' | 'mixed';
  /** First IBKR payment date — the era-splice boundary; null before the ledger. */
  dividend_ibkr_from: string | null;
  dividend_income: TaxDividendRow[];
  /** Withholding grouped by source country — DA-1 is filed per country. */
  dividend_by_country: TaxCountryRow[];
  dividend_country_note: string;
  dividend_totals: { gross: number; withholding: number; net: number };
  realized_gains: TaxRealizedRow[];
  realized_source: 'trades' | 'closed_lot_estimate';
  /** Date the holdings snapshot is valued at — 31 Dec for past years, today otherwise. */
  holdings_as_of: string;
  realized_totals: { proceeds: number; cost_basis: number; gain_loss: number };
  holdings_snapshot: TaxHoldingRow[];
  /** null when the snapshot could not be built — a missing base, not a zero one. */
  holdings_snapshot_total: number | null;
  holdings_snapshot_error?: boolean;
  holdings_snapshot_note: string;
  /** Omitted rows, FX failures, a fallback taking over — badged in the UI and CSV. */
  warnings?: string[];
}

/** One event on the ledger. Fields a kind cannot fill are null, never 0. */
export type ActivityKind = 'trade' | 'dividend' | 'cash_flow' | 'corporate_action';

export interface ActivityRow {
  date: string;
  kind: ActivityKind;
  /** BUY/SELL, DEPOSITWITHDRAW/TRANSFER_IN, FORWARDSPLIT, ibkr/yfinance_estimate. */
  subtype: string;
  symbol: string | null;
  description: string;
  quantity: number | null;
  price: number | null;
  currency: string | null;
  /** Signed, in the base currency: money leaving the account is negative. */
  amount_base: number | null;
  realized_pnl_base: number | null;
  /** Cash flows only. A transfer is never money in — see the contributions splice. */
  counts_as_money_in: boolean | null;
  /** Dividends only: 'ibkr' has real withholding, 'yfinance_estimate' is a guess. */
  source: string | null;
  ib_key: string | null;
}

export interface ActivityResponse {
  items: ActivityRow[];
  total: number;
  limit: number;
  offset: number;
  base_currency: string;
  start_date: string;
  end_date: string;
}

export interface ActivityParams {
  startDate?: string;
  endDate?: string;
  kinds?: ActivityKind[];
  symbol?: string;
  limit?: number;
  offset?: number;
}

function activityQuery(params: ActivityParams): string {
  const q = new URLSearchParams();
  if (params.startDate) q.append('start_date', params.startDate);
  if (params.endDate) q.append('end_date', params.endDate);
  if (params.kinds?.length) q.append('kind', params.kinds.join(','));
  if (params.symbol) q.append('symbol', params.symbol);
  if (params.limit != null) q.append('limit', String(params.limit));
  if (params.offset) q.append('offset', String(params.offset));
  const s = q.toString();
  return s ? `?${s}` : '';
}

/** `/health`. Identifies the running build so a deploy can be confirmed from the UI. */
export interface HealthResponse {
  status: string;
  version: string;
  commit: string;
  scheduler_enabled: boolean;
  write_auth_enabled: boolean;
  request_id?: string | null;
}

/**
 * Where the admin key lives. localStorage, not a cookie: the key only ever authorises
 * writes, and a cookie would be attached to cross-site requests automatically, which is
 * precisely what a header-based scheme avoids.
 *
 * It is a shared secret for a single-tenant deployment, not a per-user credential —
 * anyone with the key has the same access, so it is stored per browser and never sent
 * anywhere but this API.
 */
export const API_KEY_STORAGE_KEY = 'ibkr-api-key';

export function getApiKey(): string {
  try {
    return localStorage.getItem(API_KEY_STORAGE_KEY) ?? '';
  } catch {
    // Private-mode Safari and some embedded webviews throw on access.
    return '';
  }
}

export function setApiKey(key: string): void {
  try {
    if (key) localStorage.setItem(API_KEY_STORAGE_KEY, key);
    else localStorage.removeItem(API_KEY_STORAGE_KEY);
  } catch {
    /* nothing we can do; the request will simply be refused */
  }
}

/** Thrown for a 401 so callers can tell "not allowed" from "went wrong". */
export class UnauthorizedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UnauthorizedError';
  }
}

/**
 * Turn an error body into something worth showing a person.
 *
 * FastAPI answers a *validation* failure (422) with `detail` as an **array of
 * objects**, not a string — `[{loc: ["query","year"], msg: "Input should be a
 * valid integer", ...}]`. Passing that straight to `new Error()` stringifies it
 * to `[object Object]`, which is what the user saw. Ordinary `HTTPException`s
 * (400, 404, 409) do send a string, so only the validation shape needed handling.
 *
 * The `loc` prefix names the container rather than the field — `query`, `body`,
 * `path` — so it is dropped: "year: Input should be a valid integer" is the part
 * that helps.
 */
export function describeErrorBody(body: unknown, status: number, statusText: string): string {
  const detail = (body as { detail?: unknown } | null | undefined)?.detail;

  if (typeof detail === 'string' && detail.trim()) return detail;

  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) => {
        const item = entry as { loc?: unknown; msg?: unknown };
        const field = Array.isArray(item?.loc)
          ? item.loc.filter((p) => p !== 'query' && p !== 'body' && p !== 'path').join('.')
          : '';
        const msg = typeof item?.msg === 'string' ? item.msg : '';
        if (field && msg) return `${field}: ${msg}`;
        return msg || field;
      })
      .filter(Boolean);
    if (parts.length) return parts.join('; ');
  }

  return `HTTP ${status}: ${statusText}`;
}

class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string = API_BASE_URL) {
    this.baseUrl = baseUrl;
  }

  private async request<T>(endpoint: string, options?: RequestInit): Promise<T> {
    const url = `${this.baseUrl}${endpoint}`;
    const apiKey = getApiKey();

    try {
      const response = await fetch(url, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          // Sent on every request rather than only the mutating ones: the client does
          // not model which routes are gated, and the backend ignores it on reads.
          ...(apiKey ? { 'X-API-Key': apiKey } : {}),
          ...options?.headers,
        },
      });

      if (!response.ok) {
        const error = await response.json().catch(() => null);
        const detail = describeErrorBody(error, response.status, response.statusText);
        if (response.status === 401) {
          // Distinguished so the UI can point at the admin key rather than showing a
          // raw HTTP error for what is really "you haven't unlocked writes".
          throw new UnauthorizedError(
            apiKey
              ? 'The saved admin key was rejected. Update it to make changes.'
              : 'This action needs the admin key. Add it via the lock button.'
          );
        }
        throw new Error(detail);
      }

      return response.json();
    } catch (error) {
      if (error instanceof Error) {
        throw error;
      }
      throw new Error('Network request failed');
    }
  }

  // Settings endpoints
  async getSettings(): Promise<AppSettings> {
    return this.request<AppSettings>('/api/settings');
  }

  async updateBaseCurrency(baseCurrency: string): Promise<AppSettings> {
    return this.request<AppSettings>('/api/settings/base-currency', {
      method: 'PUT',
      body: JSON.stringify({ base_currency: baseCurrency }),
    });
  }

  // Portfolio endpoints
  async getPortfolioValueOverTime(
    startDate?: string,
    endDate?: string
  ): Promise<PortfolioValuePoint[]> {
    const params = new URLSearchParams();
    if (startDate) params.append('start_date', startDate);
    if (endDate) params.append('end_date', endDate);

    const query = params.toString() ? `?${params.toString()}` : '';
    return this.request<PortfolioValuePoint[]>(`/api/portfolio/value-over-time${query}`);
  }

  async getPortfolioSummary(): Promise<PortfolioSummary> {
    return this.request<PortfolioSummary>('/api/portfolio/summary');
  }

  async getContributions(): Promise<ContributionsResponse> {
    return this.request<ContributionsResponse>('/api/portfolio/contributions');
  }

  async getAnnualizedReturn(startDate: string, endDate: string): Promise<AnnualizedReturnResponse> {
    return this.request<AnnualizedReturnResponse>(
      `/api/portfolio/annualized-return?start_date=${startDate}&end_date=${endDate}`
    );
  }

  async getPositions(): Promise<Position[]> {
    return this.request<Position[]>('/api/portfolio/positions');
  }

  async getPerformanceAttribution(startDate: string, endDate: string): Promise<PerformanceAttributionResponse> {
    return this.request<PerformanceAttributionResponse>(
      `/api/portfolio/attribution?start_date=${startDate}&end_date=${endDate}`
    );
  }

  async getAvailableBenchmarks(): Promise<BenchmarkInfo[]> {
    return this.request<BenchmarkInfo[]>('/api/portfolio/benchmarks');
  }

  async getBenchmarkComparison(
    startDate: string, endDate: string, benchmark: string = 'sp500'
  ): Promise<BenchmarkResponse> {
    return this.request<BenchmarkResponse>(
      `/api/portfolio/benchmark?start_date=${startDate}&end_date=${endDate}&benchmark=${benchmark}`
    );
  }

  // Sync endpoint
  //
  // `force` re-asks IBKR for a statement it has already generated today. It normally
  // cannot succeed and each refusal spends `Code=1025` lockout budget, so it exists
  // only for the case that genuinely resets the daily generation: editing the Flex
  // Query definition in the IBKR portal.
  async syncIBKRData(force: boolean = false): Promise<SyncResponse> {
    return this.request<SyncResponse>(`/api/sync/ibkr${force ? '?force=true' : ''}`, {
      method: 'POST',
    });
  }

  // Analyst ratings endpoints
  async syncAnalystRatings(): Promise<{ status: string; securities_processed: number; ratings_updated: number; errors: number; message: string }> {
    return this.request('/api/analyst-ratings/sync', {
      method: 'POST',
    });
  }

  async getAnalystRatingsStatus(): Promise<{ total_ratings: number; stale_ratings: number; fresh_ratings: number; oldest_update: string | null; newest_update: string | null }> {
    return this.request('/api/analyst-ratings/status');
  }

  // Look-through endpoints
  async getLookthrough(limit = 50): Promise<LookthroughResponse> {
    return this.request<LookthroughResponse>(`/api/portfolio/lookthrough?limit=${limit}`);
  }

  // Allocation endpoints
  async syncAllocationData(forceRefresh: boolean = false): Promise<{ securities_processed: number; securities_updated: number; errors: number; message: string }> {
    return this.request(`/api/allocation/sync?force_refresh=${forceRefresh}`, {
      method: 'POST',
    });
  }

  async getPortfolioAllocation(): Promise<PortfolioAllocationResponse> {
    return this.request('/api/allocation/portfolio');
  }

  async getAllocationStatus(): Promise<{
    total_securities: number;
    securities_with_data: number;
    securities_without_data: number;
    stale_securities: number;
    oldest_update: string | null;
    newest_update: string | null;
  }> {
    return this.request('/api/allocation/status');
  }

  // Scheduler endpoints
  async getSyncHistory(limit = 20): Promise<SyncHistory> {
    return this.request<SyncHistory>(`/api/scheduler/history?limit=${limit}`);
  }

  async getSchedulerStatus(): Promise<SchedulerStatus> {
    return this.request<SchedulerStatus>('/api/scheduler/status');
  }

  // Fundamentals endpoints
  async syncFundamentals(forceRefresh: boolean = false): Promise<FundamentalsSyncResponse> {
    return this.request(`/api/fundamentals/sync?force_refresh=${forceRefresh}`, {
      method: 'POST',
    });
  }

  async getPortfolioFundamentals(): Promise<FundamentalMetrics[]> {
    return this.request('/api/fundamentals/portfolio');
  }

  async getUpcomingEarnings(daysAhead: number = 90): Promise<EarningsCalendarItem[]> {
    return this.request(`/api/fundamentals/earnings/upcoming?days_ahead=${daysAhead}`);
  }

  async getEarningsHistory(daysBack: number = 365): Promise<EarningsHistoryItem[]> {
    return this.request(`/api/fundamentals/earnings/history?days_back=${daysBack}`);
  }

  async getFundamentalsStatus(): Promise<FundamentalsStatus> {
    return this.request('/api/fundamentals/status');
  }

  // Watchlist endpoints
  async getWatchlist(): Promise<WatchlistItem[]> {
    return this.request('/api/watchlist');
  }

  async addToWatchlist(yahooTicker: string, notes?: string, targetPrice?: number): Promise<WatchlistItem> {
    return this.request('/api/watchlist', {
      method: 'POST',
      body: JSON.stringify({
        yahoo_ticker: yahooTicker,
        notes: notes || null,
        target_price: targetPrice || null,
      }),
    });
  }

  async updateWatchlistItem(id: number, notes?: string, targetPrice?: number): Promise<WatchlistItem> {
    return this.request(`/api/watchlist/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({
        notes: notes ?? null,
        target_price: targetPrice ?? null,
      }),
    });
  }

  async removeFromWatchlist(id: number): Promise<void> {
    await this.request(`/api/watchlist/${id}`, { method: 'DELETE' });
  }

  async syncWatchlist(force: boolean = false): Promise<{ synced: number; errors: number; message: string }> {
    return this.request(`/api/watchlist/sync?force=${force}`, { method: 'POST' });
  }

  // Dividend endpoints
  async getDividendSummary(): Promise<DividendSummaryResponse> {
    return this.request<DividendSummaryResponse>('/api/dividends/summary');
  }

  async syncDividends(): Promise<{ status: string; message: string }> {
    return this.request('/api/dividends/sync', { method: 'POST' });
  }

  /** Read-only breakdown — unlike /summary it never enqueues a background sync. */
  async getDividendBreakdown(year?: number): Promise<DividendBreakdownResponse> {
    const params = new URLSearchParams();
    if (year !== undefined) params.set('year', String(year));
    const qs = params.toString();
    return this.request<DividendBreakdownResponse>(`/api/dividends/breakdown${qs ? `?${qs}` : ''}`);
  }

  // Tax endpoints
  async getTaxReport(year: number): Promise<TaxReport> {
    return this.request<TaxReport>(`/api/tax/report?year=${year}`);
  }

  /** Absolute URL for the CSV download (used as an <a href> target). */
  getTaxReportCsvUrl(year: number): string {
    return `${this.baseUrl}/api/tax/report.csv?year=${year}`;
  }

  // Activity ledger
  async getActivity(params: ActivityParams = {}): Promise<ActivityResponse> {
    return this.request<ActivityResponse>(`/api/portfolio/activity${activityQuery(params)}`);
  }

  /** <a href> target — a plain navigation, so the browser handles the download. */
  getActivityCsvUrl(params: ActivityParams = {}): string {
    return `${this.baseUrl}/api/portfolio/activity.csv${activityQuery(params)}`;
  }

  // Health check
  async healthCheck(): Promise<HealthResponse> {
    return this.request<HealthResponse>('/health');
  }
}

export const api = new ApiClient();
