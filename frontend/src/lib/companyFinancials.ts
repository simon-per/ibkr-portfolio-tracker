import type {
  CompanyFinancialsCagrs,
  CompanyFinancialsCompany,
  CompanyFinancialsKeyStats,
  CompanyFinancialsPeriod,
  CompanyFinancialsRange,
  CompanyFinancialsResponse,
  CompanyFinancialsSeries,
  CompanyFinancialsSeriesKey,
} from './api'
import { readStored, writeStored } from './storage'

/**
 * The Company tab's wiring: which series each card draws and how its figures print.
 *
 * Everything numeric — TTM sums, CAGR, YoY, margins, per-share figures — comes from
 * `/api/company-financials/{id}`. This module only arranges the response for Recharts; it
 * computes no figure, so a number on the card is the server's number.
 */

/** How a card's values print: compact money, per-share money (2 decimals), a count, or a fraction. */
/** `yield`: a percent with two decimals, so NVDA's 0.02% does not print as 0.0%. */
export type CompanyValueFormat = 'money' | 'perShare' | 'count' | 'percent' | 'yield' | 'multiple'

export interface CompanyChartSeries {
  key: CompanyFinancialsSeriesKey
  /** Legend and tooltip name; also the label of the latest figure on a `latest` card. */
  name: string
}

/** Total figures, or the same figures per diluted share (the tab's Total | Per share switch). */
export type CompanyBasis = 'total' | 'perShare'

/** What one card draws in one basis. */
export interface CompanyChartView {
  title: string
  format: CompanyValueFormat
  /** The first series with a value is the one the card's headline, CAGRs and YoY describe. */
  series: CompanyChartSeries[]
}

export interface CompanyChartDef {
  id: string
  kind: 'bar' | 'line'
  /** `growth`: CAGRs and YoY of the lead series. `latest`: no growth (margins, balances). */
  header: 'growth' | 'latest'
  /** Drawn only when some point has a value — a company that pays no dividend has no card. */
  optional?: boolean
  /** Fewer is the shareholder's gain (the share count): growth reads the other way. */
  inverse?: boolean
  total: CompanyChartView
  /** The card in the Per share basis; a card without one (shares, margins, DPS) is the same in both. */
  perShare?: CompanyChartView
}

export const COMPANY_CHARTS: readonly CompanyChartDef[] = [
  { id: 'revenue', kind: 'bar', header: 'growth',
    total: { title: 'Revenue', format: 'money', series: [{ key: 'revenue', name: 'Revenue' }] },
    perShare: { title: 'Revenue per share', format: 'perShare',
      series: [{ key: 'revenue_per_share', name: 'Revenue per share' }] } },
  { id: 'operating-income', kind: 'bar', header: 'growth',
    total: { title: 'Operating income', format: 'money',
      series: [{ key: 'operating_income', name: 'Operating income' }] } },
  { id: 'net-income', kind: 'bar', header: 'growth',
    total: { title: 'Net income', format: 'money', series: [{ key: 'net_income', name: 'Net income' }] },
    perShare: { title: 'EPS (diluted)', format: 'perShare', series: [{ key: 'eps_diluted', name: 'EPS' }] } },
  { id: 'fcf', kind: 'bar', header: 'growth',
    total: { title: 'Free cash flow', format: 'money',
      series: [{ key: 'fcf', name: 'FCF' }, { key: 'fcf_after_sbc', name: 'FCF after SBC' }] },
    perShare: { title: 'FCF per share', format: 'perShare',
      series: [{ key: 'fcf_per_share', name: 'FCF' }, { key: 'fcf_after_sbc_per_share', name: 'FCF after SBC' }] } },
  { id: 'margins', kind: 'line', header: 'latest',
    total: { title: 'Margins', format: 'percent', series: [
      { key: 'gross_margin', name: 'Gross' },
      { key: 'operating_margin', name: 'Operating' },
      { key: 'net_margin', name: 'Net' },
      { key: 'fcf_margin', name: 'FCF' },
    ] } },
  { id: 'valuation', kind: 'line', header: 'latest', optional: true,
    total: { title: 'Valuation', format: 'multiple', series: [
      { key: 'pe_ratio', name: 'P/E' },
      { key: 'pfcf_ratio', name: 'P/FCF' },
      { key: 'ps_ratio', name: 'P/S' },
    ] } },
  { id: 'roe', kind: 'line', header: 'latest', optional: true,
    total: { title: 'Return on equity', format: 'percent', series: [{ key: 'roe', name: 'ROE' }] } },
  { id: 'shares', kind: 'bar', header: 'growth', inverse: true,
    total: { title: 'Diluted shares', format: 'count', series: [{ key: 'diluted_shares', name: 'Shares' }] } },
  { id: 'capital-returns', kind: 'bar', header: 'growth', optional: true,
    total: { title: 'Buybacks & dividends', format: 'money', series: [
      { key: 'buybacks', name: 'Buybacks' },
      { key: 'dividends_paid', name: 'Dividends' },
    ] } },
  { id: 'cash-debt', kind: 'bar', header: 'latest', optional: true,
    total: { title: 'Cash & debt', format: 'money', series: [
      { key: 'cash', name: 'Cash & equivalents' },
      { key: 'debt', name: 'Debt' },
      { key: 'net_cash', name: 'Net cash' },
    ] } },
  { id: 'dps', kind: 'bar', header: 'growth', optional: true,
    total: { title: 'Dividends per share', format: 'perShare',
      series: [{ key: 'dividends_per_share', name: 'DPS' }] } },
]

