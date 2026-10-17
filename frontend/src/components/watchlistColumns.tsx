import { displayName } from '@/lib/securityName'
import { ABSENT, formatMarketCap } from '@/lib/utils'
import type { WatchlistItem } from '@/lib/api'
import type { Column } from '@/components/ui/DataTable'

/**
 * The watchlist's eighteen columns, described once for both renderings.
 *
 * This is the table the mobile work exists for: its intrinsic width is ~1,350px, so at
 * 390px it was roughly four screens of sideways scrolling — and because ScrollableTable
 * applies no minimum width, what actually happened was that every column got squeezed
 * to its min-content and the whole thing became unreadable in place.
 *
 * A separate module because the array is long enough to bury the component, and because
 * a factory here is what lets a test import the exact columns the component renders
 * rather than a copy of them.
 *
 * Note the `hint`s. These definitions of PEG, RSI, EV/EBITDA and TTM growth are the
 * only place those terms are explained anywhere in the app, and they used to live in a
 * hover-only tooltip — no touch device could reach them. DataTable feeds them to both
 * the desktop `<th>`'s `title=` and a tap-reachable glossary, from this one source.
 */

export type WatchlistSortColumn =
  | 'symbol'
  | 'buy_score'
  | 'current_price'
  | 'pct_from_52w_high'
  | 'rsi14'
  | 'pct_from_ma200'
  | 'trailing_pe'
  | 'forward_pe'
  | 'peg_ratio'
  | 'ev_to_ebitda'
  | 'analyst_rating'
  | 'revenue_growth'
  | 'earnings_growth'
  | 'fwd_revenue_growth'
  | 'fwd_eps_growth'
  | 'profit_margins'

export const RATING_LABELS: Record<string, string> = {
  strong_buy: 'Strong Buy',
  buy: 'Buy',
  hold: 'Hold',
  sell: 'Sell',
  strong_sell: 'Strong Sell',
}

