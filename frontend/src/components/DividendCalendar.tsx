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

  return (
    <div className="space-y-2">
      <div className="text-sm font-medium">Expected next</div>

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
                    {/* Wraps rather than truncates as a whole: at 390px the notes
                        and the badge did not fit beside the symbol, and one `truncate`
                        over all of them clipped "payment pending" — the one live
                        warning on the row — first. Only the symbol truncates now; the
                        badge sits right after it and the notes wrap under. */}
                    <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5 gap-y-0.5">
                      <span className="min-w-0 max-w-full truncate">{p.symbol}</span>
                      {p.pending && (
                        <span className="shrink-0 whitespace-nowrap rounded-full bg-amber-100 px-1.5 py-0.5 text-[0.7rem] font-medium text-amber-800 dark:bg-amber-950/60 dark:text-amber-200">
                          payment pending
                        </span>
                      )}
                      {p.ex_date && p.ex_date !== p.date && (
                        <span className="whitespace-nowrap text-xs text-muted-foreground">
                          ex {dayLabel(p.ex_date)}
                        </span>
                      )}
                      {/* The date on this row IS the ex-date: nothing gave a pay date
                          (no accrual, never paid through IBKR, so no lag to measure).
                          Said on the surface, or it reads as the day the cash lands. */}
                      {p.pay_date_source === 'ex_date' && (
                        <span
                          className="whitespace-nowrap text-xs text-muted-foreground"
                          title="Pay date not yet known"
                        >
                          ex-date · pay date unknown
                        </span>
                      )}
                    </span>
                    <Amount value={p.net_eur} />
                  </li>
                ))}
              </ul>
            </div>
          )
        })}
      </div>

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

function Amount({ value }: { value: number }) {
  const formatCurrency = useFormatCurrency()
  return <span className="shrink-0 tabular-nums">{formatCurrency(value)}</span>
}
