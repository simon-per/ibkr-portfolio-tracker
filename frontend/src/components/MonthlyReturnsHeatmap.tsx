import { useState, useMemo } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { CollapsibleCardHeader } from '@/components/ui/CollapsibleCardHeader'
import { cn } from '@/lib/utils'
import { useCurrencySymbol } from '@/lib/CurrencyContext'
import { computeModifiedDietzReturn, type MonthReturn } from '@/lib/monthlyReturns'
import type { PortfolioValuePoint } from '@/lib/api'
import { ScrollableTable } from '@/components/ui/ScrollableTable'

interface MonthlyReturnsHeatmapProps {
  data: PortfolioValuePoint[] | undefined
  /**
   * The first day the chart's range asked for (`dateRange.start`). What lets a year or
   * month the range only partly covers carry the dagger — see `computeModifiedDietzReturn`.
   * Optional: without it no period is flagged on the range's account.
   */
  rangeStart?: string
  isLoading: boolean
  isError?: boolean
}

interface YearRow {
  year: number
  months: (MonthReturn | null)[]
  ytd: MonthReturn | null
}

const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/**
 * One tooltip builder for both the month cells and the YTD column — they carried
 * two copies of the same template string, so the partial note would have had to be
 * added twice, and the YTD one is the likelier to be forgotten.
 */
