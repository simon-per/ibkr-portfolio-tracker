import { useState } from 'react'
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
import { Card, CardContent } from '@/components/ui/card'
import { CollapsibleCardHeader } from '@/components/ui/CollapsibleCardHeader'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import type { PerformanceAttributionResponse } from '@/lib/api'
import { useIsCompact } from '@/lib/useMediaQuery'

interface PerformanceAttributionProps {
  data: PerformanceAttributionResponse | undefined
  isLoading: boolean
  isError?: boolean
}

export function PerformanceAttribution({ data, isLoading, isError }: PerformanceAttributionProps) {
  const formatCurrency = useFormatCurrency()
  const isCompact = useIsCompact()
  const [open, setOpen] = useState(false)

  // Build summary text for the header
  let summaryText: React.ReactNode = 'P&L contribution by security'
  if (data && data.attributions.length > 0) {
    summaryText = (
      <>
        Total P&L:{' '}
        <span className={data.total_pnl_eur >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}>
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
    Math.max(240, sorted.length * (isCompact ? 28 : 36)),
  )

  return (
    <Card>
      <CollapsibleCardHeader
        open={open}
        onToggle={() => setOpen(o => !o)}
        title="Performance Attribution"
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
          className="mx-6 mb-3 rounded-md bg-yellow-50 px-3 py-2 text-xs text-yellow-800 dark:bg-yellow-950/40 dark:text-yellow-300"
        >
          {data!.unpriced_holdings} holding{data!.unpriced_holdings === 1 ? '' : 's'} could not be
          priced for this period and {data!.unpriced_holdings === 1 ? 'is' : 'are'} left out, so the
          total covers less than the whole portfolio. That is missing price data, not a loss.
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
              Couldn't load the attribution — the backend didn't respond. It retries automatically.
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
                <CartesianGrid strokeDasharray="3 3" horizontal={false} />
                <XAxis
                  type="number"
                  tickFormatter={(value: number) => {
                    if (Math.abs(value) >= 1000) {
                      return `${(value / 1000).toFixed(1)}k`
                    }
                    return value.toFixed(0)
                  }}
                  fontSize={12}
                />
                <YAxis
                  type="category"
                  dataKey="symbol"
                  width={isCompact ? 48 : 70}
                  fontSize={12}
                  tick={{ fill: 'currentColor' }}
                />
                <Tooltip content={<AttributionTooltip />} />
                <ReferenceLine x={0} stroke="hsl(var(--muted-foreground))" strokeWidth={1} />
                <Bar dataKey="pnl_contribution_eur" radius={[0, 4, 4, 0]}>
                  {sorted.map((entry, index) => (
                    <Cell
                      key={index}
                      fill={entry.pnl_contribution_eur >= 0 ? '#10b981' : '#f43f5e'}
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

function AttributionTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: PerformanceAttributionResponse['attributions'][0] }> }) {
  const formatCurrency = useFormatCurrency()
  if (!active || !payload || payload.length === 0) return null

  const d = payload[0].payload
  const isPositive = d.pnl_contribution_eur >= 0

  return (
    <div className="bg-popover border rounded-lg shadow-md p-3 text-sm space-y-1">
      <p className="font-semibold">{d.symbol}</p>
      <p className="text-muted-foreground text-xs">{d.description}</p>
      <div className="border-t pt-1 mt-1 space-y-0.5">
        <p>
          <span className="text-muted-foreground">P&L: </span>
          <span className={`font-medium ${isPositive ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
            {isPositive ? '+' : ''}{formatCurrency(d.pnl_contribution_eur)}
          </span>
        </p>
        <p>
          <span className="text-muted-foreground">Contribution: </span>
          <span>{d.contribution_percent.toFixed(1)}%</span>
        </p>
        <p>
          <span className="text-muted-foreground">Weight: </span>
          <span>{d.weight_percent.toFixed(1)}%</span>
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
