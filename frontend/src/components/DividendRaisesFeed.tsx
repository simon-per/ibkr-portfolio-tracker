import type { DividendRaise } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { RAISE_BAND_PCT } from '@/lib/dividendGrowth'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import { formatPrice, formatQuantity, parseLocalDate } from '@/lib/utils'
import { DeltaChip } from './DeltaChip'

function shortDay(iso: string): string {
  const date = parseLocalDate(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

/**
 * Each company's dividend raises and cuts, newest first: "MSFT $3.32 → $3.64 ▲ +9.6%
 * +€12.40/yr · Sep 16".
 *
 * The rows are the server's `raises[]` already filtered to moves beyond `RAISE_BAND_PCT`
 * (`feedRaises`). The per-share figures are a year's worth at the payer's size, gross,
 * in its own currency; the amount is the annual income the move added on the shares held,
 * in the base currency — the company's share of the forecast card's "from raises".
 * Figures only, like the calendar beside it; a row wraps rather than truncates its
 * figures on a phone.
 */
export function DividendRaisesFeed({ raises, id }: { raises: DividendRaise[]; id?: string }) {
  const formatCurrency = useFormatCurrency()
  if (raises.length === 0) return null

  const signed = (value: number) =>
    `${value < 0 ? '−' : '+'}${formatCurrency(Math.abs(value))}`

  return (
    <ul
      id={id}
      aria-label="Dividend raises and cuts"
      className="-mx-1.5 max-h-72 space-y-0.5 overflow-y-auto px-1.5 [scrollbar-color:hsl(var(--border-strong))_transparent] [scrollbar-width:thin]"
    >
      {raises.map((r) => (
        <li
          key={`${r.date}-${r.security_id}`}
          className="-mx-1.5 flex items-center gap-2.5 rounded-lg px-1.5 py-1 text-sm transition-colors hover:bg-muted/40"
        >
          <span className="min-w-[3.5rem] shrink-0 whitespace-nowrap rounded-md bg-muted px-1.5 py-0.5 text-center text-xs font-medium tabular-nums text-muted-foreground">
            {shortDay(r.date)}
          </span>
          <TickerAvatar symbol={r.symbol} securityId={r.security_id} className="h-7 w-7 shrink-0 text-[10px]" />
          <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5 gap-y-0.5">
            <span className="min-w-0 max-w-full truncate font-medium">{r.symbol}</span>
            <span className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">
              {r.currency
                ? `${formatPrice(r.level_from, r.currency)} → ${formatPrice(r.level_to, r.currency)}`
                : `${formatQuantity(r.level_from)} → ${formatQuantity(r.level_to)}`}
            </span>
          </span>
          <span className="flex shrink-0 flex-col items-end gap-0.5">
            <DeltaChip pct={r.change_pct} flatBand={RAISE_BAND_PCT} />
            <span className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">
              {signed(r.annual_change_eur)}/yr
            </span>
          </span>
        </li>
      ))}
    </ul>
  )
}
