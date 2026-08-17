import { Activity, Coins, Gauge, PiggyBank, Scale, TrendingDown } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { KpiCard, KpiCardSkeleton } from '@/components/ui/KpiCard'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import type { DividendForwardYield } from '@/lib/api'

/**
 * Beta needs a benchmark, and there are three separate reasons it can be
 * absent — none of which should render as a number. `null` for the whole object
 * means nothing is selected to compare against; `beta: null` with a
 * `sampleDays` count means the window is too thin to regress.
 */
export interface RiskBeta {
  beta: number | null
  correlation: number | null
  sampleDays: number
  minSampleDays: number
  benchmarkName: string
}

export interface RiskMetrics {
  volatilityPct: number | null
  sortino: number | null
  /**
   * Both drawdowns are `null` when not one daily return in the range was measurable.
   * A `0` cannot stand in for that: this card reads a zero max as licence to print
   * "Never below its opening value", so an unmeasurable window rendered a green
   * reassurance — which is what a stalled price feed produces.
   */
  currentDrawdownPct: number | null
  maxDrawdownPct: number | null
  troughDate: string | null
  recoveredDate: string | null
  beta: RiskBeta | null
}

interface RiskMetricsCardsProps {
  metrics: RiskMetrics | null
  /**
   * The two dividend cards, from `/api/dividends/breakdown`. Its own prop rather than a
   * field on `RiskMetrics`, because everything in that object comes from the pure
   * `portfolioKpis.ts` helpers — which add no request by design — and a query result
   * living in the same type would read as licence for that module to fetch.
   *
   * `undefined` means not loaded, `null` means the backend had no rate to report.
   * Those are different things and the footnote says which.
   */
  dividend?: DividendForwardYield | null
  /** Its query failed. Absent data must be a stated failure, not a confident dash. */
  dividendError?: boolean
  isLoading?: boolean
  /**
   * The queries behind these cards failed. Rendering nothing would make an outage
   * look like a feature that does not exist — the rule `RebalanceCard` and
   * `CurrencyExposureCard` already follow, and which this row did not.
   */
  isError?: boolean
}

/**
 * A dash, not a zero — every absent metric here means "unknown", never "none". The rule
 * now lives in `KpiCard`: passing `value={null}` renders it, so the four files that each
 * had their own idea of an absent value (an em dash here, `N/A` in PerformanceMetrics)
 * cannot drift again.
 */

/** `2026-03-14` → `14 Mar` without dragging a locale into it. */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function shortDate(iso: string): string {
  const [, month, dayOfMonth] = iso.split('-')
  const monthName = MONTHS[Number(month) - 1]
  if (!monthName || !dayOfMonth) return iso
  return `${Number(dayOfMonth)} ${monthName}`
}

