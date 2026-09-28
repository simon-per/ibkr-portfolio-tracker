import { useMemo } from 'react'
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'
import type { CryptoPortfolioResponse } from '@/lib/api'
import { allocationSlices } from '@/lib/cryptoChart'
import { CHART_TOOLTIP_ITEM_STYLE, CHART_TOOLTIP_STYLE } from '@/lib/chartTooltip'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { tooltipValue } from '@/lib/utils'

/**
 * The crypto book by coin, as shares of CoinStats' whole total.
 *
 * The legend carries every slice's share in text, so nothing here is readable only on
 * hover — the donut is the picture, the list is the figures. "Not itemised" is a slice
 * of its own rather than spread over the coins (see `allocationSlices`).
 */
export function CryptoAllocationChart({ portfolio }: { portfolio: CryptoPortfolioResponse }) {
  const formatCurrency = useFormatCurrency()
  const slices = useMemo(() => allocationSlices(portfolio), [portfolio])

  if (slices.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        Nothing priced to chart yet.
      </p>
    )
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-1 [&>*]:min-w-0">
      <div className="h-[200px] w-full sm:h-[220px]">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={slices}
              dataKey="value"
              nameKey="label"
              innerRadius="58%"
              outerRadius="90%"
              paddingAngle={1}
              stroke="hsl(var(--card))"
              isAnimationActive={false}
            >
              {slices.map(slice => (
                <Cell key={slice.key} fill={slice.color} />
              ))}
            </Pie>
            <Tooltip
              contentStyle={CHART_TOOLTIP_STYLE}
              itemStyle={CHART_TOOLTIP_ITEM_STYLE}
              formatter={(value: number | undefined) => tooltipValue(value, formatCurrency)}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <ul className="space-y-1.5 text-sm" aria-label="Allocation by coin">
        {slices.map(slice => (
          <li key={slice.key} className="flex items-center gap-2">
            <span
              className="h-2.5 w-2.5 shrink-0 rounded-full"
              style={{ backgroundColor: slice.color }}
              aria-hidden="true"
            />
            <span className="min-w-0 flex-1 truncate">
              {slice.label}
              {slice.kind === 'unitemised' && (
                <span className="text-xs text-muted-foreground"> · in CoinStats' total, not in its list</span>
              )}
            </span>
            <span className="tabular-nums text-muted-foreground">
              {slice.pct == null ? '—' : `${slice.pct.toFixed(1)}%`}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
