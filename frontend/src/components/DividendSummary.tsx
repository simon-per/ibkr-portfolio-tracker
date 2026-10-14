import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardContent } from '@/components/ui/card'
import { CollapsibleCardHeader } from '@/components/ui/CollapsibleCardHeader'
import { Loader2 } from 'lucide-react'
import { cn, formatCount } from '@/lib/utils'
import { api } from '@/lib/api'
import { useCurrencySymbol } from '@/lib/CurrencyContext'
import type { DividendSummaryResponse } from '@/lib/api'
import { ScrollableTable } from '@/components/ui/ScrollableTable'

const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

interface YearRow {
  year: number
  months: (number | null)[]
  yearTotal: number
}

function getAmountColor(amount: number, maxAmount: number): string {
  if (amount <= 0) return ''
  const intensity = maxAmount > 0 ? Math.min(amount / maxAmount, 1) : 0

  if (intensity < 0.2) return 'bg-teal-100 dark:bg-teal-950/40 text-teal-800 dark:text-teal-200'
  if (intensity < 0.4) return 'bg-teal-200 dark:bg-teal-900/60 text-teal-900 dark:text-teal-100'
  if (intensity < 0.6) return 'bg-teal-300 dark:bg-teal-800/70 text-teal-900 dark:text-teal-100'
  if (intensity < 0.8) return 'bg-teal-400 dark:bg-teal-700/80 text-white dark:text-teal-50'
  return 'bg-teal-500 dark:bg-teal-600 text-white'
}

