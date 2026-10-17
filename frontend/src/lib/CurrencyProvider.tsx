import { useMemo, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from './api'
import {
  CurrencyContext, DEFAULT_SUPPORTED, FALLBACK_CURRENCY, type CurrencyContextValue,
} from './CurrencyContext'

export function CurrencyProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()

  const { data, isError: settingsFailed } = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.getSettings(),
    staleTime: Infinity,
  })

  const mutation = useMutation({
    mutationFn: (currency: string) => api.updateBaseCurrency(currency),
    onSuccess: (updated) => {
      queryClient.setQueryData(['settings'], updated)
      // Every money figure depends on the base currency — refetch all data so
      // portfolio, dividends, allocation, benchmarks, etc. re-render in the new base.
      queryClient.invalidateQueries()
    },
  })

  // Memoized on primitives: this context is consumed at the Dashboard root, so a
  // fresh object identity per provider render re-rendered the entire tree
  // (including the ~800-point chart) even when nothing changed.
  const { mutate } = mutation
  const value = useMemo<CurrencyContextValue>(() => ({
    baseCurrency: data?.base_currency ?? FALLBACK_CURRENCY,
    supportedCurrencies: data?.supported_currencies ?? DEFAULT_SUPPORTED,
    setBaseCurrency: (currency: string) => mutate(currency),
    isUpdating: mutation.isPending,
    updateError: mutation.isError
      ? 'Currency change failed — still showing the previous currency.'
      : null,
    // Only once the fetch has actually failed. While it is still in flight the
    // fallback is a placeholder, not a claim, and alarming on first paint would
    // train the reader to ignore it. A later refetch that errors keeps `data`,
    // so this stays false — the label is still the account's own.
    currencyIsAssumed: settingsFailed && data === undefined,
    unavailableBase: data?.base_currency_unavailable ?? null,
  }), [data, mutate, mutation.isPending, mutation.isError, settingsFailed])

  return <CurrencyContext.Provider value={value}>{children}</CurrencyContext.Provider>
}
