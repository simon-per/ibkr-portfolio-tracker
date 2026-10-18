import { createContext, useCallback, useContext } from 'react'
import { keepPreviousData } from '@tanstack/react-query'
import {
  formatCurrency as formatCurrencyUtil,
  formatMoneyWhole as formatMoneyWholeUtil,
  currencySymbol as currencySymbolUtil,
} from './utils'

export interface CurrencyContextValue {
  baseCurrency: string
  supportedCurrencies: string[]
  setBaseCurrency: (currency: string) => void
  isUpdating: boolean
  /** Set when the last currency change failed — the dropdown silently reverts otherwise. */
  updateError: string | null
  /**
   * True when `baseCurrency` is the fallback rather than the account's setting.
   *
   * `/api/settings` is fetched with `staleTime: Infinity` because the value only
   * changes through our own mutation — which means a single failed fetch is not a
   * blip but sticky for the session. Without this flag the app then renders every
   * money figure with a `€` while the numbers behind them are whatever the account
   * actually uses, and nothing anywhere says so. The figures are not wrong; the
   * label is, which is worse than an obvious gap.
   */
  currencyIsAssumed: boolean
  /**
   * The chosen base currency when the server cannot use it — no EUR->base rate exists
   * at all, so every figure is served in EUR and `baseCurrency` says EUR. The header
   * names it rather than letting the switch look ignored.
   */
  unavailableBase: string | null
}

export const FALLBACK_CURRENCY = 'EUR'
export const DEFAULT_SUPPORTED = ['EUR', 'CHF', 'USD']

/**
 * The context object, the value's shape and the hooks that read it. The provider is
 * `CurrencyProvider.tsx`: a module that exports a component and anything else defeats
 * React Fast Refresh, and the hooks are imported in four times as many files.
 */
export const CurrencyContext = createContext<CurrencyContextValue>({
  baseCurrency: FALLBACK_CURRENCY,
  supportedCurrencies: DEFAULT_SUPPORTED,
  setBaseCurrency: () => {},
  isUpdating: false,
  updateError: null,
  currencyIsAssumed: false,
  unavailableBase: null,
})

export function useBaseCurrency() {
  return useContext(CurrencyContext)
}

/**
 * Returns a formatter bound to the current base currency (override optional).
 *
 * The same function until the base currency changes: the tables memoise their column
 * factories on it, and a fresh closure per render rebuilt every column every render.
 */
export function useFormatCurrency() {
  const { baseCurrency } = useContext(CurrencyContext)
  return useCallback(
    (value: number, currencyOverride?: string) =>
      formatCurrencyUtil(value, currencyOverride ?? baseCurrency),
    [baseCurrency],
  )
}

/** Returns a whole-unit formatter (`formatMoneyWhole`) bound to the current base currency. */
export function useFormatMoneyWhole() {
  const { baseCurrency } = useContext(CurrencyContext)
  // Stable per base currency, like useFormatCurrency (#48): a new function per render
  // invalidates every memo it is a dependency of.
  return useCallback((value: number) => formatMoneyWholeUtil(value, baseCurrency), [baseCurrency])
}

/**
 * `keepPreviousData` that never carries a figure across a base-currency switch (#68, #22).
 *
 * A query keyed on a control (Top 25/50/100, a page, a year) keeps its last rows on screen
 * while the next ones load, so the control the reader pressed stays mounted. The switch
 * resets every money query, which leaves this function nothing to keep — but TanStack reuses
 * a placeholder ALREADY on screen without calling the function again while it is the same
 * function (query-core `createResult`). So it is one function per base currency: the render
 * that relabels the figures also replaces it, and the reset has emptied what it would keep.
 */
export function useKeepPreviousData(): typeof keepPreviousData {
  const { baseCurrency } = useContext(CurrencyContext)
  let keep = keepersByCurrency.get(baseCurrency)
  if (!keep) {
    keep = (previous) => keepPreviousData(previous)
    keepersByCurrency.set(baseCurrency, keep)
  }
  return keep
}
const keepersByCurrency = new Map<string, typeof keepPreviousData>()

/** Returns the short symbol/prefix for the current base currency. */
export function useCurrencySymbol() {
  const { baseCurrency } = useContext(CurrencyContext)
  return currencySymbolUtil(baseCurrency)
}
