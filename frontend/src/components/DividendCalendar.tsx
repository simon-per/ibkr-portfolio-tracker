import { useMemo } from 'react'
import { CalendarClock } from 'lucide-react'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import type { DividendUpcomingPayment } from '@/lib/api'
import { formatDelta } from '@/lib/delta'
import { payoutVerdict } from '@/lib/dividendGrowth'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import { DividendSubheading } from './DividendSubheading'

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
 *
 * Each row leads with the date as a chip and the holding's logo; the small swatch beside
 * the logo is the holding's chart colour, so a row can still be matched to its segment.
 *
 * An announced payment that raises the dividend carries "+x%": its per share against the
 * last regular payment (`per_share_change_pct`), a raise by `payoutVerdict` — the band the
 * "raised · cut" count uses, so the two cannot disagree about what a raise is.
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
    <section aria-label="Expected dividends" className="space-y-3">
      <DividendSubheading title="Expected next" icon={CalendarClock} />

      {/* Bounded height: 12 months of a 20-payer portfolio is ~60 rows, which
          would otherwise push the position table off the page.
          `-mx-1.5 px-1.5` gives the rows' `-mx-1.5` hover wash room INSIDE the
          scroller: with only `pr-1` the rows overflowed it by 2px, and because
          `overflow-y-auto` forces `overflow-x` to auto that bought a sideways
          scrollbar for nothing. The scrollbar is thin and themed; the native one
          rendered as a white bar in dark mode. */}
      <div className="-mx-1.5 max-h-72 space-y-3 overflow-y-auto px-1.5 [scrollbar-color:hsl(var(--border-strong))_transparent] [scrollbar-width:thin]">
        {groups.map(([month, rows]) => {
          const monthTotal = rows.reduce((s, r) => s + r.net_eur, 0)
          return (
            <div key={month}>
              {/* Sticky inside the scroller, so a long month keeps its heading and total.
                  Bleeds over the scroller's padding so a row scrolling under it is covered edge to edge. */}
              <div className="sticky top-0 z-[1] -mx-1.5 mb-1 flex items-baseline justify-between border-b border-border/60 bg-card px-1.5 pb-1">
                <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {monthHeading(month)}
                </span>
                <MonthTotal total={monthTotal} />
              </div>
              <ul className="space-y-0.5">
                {rows.map(p => (
                  <li
                    key={`${p.date}-${p.security_id}`}
                    className="-mx-1.5 flex items-center gap-2.5 rounded-lg px-1.5 py-1 text-sm transition-colors hover:bg-muted/40"
                  >
                    <span className="min-w-[3.5rem] shrink-0 whitespace-nowrap rounded-md bg-muted px-1.5 py-0.5 text-center text-xs font-medium tabular-nums text-muted-foreground">
                      {dayLabel(p.date)}
                    </span>
                    <span className="relative shrink-0">
                      <TickerAvatar symbol={p.symbol} securityId={p.security_id} className="h-7 w-7 text-[10px]" />
                      <span
                        className="absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full ring-2 ring-card"
                        style={{ backgroundColor: colorOf(p.symbol) }}
                        aria-hidden="true"
                      />
                    </span>
                    {/* Wraps rather than truncates as a whole: at 390px the notes
                        and the badge did not fit beside the symbol, and one `truncate`
                        over all of them clipped "payment pending" — the one live
                        warning on the row — first. Only the symbol truncates now; the
                        badge sits right after it and the notes wrap under. */}
                    <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5 gap-y-0.5">
                      <span className="min-w-0 max-w-full truncate font-medium">{p.symbol}</span>
                      {payoutVerdict(p.per_share_change_pct) === 'raised' && (
                        <span
                          className="shrink-0 whitespace-nowrap rounded-full bg-gain-soft px-1.5 py-0.5 text-[0.7rem] font-medium tabular-nums text-gain"
                          title="Per share vs last payment"
                        >
                          {formatDelta(p.per_share_change_pct)}
                        </span>
                      )}
                      {p.pending && (
                        <span className="shrink-0 whitespace-nowrap rounded-full bg-warn-soft px-1.5 py-0.5 text-[0.7rem] font-medium text-warn">
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
    </section>
  )
}

function MonthTotal({ total }: { total: number }) {
  const formatCurrency = useFormatCurrency()
  return (
    <span className="text-xs font-medium tabular-nums text-muted-foreground">
      {formatCurrency(total)}
    </span>
  )
}

function Amount({ value }: { value: number }) {
  const formatCurrency = useFormatCurrency()
  return <span className="shrink-0 font-medium tabular-nums">{formatCurrency(value)}</span>
}
