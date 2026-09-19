import { useMemo } from 'react'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import type { DividendUpcomingPayment } from '@/lib/api'

interface DividendCalendarProps {
  upcoming: DividendUpcomingPayment[]
  /** Colour per stack symbol, shared with the chart so the two agree. */
  colorOf: (symbol: string) => string
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function dayLabel(iso: string): string {
  const [, m, d] = iso.split('-')
  return `${Number(d)} ${MONTH_NAMES[Number(m) - 1] ?? m}`
}

function monthHeading(iso: string): string {
  const [y, m] = iso.split('-')
  return `${MONTH_NAMES[Number(m) - 1] ?? m} ${y}`
}

/**
 * The next expected payments, dated and grouped by month.
 *
 * The projection has always produced dated payments; the chart aggregated them
 * into month buckets and discarded the days, so this costs nothing new.
 *
 * **Every date here is when the CASH is expected.** It used to be the ex-date, because
 * the cadence is inferred from yfinance's ex-date series and nothing shifted it — so a
 * row vanished on the day the dividend went ex and did not come back until IBKR posted
 * the money, up to a month later. `ex_date` rides beside each row so the shift can be
 * checked rather than taken on trust, and `pay_date_source` says what produced it.
 *
 * A `pending` row is the one this exists for: gone ex, cash overdue, in none of the
 * totals. It is badged **on the surface** rather than in a `title=`, which a touch
 * device cannot reach — the lesson this tab already learned once.
 */
export function DividendCalendar({ upcoming, colorOf }: DividendCalendarProps) {
  const groups = useMemo(() => {
    const byMonth = new Map<string, DividendUpcomingPayment[]>()
    for (const p of upcoming) {
      const key = p.date.slice(0, 7)
      const list = byMonth.get(key)
      if (list) list.push(p)
      else byMonth.set(key, [p])
    }
    return [...byMonth.entries()]
  }, [upcoming])

  if (upcoming.length === 0) return null

  const anyEstimate = upcoming.some(p => p.basis === 'gross_estimate')
  const anyPending = upcoming.some(p => p.pending)
  // An announced pay date is IBKR's own; an inferred one is not. Saying "not an
  // announced schedule" over rows that ARE announced would understate them, and saying
  // nothing over rows that are not would overstate them — so the caption follows the
  // weakest provenance actually on screen.
  const allAnnounced = upcoming.every(p => p.pay_date_source === 'accrual')

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-sm font-medium">Expected next</span>
        <span className="text-xs text-muted-foreground">
          {allAnnounced
            ? 'pay dates as announced by IBKR'
            : "dated when the cash is expected — projected from each holding's payout cadence"}
        </span>
      </div>

      {/* Bounded height: 12 months of a 20-payer portfolio is ~60 rows, which
          would otherwise push the position table off the page. */}
      <div className="max-h-64 space-y-3 overflow-y-auto pr-1">
        {groups.map(([month, rows]) => {
          const monthTotal = rows.reduce((s, r) => s + r.net_eur, 0)
          return (
            <div key={month}>
              <div className="mb-1 flex items-baseline justify-between border-b border-border/60 pb-0.5">
                <span className="text-xs font-medium text-muted-foreground">
                  {monthHeading(month)}
                </span>
                <MonthTotal total={monthTotal} />
              </div>
              <ul className="space-y-0.5">
                {rows.map(p => (
                  <li
                    key={`${p.date}-${p.security_id}`}
                    className="flex items-center gap-2 text-sm"
                  >
                    <span className="w-14 shrink-0 tabular-nums text-xs text-muted-foreground">
                      {dayLabel(p.date)}
                    </span>
                    <span
                      className="h-2 w-2 shrink-0 rounded-sm"
                      style={{ backgroundColor: colorOf(p.symbol) }}
                      aria-hidden="true"
                    />
                    <span className="min-w-0 flex-1 truncate">
                      {p.symbol}
                      {p.ex_date && p.ex_date !== p.date && (
                        <span className="ml-1.5 whitespace-nowrap text-xs text-muted-foreground">
                          ex {dayLabel(p.ex_date)}
                        </span>
                      )}
                      {p.pending && (
                        <span className="ml-1.5 whitespace-nowrap rounded-full bg-amber-100 px-1.5 py-0.5 text-[0.7rem] font-medium text-amber-800 dark:bg-amber-950/60 dark:text-amber-200">
                          payment pending
                        </span>
                      )}
                    </span>
                    <Amount value={p.net_eur} estimate={p.basis === 'gross_estimate'} />
                  </li>
                ))}
              </ul>
            </div>
          )
        })}
      </div>

      {anyPending && (
        <div className="text-xs text-muted-foreground">
          <span className="font-medium text-amber-700 dark:text-amber-300">
            payment pending
          </span>{' '}
          — the dividend has gone ex and the cash has not reached the account yet, so it
          is not counted in any total on this tab until it does
        </div>
      )}

      {anyEstimate && (
        <div className="text-xs text-muted-foreground">
          <span className="text-amber-600 dark:text-amber-500">*</span>{' '}
          estimated net from published gross dividends using an assumed withholding
          deduction; actual net payments may differ
        </div>
      )}
    </div>
  )
}

function MonthTotal({ total }: { total: number }) {
  const formatCurrency = useFormatCurrency()
  return (
    <span className="text-xs tabular-nums text-muted-foreground">
      {formatCurrency(total)}
    </span>
  )
}

function Amount({ value, estimate }: { value: number; estimate: boolean }) {
  const formatCurrency = useFormatCurrency()
  return (
    <span className="shrink-0 tabular-nums">
      {formatCurrency(value)}
      {estimate && (
        <span
          className="ml-0.5 text-amber-600 dark:text-amber-500"
          title="Estimated net from gross — assumed withholding"
        >
          *
        </span>
      )}
    </span>
  )
}
