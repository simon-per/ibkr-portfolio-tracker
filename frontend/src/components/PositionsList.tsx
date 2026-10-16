import { useState, useMemo } from 'react'
import type { Position } from '@/lib/api'
import { formatPercent, formatQuantity } from '@/lib/utils'
import { isUnpriced } from '@/lib/positionValuation'
import { getRatingScore } from '@/lib/analystRating'
import { cashIsTracked } from '@/lib/portfolioCash'
import type { CashSource } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import { displayName } from '@/lib/securityName'

interface PositionsListProps {
  positions: Position[]
  isLoading?: boolean
  isError?: boolean
  /**
   * Projected yield on cost per `security_id`, from the dividends breakdown the
   * Dashboard already fetches for the KPI cards. Optional so the table still renders
   * before that query resolves — every row simply shows a dash until it does.
   */
  yieldOnCost?: Map<number, number | null>
  /**
   * The account's uninvested cash and where it came from, from `/api/portfolio/summary`.
   *
   * It belongs here because **weight is a share of the account, not of its holdings**.
   * With 12,229 CHF of a 68,921 CHF account sitting in cash after a rotation, dividing
   * by holdings alone inflated every row by 21% and the column still called itself
   * "% of portfolio" — the same mislabelling the allocation charts carried when they
   * dropped an unvaluable holding and still summed to 100.
   *
   * Optional, and absent means a backend that does not track cash: the denominator then
   * falls back to holdings, which is what it always was.
   */
  cash?: { amount: number; cash_source?: CashSource }
}

type SortColumn = 'symbol' | 'description' | 'rating' | 'quantity' | 'cost_basis_eur' | 'market_value_eur' | 'gain_loss_eur' | 'gain_loss_percent' | 'portfolio_percent' | 'yield_on_cost'
type SortDirection = 'asc' | 'desc'

/** A soft chip per consensus: the rating is context for a holding, not an alarm. */
const getRatingBadgeColor = (consensus: string): string => {
  switch (consensus.toLowerCase()) {
    case 'strong buy':
    case 'buy':
      return 'bg-green-500/10 text-green-700 dark:text-green-400'
    case 'hold':
      return 'bg-yellow-500/10 text-yellow-700 dark:text-yellow-400'
    case 'sell':
    case 'strong sell':
      return 'bg-red-500/10 text-red-700 dark:text-red-400'
    default:
      return 'bg-muted text-muted-foreground'
  }
}

// The rating sort score lives in `lib/analystRating.ts`, shared with the watchlist — the
// two tables ranked the same ratings by different rules until 2026-09-12.

const exchangeOf = (p: Position) => p.exchange || '—'

/**
 * A short label for the account a row belongs to, or null for the brokerage one.
 *
 * Null rather than 'IBKR' deliberately: badging every row of a single-account book
 * is noise that teaches the reader to skip the badge, which is exactly what it must
 * not do the day a second account appears. Absent `account` reads as IBKR, the same
 * backward-compatible default the field itself documents.
 */
const accountBadge = (p: Position): string | null =>
  p.account && p.account.startsWith('pillar3a') ? '3a' : null

/**
 * A holding the backend could not value, on the shared two-clause predicate.
 *
 * The table used to render these as an ordinary, very bad position. `market_value_eur`
 * is 0.00 for an unpriced holding, so `gain_loss_eur` is `-cost` and
 * `gain_loss_percent` is exactly `-100.00` — printed in red, weighted at 0.00%, with no
 * marker of any kind. Three rows of KPI cards above, Win Rate already says "N unpriced,
 * not judged": the app named the condition there and published the fabricated loss
 * here, on the one screen a reader opens to find out *which* holding.
 *
 * A missing FX rate is the second route into this state and does not clear the price,
 * which is why `isUnpriced` is imported rather than `market_price === null` tested here
 * — see `lib/positionValuation.ts`.
 */
const cannotValue = (p: Position) => isUnpriced(p)

/** What every figure derived from a valuation shows when there is no valuation. */
const NO_VALUE = '—'

const gainTone = (p: Position) =>
  cannotValue(p) ? 'text-muted-foreground' : p.gain_loss_eur >= 0 ? 'text-green-600' : 'text-red-600'

