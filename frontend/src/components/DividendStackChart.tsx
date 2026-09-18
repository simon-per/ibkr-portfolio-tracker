import type { ReactNode } from 'react'
import {
  Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { dividendMonthLabel, FC } from '@/lib/dividendChart'
import { useIsCompact } from '@/lib/useMediaQuery'

/**
 * One height for the chart and every placeholder that stands in for it, so a mode
 * switch or an empty range cannot make the page jump. Exported because the
 * loading skeleton lives in DividendsTab: the literal was written out four times
 * across the two files before this.
 */
export const DIVIDEND_CHART_BOX = 'h-[240px] w-full sm:h-[320px]'

interface DividendStackChartProps {
  data: Record<string, number | string>[]
  /** Stack order IS palette order — see the docblock in lib/dividendColors. */
  stackSymbols: string[]
  colorOf: (symbol: string) => string
  showForecast: boolean
  /** Put the year on the tick labels: the axis spans more than one. */
  multiYear: boolean
  /** Heading of the tooltip for a given month key. */
  tooltipTitle: (month: string) => string
  /** Anything below the total — delta chips, provenance caveats. */
  tooltipFooter?: (month: string) => ReactNode
}

/**
 * The dividend stack, drawn once for two views.
 *
 * The monthly chart and the rolling twelve-month chart are the same picture over
 * different buckets: same symbols, same colours, same actual/forecast split. They
 * were about to be two near-identical fifty-line recharts trees with two copies
 * of the tooltip's payload plumbing — the shape that made sixteen hand-written
 * KPI cards disagree about what an absent value looks like. One component, two
 * callers, and a series can no longer reach one view and not the other.
 *
 * Both halves of a symbol share a `stackId`, so each column reads as one total
 * with the projected part hatched rather than as two bars side by side.
 */
export function DividendStackChart({
  data, stackSymbols, colorOf, showForecast, multiYear, tooltipTitle, tooltipFooter,
}: DividendStackChartProps) {
  const curSym = useCurrencySymbol()
  const formatCurrency = useFormatCurrency()
  const isCompact = useIsCompact()

  const formatAxisTick = (v: number) =>
    Math.abs(v) >= 1000 ? `${curSym}${(v / 1000).toFixed(1)}k` : `${curSym}${v}`

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const renderTooltip = ({ active, payload, label }: any) => {
    if (!active || !payload?.length) return null
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const rows = payload.filter((p: any) => typeof p.value === 'number' && p.value > 0)
    // Actual vs forecast is read off the key prefix, not off stackSymbols, so the
    // split works for whatever buckets the caller handed in.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const actual = rows.filter((p: any) => !String(p.dataKey).startsWith(FC))
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const forecast = rows.filter((p: any) => String(p.dataKey).startsWith(FC))
    const footer = tooltipFooter?.(label)
    if (!actual.length && !forecast.length) return null
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const total = rows.reduce((s: number, p: any) => s + p.value, 0)
    return (
      <div className="rounded-lg border border-border bg-card px-3 py-2 text-sm shadow-md">
        <div className="mb-1 font-medium">{tooltipTitle(label)}</div>
        {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
        {actual.map((p: any) => (
          <div key={p.dataKey} className="flex items-center justify-between gap-6">
            <span className="inline-flex items-center gap-1.5 text-muted-foreground">
              <span className="h-2 w-2 rounded-sm" style={{ backgroundColor: p.fill }} />
              {p.dataKey}
            </span>
            <span className="tabular-nums">{formatCurrency(p.value)}</span>
          </div>
        ))}
        {forecast.length > 0 && (
          <div className="mt-1 border-t border-border/50 pt-1 text-xs text-muted-foreground">
            Forecast
          </div>
        )}
        {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
        {forecast.map((p: any) => (
          <div key={p.dataKey} className="flex items-center justify-between gap-6">
            <span className="inline-flex items-center gap-1.5 text-muted-foreground">
              <span
                className="h-2 w-2 rounded-sm border border-dashed"
                style={{ borderColor: p.stroke, backgroundColor: 'transparent' }}
              />
              {String(p.dataKey).slice(FC.length)}
            </span>
            <span className="tabular-nums">{formatCurrency(p.value)}</span>
          </div>
        ))}
        <div className="mt-1 flex items-center justify-between gap-6 border-t border-border/50 pt-1 font-medium">
          <span>Total</span>
          <span className="tabular-nums">{formatCurrency(total)}</span>
        </div>
        {footer && (
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border/50 pt-1">
            {footer}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className={DIVIDEND_CHART_BOX}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-muted" vertical={false} />
          <XAxis
            dataKey="month"
            tickFormatter={(m: string) => dividendMonthLabel(m, multiYear)}
            tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 12 }}
            tickLine={false}
            axisLine={{ stroke: 'hsl(var(--border))' }}
            interval={multiYear ? 'preserveStartEnd' : isCompact ? 1 : 0}
            minTickGap={isCompact ? 24 : 16}
          />
          <YAxis
            tickFormatter={formatAxisTick}
            tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 12 }}
            tickLine={false}
            axisLine={false}
            width={isCompact ? 40 : 60}
          />
          <Tooltip content={renderTooltip} cursor={{ fill: 'hsl(var(--muted))', opacity: 0.35 }} />
          {stackSymbols.map((s, i) => (
            <Bar
              key={s}
              dataKey={s}
              stackId="d"
              fill={colorOf(s)}
              stroke="hsl(var(--card))"
              strokeWidth={1}
              isAnimationActive={false}
              // Only the topmost series gets the rounded cap, or every
              // segment in the stack would look like its own bar.
              radius={i === stackSymbols.length - 1 && !showForecast
                ? [3, 3, 0, 0] : undefined}
            />
          ))}
          {showForecast &&
            stackSymbols.map((s, i) => (
              <Bar
                key={FC + s}
                dataKey={FC + s}
                stackId="d"
                fill={colorOf(s)}
                fillOpacity={0.4}
                stroke={colorOf(s)}
                strokeDasharray="3 2"
                strokeWidth={1}
                isAnimationActive={false}
                radius={i === stackSymbols.length - 1 ? [3, 3, 0, 0] : undefined}
              />
            ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
