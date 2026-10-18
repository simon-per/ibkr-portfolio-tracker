import { useMemo, useState } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { CryptoHistoryPoint } from '@/lib/api'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import {
  CRYPTO_RANGES,
  rangePnlSeries,
  sliceRange,
  valueSeries,
  type CryptoRange,
} from '@/lib/cryptoChart'
import { axisFloor, niceTicks } from '@/lib/niceTicks'
import {
  CHART_TOOLTIP_ITEM_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
  CHART_TOOLTIP_STYLE,
  CHART_GRID_PROPS,
} from '@/lib/chartTooltip'
import { formatDate, tooltipValue, formatAxisMoney } from '@/lib/utils'
import { dateAxis } from '@/lib/dateAxis'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { useIsCompact } from '@/lib/useMediaQuery'

/** One height for the chart and its placeholder, so a range switch cannot make the page jump. */
const CHART_BOX = 'h-[260px] sm:h-[340px]'

type Metric = 'value' | 'pnl'

/**
 * The book's daily history — value or P&L — over a range sliced client-side.
 *
 * A missing point is a gap in the line, never a zero: `connectNulls` is off on purpose,
 * because a line drawn through a day nobody could price asserts a value for it. Days
 * before the first synced holdings are drawn like synced ones (owner, 2026-10-14).
 */
export function CryptoHistoryChart({ points }: { points: readonly CryptoHistoryPoint[] }) {
  const [metric, setMetric] = useState<Metric>('value')
  const [range, setRange] = useState<CryptoRange>('YTD')
  const formatCurrency = useFormatCurrency()
  const curSym = useCurrencySymbol()
  const isCompact = useIsCompact()

  const visible = useMemo(() => sliceRange(points, range), [points, range])
  const data = useMemo(() => {
    // Daily values, unknown days as gaps — see `valueSeries`; daily P&L summed over what
    // is on screen — see `rangePnlSeries`.
    const ys = metric === 'value' ? valueSeries(visible) : rangePnlSeries(visible)
    return visible.map((p, i) => ({ date: p.date, y: ys[i], excluded: p.excluded ?? [] }))
  }, [visible, metric])

  const axis = useMemo(() => {
    const values = data.map(d => d.y).filter((v): v is number => v != null)
    if (values.length === 0) return null
    const min = Math.min(...values)
    const max = Math.max(...values)
    const pad = (max - min || Math.abs(max) || 1) * 0.1
    const paddedMin = min - pad
    // A value line never needs an axis below zero; a P&L line can be genuinely negative,
    // and there the floor is simply the padded minimum.
    const floor = metric === 'value' ? axisFloor(min, paddedMin, paddedMin) : undefined
    return niceTicks(paddedMin, max + pad, isCompact ? 4 : 6, floor)
  }, [data, metric, isCompact])

  const xAxis = useMemo(() => dateAxis(data.map((p) => p.date)), [data])
  const formatYAxisTick = (value: number) => formatAxisMoney(value, curSym, axis?.step)

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SegmentedControl<Metric>
          label="Chart series"
          value={metric}
          onChange={setMetric}
          options={[
            { value: 'value', label: 'Value' },
            { value: 'pnl', label: 'P&L' },
          ]}
        />
        <SegmentedControl<CryptoRange>
          label="Time range"
          variant="buttons"
          value={range}
          onChange={setRange}
          className="min-w-0 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          buttonClassName="shrink-0"
          options={CRYPTO_RANGES.map(r => ({ value: r, label: r }))}
        />
      </div>

      {axis === null ? (
        <div className={`flex w-full ${CHART_BOX} items-center justify-center rounded-lg border border-dashed bg-muted/10 px-4 text-center text-sm text-muted-foreground`}>
          {points.length === 0
            ? 'No history yet — it starts with the first crypto sync.'
            : 'Nothing measured in this range.'}
        </div>
      ) : (
        <div className={`w-full ${CHART_BOX}`}>
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data} margin={{ top: 5, right: isCompact ? 8 : 24, left: 0, bottom: 5 }}>
              <defs>
                <linearGradient id="crypto-value-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.18} />
                  <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid {...CHART_GRID_PROPS} />
              <XAxis
                dataKey="date"
                className="text-xs"
                tickMargin={8}
                tick={{ fill: 'hsl(var(--muted-foreground))' }}
                ticks={xAxis.ticks}
                tickFormatter={xAxis.tickFormatter}
                interval="preserveStartEnd"
                minTickGap={isCompact ? 44 : 60}
              />
              <YAxis
                className="text-xs"
                tick={{ fill: 'hsl(var(--muted-foreground))' }}
                tickFormatter={formatYAxisTick}
                domain={axis.domain}
                ticks={axis.ticks}
                width="auto"
              />
              {metric === 'pnl' && <ReferenceLine y={0} stroke="hsl(var(--border))" />}
              <Tooltip
                contentStyle={CHART_TOOLTIP_STYLE}
                itemStyle={CHART_TOOLTIP_ITEM_STYLE}
                labelStyle={CHART_TOOLTIP_LABEL_STYLE}
                labelFormatter={(label, payload) => {
                  const text = typeof label === 'string' ? formatDate(label) : label
                  const out: string[] = payload?.[0]?.payload?.excluded ?? []
                  return out.length > 0 ? `${text} · excludes ${out.join(', ')} (no price)` : text
                }}
                formatter={(value: number | undefined) => tooltipValue(value, formatCurrency)}
              />
              {/* The value as the stock chart draws its headline series — one soft area —
                  and P&L as a plain line read against the zero rule. */}
              {metric === 'value' ? (
                <Area
                  type="monotone"
                  dataKey="y"
                  name="Value"
                  stroke="hsl(var(--primary))"
                  strokeWidth={2}
                  fill="url(#crypto-value-fill)"
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 0 }}
                  connectNulls={false}
                  isAnimationActive={false}
                />
              ) : (
                <Line
                  type="monotone"
                  dataKey="y"
                  name="P&L"
                  stroke="hsl(var(--primary))"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 0 }}
                  connectNulls={false}
                  isAnimationActive={false}
                />
              )}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  )
}
