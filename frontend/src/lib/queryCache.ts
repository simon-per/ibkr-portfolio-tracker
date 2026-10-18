import { queryOptions, type Query } from '@tanstack/react-query'
import { api } from './api'

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
