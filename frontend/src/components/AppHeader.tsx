import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useBaseCurrency } from '@/lib/CurrencyContext'
import { ThemeToggle } from './ThemeToggle'
import { AdminKeyButton } from './AdminKeyButton'
import { ModeToggle } from './ModeToggle'
import { cn } from '@/lib/utils'

interface AppHeaderProps {
  /** Under the title at every width: the view's last sync and its warnings. */
  status?: ReactNode
  /** After the shared controls: the view's own sync button. */
  actions?: ReactNode
  /** Under the whole row: the outcome of the last sync press. */
  below?: ReactNode
  /**
   * Close the bar with its own rule. The stock view's section strip draws the rule under
   * both; the crypto view has no strip, so its header ends the bar itself.
   */
  bordered?: boolean
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
export function AppHeader({ status, actions, below, bordered }: AppHeaderProps) {
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
    // One white app bar with the section strip under it (the strip is the sticky half):
    // the canvas below stays grey, so the bar reads as chrome and the cards as content.
    <div className={cn('bg-card', bordered && 'border-b border-border/80')}>
      <div className={cn('mx-auto w-full max-w-[1400px] px-4 pt-3 sm:px-6 sm:pt-4', bordered ? 'pb-3 sm:pb-4' : 'pb-2')}>
        {/* Wraps below `sm`: the brand and the controls cannot share a 358px row. */}
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3">
          <div className="flex min-w-0 items-center gap-3">
            <BrandMark />
            <div className="min-w-0">
              <h1 className="text-base font-semibold leading-tight tracking-tight sm:text-lg">Portfolio Analyzer</h1>
              {status}
            </div>
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
              className="h-9 rounded-lg border border-border bg-card px-2.5 text-sm font-medium transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
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

/** The app's mark: a rising line on the primary gradient, the same drawing as the favicon. */
function BrandMark() {
  return (
    <svg viewBox="0 0 32 32" className="h-9 w-9 shrink-0" aria-hidden="true">
      <defs>
        <linearGradient id="brand-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#3b82f6" />
          <stop offset="1" stopColor="#6366f1" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#brand-mark)" />
      <path
        d="M7 21.5 L12.5 15.5 L17 19 L25 10.5"
        fill="none"
        stroke="#fff"
        strokeWidth="2.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="25" cy="10.5" r="2.2" fill="#fff" />
    </svg>
  )
}