/**
 * The view's series that have a value somewhere — a gross margin a card network never files
 * gets no legend entry — or its first series when none has.
 */
export function seriesWithData(response: CompanyFinancialsResponse, view: CompanyChartView): CompanyChartSeries[] {
  const present = view.series.filter((s) => hasAnyValue(seriesOf(response, s.key)))
  return present.length > 0 ? present : view.series.slice(0, 1)
}

/** The card as drawn in `basis`. */
export function chartView(chart: CompanyChartDef, basis: CompanyBasis): CompanyChartView {
  return basis === 'perShare' && chart.perShare ? chart.perShare : chart.total
}

/**
 * One x position: the period, each series' value there (`null` where it has none) and, under
 * `yoy`, the server's YoY of that point — what the card's header shows while it is hovered.
 */
export type CompanyChartRow = {
  period_end: string
  label: string
  yoy: Partial<Record<CompanyFinancialsSeriesKey, number | null>>
} & Partial<Record<CompanyFinancialsSeriesKey, number | null>>

/** A series the response lacks reads as empty, never as a crash or a zero. */
function seriesOf(
  response: CompanyFinancialsResponse,
  key: CompanyFinancialsSeriesKey,
): CompanyFinancialsSeries | undefined {
  return response.series?.[key]
}

/**
 * The card's rows: every period any of its series has, oldest first, each series' value
 * or `null`. A null stays null — Recharts draws no bar and breaks the line there — so a
 * gap in the filings is a gap on the chart and never a bar at zero.
 */
export function chartRows(
  response: CompanyFinancialsResponse,
  keys: readonly CompanyFinancialsSeriesKey[],
): CompanyChartRow[] {
  const byPeriod = new Map<string, CompanyChartRow>()
  for (const key of keys) {
    for (const point of seriesOf(response, key)?.points ?? []) {
      let row = byPeriod.get(point.period_end)
      if (!row) {
        row = { period_end: point.period_end, label: point.label, yoy: {} }
        for (const k of keys) {
          row[k] = null
          row.yoy[k] = null
        }
        byPeriod.set(point.period_end, row)
      }
      row[key] = point.value
      row.yoy[key] = point.yoy ?? null
    }
  }
  // ISO dates sort as strings.
  return [...byPeriod.values()].sort((a, b) => a.period_end.localeCompare(b.period_end))
}

const YEAR_END_LABEL = /^(?:Q4\s+)?FY(\d{4})$/

/** A fiscal-year end as an axis prints it: "Q4 FY2025" and "FY2025" → "FY25"; others unchanged. */
export function shortPeriodLabel(label: string): string {
  const m = YEAR_END_LABEL.exec(label)
  return m ? `FY${m[1].slice(2)}` : label
}