export function RiskMetricsCards({
  metrics, dividend, dividendError, isLoading, isError,
}: RiskMetricsCardsProps) {
  const formatCurrency = useFormatCurrency()

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
          Couldn't load the risk metrics — the backend didn't respond. Volatility, Sortino, beta, drawdown and the dividend rates are
          unavailable. It retries automatically.
        </CardContent>
      </Card>
    )
  }

  if (!metrics) {
    return null
  }

  const { beta } = metrics

  // Three states, and the footnote is the only thing that tells them apart — the value
  // is a dash in all three. `isLoading` deliberately does NOT include the dividend
  // query: gating this row on it would hide four already-computed metrics behind the
  // slowest of three requests, and a retrying dividend endpoint would keep the whole
  // row a skeleton for seconds.
  const dividendUnavailable = dividendError === true || dividend === undefined
  const dividendSub = dividendUnavailable
    ? "Couldn't load dividend data"
    : dividend === null
      ? 'No projected dividends'
      : [
          `${formatCurrency(dividend.annual_eur)}/yr`,
          // 'part gross', not 'part est.' — everything here is an estimate, and the
          // thing worth saying is that some of it deducts no withholding and so runs
          // high against the net figure the label otherwise implies.
          dividend.basis === 'net' ? 'projected' : 'projected, part gross',
          `${dividend.paying_holdings} of ${dividend.priced_holdings} pay`,
          ...(dividend.unpriced_holdings > 0
            ? [`${dividend.unpriced_holdings} unpriced`]
            : []),
        ].join(' · ')

  // Prose the footnote has no room for. It carries no figure that exists nowhere else,
  // because a caveat reachable only by hovering does not exist on a touch device.
  const dividendTitle =
    'Projected over the next 12 months from each payer’s own cadence, against current ' +
    'market value. Accumulating ETFs and non-payers correctly contribute nothing, and ' +
    'unpriced holdings are excluded from both sides.'
  // The current fall is the one that describes now; the max describes the worst
  // the account has survived. Showing only the max reads as a live warning long
  // after the recovery.
  const inDrawdown =
    metrics.currentDrawdownPct !== null && metrics.currentDrawdownPct < -0.05

  return (
    <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-3 lg:grid-cols-6">
      <KpiCard
        label="Volatility"
        icon={<Activity className="h-4 w-4 text-muted-foreground" />}
        value={metrics.volatilityPct !== null ? `${metrics.volatilityPct.toFixed(1)}%` : null}
        sub={metrics.volatilityPct !== null
          ? 'Annualised, the risk Sharpe divides by'
          : 'Not enough history in this range'}
      />

      <KpiCard
        label="Sortino Ratio"
        icon={<Gauge className="h-4 w-4 text-muted-foreground" />}
        value={metrics.sortino !== null ? metrics.sortino.toFixed(2) : null}
        tone={metrics.sortino === null ? 'muted'
          : metrics.sortino >= 1 ? 'positive'
          : metrics.sortino >= 0 ? 'warning'
          : 'negative'}
        sub={metrics.sortino !== null
          ? 'Return per unit of downside only'
          : 'No down days to measure against'}
      />

      {/* Beta vs the primary benchmark. Three distinct reasons it can be absent, and the
          footnote is what tells them apart — the value is a dash in all three. */}
      <KpiCard
        label="Beta"
        icon={<Scale className="h-4 w-4 text-muted-foreground" />}
        value={beta?.beta != null ? beta.beta.toFixed(2) : null}
        sub={beta === null
          ? 'Pick a benchmark to compare against'
          : beta.beta === null
            ? `Needs ${beta.minSampleDays} flow-free days (${beta.sampleDays} so far)`
            : `vs ${beta.benchmarkName}${
                beta.correlation !== null ? ` · r ${beta.correlation.toFixed(2)}` : ''
              }`}
      />

      {/* Current drawdown, with the worst one for context */}
      <KpiCard
        label="Current Drawdown"
        icon={<TrendingDown
          className={`h-4 w-4 ${inDrawdown ? 'text-red-600' : 'text-muted-foreground'}`}
        />}
        value={metrics.currentDrawdownPct !== null
          ? `${metrics.currentDrawdownPct.toFixed(2)}%`
          : null}
        tone={metrics.currentDrawdownPct === null ? 'muted'
          : inDrawdown ? 'negative' : 'positive'}
        // "Never below its opening value" is a CLAIM, so it may only be made about a
        // measured zero. With no measurable daily return in the range there is nothing to
        // claim — and that is exactly what a stalled price feed looks like, so a green
        // reassurance was the worst available answer. Worded like the Sharpe card, which
        // distinguishes its two absences for the same reason.
        sub={metrics.maxDrawdownPct === null
          ? 'Not enough history in this range'
          : metrics.maxDrawdownPct === 0
            ? 'Never below its opening value'
            : metrics.recoveredDate !== null
              ? `Recovered ${shortDate(metrics.recoveredDate)} from ${metrics.maxDrawdownPct.toFixed(1)}%`
              : `Worst ${metrics.maxDrawdownPct.toFixed(1)}%${
                  metrics.troughDate ? ` on ${shortDate(metrics.troughDate)}` : ''
                }`}
      />

      {/* The portfolio's dividend rate. This IS the market-value-weighted average of
          the per-security yields — sum(Vi/V x Di/Vi) == sum(Di)/V — so nothing is
          weighted on the client; the backend divides two figures it already holds.
          The per-security column on the Dividends tab is the audit. */}
      <KpiCard
        label="Dividend Yield"
        icon={<Coins className="h-4 w-4 text-muted-foreground" />}
        value={dividend != null ? `${dividend.pct.toFixed(2)}%` : null}
        title={dividendTitle}
        sub={dividendSub}
      />

      {/* Yield on cost, beside it rather than instead of it: the same income over what
          the holdings cost, so the gap between the two cards is the book's own
          appreciation. Both denominators cover the same securities, which is the only
          reason that reading is valid. */}
      <KpiCard
        label="Yield on Cost"
        icon={<PiggyBank className="h-4 w-4 text-muted-foreground" />}
        value={dividend?.on_cost_pct != null ? `${dividend.on_cost_pct.toFixed(2)}%` : null}
        title="The same projected income measured against what the holdings cost rather
               than what they are worth. On an appreciated position the two diverge, and
               the gap is the part a current-yield figure hides."
        sub={dividendUnavailable
          ? "Couldn't load dividend data"
          : dividend?.on_cost_pct == null
            ? 'No projected dividends'
            : 'Same income over what it cost'}
      />
    </div>
  )
}
