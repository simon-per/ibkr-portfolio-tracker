import { TrendingUp, TrendingDown, Wallet, Target } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { KpiCard, KpiCardSkeleton } from '@/components/ui/KpiCard'
import { cashIsTracked } from '@/lib/portfolioCash'
import { DeltaChip } from './DeltaChip'
import type { PortfolioSummary } from '@/lib/api'
import { formatPercent } from '@/lib/utils'
import { useFormatCurrency } from '@/lib/CurrencyContext'

interface PortfolioSummaryCardsProps {
  summary: PortfolioSummary | undefined
  isLoading?: boolean
  isError?: boolean
  /**
   * Change in market value over the chart's selected range, and the label for it.
   * Optional so the card still stands alone before the timeline resolves.
   */
  periodChangePct?: number | null
  periodLabel?: string
}

export function PortfolioSummaryCards({
  summary, isLoading, isError, periodChangePct, periodLabel,
}: PortfolioSummaryCardsProps) {
  const formatCurrency = useFormatCurrency()

  if (isLoading) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-5">
        <KpiCardSkeleton count={5} />
      </div>
    )
  }

  if (isError) {
    // Not the sync CTA: a backend error is indistinguishable from an empty
    // portfolio otherwise, and syncing won't fix a server that isn't answering.
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground">
          Couldn't load the portfolio summary — the backend didn't respond. It retries automatically.
        </CardContent>
      </Card>
    )
  }

  if (!summary) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground">
          No portfolio data yet — click "Sync IBKR Data" to get started.
        </CardContent>
      </Card>
    )
  }

  const isProfit = summary.total_gain_loss_eur >= 0
  const isRealizedProfit = summary.total_realized_gain_loss_eur >= 0

  // Holdings the backend could not value today. Their cost still counts, so the total is
  // a partial sum while Cost Basis is whole — which is the SBI shape exactly: deleting one
  // security's poisoned prices took 446.93 CHF off this number and only a sync warning
  // said so. Absent means an older backend, read as complete.
  const unpriced = summary.unpriced_holdings ?? 0

  // Holdings plus uninvested cash: what the account is actually worth. The hero card
  // reported the holdings-only figure until 2026-08-26, so a rotation that left 12,229
  // CHF undeployed understated this account by 18% with nothing on the page saying so —
  // the cash was not zero, it was invisible. `cashIsTracked` refuses the two states
  // that would put a confident 0.00 here instead.
  const hasCash = cashIsTracked(summary)
  const cash = summary.total_cash_eur ?? 0
  const headline = hasCash
    ? (summary.total_value_eur ?? summary.total_market_value_eur + cash)
    : summary.total_market_value_eur

  return (
    /* Two-up rather than one card per row below `md`: stacked, the three KPI rows came
       to ~1,660px of scrolling past numbers before the chart — about two phone screens,
       and the opposite of the reading order this page wants. */
    <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-5">
      {unpriced > 0 && (
        <div
          role="alert"
          className="col-span-2 lg:col-span-5 rounded-md border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500"
        >
          <span className="font-medium">
            {hasCash ? 'Total Value' : 'Market Value'} is incomplete — {unpriced}{' '}
            {unpriced === 1 ? 'holding has' : 'holdings have'} no usable price today
          </span>
          {' '}— their cost still counts toward Cost Basis, so the total and both gain figures
          understate by whatever those holdings are worth. Check the market-data sync's
          warnings and the position's ticker mapping.
        </div>
      )}

      {/* The hero. Market Value spans both columns on a phone so it keeps the full
          324px and its `text-2xl`, and the other four tile underneath — which is what
          makes the top of the screen read like a portfolio rather than a dashboard. */}
      <KpiCard
        hero
        label={hasCash ? 'Total Value' : 'Market Value'}
        icon={<Wallet className="h-4 w-4 text-muted-foreground" />}
        value={formatCurrency(headline)}
        // `footer`, not `sub`: the chip carries its own colours and must not inherit the
        // muted footnote class.
        footer={periodChangePct != null ? (
          <div className="flex items-center gap-1.5">
            {/* flatBand 0: unlike dividend income, every move in portfolio value is
                signal — muting a 3% quarter as "flat" would understate it. */}
            <DeltaChip
              pct={periodChangePct}
              flatBand={0}
              label={periodLabel ? `over ${periodLabel}` : undefined}
              title="Change in total holdings value over the selected chart range. Includes money added — see Period Gain for the return."
            />
          </div>
        ) : undefined}
        // The split is stated rather than left to be inferred, and it goes here rather
        // than in a sixth card: the grid is five wide, and a reader looking at one
        // number needs to know which two it is made of — especially while a rotation
        // has a fifth of the account sitting in cash. `sub` renders beside `footer`,
        // so the period chip is not displaced.
        sub={hasCash
          ? `${formatCurrency(summary.total_market_value_eur)} in holdings + ${formatCurrency(cash)} cash`
          : periodChangePct != null ? undefined : 'Current portfolio value'}
      />

      <KpiCard
        label="Cost Basis"
        icon={<Target className="h-4 w-4 text-muted-foreground" />}
        value={formatCurrency(summary.total_cost_basis_eur)}
        sub="Total amount invested"
      />

      <KpiCard
        label="Unrealized Gain/Loss"
        icon={isProfit
          ? <TrendingUp className="h-4 w-4 text-green-600" />
          : <TrendingDown className="h-4 w-4 text-red-600" />}
        value={formatCurrency(summary.total_gain_loss_eur)}
        tone={isProfit ? 'positive' : 'negative'}
        sub={formatPercent(summary.total_gain_loss_percent)}
      />

      <KpiCard
        label="Realized Gain/Loss"
        icon={isRealizedProfit
          ? <TrendingUp className="h-4 w-4 text-green-600" />
          : <TrendingDown className="h-4 w-4 text-red-600" />}
        value={formatCurrency(summary.total_realized_gain_loss_eur)}
        tone={isRealizedProfit ? 'positive' : 'negative'}
        sub={summary.num_closed_positions === 0
          ? 'No closed positions yet'
          : `From ${summary.num_closed_positions} closed position${summary.num_closed_positions === 1 ? '' : 's'}`}
      />

      <KpiCard
        label="Positions"
        value={summary.num_positions}
        sub="Unique securities held"
      />
    </div>
  )
}