function formatEur(amount: number): string {
  return amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export function DividendSummary() {
  const curSym = useCurrencySymbol()
  const [open, setOpen] = useState(false)

  const { data, isLoading, isError } = useQuery<DividendSummaryResponse>({
    queryKey: ['dividends', 'summary'],
    queryFn: () => api.getDividendSummary(),
    staleTime: 30 * 60 * 1000, // 30 min
    refetchInterval: (query) => {
      // Poll every 30s while sync is in progress
      return query.state.data?.sync_in_progress ? 30_000 : false
    },
  })

  const { yearRows, maxMonthlyAmount, maxYearTotal } = useMemo(() => {
    if (!data?.monthly?.length) return { yearRows: [], maxMonthlyAmount: 0, maxYearTotal: 0 }

    // Build year → month grid
    const yearMap = new Map<number, (number | null)[]>()
    let maxAmt = 0

    for (const item of data.monthly) {
      const year = parseInt(item.month.substring(0, 4))
      const monthIdx = parseInt(item.month.substring(5, 7)) - 1

      if (!yearMap.has(year)) {
        yearMap.set(year, new Array(12).fill(null))
      }
      yearMap.get(year)![monthIdx] = item.amount_eur
      if (item.amount_eur > maxAmt) maxAmt = item.amount_eur
    }

    const rows: YearRow[] = []
    for (const [year, months] of yearMap) {
      const yearTotal = months.reduce<number>((sum, v) => sum + (v ?? 0), 0)
      rows.push({ year, months, yearTotal })
    }

    rows.sort((a, b) => b.year - a.year)
    // Filter out years with no dividend income
    const filtered = rows.filter(r => r.yearTotal > 0)
    return {
      yearRows: filtered,
      maxMonthlyAmount: maxAmt,
      maxYearTotal: filtered.reduce((m, r) => Math.max(m, r.yearTotal), 0),
    }
  }, [data])

  // Summary text for collapsed state. These figures are NET of withholding and
  // era-spliced (estimates before ibkr_from, IBKR actuals from there on) — the
  // card used to call them gross Yahoo estimates, so the Performance tab silently
  // disagreed with the Dividends tab and DA-1 withholding read as pre-tax income.
  let summaryText: React.ReactNode = 'Net dividend income by month'
  if (isError) {
    summaryText = 'Could not load dividend income'
  } else if (data) {
    const parts: React.ReactNode[] = []
    if (data.ytd_eur > 0) {
      parts.push(
        <span key="ytd">
          YTD: <span className="text-teal-600 dark:text-teal-400">{curSym}{formatEur(data.ytd_eur)}</span>
        </span>
      )
    }
    if (data.total_eur > 0) {
      parts.push(
        <span key="total">
          {parts.length > 0 ? ' \u00B7 ' : ''}Total: <span className="text-teal-600 dark:text-teal-400">{curSym}{formatEur(data.total_eur)}</span>
        </span>
      )
    }
    if (data.sync_in_progress) {
      parts.push(
        <span key="sync" className="inline-flex items-center gap-1 text-muted-foreground">
          {parts.length > 0 ? ' \u00B7 ' : ''}<Loader2 className="h-3 w-3 animate-spin" /> Syncing...
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
        title="Dividend Income"
        description={summaryText}
        contentId="dividend-summary-content"
      />
      {open && (
        <CardContent id="dividend-summary-content">
          {isLoading ? (
            <div className="h-[200px] w-full animate-pulse rounded-md bg-muted" />
          ) : isError ? (
            // "No dividend data available yet" reads as a fact about the portfolio.
            // A failed request is a fact about the backend; say which one it is.
            <p className="text-muted-foreground text-center py-8">
              Couldn't load dividend income — the backend didn't respond. It retries automatically.
            </p>
          ) : yearRows.length === 0 ? (
            <div className="text-center py-8">
              <p className="text-muted-foreground">
                {data?.sync_in_progress
                  ? 'Syncing dividend data... This may take a few minutes.'
                  : 'No dividend data available yet.'}
              </p>
              {data?.sync_in_progress && <Loader2 className="h-5 w-5 animate-spin mx-auto mt-2 text-muted-foreground" />}
            </div>
          ) : (
            <>
              <ScrollableTable label="Dividend income by month">
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      <th className="text-left font-medium text-muted-foreground px-2 py-1.5">Year</th>
                      {MONTH_LABELS.map(m => (
                        <th key={m} className="text-center font-medium text-muted-foreground px-1 py-1.5 min-w-[40px] sm:min-w-[52px]">{m}</th>
                      ))}
                      <th className="text-center font-medium text-muted-foreground px-2 py-1.5 min-w-[52px] sm:min-w-[70px] border-l">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {yearRows.map(row => (
                      <tr key={row.year}>
                        <td className="font-medium px-2 py-1">{row.year}</td>
                        {row.months.map((amount, i) => (
                          <td key={i} className="px-0.5 py-0.5">
                            {amount !== null && amount > 0 ? (
                              <div
                                className={cn(
                                  'rounded px-1.5 py-1 text-center text-xs font-medium',
                                  getAmountColor(amount, maxMonthlyAmount)
                                )}
                                title={`${MONTH_LABELS[i]} ${row.year}: ${curSym}${formatEur(amount)}`}
                              >
                                {curSym}{amount < 10 ? formatEur(amount) : formatCount(Math.round(amount))}
                              </div>
                            ) : (
                              <div className="text-center text-xs text-muted-foreground py-1">{'\u2013'}</div>
                            )}
                          </td>
                        ))}
                        <td className="px-0.5 py-0.5 border-l">
                          {row.yearTotal > 0 ? (
                            // Scaled against the other year totals, not the monthly
                            // max — a year always exceeds its own biggest month, so
                            // this column was uniformly the darkest step and carried
                            // no information.
                            <div
                              className={cn(
                                'rounded px-1.5 py-1 text-center text-xs font-medium',
                                getAmountColor(row.yearTotal, maxYearTotal)
                              )}
                              title={`Total ${row.year}: ${curSym}${formatEur(row.yearTotal)}`}
                            >
                              {curSym}{formatEur(row.yearTotal)}
                            </div>
                          ) : (
                            <div className="text-center text-xs text-muted-foreground py-1">{'\u2013'}</div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollableTable>
            </>
          )}
        </CardContent>
      )}
    </Card>
  )
}