export function formatPercent(value: number | null): string {
  if (value === null) return ABSENT
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`
}

// Re-exported so existing importers of this module keep working; the definition
// lives in lib/utils.ts, because it was byte-identical here and in FundamentalsTab.
export { formatMarketCap }

export function colorClass(value: number | null, invertGood?: boolean): string {
  if (value === null) return 'text-muted-foreground'
  const isGood = invertGood ? value > 0 : value < 0
  if (Math.abs(value) < 5) return 'text-foreground'
  return isGood ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'
}

export function rsiColorClass(rsi: number | null): string {
  if (rsi === null) return 'text-muted-foreground'
  if (rsi < 35) return 'text-green-600 dark:text-green-400'
  if (rsi > 65) return 'text-red-600 dark:text-red-400'
  return 'text-foreground'
}

export function scoreColorClass(score: number | null): string {
  if (score === null) return 'text-muted-foreground'
  if (score >= 70) return 'text-green-600 dark:text-green-400'
  if (score >= 40) return 'text-yellow-600 dark:text-yellow-400'
  return 'text-red-600 dark:text-red-400'
}

export function scoreBgClass(score: number | null): string {
  if (score === null) return 'bg-muted'
  if (score >= 70) return 'bg-green-100 dark:bg-green-900/40'
  if (score >= 40) return 'bg-yellow-100 dark:bg-yellow-900/40'
  return 'bg-red-100 dark:bg-red-900/40'
}

export function ratingColorClass(rating: string | null): string {
  if (!rating) return 'text-muted-foreground'
  if (rating === 'strong_buy' || rating === 'buy') return 'text-green-600 dark:text-green-400'
  if (rating === 'hold') return 'text-yellow-600 dark:text-yellow-400'
  return 'text-red-600 dark:text-red-400'
}

/** Upside to the analyst mean target, or null when either side is missing. */
export function analystUpside(item: WatchlistItem): number | null {
  return item.analyst_target && item.current_price && item.current_price > 0
    ? ((item.analyst_target - item.current_price) / item.current_price) * 100
    : null
}

/** Stored PEG, falling back to P/E over forward EPS growth. */
export function pegOf(item: WatchlistItem): number | null {
  return (
    item.peg_ratio ??
    (item.trailing_pe && item.fwd_eps_growth && item.fwd_eps_growth > 0
      ? item.trailing_pe / (item.fwd_eps_growth * 100)
      : null)
  )
}

const pct = (v: number | null) => (v != null ? `${(v * 100).toFixed(1)}%` : ABSENT)
const num = (v: number | null, dp = 1) => (v != null ? v.toFixed(dp) : ABSENT)

export function watchlistColumns(deps: {
  formatCurrency: (value: number | null, currency: string | null) => string
}): Column<WatchlistItem, WatchlistSortColumn>[] {
  const { formatCurrency } = deps

  return [
    {
      key: 'symbol',
      header: 'Stock',
      shortHeader: 'Symbol',
      sortKey: 'symbol',
      mobile: 'title',
      headerClassName: 'min-w-[160px]',
      cell: (i, view) =>
        view === 'cards' ? (
          i.symbol || i.yahoo_ticker
        ) : (
          <div>
            <div className="font-medium">{i.symbol || i.yahoo_ticker}</div>
            <div className="text-xs text-muted-foreground truncate max-w-[140px]">
              {displayName(i.company_name) || i.yahoo_ticker}
            </div>
            {(i.notes || i.target_price !== null) && (
              <div className="text-[10px] text-muted-foreground/70 mt-0.5 space-x-2">
                {i.notes && (
                  <span className="truncate inline-block max-w-[100px] align-bottom">{i.notes}</span>
                )}
                {i.target_price !== null && (
                  <span>TP: {formatCurrency(i.target_price, i.data_currency)}</span>
                )}
              </div>
            )}
          </div>
        ),
    },
    // The desktop stacks these three under the symbol; on a card they are their own
    // lines, which is why they are `desktop: 'hide'` rather than duplicated markup.
    {
      key: 'company',
      header: 'Company',
      shortHeader: 'Company',
      desktop: 'hide',
      mobile: 'meta',
      cell: (i) => displayName(i.company_name) || i.yahoo_ticker,
    },
    {
      key: 'note',
      header: 'Note',
      shortHeader: 'Note',
      desktop: 'hide',
      mobile: 'meta',
      cell: (i) =>
        i.notes || i.target_price !== null ? (
          <span className="space-x-2">
            {i.notes && <span>{i.notes}</span>}
            {i.target_price !== null && (
              <span>TP: {formatCurrency(i.target_price, i.data_currency)}</span>
            )}
          </span>
        ) : null,
    },
    {
      key: 'buy_score',
      header: 'Score',
      shortHeader: 'Score',
      sortKey: 'buy_score',
      mobile: 'badge',
      hint: {
        description: 'Composite buy score',
      },
      cell: (i) =>
        i.buy_score !== null ? (
          <span
            className={`inline-flex items-center justify-center w-9 h-6 rounded text-xs font-bold ${scoreColorClass(i.buy_score)} ${scoreBgClass(i.buy_score)}`}
          >
            {Math.round(i.buy_score)}
          </span>
        ) : (
          <span className="text-muted-foreground">-</span>
        ),
    },
    {
      key: 'current_price',
      header: 'Price',
      shortHeader: 'Price',
      sortKey: 'current_price',
      mobile: 'value',
      cellClassName: 'font-medium',
      cell: (i) => formatCurrency(i.current_price, i.data_currency),
    },
    {
      key: 'pct_from_52w_high',
      header: '% 52w High',
      shortHeader: '% from 52w high',
      sortKey: 'pct_from_52w_high',
      mobile: 'delta',
      tone: (i) => colorClass(i.pct_from_52w_high),
      cellClassName: 'font-medium',
      hint: {
        description: 'Distance below 52-week high',
      },
      cell: (i) => formatPercent(i.pct_from_52w_high),
    },
    {
      key: 'analyst_rating',
      header: 'Analyst',
      shortHeader: 'Analyst rating',
      sortKey: 'analyst_rating',
      mobile: 'badge',
      hint: {
        description: 'Analyst rating and target upside',
      },
      cell: (i) => {
        const upside = analystUpside(i)
        return (
          // `whitespace-nowrap`: "Strong Buy" over "+35% upside" broke into three and four
          // lines in a column that only needs the width of its longest phrase.
          <div className="whitespace-nowrap">
            <div className={`text-xs font-medium ${ratingColorClass(i.analyst_rating)}`}>
              {i.analyst_rating ? RATING_LABELS[i.analyst_rating] || i.analyst_rating : ABSENT}
            </div>
            {upside !== null && (
              <div
                className={`text-[11px] ${upside > 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}
              >
                {upside >= 0 ? '+' : ''}
                {upside.toFixed(0)}% upside
              </div>
            )}
          </div>
        )
      },
    },
    // The first four details are the ones a watchlist is scanned for; the eight below
    // sit behind the disclosure. Order here is the order they appear on the card.
    {
      key: 'rsi14',
      header: 'RSI',
      shortHeader: 'RSI',
      sortKey: 'rsi14',
      tone: (i) => rsiColorClass(i.rsi14),
      cellClassName: 'font-medium',
      hint: {
        description: 'RSI, 14-day',
      },
      cell: (i) => num(i.rsi14),
    },
    {
      key: 'pct_from_ma200',
      header: 'vs MA200',
      shortHeader: 'vs MA200',
      sortKey: 'pct_from_ma200',
      tone: (i) => colorClass(i.pct_from_ma200),
      cellClassName: 'font-medium',
      hint: {
        description: 'Distance from 200-day MA',
      },
      cell: (i) => formatPercent(i.pct_from_ma200),
    },
    {
      key: 'peg_ratio',
      header: 'PEG',
      shortHeader: 'PEG',
      sortKey: 'peg_ratio',
      hint: {
        description: 'P/E over EPS growth',
      },
      cell: (i) => {
        const peg = pegOf(i)
        return peg !== null ? (
          <span
            className={
              peg <= 1
                ? 'text-green-600 dark:text-green-400'
                : peg > 2
                  ? 'text-red-600 dark:text-red-400'
                  : ''
            }
          >
            {peg.toFixed(2)}
          </span>
        ) : (
          ABSENT
        )
      },
    },
    {
      key: 'forward_pe',
      header: 'Fwd P/E',
      shortHeader: 'Fwd P/E',
      sortKey: 'forward_pe',
      hint: {
        description: 'Forward P/E',
      },
      cell: (i) => num(i.forward_pe),
    },
    {
      key: 'trailing_pe',
      header: 'P/E',
      shortHeader: 'P/E',
      sortKey: 'trailing_pe',
      hint: {
        description: 'Trailing P/E',
      },
      cell: (i) => num(i.trailing_pe),
    },
    {
      key: 'ev_to_ebitda',
      header: 'EV/EBITDA',
      shortHeader: 'EV/EBITDA',
      sortKey: 'ev_to_ebitda',
      hint: {
        description: 'EV / EBITDA',
      },
      cell: (i) => num(i.ev_to_ebitda),
    },
    {
      key: 'revenue_growth',
      header: 'Rev Grw',
      shortHeader: 'Revenue growth',
      sortKey: 'revenue_growth',
      tone: (i) => colorClass(i.revenue_growth != null ? i.revenue_growth * 100 : null, true),
      hint: {
        description: 'TTM revenue growth',
      },
      cell: (i) => pct(i.revenue_growth),
    },
    {
      key: 'earnings_growth',
      header: 'EPS Grw',
      shortHeader: 'EPS growth',
      sortKey: 'earnings_growth',
      tone: (i) => colorClass(i.earnings_growth != null ? i.earnings_growth * 100 : null, true),
      hint: {
        description: 'TTM EPS growth',
      },
      cell: (i) => pct(i.earnings_growth),
    },
    {
      key: 'fwd_revenue_growth',
      header: 'Fwd Rev',
      shortHeader: 'Fwd revenue growth',
      sortKey: 'fwd_revenue_growth',
      tone: (i) =>
        colorClass(i.fwd_revenue_growth != null ? i.fwd_revenue_growth * 100 : null, true),
      hint: {
        description: 'Next-12M revenue growth est.',
      },
      cell: (i) => pct(i.fwd_revenue_growth),
    },
    {
      key: 'fwd_eps_growth',
      header: 'Fwd EPS',
      shortHeader: 'Fwd EPS growth',
      sortKey: 'fwd_eps_growth',
      tone: (i) => colorClass(i.fwd_eps_growth != null ? i.fwd_eps_growth * 100 : null, true),
      hint: {
        description: 'Next-12M EPS growth est.',
      },
      cell: (i) => pct(i.fwd_eps_growth),
    },
    {
      key: 'profit_margins',
      header: 'Margin',
      shortHeader: 'Margin',
      sortKey: 'profit_margins',
      hint: {
        description: 'TTM net margin',
      },
      cell: (i) => pct(i.profit_margins),
    },
    {
      key: 'market_cap',
      header: 'Mkt Cap',
      shortHeader: 'Market cap',
      // Not sortable, as before — and its definition used to be `onMouseEnter`-only on
      // a non-focusable <th>, so it was unreachable by keyboard as well as by touch.
      hint: {
        description: 'Market cap, native currency',
      },
      cellClassName: 'text-muted-foreground',
      cell: (i) => formatMarketCap(i.market_cap),
    },
  ]
}
