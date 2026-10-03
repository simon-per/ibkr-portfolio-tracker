import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
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
  excludedInRange,
  rangePnlSeries,
  reconstructedCaption,
  reconstructedSpan,
  sliceRange,
  valueSeries,
  type CryptoRange,
} from '@/lib/cryptoChart'
import { axisFloor, niceTicks } from '@/lib/niceTicks'
import {
  CHART_TOOLTIP_ITEM_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
  CHART_TOOLTIP_STYLE,
} from '@/lib/chartTooltip'
import { formatDate, parseLocalDate, tooltipValue } from '@/lib/utils'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { useIsCompact } from '@/lib/useMediaQuery'

/** One height for the chart and its placeholder, so a range switch cannot make the page jump. */
const CHART_BOX = 'h-[260px] sm:h-[340px]'

type Metric = 'value' | 'pnl'

/** The P&L line starts at 0 on the range's first day; the caption names that day. */
function pnlCaption(firstDate: string | undefined): string | null {
  return firstDate ? `From 0 on ${formatDate(firstDate)}` : null
}

/**
 * The book's daily history — value or P&L — over a range sliced client-side.
 *
 * A missing point is a gap in the line, never a zero: `connectNulls` is off on purpose,
 * because a line drawn through a day nobody could price asserts a value for it. The days
 * before the first synced holdings are shaded, and the sentence under the chart says what
 * they are — on the surface, not in a hover.
 */
export function CryptoHistoryChart({
  points, startDate, basketDate, firstSnapshotDate,
}: {
  points: readonly CryptoHistoryPoint[]
  startDate: string
  basketDate: string | null
  firstSnapshotDate: string | null
}) {
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
  const excluded = useMemo(() => excludedInRange(visible), [visible])
  const shaded = useMemo(() => reconstructedSpan(visible), [visible])
  const caption = reconstructedCaption(startDate, basketDate, firstSnapshotDate, formatDate)

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

  const shortRange = range === '1W' || range === 'MTD' || range === '1M' || range === '3M'
  const formatXAxisTick = (value: string) => {
    const date = parseLocalDate(value)
    if (Number.isNaN(date.getTime())) return ''
    return date.toLocaleDateString(
      'en-US',
      shortRange ? { month: 'short', day: 'numeric' } : { month: 'short', year: '2-digit' },
    )
  }
  const formatYAxisTick = (value: number) => {
    const abs = Math.abs(value)
    if (abs >= 1000) return `${value < 0 ? '-' : ''}${curSym}${(abs / 1000).toFixed(abs >= 10_000 ? 0 : 1)}k`
    return `${value < 0 ? '-' : ''}${curSym}${abs.toFixed(0)}`
  }

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
      {metric === 'pnl' && pnlCaption(data[0]?.date) && (
        <p className="text-xs text-muted-foreground">{pnlCaption(data[0]?.date)}</p>
      )}

      {axis === null ? (
        <div className={`flex w-full ${CHART_BOX} items-center justify-center rounded-lg border border-dashed bg-muted/10 px-4 text-center text-sm text-muted-foreground`}>
          {points.length === 0
            ? 'No history yet — it starts with the first crypto sync.'
            : 'Nothing measured in this range.'}
        </div>
      ) : (
        <div className={`w-full ${CHART_BOX}`}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 5, right: isCompact ? 8 : 24, left: 0, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
              <XAxis
                dataKey="date"
                className="text-xs"
                tick={{ fill: 'hsl(var(--muted-foreground))' }}
                tickFormatter={formatXAxisTick}
                interval="preserveStartEnd"
                minTickGap={isCompact ? 44 : 60}
              />
              <YAxis
                className="text-xs"
                tick={{ fill: 'hsl(var(--muted-foreground))' }}
                tickFormatter={formatYAxisTick}
                domain={axis.domain}
                ticks={axis.ticks}
                width={isCompact ? 48 : 64}
              />
              {shaded && (
                <ReferenceArea
                  x1={shaded.from}
                  x2={shaded.to}
                  fill="hsl(var(--muted-foreground))"
                  fillOpacity={0.08}
                  ifOverflow="hidden"
                />
              )}
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
              <Line
                type="monotone"
                dataKey="y"
                name={metric === 'value' ? 'Value' : 'P&L'}
                stroke="hsl(var(--primary))"
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 5 }}
                connectNulls={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      {excluded.length > 0 && (
        <p className="text-xs text-muted-foreground">
          No price on some days, left out: {excluded.join(', ')}
        </p>
      )}
      {shaded && caption && (
        <p className="text-xs text-muted-foreground">{caption}</p>
      )}
    </div>
  )
}
