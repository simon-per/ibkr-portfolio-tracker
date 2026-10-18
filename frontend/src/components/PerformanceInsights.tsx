import type { ReactNode } from 'react'
import { ChartPie, HandCoins, TrendingDown, TrendingUp, type LucideIcon } from 'lucide-react'
import { KpiPanel } from '@/components/ui/KpiCard'
import { IconChip } from '@/components/ui/IconChip'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import type {
  DividendUpcomingPayment, PerformanceAttributionResponse, Position,
} from '@/lib/api'
import type { AccountCash } from '@/lib/portfolioCash'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import type { TimeRange } from '@/lib/dateRanges'
import { toLocalISODate } from '@/lib/dateRanges'
import { largestHolding, nextDividend, topMover } from '@/lib/performanceInsights'
import { displayName } from '@/lib/securityName'
import { TONE_TEXT, type Tone } from '@/lib/tone'
import { ABSENT, cn, formatSigned, parseLocalDate } from '@/lib/utils'

interface PerformanceInsightsProps {
  /** The selected range's attribution; `undefined` while loading or after a failure. */
  attribution: PerformanceAttributionResponse | undefined
  positions: Position[] | undefined
  /**
   * What `PositionsList` takes, so the largest holding's weight matches its row. `undefined`
   * is a summary not loaded, and the tile shows a dash (`AccountCash`).
   */
  cash: AccountCash | undefined
  /** `dividendBreakdown.upcoming`; `undefined` while loading or from an older backend. */
  upcoming: DividendUpcomingPayment[] | undefined
  range: TimeRange
  /** `YYYY-MM-DD`; the viewer's local today by default. A prop so a test can pin it. */
  today?: string
}

const shortDay = (iso: string) =>
  new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' }).format(parseLocalDate(iso))

/**
 * Four compact figures under the hero: the range's best and worst contributor, the largest
 * holding and the next dividend. Figures and names only — every number is read from a
 * response the tab already holds (`lib/performanceInsights.ts`).
 *
 * A dash is "not loaded"; "None" is "loaded, and there is no such row".
 */
export function PerformanceInsights({
  attribution, positions, cash, upcoming, range, today = toLocalISODate(new Date()),
}: PerformanceInsightsProps) {
  const money = useFormatCurrency()
  // An empty book has nothing to single out; the hero already says so.
  if (positions && positions.length === 0) return null

  const best = topMover(attribution?.attributions, 'best')
  const worst = topMover(attribution?.attributions, 'worst')
  const largest = largestHolding(positions, cash)
  const dividend = nextDividend(upcoming, today)
  const signed = (n: number) => formatSigned(n, money)

  return (
    <KpiPanel columns={4}>
      <InsightTile
        icon={TrendingUp}
        tone="positive"
        label="Top mover"
        tag={range}
        value={best && signed(best.pnl)}
        absent={best}
        who={best && { symbol: best.symbol, securityId: best.securityId, detail: displayName(best.description) }}
      />
      <InsightTile
        icon={TrendingDown}
        tone="negative"
        label="Biggest drag"
        tag={range}
        value={worst && signed(worst.pnl)}
        absent={worst}
        who={worst && { symbol: worst.symbol, securityId: worst.securityId, detail: displayName(worst.description) }}
      />
      <InsightTile
        icon={ChartPie}
        tone="neutral"
        label="Largest holding"
        value={largest && `${largest.weightPct.toFixed(1)}%`}
        absent={largest}
        who={largest && { symbol: largest.symbol, securityId: largest.securityId, detail: money(largest.marketValue) }}
      />
      <InsightTile
        icon={HandCoins}
        tone="neutral"
        label="Next dividend"
        value={dividend && (
          <>
            {money(dividend.amount)}
            {dividend.estimated && (
              <span className="ml-1.5 align-middle text-xs font-medium text-muted-foreground">est.</span>
            )}
          </>
        )}
        absent={dividend}
        who={dividend && { symbol: dividend.symbol, securityId: dividend.securityId, detail: shortDay(dividend.date) }}
      />
    </KpiPanel>
  )
}

interface InsightTileProps {
  icon: LucideIcon
  /** The well's tint, and the value's ink when there is a value. */
  tone: Tone
  label: string
  /** A short qualifier at the label row's end (the range). */
  tag?: string
  value: ReactNode
  /** The pick itself: `undefined` renders a dash, `null` renders "None". */
  absent: unknown
  who: { symbol: string; securityId: number; detail: string } | null | undefined
}

function InsightTile({ icon, tone, label, tag, value, absent, who }: InsightTileProps) {
  const known = absent !== undefined
  const present = absent != null
  return (
    <div className="flex min-w-0 flex-col gap-2 p-4 sm:p-5">
      <div className="flex min-w-0 items-center gap-2">
        <IconChip icon={icon} tone={present ? tone : 'muted'} />
        <span className="min-w-0 truncate text-xs font-medium text-muted-foreground">{label}</span>
        {tag && (
          <span className="ml-auto shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
            {tag}
          </span>
        )}
      </div>
      <div
        className={cn(
          'text-lg font-semibold leading-tight tracking-tight sm:text-[1.375rem]',
          present ? TONE_TEXT[tone] : 'text-muted-foreground',
        )}
      >
        {present ? value : known ? 'None' : ABSENT}
      </div>
      {who && (
        <div className="flex min-w-0 items-center gap-2 text-xs">
          <TickerAvatar
            symbol={who.symbol}
            securityId={who.securityId}
            className="h-5 w-5 text-[8px]"
          />
          <span className="shrink-0 font-semibold">{who.symbol}</span>
          <span className="min-w-0 truncate text-muted-foreground">{who.detail}</span>
        </div>
      )}
    </div>
  )
}
