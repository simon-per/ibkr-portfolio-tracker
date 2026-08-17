import { TrendingUp, TrendingDown, Activity, Target, Shield, PieChart } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { KpiCard, KpiCardSkeleton } from '@/components/ui/KpiCard'

interface PerformanceMetricsCardsProps {
  metrics: {
    xirr: number | null
    xirrMethod?: string
    /**
     * Holdings the backend could not value at one end of the window the return covers.
     * Above 0 means XIRR — and the Calmar built on it — was measured against a partial
     * book, so the notice above the row says so rather than letting a plausible
     * understatement pass as a measurement.
     */
    xirrUnpriced?: number
    /** `null` when no daily return in the range was measurable. A `0` would claim the
     *  portfolio never fell, which is what a stalled price feed looks like. */
    maxDrawdown: number | null
    /** `null` below the minimum sample or with no volatility to divide by — a 0.00 there
     *  is a plausible-looking Sharpe and so the worst possible stand-in for "unknown". */
    sharpeRatio: number | null
    /** `null` when nothing could be valued. Unvaluable holdings are excluded from both
     *  sides — counted as losses, they read this figure ~8 pp low on a real morning. */
    winRate: number | null
    profitablePositions: number
    /** Positions actually judged, i.e. excluding the unvaluable ones. */
    totalPositions: number
    unvaluedPositions?: number
    calmarRatio: number | null
    /** `null` when nothing is priced. A 0 here reads as *good* concentration, so it is
     *  the one absence in this row that actively reassures. */
    top5Weight: number | null
    /**
     * Herfindahl effective holdings, footnoted on the Top 5 card rather than given a
     * card of its own. A top-5 weight cannot tell five equal positions from one
     * dominant one, which is why the figure exists; it does not need the space.
     * `null` when nothing is priced — the top-5 percentage is a confident 0 in that
     * case, so the footnote must not imply a count it does not have.
     */
    effectiveHoldings: number | null
  } | null
  isLoading?: boolean
  /**
   * The queries behind these cards failed. Rendering nothing would make an outage
   * look like a feature that does not exist — the rule `RebalanceCard` and
   * `CurrencyExposureCard` already follow, and which this row did not.
   */
  isError?: boolean
}