function cellTitle(label: string, m: MonthReturn, curSym: string): string {
  const money = (n: number) => `${curSym}${n.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
  const sign = m.returnPercent >= 0 ? '+' : ''
  return [
    `${label}: ${sign}${m.returnPercent.toFixed(2)}% (${money(m.startValue)} → ${money(m.endValue)})`,
    ...(m.newInvestment > 0 ? [`+${money(m.newInvestment)} invested`] : []),
    // Names the days it actually covers, not just that some were dropped: the label is
    // the thing that is wrong on a trimmed figure, so "part of the period" alone leaves
    // the reader no way to tell a lost day from a lost half-year. And names the cause,
    // because a stalled sync and the 1Y button produce the same dagger.
    ...(m.measured
      ? [`† measured ${m.measured.from} → ${m.measured.to}; ${shortenedReason(m)}`]
      : []),
  ].join(' · ')
}

/** Why a cell's window is shorter than its label, in the words the footnote uses. */
function shortenedReason(m: MonthReturn): string {
  const causes: string[] = []
  if (m.shortenedBy?.range) causes.push('the selected range starts inside this period')
  if (m.shortenedBy?.unpriced) causes.push('the edge days could not be fully valued')
  return causes.length ? causes.join(' and ') : 'part of the period only'
}

function getReturnColor(pct: number): string {
  // Clamp to [-10, 10] for color scaling
  const clamped = Math.max(-10, Math.min(10, pct))
  const intensity = Math.abs(clamped) / 10

  if (pct >= 0) {
    // Green scale - light to dark
    if (intensity < 0.2) return 'bg-green-100 dark:bg-green-950/40 text-green-800 dark:text-green-200'
    if (intensity < 0.4) return 'bg-green-200 dark:bg-green-900/60 text-green-900 dark:text-green-100'
    if (intensity < 0.6) return 'bg-green-300 dark:bg-green-800/70 text-green-900 dark:text-green-100'
    if (intensity < 0.8) return 'bg-green-400 dark:bg-green-700/80 text-white dark:text-green-50'
    return 'bg-green-500 dark:bg-green-600 text-white'
  } else {
    // Red scale - light to dark
    if (intensity < 0.2) return 'bg-red-100 dark:bg-red-950/40 text-red-800 dark:text-red-200'
    if (intensity < 0.4) return 'bg-red-200 dark:bg-red-900/60 text-red-900 dark:text-red-100'
    if (intensity < 0.6) return 'bg-red-300 dark:bg-red-800/70 text-red-900 dark:text-red-100'
    if (intensity < 0.8) return 'bg-red-400 dark:bg-red-700/80 text-white dark:text-red-50'
    return 'bg-red-500 dark:bg-red-600 text-white'
  }
}

export function MonthlyReturnsHeatmap({ data, rangeStart, isLoading, isError }: MonthlyReturnsHeatmapProps) {
  const curSym = useCurrencySymbol()
  const [open, setOpen] = useState(false)

  const yearRows = useMemo(() => {
    if (!data || data.length < 2) return []

    // Group data points by YYYY-MM
    const monthGroups = new Map<string, PortfolioValuePoint[]>()
    for (const point of data) {
      const key = point.date.substring(0, 7) // "YYYY-MM"
      const group = monthGroups.get(key)
      if (group) {
        group.push(point)
      } else {
        monthGroups.set(key, [point])
      }
    }

    // Compute Modified Dietz return per month. `periodStart` is the month's first
    // calendar day, so a month the range began inside carries the dagger.
    const monthReturns = new Map<string, MonthReturn>()
    for (const [key, points] of monthGroups) {
      const result = computeModifiedDietzReturn(points, { periodStart: `${key}-01`, rangeStart })
      if (result) monthReturns.set(key, result)
    }

    // Organize into year rows
    const yearMap = new Map<number, (MonthReturn | null)[]>()
    for (const [key] of monthReturns) {
      const year = parseInt(key.substring(0, 4))
      if (!yearMap.has(year)) {
        yearMap.set(year, new Array(12).fill(null))
      }
      const monthIndex = parseInt(key.substring(5, 7)) - 1
      yearMap.get(year)![monthIndex] = monthReturns.get(key)!
    }

    // Compute YTD for each year using Modified Dietz. `yearPoints` is whatever the
    // selected RANGE holds for that year, so on 1Y last year's "YTD" runs from today's
    // date to 31 December — `periodStart` is what gets that badged.
    const rows: YearRow[] = []
    for (const [year, months] of yearMap) {
      const yearPrefix = String(year)
      const yearPoints = data.filter(p => p.date.startsWith(yearPrefix))
      const ytd = computeModifiedDietzReturn(yearPoints, { periodStart: `${yearPrefix}-01-01`, rangeStart })
      rows.push({ year, months, ytd })
    }

    // Sort descending (most recent on top)
    rows.sort((a, b) => b.year - a.year)
    return rows
  }, [data, rangeStart])

  const cells = yearRows.flatMap(row => [row.ytd, ...row.months])
  const hasPartial = cells.some(m => m?.partial)
  // Which causes the footnote has to explain — only the ones on the page, so a reader
  // is not sent looking for a stalled sync when the 1Y button is the whole story.
  const shortenedByRange = cells.some(m => m?.shortenedBy?.range)
  const shortenedByUnpriced = cells.some(m => m?.shortenedBy?.unpriced)
  const footnoteCauses = [
    shortenedByRange ? 'the selected range starts inside it' : null,
    shortenedByUnpriced
      ? 'some days could not be fully valued, so they are excluded rather than counted at zero (a stalled market-data sync is the usual cause)'
      : null,
  ]
    .filter((s): s is string => s !== null)
    .join(', or ')

  // Summary text for collapsed state
  let summaryText: React.ReactNode = 'Monthly return percentages by year'
  if (isError) {
    summaryText = 'Could not load the value history'
  } else if (yearRows.length > 0) {
    const topRow = yearRows[0]
    // Find last filled month
    let lastMonthIdx = -1
    for (let i = 11; i >= 0; i--) {
      if (topRow.months[i] !== null) {
        lastMonthIdx = i
        break
      }
    }
    const parts: React.ReactNode[] = []
    if (lastMonthIdx >= 0) {
      const m = topRow.months[lastMonthIdx]!
      parts.push(
        <span key="month">
          {MONTH_LABELS[lastMonthIdx]}:{' '}
          <span className={m.returnPercent >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}>
            {m.returnPercent >= 0 ? '+' : ''}{m.returnPercent.toFixed(1)}%
          </span>
          {m.partial && <span aria-hidden="true">†</span>}
        </span>
      )
    }
    if (topRow.ytd) {
      parts.push(
        <span key="ytd">
          {' · '}YTD:{' '}
          <span className={topRow.ytd.returnPercent >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}>
            {topRow.ytd.returnPercent >= 0 ? '+' : ''}{topRow.ytd.returnPercent.toFixed(1)}%
          </span>
          {topRow.ytd.partial && <span aria-hidden="true">†</span>}
        </span>
      )
    }
    // The card is COLLAPSED by default, so this line is the only thing most readers see —
    // and it carried the figure with no marker at all while the table below badged it. A
    // trimmed "YTD" measuring six weeks of a year is exactly the plausible-looking number
    // this codebase keeps being bitten by, so the caveat has to survive the collapse. The
    // legend is spelled out inline rather than left to the dagger, because the footnote
    // explaining it lives inside the body that is not rendered yet.
    const summaryPartial = topRow.ytd?.partial || (lastMonthIdx >= 0 && topRow.months[lastMonthIdx]!.partial)
    if (summaryPartial) {
      parts.push(
        <span key="note" className="text-muted-foreground">
          {' · '}<span aria-hidden="true">† </span>part of the period only
        </span>
      )
    }
    if (parts.length > 0) summaryText = <>{parts}</>
  }

  return (
    <Card>
      <CollapsibleCardHeader
        open={open}
        onToggle={() => setOpen(o => !o)}
        title="Monthly Returns"
        description={summaryText}
        contentId="monthly-returns-content"
      />
      {open && (
        <CardContent id="monthly-returns-content">
          {isLoading ? (
            <div className="h-[200px] w-full animate-pulse rounded-md bg-muted" />
          ) : isError ? (
            // "Not enough data" would be a lie when the request simply failed.
            <p className="text-muted-foreground text-center py-8">
              Couldn't load the value history — the backend didn't respond. It retries automatically.
            </p>
          ) : yearRows.length === 0 ? (
            <p className="text-muted-foreground text-center py-8">
              Not enough data to compute monthly returns.
            </p>
          ) : (
            /* A 12-month x N-year matrix is legitimately wide, so it keeps scrolling
               rather than being transposed — transposing would buy one honest scroll
               for a much longer page plus a second rendering of the same data. What it
               gains is the shared primitive's edge fade and keyboard reachability, and
               cells narrow enough on a phone to drop the hard minimum from ~755px to
               ~570px. */
            <ScrollableTable label="Monthly returns by year">
              <table className="w-full text-sm">
                <thead>
                  <tr>
                    <th className="text-left font-medium text-muted-foreground px-2 py-1.5">Year</th>
                    {MONTH_LABELS.map(m => (
                      <th key={m} className="text-center font-medium text-muted-foreground px-1 py-1.5 min-w-[40px] sm:min-w-[52px]">{m}</th>
                    ))}
                    <th className="text-center font-medium text-muted-foreground px-2 py-1.5 min-w-[48px] sm:min-w-[60px] border-l">YTD</th>
                  </tr>
                </thead>
                <tbody>
                  {yearRows.map(row => (
                    <tr key={row.year}>
                      <td className="font-medium px-2 py-1">{row.year}</td>
                      {row.months.map((m, i) => (
                        <td key={i} className="px-0.5 py-0.5">
                          {m ? (
                            <div
                              className={cn(
                                'rounded px-1.5 py-1 text-center text-xs font-medium',
                                getReturnColor(m.returnPercent)
                              )}
                              title={cellTitle(`${MONTH_LABELS[i]} ${row.year}`, m, curSym)}
                            >
                              {m.returnPercent >= 0 ? '+' : ''}{m.returnPercent.toFixed(1)}%
                              {m.partial && <span aria-hidden="true">†</span>}
                            </div>
                          ) : (
                            <div className="text-center text-xs text-muted-foreground py-1">–</div>
                          )}
                        </td>
                      ))}
                      <td className="px-0.5 py-0.5 border-l">
                        {row.ytd ? (
                          <div
                            className={cn(
                              'rounded px-1.5 py-1 text-center text-xs font-medium',
                              getReturnColor(row.ytd.returnPercent)
                            )}
                            title={cellTitle(`YTD ${row.year}`, row.ytd, curSym)}
                          >
                            {row.ytd.returnPercent >= 0 ? '+' : ''}{row.ytd.returnPercent.toFixed(1)}%
                            {row.ytd.partial && <span aria-hidden="true">†</span>}
                          </div>
                        ) : (
                          <div className="text-center text-xs text-muted-foreground py-1">–</div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ScrollableTable>
          )}
          {/* The dagger has to be explained somewhere reachable without a pointer —
              a caveat that only exists in a `title` attribute does not exist on a
              phone, which this codebase has already learned once. */}
          {hasPartial && (
            <p className="mt-2 text-xs text-muted-foreground">
              † Measured over part of the period: {footnoteCauses}. Hover a cell for the days
              it does cover, which can be much shorter than the column it sits in.
            </p>
          )}
        </CardContent>
      )}
    </Card>
  )
}
