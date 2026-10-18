import { createContext, useCallback, useContext } from 'react'
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

/** Returns the short symbol/prefix for the current base currency. */
export function useCurrencySymbol() {
  const { baseCurrency } = useContext(CurrencyContext)
  return currencySymbolUtil(baseCurrency)
}