/**
 * The periods an x axis labels: fiscal-year ends only ("Q4 FY2025", "FY2025"), thinned to at
 * most `max` and always keeping the newest. A full "Q1 FY2016" under every fourth bar of a
 * 300px card ran into its neighbours. A window with fewer than two year ends spaces its own
 * periods instead.
 */
export function axisTicks(rows: readonly Pick<CompanyChartRow, 'label'>[], max: number): string[] {
  const yearEnds = rows.filter((r) => YEAR_END_LABEL.test(r.label)).map((r) => r.label)
  const pool = yearEnds.length >= 2 ? yearEnds : rows.map((r) => r.label)
  const step = Math.max(1, Math.ceil(pool.length / Math.max(1, max)))
  const ticks: string[] = []
  for (let i = pool.length - 1; i >= 0; i -= step) ticks.unshift(pool[i])
  return ticks
}

/** Whether a series has at least one value — the DPS card's and the no-data state's test. */
export function hasAnyValue(series: CompanyFinancialsSeries | undefined): boolean {
  return (series?.points ?? []).some((p) => p.value != null)
}

/** Whether any series in the response has a value at all. */
export function hasAnyData(response: CompanyFinancialsResponse): boolean {
  return Object.values(response.series ?? {}).some(hasAnyValue)
}

/** The cards to draw: every chart, less an optional one whose series are all empty. */
export function visibleCharts(
  response: CompanyFinancialsResponse,
  basis: CompanyBasis = 'total',
): CompanyChartDef[] {
  return COMPANY_CHARTS.filter(
    (chart) => !chart.optional
      || chartView(chart, basis).series.some((s) => hasAnyValue(seriesOf(response, s.key))),
  )
}

/** The newest point's value, `null` when that period has none (not an older period's value). */
export function latestValue(series: CompanyFinancialsSeries | undefined): number | null {
  const points = series?.points ?? []
  if (points.length === 0) return null
  const newest = points.reduce((a, b) => (b.period_end > a.period_end ? b : a))
  return newest.value
}

const NO_CAGRS: CompanyFinancialsCagrs = { y3: null, y5: null, y10: null }

export function growthOf(
  response: CompanyFinancialsResponse,
  key: CompanyFinancialsSeriesKey,
): { cagr: number | null; yoy: number | null; cagrs: CompanyFinancialsCagrs } {
  const series = seriesOf(response, key)
  return { cagr: series?.cagr ?? null, yoy: series?.yoy ?? null, cagrs: series?.cagrs ?? NO_CAGRS }
}

/** The CAGR spans a card's header quotes, shortest first. */
export const CAGR_SPANS: readonly { key: keyof CompanyFinancialsCagrs; label: string }[] = [
  { key: 'y3', label: '3Y' },
  { key: 'y5', label: '5Y' },
  { key: 'y10', label: '10Y' },
]

export type KeyStatKey = Exclude<keyof CompanyFinancialsKeyStats, 'label' | 'period_end'>

/** A figure under a key stat: its growth (`signed`), or a ratio beside it (`percent`). */
export interface KeyStatNote {
  key: KeyStatKey
  label: string
  format: 'signed' | 'percent'
}

export interface KeyStatDef {
  key: KeyStatKey
  label: string
  /** `signed`: a growth rate, printed with its sign and inked by it. */
  format: CompanyValueFormat | 'signed'
  note?: KeyStatNote
  /** Fewer is the shareholder's gain (the share count): growth reads the other way. */
  inverse?: boolean
}

