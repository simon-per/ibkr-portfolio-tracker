import { queryOptions, type Query } from '@tanstack/react-query'
import { api, type ActivityParams } from './api'
import type { DateRange } from './dateRanges'

/**
 * Which cached queries a write makes stale (docs/frontend.md, *The query cache*).
 *
 * Both predicates name what they leave OUT, not what they refresh: a list of the roots to
 * refresh is how the manual sync once refreshed only `['portfolio']` and left the dividend
 * and tax figures on the same screen pre-sync. A root added later is refreshed by default,
 * which costs a request; leaving it out would cost a wrong figure.
 */

/** Roots that depend on neither the account's rows nor the base currency. */
const INDEPENDENT_ROOTS: readonly unknown[] = ['settings', 'health', 'logos']

/** The crypto book is projected into the base currency but has its own tables and sync. */
const NOT_WRITTEN_BY_IBKR_SYNC: readonly unknown[] = [...INDEPENDENT_ROOTS, 'crypto']

/** A base-currency switch: every query whose figures were converted into the old one. */
export function dependsOnBaseCurrency(query: Query): boolean {
  return !INDEPENDENT_ROOTS.includes(query.queryKey[0])
}

/** A manual IBKR sync: every query reading rows a Flex statement can write. */
export function readsIbkrRows(query: Query): boolean {
  return !NOT_WRITTEN_BY_IBKR_SYNC.includes(query.queryKey[0])
}

/**
 * The summary and the positions are one valuation read through two endpoints. One
 * `staleTime` for both, defined once, so a tab mounted later never refetches one of them
 * without the other.
 */
const VALUATION_STALE_TIME = 30 * 60 * 1000

export const summaryQuery = queryOptions({
  queryKey: ['portfolio', 'summary'],
  queryFn: () => api.getPortfolioSummary(),
  staleTime: VALUATION_STALE_TIME,
})

export const positionsQuery = queryOptions({
  queryKey: ['portfolio', 'positions'],
  queryFn: () => api.getPositions(),
  staleTime: VALUATION_STALE_TIME,
})

/*
 * Every query read from more than one place is defined here, once (#70). Written out at
 * each reader, two copies of one key drifted on the option nobody looks at: the Analytics
 * tab's value-over-time took the 5-minute default where the Performance tab's took 30, so
 * the same cache entry went stale on whichever tab mounted it first.
 * `queryKeyFamily.test.ts` fails a query key written at two definition sites.
 */

/** The valuation over a window; the window's last point is the summary's valuation. */
export const valueOverTimeQuery = (range: DateRange) =>
  queryOptions({
    queryKey: ['portfolio', 'value-over-time', range],
    queryFn: () => api.getPortfolioValueOverTime(range.start, range.end),
    staleTime: VALUATION_STALE_TIME,
  })

/** Money in by month and the first contribution date (Performance, Activity, Tax, Forecast). */
export const contributionsQuery = queryOptions({
  queryKey: ['portfolio', 'contributions'],
  queryFn: () => api.getContributions(),
  staleTime: 30 * 60 * 1000,
})

/** The benchmark list: fixed per deploy. */
export const availableBenchmarksQuery = queryOptions({
  queryKey: ['portfolio', 'benchmarks'],
  queryFn: () => api.getAvailableBenchmarks(),
  staleTime: Infinity,
})

/** The build, the scheduler flag and write-auth: one request per session. */
export const healthQuery = queryOptions({
  queryKey: ['health'],
  queryFn: () => api.healthCheck(),
  staleTime: Infinity,
})

/** The forecast's daily history (the Dividends tiles' sparklines and the forecast card). */
export const forecastHistoryQuery = queryOptions({
  queryKey: ['dividends', 'forecast-history'],
  queryFn: () => api.getDividendForecastHistory(),
})

/**
 * The dividends breakdown for a calendar year or the trailing 24 months. The Performance tab
 * reads the current year's for the KPI cards and yield on cost; the Dividends tab, any.
 */
export const dividendBreakdownQuery = (year: number | 'all' | '24m') =>
  queryOptions({
    queryKey: ['dividends', 'breakdown', year],
    queryFn: () => api.getDividendBreakdown(
      typeof year === 'number' ? year : undefined,
      year === '24m' ? '24m' : undefined,
    ),
    staleTime: 30 * 60 * 1000,
  })

/** One page of the activity ledger, or a whole window at the endpoint's ceiling. */
export const activityQuery = (params: ActivityParams) =>
  queryOptions({
    queryKey: ['portfolio', 'activity', params],
    queryFn: () => api.getActivity(params),
    staleTime: 5 * 60 * 1000,
  })

/**
 * A `useQueries` `combine`: each query's data, in query order.
 *
 * Without a `combine`, `useQueries` returns a new array on every render, so every memo
 * keyed on it recomputes every render. Its combined result is structurally shared, so this
 * one keeps its identity until some query's data changes. Module-level on purpose: an
 * inline `combine` is a new function each render and is re-run each render.
 */
export function dataOf<T>(results: readonly { data: T | undefined }[]): (T | undefined)[] {
  return results.map((r) => r.data)
}
