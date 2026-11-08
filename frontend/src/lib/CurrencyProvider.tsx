import { useEffect, useMemo, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from './api'
import {
  CurrencyContext, DEFAULT_SUPPORTED, FALLBACK_CURRENCY, type CurrencyContextValue,
} from './CurrencyContext'
import { dependsOnBaseCurrency, servedBaseCurrency, settingsQuery } from './queryCache'
import type { AppSettings } from './api'

export function CurrencyProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()

  const { data, isError: settingsFailed } = useQuery(settingsQuery)

  // A response converted into another base currency than the cached settings name means
  // the base changed elsewhere (another device, a CLI) while these settings are kept for
  // the session (`staleTime: Infinity`): the label would sit on figures in the other
  // currency (review item C3, 2026-11-08). Refresh the settings once per such currency,
  // and if the base did change, reset what was converted into the old one — what a switch
  // made here does.
  const settingsBase = data?.base_currency
  useEffect(() => {
    if (!settingsBase) return
    let askedAbout: string | null = null
    const check = (key: readonly unknown[], response: unknown) => {
      if (key[0] === 'settings') return
      const served = servedBaseCurrency(response)
      if (served === null || served === settingsBase || served === askedAbout) return
      askedAbout = served
      void queryClient.invalidateQueries({ queryKey: ['settings'] }).then(() => {
        const now = queryClient.getQueryData<AppSettings>(['settings'])?.base_currency
        if (now && now !== settingsBase) {
          void queryClient.resetQueries({ predicate: dependsOnBaseCurrency })
        }
      })
    }
    const cache = queryClient.getQueryCache()
    // What arrived before the settings did, then everything after.
    for (const query of cache.getAll()) check(query.queryKey, query.state.data)
    return cache.subscribe((event) => {
      if (event.type === 'updated' && event.action.type === 'success') {
        check(event.query.queryKey, event.action.data)
      }
    })
  }, [queryClient, settingsBase])

  const mutation = useMutation({
    mutationFn: (currency: string) => api.updateBaseCurrency(currency),
    onSuccess: (updated) => {
      queryClient.setQueryData(['settings'], updated)
      // Reset, not invalidate: no query key carries the base currency, so invalidating
      // kept every old-currency figure on screen under the new label until its refetch
      // landed — and for good if that refetch failed. A reset drops the data: active
      // queries show their loading state, inactive ones fetch fresh on mount.
      queryClient.resetQueries({ predicate: dependsOnBaseCurrency })
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
