import { useMemo } from 'react'
import { CalendarDays } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { CollapsibleCardHeader } from '@/components/ui/CollapsibleCardHeader'
import { ABSENT, cn } from '@/lib/utils'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { monthlyReturnRows, type MonthReturn } from '@/lib/monthlyReturns'
import type { PortfolioValuePoint } from '@/lib/api'
import { ScrollableTable } from '@/components/ui/ScrollableTable'
import { useRememberedOpen } from '@/lib/storage'

interface MonthlyReturnsHeatmapProps {
  data: PortfolioValuePoint[] | undefined
  /**
   * The first day the chart's range asked for (`dateRange.start`). What lets a year or
   * month the range only partly covers name its days in the tooltip — see
   * `computeModifiedDietzReturn`.
   * Optional: without it no period is flagged on the range's account.
   */
  rangeStart?: string
  isLoading: boolean
  isError?: boolean
  /**
   * The calendar month still running, `YYYY-MM`; its cell is tagged `MTD`. Defaults to
   * the viewer's local month. A prop so a test can pin it.
   */
  runningMonth?: string
}

const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function localMonth(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

/**
 * One tooltip builder for both the month cells and the YTD column — they carried
 * two copies of the same template string, so the partial note would have had to be
 * added twice, and the YTD one is the likelier to be forgotten.
 */
function cellTitle(label: string, m: MonthReturn, money: (n: number) => string): string {
  const sign = m.returnPercent >= 0 ? '+' : ''
  return [
    `${label}: ${sign}${m.returnPercent.toFixed(2)}% (${money(m.startValue)} → ${money(m.endValue)})`,
    ...(m.newInvestment > 0 ? [`+${money(m.newInvestment)} invested`] : []),
    // Names the days it actually covers, not just that some were dropped: the label is
    // the thing that is wrong on a trimmed figure, so "part of the period" alone leaves
    // the reader no way to tell a lost day from a lost half-year. And names the cause,
    // because a stalled sync and the 1Y button produce the same dagger.
    ...(m.measured
      ? [`measured ${m.measured.from} → ${m.measured.to}; ${shortenedReason(m)}`]
      : []),
  ].join(' · ')
}

/** Why a cell's window is shorter than its label. */
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

  // Washes of the gain / loss tokens, so the cells are the same green and red as every
  // other figure on the tab. The two strongest steps flip to the page colour for the ink:
  // white on the light theme's deep green, near-black on the dark theme's bright one.
  if (pct >= 0) {
    if (intensity < 0.2) return 'bg-gain/10 text-gain'
    if (intensity < 0.4) return 'bg-gain/20 text-gain'
    if (intensity < 0.6) return 'bg-gain/30 text-gain'
    if (intensity < 0.8) return 'bg-gain/80 text-background'
    return 'bg-gain text-background'
  } else {
    if (intensity < 0.2) return 'bg-loss/10 text-loss'
    if (intensity < 0.4) return 'bg-loss/20 text-loss'
    if (intensity < 0.6) return 'bg-loss/30 text-loss'
    if (intensity < 0.8) return 'bg-loss/80 text-background'
    return 'bg-loss text-background'
  }
}

export function MonthlyReturnsHeatmap({
  data, rangeStart, isLoading, isError, runningMonth = localMonth(),
}: MonthlyReturnsHeatmapProps) {
  const money = useFormatCurrency()
  const [open, setOpen] = useRememberedOpen('monthly-returns')

  // Each month runs from the previous month's last point, and YTD is the months
  // compounded — see `monthlyReturnRows`. A year without a single month return has
  // nothing to show and no row.
  const yearRows = useMemo(
    () => (data ? monthlyReturnRows(data, rangeStart).filter((r) => r.months.some((m) => m !== null)) : []),
    [data, rangeStart],
  )
  const isRunning = (year: number, monthIndex: number) =>
    runningMonth === `${year}-${String(monthIndex + 1).padStart(2, '0')}`

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
          {MONTH_LABELS[lastMonthIdx]}
          {isRunning(topRow.year, lastMonthIdx) ? ' MTD' : ''}:{' '}
          <span className={m.returnPercent >= 0 ? 'text-gain' : 'text-loss'}>
            {m.returnPercent >= 0 ? '+' : ''}{m.returnPercent.toFixed(1)}%
          </span>
        </span>
      )
    }
    if (topRow.ytd) {
      parts.push(
        <span key="ytd">
          {' · '}YTD:{' '}
          <span className={topRow.ytd.returnPercent >= 0 ? 'text-gain' : 'text-loss'}>
            {topRow.ytd.returnPercent >= 0 ? '+' : ''}{topRow.ytd.returnPercent.toFixed(1)}%
          </span>
        </span>
      )
    }
    // A month or YTD measured over part of its period is not marked on the surface —
    // the owner's call (2026-10-14); the cell tooltip still names the days it covers.
    if (parts.length > 0) summaryText = <>{parts}</>
  }

  return (
    <Card>
      <CollapsibleCardHeader
        open={open}
        onToggle={() => setOpen(o => !o)}
        title="Monthly Returns"
        icon={CalendarDays}
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
              Couldn't load the value history — the server didn't respond. It retries automatically.
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
                                'rounded-md px-1.5 py-1 text-center text-xs font-medium tabular-nums',
                                getReturnColor(m.returnPercent)
                              )}
                              title={cellTitle(`${MONTH_LABELS[i]} ${row.year}`, m, money)}
                            >
                              {m.returnPercent >= 0 ? '+' : ''}{m.returnPercent.toFixed(1)}%
                              {isRunning(row.year, i) && (
                                <div className="text-[9px] font-normal leading-none opacity-80">MTD</div>
                              )}
                            </div>
                          ) : (
                            <div className="text-center text-xs text-muted-foreground py-1">{ABSENT}</div>
                          )}
                        </td>
                      ))}
                      <td className="px-0.5 py-0.5 border-l">
                        {row.ytd ? (
                          <div
                            className={cn(
                              'rounded-md px-1.5 py-1 text-center text-xs font-medium tabular-nums',
                              getReturnColor(row.ytd.returnPercent)
                            )}
                            title={cellTitle(`YTD ${row.year}`, row.ytd, money)}
                          >
                            {row.ytd.returnPercent >= 0 ? '+' : ''}{row.ytd.returnPercent.toFixed(1)}%
                          </div>
                        ) : (
                          <div className="text-center text-xs text-muted-foreground py-1">{ABSENT}</div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ScrollableTable>
          )}
        </CardContent>
      )}
    </Card>
  )
}