export function PerformanceMetricsCards({
  metrics, isLoading, isError,
}: PerformanceMetricsCardsProps) {
  if (isLoading) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-3 lg:grid-cols-6">
        <KpiCardSkeleton count={6} />
      </div>
    )
  }

  if (isError) {
    // Same shape and reasoning as PortfolioSummaryCards: a backend error is
    // indistinguishable from an empty portfolio otherwise, and a row that simply
    // disappears reads as "this was never here" rather than "this failed".
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground">
          Couldn't load the performance metrics — the backend didn't respond. Annual return, drawdown, Sharpe, win rate, Calmar and concentration are
          unavailable. It retries automatically.
        </CardContent>
      </Card>
    )
  }

  if (!metrics) {
    return null
  }

  const isPositiveXIRR = metrics.xirr !== null && metrics.xirr >= 0
  const isPositiveSharpe = metrics.sharpeRatio !== null && metrics.sharpeRatio >= 0
  const xirrUnpriced = metrics.xirrUnpriced ?? 0
  const unvalued = metrics.unvaluedPositions ?? 0

  return (
    <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-3 lg:grid-cols-6">
      {/* Outside the cards, in the row's own flow — the rule MonthlyReturnsHeatmap and
          PerformanceAttribution both had to learn: a caveat you have to open something to
          reach is as good as absent, and the figure it qualifies is on screen either way.
          Same shape and colours as PortfolioSummaryCards' notice, because it is the same
          condition reaching a different set of numbers. */}
      {xirrUnpriced > 0 && (
        <div
          role="alert"
          className="col-span-2 md:col-span-3 lg:col-span-6 rounded-md border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500"
        >
          <span className="font-medium">
            Annual Return is measured against an incomplete valuation — {xirrUnpriced}{' '}
            {xirrUnpriced === 1 ? 'holding had' : 'holdings had'} no usable price at one end
            of this period
          </span>
          {' '}— their purchases still count as money in, so the return and the Calmar ratio
          understate. Check the market-data sync's warnings and the position's ticker mapping.
        </div>
      )}

      {/* Annual Return (XIRR). A window under 30 days reports simple_period instead, and
          the label follows it rather than annualising a few days of noise. */}
      <KpiCard
        label={metrics.xirrMethod === 'simple_period' ? 'Period Return' : 'Annual Return (XIRR)'}
        icon={isPositiveXIRR
          ? <TrendingUp className="h-4 w-4 text-green-600" />
          : <TrendingDown className="h-4 w-4 text-red-600" />}
        value={metrics.xirr !== null
          ? `${isPositiveXIRR ? '+' : ''}${metrics.xirr.toFixed(2)}%`
          : null}
        tone={isPositiveXIRR ? 'positive' : 'negative'}
        sub="Money-weighted, adjusted for deposits"
      />

      <KpiCard
        label="Max Drawdown"
        icon={<TrendingDown className="h-4 w-4 text-red-600" />}
        value={metrics.maxDrawdown !== null ? `${metrics.maxDrawdown.toFixed(2)}%` : null}
        tone={metrics.maxDrawdown === null ? 'muted' : 'negative'}
        // Not "no decline": with nothing measurable in the range there is no decline to
        // measure, and the two must not share a rendering.
        sub={metrics.maxDrawdown !== null
          ? 'Largest peak-to-trough decline'
          : 'Not enough history in this range'}
      />

      <KpiCard
        label="Sharpe Ratio"
        icon={<Activity className="h-4 w-4 text-muted-foreground" />}
        value={metrics.sharpeRatio !== null ? metrics.sharpeRatio.toFixed(2) : null}
        tone={metrics.sharpeRatio === null ? 'muted' : isPositiveSharpe ? 'positive' : 'negative'}
        // Says which of the two absences it is, the way the Volatility and Sortino
        // cards do — the range is too short far more often than the market is flat.
        sub={metrics.sharpeRatio !== null
          ? 'Risk-adjusted return'
          : 'Not enough history in this range'}
      />

      <KpiCard
        label="Win Rate"
        icon={<Target className="h-4 w-4 text-muted-foreground" />}
        value={metrics.winRate !== null ? `${metrics.winRate.toFixed(1)}%` : null}
        // The excluded count belongs beside the ratio, not only in the alert above: a
        // holding valued at 0.00 carries a gain of -cost, so counting it would move this
        // figure while looking like an ordinary loser.
        sub={metrics.winRate === null
          ? 'No priced positions'
          : `${metrics.profitablePositions} of ${metrics.totalPositions} profitable` +
            (unvalued > 0 ? ` · ${unvalued} unpriced, not judged` : '')}
      />

      <KpiCard
        label="Calmar Ratio"
        icon={<Shield className="h-4 w-4 text-muted-foreground" />}
        value={metrics.calmarRatio !== null ? metrics.calmarRatio.toFixed(2) : null}
        tone={metrics.calmarRatio === null ? 'muted'
          : metrics.calmarRatio >= 1 ? 'positive'
          : metrics.calmarRatio >= 0 ? 'warning'
          : 'negative'}
        sub="Return / drawdown"
      />

      <KpiCard
        label="Top 5 Weight"
        icon={<PieChart className="h-4 w-4 text-muted-foreground" />}
        value={metrics.top5Weight !== null ? `${metrics.top5Weight.toFixed(1)}%` : null}
        tone={metrics.top5Weight === null ? 'muted'
          : metrics.top5Weight > 70 ? 'negative'
          : metrics.top5Weight > 50 ? 'warning'
          : 'positive'}
        sub={metrics.top5Weight === null
          // Both figures are absent on the same condition — nothing priced — so say that
          // rather than leaving a bare "Concentration risk" under a dash.
          ? 'No priced positions'
          : metrics.effectiveHoldings !== null
            ? `Concentration risk · ${metrics.effectiveHoldings.toFixed(1)} effective`
            : 'Concentration risk'}
      />
    </div>
  )
}