/**
 * The positions table, described once for both renderings.
 *
 * A factory rather than a module constant because every cell closes over the currency
 * formatter and the portfolio total — which is also what lets a test import the exact
 * array the component renders instead of a copy of it.
 *
 * Note where the two views legitimately differ. The desktop table seats a monogram, the
 * name and the ticker in one cell and puts the percentage under the gain; the card puts
 * the name on its title line and the rest in the detail grid, so those get
 * `desktop: 'hide'` companions. The *values* come from one place either way.
 */
export function positionColumns(deps: {
  formatCurrency: (value: number) => string
  /**
   * The denominator for `weight`: holdings **plus** uninvested cash wherever cash is
   * tracked, because the column is labelled "% of portfolio" and cash is part of one.
   */
  totalMarketValue: number
  /**
   * Projected next-12-month income over cost, per security, from
   * `/api/dividends/breakdown`. Keyed on `security_id` because identity is
   * isin + exchange — ASML is two securities and must not share a yield.
   *
   * A security **absent** from the map is the normal case, not an error: the breakdown
   * only carries securities with payments or a projection, so every accumulating ETF and
   * non-payer is missing rather than present-with-null. Both mean "no rate to show".
   */
  yieldOnCost: Map<number, number | null>
}): Column<Position, SortColumn>[] {
  const { formatCurrency, totalMarketValue, yieldOnCost } = deps
  // `null` rather than 0 for a holding with no weight to compute — a 0.00% here reads
  // as "this is a negligible part of the book", which is a claim, and `RebalanceCard`'s
  // Current column already prints a dash for the same row.
  const weightOf = (p: Position): number | null =>
    cannotValue(p) || totalMarketValue <= 0
      ? null
      : (p.market_value_eur / totalMarketValue) * 100
  const yocOf = (p: Position) => yieldOnCost.get(p.security_id) ?? null

  return [
    {
      key: 'symbol',
      header: 'Position',
      shortHeader: 'Name',
      sortKey: 'description',
      mobile: 'title',
      cell: (p, view) => {
        const badge = accountBadge(p)
        // One Column[] renders both the table and the phone card list, so the badge
        // reaches mobile without a second edit — the whole reason DataTable exists.
        const tag = badge ? (
          <span
            className="ml-1.5 rounded bg-muted px-1 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground align-middle"
            title="Pillar 3a account"
          >
            {badge}
          </span>
        ) : null
        const name = displayName(p.description) || p.symbol
        return view === 'table' ? (
          <div className="flex items-center gap-3">
            <TickerAvatar symbol={p.symbol} />
            <div className="min-w-0">
              <div className="max-w-[16rem] truncate font-medium" title={p.description}>
                {name}
                {tag}
              </div>
              <div className="text-xs text-muted-foreground">
                {p.symbol} · {exchangeOf(p)}
              </div>
            </div>
          </div>
        ) : (
          <>
            {name}
            {tag}
          </>
        )
      },
    },
    {
      key: 'ticker',
      header: 'Ticker',
      shortHeader: 'Ticker',
      desktop: 'hide',
      mobile: 'meta',
      cell: (p) => `${p.symbol} · ${exchangeOf(p)}`,
    },
    {
      key: 'rating',
      header: 'Analysts',
      shortHeader: 'Analysts',
      sortKey: 'rating',
      align: 'center',
      mobile: 'badge',
      cell: (p, view) =>
        !p.analyst_rating ? (
          // A table cell cannot be empty, so the dash is the desktop placeholder. In a
          // card it would be a stray dash floating in the badge row, saying nothing.
          view === 'table' ? <span className="text-xs text-muted-foreground">—</span> : null
        ) : (
          <span
            className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${getRatingBadgeColor(p.analyst_rating.consensus)}`}
            title={`Strong Buy ${p.analyst_rating.strong_buy} · Buy ${p.analyst_rating.buy} · Hold ${p.analyst_rating.hold} · Sell ${p.analyst_rating.sell} · Strong Sell ${p.analyst_rating.strong_sell}`}
          >
            {p.analyst_rating.consensus}
          </span>
        ),
    },
    {
      key: 'market_value',
      header: 'Value',
      shortHeader: 'Value',
      sortKey: 'market_value_eur',
      align: 'right',
      mobile: 'value',
      cell: (p) => (cannotValue(p) ? NO_VALUE : formatCurrency(p.market_value_eur)),
    },
    {
      key: 'gain_loss_percent',
      header: 'Return',
      shortHeader: 'Return %',
      sortKey: 'gain_loss_percent',
      align: 'right',
      mobile: 'delta',
      // Carried under the gain on desktop.
      desktop: 'hide',
      tone: gainTone,
      cell: (p) => (cannotValue(p) ? NO_VALUE : formatPercent(p.gain_loss_percent)),
    },
    {
      key: 'quantity',
      header: 'Shares',
      shortHeader: 'Shares',
      sortKey: 'quantity',
      align: 'right',
      cell: (p) => formatQuantity(p.quantity),
    },
    {
      key: 'cost_basis',
      header: 'Cost basis',
      shortHeader: 'Cost basis',
      sortKey: 'cost_basis_eur',
      align: 'right',
      cell: (p) => formatCurrency(p.cost_basis_eur),
    },
    {
      key: 'gain_loss',
      header: 'Gain',
      shortHeader: 'Gain',
      sortKey: 'gain_loss_eur',
      align: 'right',
      tone: gainTone,
      cell: (p, view) =>
        cannotValue(p) ? NO_VALUE
        : view === 'table' ? (
          <>
            <div>{formatCurrency(p.gain_loss_eur)}</div>
            <div className="text-xs">{formatPercent(p.gain_loss_percent)}</div>
          </>
        ) : formatCurrency(p.gain_loss_eur),
    },
    {
      key: 'weight',
      header: 'Weight',
      shortHeader: 'Weight',
      sortKey: 'portfolio_percent',
      align: 'right',
      cellClassName: 'text-muted-foreground',
      cell: (p) => {
        const w = weightOf(p)
        return w === null ? NO_VALUE : `${w.toFixed(2)}%`
      },
    },
    {
      key: 'yoc',
      header: 'YoC',
      shortHeader: 'Yield on cost',
      sortKey: 'yield_on_cost',
      align: 'right',
      cellClassName: 'text-muted-foreground',
      // A dash is a holding that distributes nothing: every accumulating ETF, correctly.
      hint: { description: 'Yield on cost' },
      // `toFixed(2)` and an em dash, matching DividendsTab's `yoc` column and the KPI
      // card rather than this file's `formatPercent`/hyphen: it is the same number on a
      // third screen, and the hyphen belongs to the rating badge, where a lone dash in a
      // chip row says nothing. A detail row needs a value beside its label.
      cell: (p) => {
        const y = yocOf(p)
        return y != null ? `${y.toFixed(2)}%` : '—'
      },
    },
  ]
}

const NO_YIELDS: Map<number, number | null> = new Map()

export function PositionsList({
  positions, isLoading, isError, yieldOnCost = NO_YIELDS, cash,
}: PositionsListProps) {
  const formatCurrency = useFormatCurrency()
  const [sortColumn, setSortColumn] = useState<SortColumn>('market_value_eur')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')

  // Calculate total portfolio market value for percentage calculations.
  // Unpriced holdings contribute 0.00 anyway, so this is unchanged in value — but
  // reading it through the predicate keeps the denominator and the rows that are
  // allowed a weight describing the same set.
  const cashTracked = cashIsTracked(cash)
  const cashAmount = cashTracked ? cash!.amount : 0
  const totalHoldings = useMemo(() => {
    if (!positions || positions.length === 0) return 0
    return positions.reduce((sum, p) => (cannotValue(p) ? sum : sum + p.market_value_eur), 0)
  }, [positions])
  const totalMarketValue = totalHoldings + cashAmount

  /** Named on screen, in the order the table shows them. */
  const unpriced = useMemo(
    () => (positions ?? []).filter(cannotValue),
    [positions]
  )

  const handleSort = (column: SortColumn) => {
    if (sortColumn === column) {
      // Toggle direction if clicking the same column
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc')
    } else {
      // Default to descending for new column
      setSortColumn(column)
      setSortDirection('desc')
    }
  }

  const sortedPositions = useMemo(() => {
    if (!positions || positions.length === 0) return []

    const sorted = [...positions].sort((a, b) => {
      let aValue: string | number
      let bValue: string | number

      switch (sortColumn) {
        case 'symbol':
          aValue = a.symbol.toLowerCase()
          bValue = b.symbol.toLowerCase()
          break
        case 'description':
          aValue = displayName(a.description).toLowerCase()
          bValue = displayName(b.description).toLowerCase()
          break
        case 'rating':
          aValue = getRatingScore(a.analyst_rating?.consensus)
          bValue = getRatingScore(b.analyst_rating?.consensus)
          break
        case 'quantity':
          aValue = a.quantity
          bValue = b.quantity
          break
        case 'cost_basis_eur':
          aValue = a.cost_basis_eur
          bValue = b.cost_basis_eur
          break
        case 'market_value_eur':
          aValue = a.market_value_eur
          bValue = b.market_value_eur
          break
        case 'gain_loss_eur':
          aValue = a.gain_loss_eur
          bValue = b.gain_loss_eur
          break
        case 'gain_loss_percent':
          aValue = a.gain_loss_percent
          bValue = b.gain_loss_percent
          break
        case 'portfolio_percent':
          aValue = totalMarketValue > 0 ? (a.market_value_eur / totalMarketValue) * 100 : 0
          bValue = totalMarketValue > 0 ? (b.market_value_eur / totalMarketValue) * 100 : 0
          break
        case 'yield_on_cost':
          // A sentinel below every real yield, so descending — the default, and the
          // direction someone clicking this column wants — puts the 17 rows with no rate
          // beneath the 19 that have one. Ascending puts them first, which is the same
          // trade-off `getRatingScore` makes for the `rating` column and is honest enough
          // here: a holding that distributes nothing really is at the bottom of this ranking.
          aValue = yieldOnCost.get(a.security_id) ?? -1
          bValue = yieldOnCost.get(b.security_id) ?? -1
          break
        default:
          return 0
      }

      if (aValue < bValue) return sortDirection === 'asc' ? -1 : 1
      if (aValue > bValue) return sortDirection === 'asc' ? 1 : -1
      return 0
    })

    return sorted
    // `totalMarketValue` and `yieldOnCost` are read by two of the cases above, so they
    // belong here. The former was already missing — harmless only because a change in
    // the total always came with a change in `positions`; adding a case that reads a
    // separately-fetched map would have made the omission actually bite.
  }, [positions, sortColumn, sortDirection, totalMarketValue, yieldOnCost])

  const columns = useMemo(
    () => positionColumns({ formatCurrency, totalMarketValue, yieldOnCost }),
    [formatCurrency, totalMarketValue, yieldOnCost]
  )

  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Positions</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-center text-muted-foreground py-8">
            Loading positions...
          </div>
        </CardContent>
      </Card>
    )
  }

  // A server error must not impersonate an empty portfolio — the sync CTA
  // below can't fix a backend that isn't answering.
  if (isError) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Positions</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-center text-muted-foreground py-8">
            Couldn't load positions — the server didn't respond. It retries automatically.
          </div>
        </CardContent>
      </Card>
    )
  }

  if (!positions || positions.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Positions</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-center text-muted-foreground py-8">
            No positions yet — sync your account to see your holdings.
          </div>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Positions <span className="font-normal text-muted-foreground">{positions.length}</span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {unpriced.length > 0 && (
          // Outside no collapsible here — the table is always open — but stated above
          // it rather than as a footnote, for the reason `MonthlyReturnsHeatmap` learned:
          // the qualifier has to be where the figures are read. Every affected row shows
          // a dash rather than a number, so without this the reader knows *that*
          // something is missing and not *why* or what to do about it.
          <div
            role="alert"
            className="mb-4 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2 text-sm text-yellow-900 dark:text-yellow-200"
          >
            <span className="font-medium">
              {unpriced.length} {unpriced.length === 1 ? 'holding' : 'holdings'} could not be valued
            </span>{' '}
            ({unpriced.map((p) => p.symbol).join(', ')}) — no price or exchange rate is
            available, so totals exclude them.
          </div>
        )}
        {cashTracked && totalMarketValue > 0 && (
          <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 rounded-lg bg-muted/60 px-3 py-2.5 text-sm">
            <span className="font-medium">Cash</span>
            <span className="tabular-nums">
              {formatCurrency(cashAmount)}
              <span className="ml-2 text-muted-foreground">
                {((cashAmount / totalMarketValue) * 100).toFixed(2)}%
              </span>
            </span>
          </div>
        )}
        <DataTable
          rows={sortedPositions}
          columns={columns}
          getRowKey={(p) => p.security_id}
          label="Positions table"
          density="comfortable"
          // Quantity, cost basis, gain and weight are what a review is for; the
          // exchange and ISIN are identifiers you go looking for, so they sit behind
          // the disclosure rather than adding a line to all 29 cards.
          // 5, not 4, so the new yield-on-cost row does not push Weight behind the
          // "Show all N metrics" disclosure on a phone.
          detailLimit={5}
          sort={{ column: sortColumn, direction: sortDirection, onSort: handleSort }}
        />
      </CardContent>
    </Card>
  )
}