/** The key figures strip, in reading order. A figure the company does not file is left out. */
export const KEY_STATS: readonly KeyStatDef[] = [
  { key: 'revenue', label: 'Revenue', format: 'money', note: { key: 'revenue_yoy', label: 'YoY', format: 'signed' } },
  { key: 'revenue_cagr_5y', label: 'Revenue CAGR 5Y', format: 'signed' },
  { key: 'net_income', label: 'Net income', format: 'money' },
  { key: 'eps_diluted', label: 'EPS', format: 'perShare', note: { key: 'eps_yoy', label: 'YoY', format: 'signed' } },
  { key: 'eps_cagr_5y', label: 'EPS CAGR 5Y', format: 'signed' },
  { key: 'market_cap', label: 'Market cap', format: 'money' },
  { key: 'enterprise_value', label: 'Enterprise value', format: 'money' },
  { key: 'net_cash', label: 'Net cash', format: 'money' },
  { key: 'ev_ebit', label: 'EV/EBIT', format: 'multiple' },
  { key: 'pe_ratio', label: 'P/E', format: 'multiple' },
  { key: 'ps_ratio', label: 'P/S', format: 'multiple' },
  { key: 'pfcf_ratio', label: 'P/FCF', format: 'multiple' },
  { key: 'fcf', label: 'Free cash flow', format: 'money', note: { key: 'fcf_yoy', label: 'YoY', format: 'signed' } },
  { key: 'fcf_per_share', label: 'FCF per share', format: 'perShare' },
  { key: 'gross_margin', label: 'Gross margin', format: 'percent' },
  { key: 'operating_margin', label: 'Operating margin', format: 'percent' },
  { key: 'net_margin', label: 'Net margin', format: 'percent' },
  { key: 'fcf_margin', label: 'FCF margin', format: 'percent' },
  { key: 'roe', label: 'Return on equity', format: 'percent' },
  { key: 'diluted_shares', label: 'Diluted shares', format: 'count', inverse: true,
    note: { key: 'shares_yoy', label: 'YoY', format: 'signed' } },
  { key: 'dividends_per_share', label: 'Dividends per share', format: 'perShare',
    note: { key: 'payout_ratio', label: 'Payout', format: 'percent' } },
  { key: 'dividend_yield', label: 'Dividend yield', format: 'yield' },
  { key: 'buybacks', label: 'Buybacks', format: 'money' },
  { key: 'dividends_paid', label: 'Dividends paid', format: 'money' },
]

/** The key stats the company has a figure for. */
export function visibleKeyStats(stats: CompanyFinancialsKeyStats | undefined): KeyStatDef[] {
  if (!stats) return []
  return KEY_STATS.filter((def) => stats[def.key] != null)
}

export function latestOf(response: CompanyFinancialsResponse, key: CompanyFinancialsSeriesKey): number | null {
  return latestValue(seriesOf(response, key))
}

export const PERIOD_OPTIONS: readonly { value: CompanyFinancialsPeriod; label: string }[] = [
  { value: 'quarterly', label: 'Quarterly' },
  { value: 'ttm', label: 'TTM' },
  { value: 'annual', label: 'Annual' },
]

export const BASIS_OPTIONS: readonly { value: CompanyBasis; label: string }[] = [
  { value: 'total', label: 'Total' },
  { value: 'perShare', label: 'Per share' },
]

export const RANGE_OPTIONS: readonly { value: CompanyFinancialsRange; label: string }[] = [
  { value: '5y', label: '5Y' },
  { value: '10y', label: '10Y' },
  { value: 'max', label: 'Max' },
]

/** Where this browser remembers the last company picked, by CIK (security ids until 2026-10-25). */
export const COMPANY_PICK_KEY = 'company-financials:cik'

export function readCompanyPick(): number | null {
  const raw = readStored(COMPANY_PICK_KEY)
  if (raw === null || !/^\d+$/.test(raw)) return null
  return Number(raw)
}

export function writeCompanyPick(cik: number): void {
  writeStored(COMPANY_PICK_KEY, String(cik))
}

/**
 * The company to show: the one picked, else the one this browser picked last, else the
 * first held, else the first. A pick no longer in the list (sold, or out of the index)
 * falls through rather than asking the server for a company it no longer lists.
 */
export function resolveCompany(
  companies: readonly CompanyFinancialsCompany[],
  picked: number | null,
  stored: number | null,
): CompanyFinancialsCompany | null {
  return (
    companies.find((c) => c.cik === picked)
    ?? companies.find((c) => c.cik === stored)
    ?? companies.find((c) => c.held)
    ?? companies[0]
    ?? null
  )
}

/** The picker's groups, in the server's order within each: held first, then the Nasdaq-100. */
export function companyGroups(
  companies: readonly CompanyFinancialsCompany[],
): { label: string; companies: CompanyFinancialsCompany[] }[] {
  return [
    { label: 'Held', companies: companies.filter((c) => c.held) },
    { label: 'Nasdaq-100', companies: companies.filter((c) => !c.held) },
  ].filter((g) => g.companies.length > 0)
}
