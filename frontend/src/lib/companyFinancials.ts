import type {
  CompanyFinancialsCompany,
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
export type CompanyValueFormat = 'money' | 'perShare' | 'count' | 'percent'

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
  /** First series is the one the card's headline, CAGR and YoY describe. */
  series: CompanyChartSeries[]
}

export interface CompanyChartDef {
  id: string
  kind: 'bar' | 'line'
  /** `growth`: CAGR and YoY of the first series. `latest`: no growth (the margins). */
  header: 'growth' | 'latest'
  /** Drawn only when some point has a value — a company that pays no dividend has no card. */
  optional?: boolean
  total: CompanyChartView
  /** The card in the Per share basis; a card without one (shares, margins, DPS) is the same in both. */
  perShare?: CompanyChartView
}

export const COMPANY_CHARTS: readonly CompanyChartDef[] = [
  { id: 'revenue', kind: 'bar', header: 'growth',
    total: { title: 'Revenue', format: 'money', series: [{ key: 'revenue', name: 'Revenue' }] },
    perShare: { title: 'Revenue per share', format: 'perShare',
      series: [{ key: 'revenue_per_share', name: 'Revenue per share' }] } },
  { id: 'net-income', kind: 'bar', header: 'growth',
    total: { title: 'Net income', format: 'money', series: [{ key: 'net_income', name: 'Net income' }] },
    perShare: { title: 'EPS (diluted)', format: 'perShare', series: [{ key: 'eps_diluted', name: 'EPS' }] } },
  { id: 'fcf', kind: 'bar', header: 'growth',
    total: { title: 'Free cash flow', format: 'money',
      series: [{ key: 'fcf', name: 'FCF' }, { key: 'fcf_after_sbc', name: 'FCF after SBC' }] },
    perShare: { title: 'FCF per share', format: 'perShare',
      series: [{ key: 'fcf_per_share', name: 'FCF' }, { key: 'fcf_after_sbc_per_share', name: 'FCF after SBC' }] } },
  { id: 'shares', kind: 'bar', header: 'growth',
    total: { title: 'Diluted shares', format: 'count', series: [{ key: 'diluted_shares', name: 'Shares' }] } },
  { id: 'margins', kind: 'line', header: 'latest',
    total: { title: 'Margins', format: 'percent', series: [
      { key: 'operating_margin', name: 'Operating' },
      { key: 'net_margin', name: 'Net' },
      { key: 'fcf_margin', name: 'FCF' },
    ] } },
  { id: 'dps', kind: 'bar', header: 'growth', optional: true,
    total: { title: 'Dividends per share', format: 'perShare',
      series: [{ key: 'dividends_per_share', name: 'DPS' }] } },
]

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

export function growthOf(
  response: CompanyFinancialsResponse,
  key: CompanyFinancialsSeriesKey,
): { cagr: number | null; yoy: number | null } {
  const series = seriesOf(response, key)
  return { cagr: series?.cagr ?? null, yoy: series?.yoy ?? null }
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

/** Where this browser remembers the last company picked. */
export const COMPANY_PICK_KEY = 'company-financials:security'

export function readCompanyPick(): number | null {
  const raw = readStored(COMPANY_PICK_KEY)
  if (raw === null || !/^\d+$/.test(raw)) return null
  return Number(raw)
}

export function writeCompanyPick(securityId: number): void {
  writeStored(COMPANY_PICK_KEY, String(securityId))
}

/**
 * The company to show: the one picked, else the one this browser picked last, else the
 * first. A pick no longer in the list (sold, or no longer filed) falls through rather
 * than asking the server for a company it will 404.
 */
export function resolveCompany(
  companies: readonly CompanyFinancialsCompany[],
  picked: number | null,
  stored: number | null,
): CompanyFinancialsCompany | null {
  return (
    companies.find((c) => c.security_id === picked)
    ?? companies.find((c) => c.security_id === stored)
    ?? companies[0]
    ?? null
  )
}
