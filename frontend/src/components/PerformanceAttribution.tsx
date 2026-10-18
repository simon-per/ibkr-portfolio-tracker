import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  Cell,
  ResponsiveContainer,
} from 'recharts'
import { ChartBar } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { GAIN_COLOR, LOSS_COLOR } from '@/lib/chartPalette'
import { CollapsibleCardHeader } from '@/components/ui/CollapsibleCardHeader'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import type { PerformanceAttributionResponse } from '@/lib/api'
import { useIsCompact } from '@/lib/useMediaQuery'
import { displayName } from '@/lib/securityName'
import { ABSENT, formatAxisMoney } from '@/lib/utils'
import { useRememberedOpen } from '@/lib/storage'
import { CHART_GRID_PROPS } from '@/lib/chartTooltip'

interface PerformanceAttributionProps {
  data: PerformanceAttributionResponse | undefined
  isLoading: boolean
  isError?: boolean
}

export function PerformanceAttribution({ data, isLoading, isError }: PerformanceAttributionProps) {
  const formatCurrency = useFormatCurrency()
  const isCompact = useIsCompact()
  const [open, setOpen] = useRememberedOpen('performance-attribution')

  // Build summary text for the header
  let summaryText: React.ReactNode = 'P&L contribution by security'
  if (data && data.attributions.length > 0) {
    summaryText = (
      <>
        Total P&L:{' '}
        <span className={data.total_pnl_eur >= 0 ? 'text-gain' : 'text-loss'}>
          {data.total_pnl_eur >= 0 ? '+' : ''}{formatCurrency(data.total_pnl_eur)}
        </span>
      </>
    )
  }

  // Filter and sort data for the chart
  const sorted = (() => {
    if (!data || data.attributions.length === 0) return []
    const filtered = data.attributions.filter(a => Math.abs(a.pnl_contribution_eur) >= 1)
    const positive = filtered.filter(a => a.pnl_contribution_eur >= 0).sort((a, b) => b.pnl_contribution_eur - a.pnl_contribution_eur)
    const negative = filtered.filter(a => a.pnl_contribution_eur < 0).sort((a, b) => b.pnl_contribution_eur - a.pnl_contribution_eur)
    return [...positive, ...negative]
  })()

  // Height stays a number here, unlike every other chart: it is *data*-driven — one
  // row per security — so a CSS height would squash thirty bars into 240px. Only the
  // per-row allowance and the ceiling are re-parameterised for a phone.
  const chartHeight = Math.min(
    isCompact ? 600 : 900,
    Math.max(240, sorted.length * (isCompact ? 24 : 26)),
  )

  return (
    <Card>
      <CollapsibleCardHeader
        open={open}
        onToggle={() => setOpen(o => !o)}
        title="Performance Attribution"
        icon={ChartBar}
        description={summaryText}
        contentId="performance-attribution-content"
      />
      {/* Securities the backend could not value at an endpoint are excluded rather than
          counted at zero — a zero end value renders as a bar of -(the whole position),
          which on a per-security chart is the most legible place in the app to publish
          a fabricated loss. Excluding them is right; showing fewer bars under a total
          labelled as the portfolio's without saying so is not.

          OUTSIDE the `open &&` block deliberately. This card is collapsed by default,
          and the collapsed summary shows `total_pnl_eur` — the very figure the notice
          qualifies. A caveat you have to expand a card to reach is as good as absent,
          which is the same rule that put the dividend basis in a footnote rather than a
          tooltip. */}
      {(data?.unpriced_holdings ?? 0) > 0 && (
        <p
          role="alert"
          className="mx-[var(--card-padding)] mb-3 rounded-md bg-yellow-50 px-3 py-2 text-xs text-yellow-800 dark:bg-yellow-950/40 dark:text-yellow-300"
        >
          {data!.unpriced_holdings} holding{data!.unpriced_holdings === 1 ? '' : 's'} could not be
          priced for this period and {data!.unpriced_holdings === 1 ? 'is' : 'are'} left out, so the
          total covers less than the whole portfolio — missing prices, not a loss.
        </p>
      )}
      {open && (
        <CardContent id="performance-attribution-content">
          {isLoading ? (
            <div className="h-[280px] w-full animate-pulse rounded-md bg-muted sm:h-[400px]" />
          ) : isError ? (
            // A server error must not impersonate "no P&L changes" — see
            // PortfolioValueChart's error state for the same pattern.
            <p className="text-muted-foreground text-center py-8">
              Couldn't load the attribution — the server didn't respond. It retries automatically.
            </p>
          ) : sorted.length === 0 ? (
            <p className="text-muted-foreground text-center py-8">
              No significant P&L changes in this period.
            </p>
          ) : (
            <ResponsiveContainer width="100%" height={chartHeight}>
              <BarChart
                data={sorted}
                layout="vertical"
                margin={{ top: 5, right: isCompact ? 8 : 30, left: 0, bottom: 5 }}
              >
                <CartesianGrid {...CHART_GRID_PROPS} vertical horizontal={false} />
                <XAxis
                  type="number"
                  tickFormatter={(value: number) => formatAxisMoney(value)}
                  fontSize={12}
                />
                <YAxis
                  type="category"
                  dataKey="symbol"
                  width={isCompact ? 64 : 84}
                  fontSize={12}
                  // Every bar keeps its label; Recharts drops alternate ones by default.
                  interval={0}
                  tick={{ fill: 'currentColor' }}
                  tickFormatter={(s: string) => (s.length > 9 ? `${s.slice(0, 8)}…` : s)}
                />
                <Tooltip content={<AttributionTooltip />} />
                <ReferenceLine x={0} stroke="hsl(var(--muted-foreground))" strokeWidth={1} />
                <Bar dataKey="pnl_contribution_eur" radius={[0, 4, 4, 0]} maxBarSize={16}>
                  {sorted.map((entry, index) => (
                    <Cell
                      key={index}
                      fill={entry.pnl_contribution_eur >= 0 ? GAIN_COLOR : LOSS_COLOR}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      )}
    </Card>
  )
}

/** A share of the window's total, or the dash when that total is zero (`null`). */
const shareOf = (pct: number | null): string => (pct == null ? ABSENT : `${pct.toFixed(1)}%`)

export function AttributionTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: PerformanceAttributionResponse['attributions'][0] }> }) {
  const formatCurrency = useFormatCurrency()
  if (!active || !payload || payload.length === 0) return null

  const d = payload[0].payload
  const isPositive = d.pnl_contribution_eur >= 0

  return (
    <div className="bg-popover border rounded-lg shadow-md p-3 text-sm space-y-1">
      <p className="font-semibold">{d.symbol}</p>
      <p className="text-muted-foreground text-xs">{displayName(d.description)}</p>
      <div className="border-t pt-1 mt-1 space-y-0.5">
        <p>
          <span className="text-muted-foreground">P&L: </span>
          <span className={`font-medium ${isPositive ? 'text-gain' : 'text-loss'}`}>
            {isPositive ? '+' : ''}{formatCurrency(d.pnl_contribution_eur)}
          </span>
        </p>
        <p>
          <span className="text-muted-foreground">Contribution: </span>
          <span>{shareOf(d.contribution_percent)}</span>
        </p>
        <p>
          <span className="text-muted-foreground">Weight: </span>
          <span>{shareOf(d.weight_percent)}</span>
        </p>
        {d.new_investment_eur > 0 && (
          <p>
            <span className="text-muted-foreground">New investment: </span>
            <span>{formatCurrency(d.new_investment_eur)}</span>
          </p>
        )}
      </div>
    </div>
  )
}
