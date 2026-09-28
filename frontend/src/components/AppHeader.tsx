import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useBaseCurrency } from '@/lib/CurrencyContext'
import { ThemeToggle } from './ThemeToggle'
import { AdminKeyButton } from './AdminKeyButton'
import { ModeToggle } from './ModeToggle'

interface AppHeaderProps {
  /** Under the title from `sm` up; what this view is. */
  strapline: ReactNode
  /** Under the title at every width: the view's last/next sync and its warnings. */
  status?: ReactNode
  /** After the shared controls: the view's own sync button. */
  actions?: ReactNode
  /** Under the whole row: the outcome of the last sync press. */
  below?: ReactNode
}

/**
 * The header both books share: title, base currency, Stocks | Crypto, theme, admin key.
 *
 * Extracted from `Dashboard` on 2026-09-28 when the crypto view needed the same header
 * with its own status line and its own Sync button. The mode-specific parts are slots
 * rather than props describing them, so each view keeps the queries and mutation that
 * feed them and this component knows nothing about IBKR or CoinStats. The markup is the
 * one `Dashboard` rendered, moved verbatim, plus the mode toggle before the theme toggle.
 */
export function AppHeader({ strapline, status, actions, below }: AppHeaderProps) {
  const {
    baseCurrency, supportedCurrencies, setBaseCurrency,
    isUpdating: currencyUpdating, updateError: currencyError, currencyIsAssumed,
  } = useBaseCurrency()

  // Same key and staleTime as the footer's read, so it is one request per session.
  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: () => api.healthCheck(),
    staleTime: Infinity,
  })

  return (
    <div className="border-b border-border/70 bg-card/60 backdrop-blur supports-[backdrop-filter]:bg-card/60">
      <div className="mx-auto w-full max-w-[1400px] px-4 py-3 sm:px-6 sm:py-4">
        {/* Wraps below `sm`: the title block and the controls cannot share a 358px row,
            and `justify-between` on a row that cannot wrap is what pushed the cluster
            past the viewport edge. `items-start` so the controls sit level with the
            title rather than with the bottom of the status block. */}
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">Portfolio Analyzer</h1>
            {/* Hidden below `sm`: the strapline costs a line of an 844px screen and
                tells a returning user nothing they do not already know. */}
            <p className="text-muted-foreground mt-1 hidden sm:block">{strapline}</p>
            {status}
          </div>
          {/* Wraps: at 390px the currency select plus the sync button are wider than
              the viewport, and without this the whole page scrolled horizontally by
              ~25px on every tab. */}
          <div className="flex flex-wrap items-center justify-end gap-2">
            <select
              value={baseCurrency}
              onChange={(e) => setBaseCurrency(e.target.value)}
              disabled={currencyUpdating}
              title="Base currency"
              className="h-9 rounded-md border border-input bg-background px-3 text-sm font-medium disabled:opacity-50"
            >
              {supportedCurrencies.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
            {currencyError && (
              <span className="max-w-[12rem] text-xs leading-tight text-red-600 dark:text-red-400" role="alert">
                {currencyError}
              </span>
            )}
            {/* Without this the app labels every figure `€` on a failed settings
                fetch while the numbers behind them are whatever the account
                actually uses — and `staleTime: Infinity` makes that stick for the
                session rather than blink. */}
            {currencyIsAssumed && !currencyError && (
              <span className="max-w-[12rem] text-xs leading-tight text-amber-700 dark:text-amber-400" role="alert">
                Couldn't read your display currency — showing {baseCurrency}, which may not be it.
              </span>
            )}
            <ModeToggle />
            <ThemeToggle />
            <AdminKeyButton writeAuthEnabled={health?.write_auth_enabled} />
            {actions}
          </div>
        </div>
        {below}
      </div>
    </div>
  )
}
