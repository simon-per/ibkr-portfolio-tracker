import { TrendingUp, TrendingDown, Activity, Target, Shield, ChartPie, ChartLine } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { KpiCard, KpiCardSkeleton, KpiPanel } from '@/components/ui/KpiCard'
import { SectionHeader } from '@/components/ui/SectionHeader'

/** The row's heading: return figures here, risk and income in `RiskMetricsCards`. */
const heading = <SectionHeader variant="bare" title="Returns" icon={ChartLine} className="mb-3" />

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
      <section>
        {heading}
        <KpiPanel>
          <KpiCardSkeleton tile count={6} />
        </KpiPanel>
      </section>
    )
  }

  if (isError) {
    // Same shape and reasoning as PortfolioSummaryCards: a backend error is
    // indistinguishable from an empty portfolio otherwise, and a row that simply
    // disappears reads as "this was never here" rather than "this failed".
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground">
          Couldn't load the performance metrics — the server didn't respond. It retries automatically.
        </CardContent>
      </Card>
    )
  }

  if (!metrics) {
    return null
  }

  const isPositiveXIRR = metrics.xirr !== null && metrics.xirr >= 0
  const xirrUnpriced = metrics.xirrUnpriced ?? 0
  const unvalued = metrics.unvaluedPositions ?? 0

  return (
    <section>
      {heading}
      <KpiPanel>
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
              Annual return is understated — {xirrUnpriced}{' '}
              {xirrUnpriced === 1 ? 'holding had' : 'holdings had'} no usable price at one end
              of this period
            </span>
          </div>
        )}

        {/* Annual Return (XIRR). A window under 30 days reports simple_period instead, and
            the label follows it rather than annualising a few days of noise. */}
        <KpiCard
          tile
          label={metrics.xirrMethod === 'simple_period' ? 'Period return' : 'Annual return'}
          // Three states, not two. `isPositiveXIRR` is false for a *negative* return and
          // for an *unknown* one alike, so a refused XIRR — too few cash flows in the
          // window, which 1W on a week with no trades produces — drew a red downward
          // arrow and `tone: 'negative'` over a dash. A tone ladder asserting a loss about
          // a value the backend declined to compute. `KpiCard` forces a null value to a
          // muted tone, but the icon is chosen here, so that guard could not reach it.
          // Every sibling card on these two rows already branches to muted. The well takes
          // `tone`, which is muted for the unknown case.
          icon={metrics.xirr !== null && !isPositiveXIRR ? TrendingDown : TrendingUp}
          value={metrics.xirr !== null
            ? `${isPositiveXIRR ? '+' : ''}${metrics.xirr.toFixed(2)}%`
            : null}
          tone={metrics.xirr === null ? 'muted' : isPositiveXIRR ? 'positive' : 'negative'}
        />

        <KpiCard
          tile
          label="Max drawdown"
          icon={TrendingDown}
          value={metrics.maxDrawdown !== null ? `${metrics.maxDrawdown.toFixed(2)}%` : null}
          tone={metrics.maxDrawdown === null ? 'muted' : 'negative'}
          // Not "no decline": with nothing measurable in the range there is no decline to
          // measure, and the two must not share a rendering.
          sub={metrics.maxDrawdown !== null ? undefined : 'Not enough history in this range'}
        />

        <KpiCard
          tile
          label="Sharpe ratio"
          icon={Activity}
          value={metrics.sharpeRatio !== null ? metrics.sharpeRatio.toFixed(2) : null}
          // Says which of the two absences it is, the way the Volatility and Sortino
          // cards do — the range is too short far more often than the market is flat.
          sub={metrics.sharpeRatio !== null ? undefined : 'Not enough history in this range'}
        />

        <KpiCard
          tile
          label="Win rate"
          icon={Target}
          value={metrics.winRate !== null ? `${metrics.winRate.toFixed(1)}%` : null}
          // The excluded count belongs beside the ratio, not only in the alert above: a
          // holding valued at 0.00 carries a gain of -cost, so counting it would move this
          // figure while looking like an ordinary loser.
          sub={metrics.winRate === null
            ? 'No priced positions'
            : `${metrics.profitablePositions} of ${metrics.totalPositions} in profit` +
              (unvalued > 0 ? ` · ${unvalued} unpriced` : '')}
        />

        <KpiCard
          tile
          label="Calmar ratio"
          icon={Shield}
          value={metrics.calmarRatio !== null ? metrics.calmarRatio.toFixed(2) : null}
        />

        <KpiCard
          tile
          label="Top 5 weight"
          icon={ChartPie}
          value={metrics.top5Weight !== null ? `${metrics.top5Weight.toFixed(1)}%` : null}
          sub={metrics.top5Weight === null
            // Both figures are absent on the same condition — nothing priced — so say that
            // rather than leaving a bare "Concentration risk" under a dash.
            ? 'No priced positions'
            : metrics.effectiveHoldings !== null
              ? `${metrics.effectiveHoldings.toFixed(1)} effective holdings`
              : undefined}
        />
      </KpiPanel>
    </section>
  )
}
