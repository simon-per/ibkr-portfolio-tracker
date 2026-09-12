import { useState, useMemo } from 'react'
import type { Position } from '@/lib/api'
import { formatPercent } from '@/lib/utils'
import { isUnpriced } from '@/lib/positionValuation'
import { getRatingScore } from '@/lib/analystRating'
import { cashCaveat, cashIsTracked } from '@/lib/portfolioCash'
import type { CashSource } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { DataTable, type Column } from '@/components/ui/DataTable'

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

const getRatingBadgeColor = (consensus: string): string => {
  switch (consensus.toLowerCase()) {
    case 'strong buy':
      return 'bg-green-600 text-white'
    case 'buy':
      return 'bg-green-500 text-white'
    case 'hold':
      return 'bg-yellow-500 text-white'
    case 'sell':
      return 'bg-red-500 text-white'
    case 'strong sell':
      return 'bg-red-600 text-white'
    default:
      return 'bg-gray-500 text-white'
  }
}

// The rating sort score lives in `lib/analystRating.ts`, shared with the watchlist — the
// two tables ranked the same ratings by different rules until 2026-09-12.

const exchangeOf = (p: Position) => p.exchange || 'N/A'

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
 * Note where the two views legitimately differ. The desktop table stacks the exchange
 * under the symbol and the ISIN under the description; the card puts the symbol on its
 * title line and the other two in the detail grid, so those get `desktop: 'hide'`
 * companions. The *values* come from one place either way.
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
      header: 'Symbol',
      shortHeader: 'Symbol',
      sortKey: 'symbol',
      mobile: 'title',
      cell: (p, view) => {
        const badge = accountBadge(p)
        // One Column[] renders both the table and the phone card list, so the badge
        // reaches mobile without a second edit — the whole reason DataTable exists.
        const tag = badge ? (
          <span
            className="ml-1.5 rounded bg-muted px-1 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground align-middle"
            title="Held in your pillar 3a account. Excluded from the tax report's wealth and income sections."
          >
            {badge}
          </span>
        ) : null
        return view === 'table' ? (
          <>
            <div className="font-medium">
              {p.symbol}
              {tag}
            </div>
            <div className="text-xs text-muted-foreground">{exchangeOf(p)}</div>
          </>
        ) : (
          <>
            {p.symbol}
            {tag}
          </>
        )
      },
    },
    {
      key: 'description',
      header: 'Description',
      shortHeader: 'Description',
      sortKey: 'description',
      mobile: 'meta',
      cellClassName: 'max-w-xs',
      cell: (p, view) =>
        view === 'table' ? (
          <>
            <div className="max-w-xs truncate text-sm">{p.description}</div>
            <div className="text-xs text-muted-foreground">{p.isin}</div>
          </>
        ) : (
          p.description
        ),
    },
    {
      key: 'rating',
      header: 'Rating',
      shortHeader: 'Rating',
      sortKey: 'rating',
      align: 'center',
      mobile: 'badge',
      cell: (p, view) =>
        !p.analyst_rating ? (
          // A table cell cannot be empty, so the dash is the desktop placeholder. In a
          // card it would be a stray "-" floating in the badge row, saying nothing.
          view === 'table' ? <div className="text-center text-xs text-muted-foreground">-</div> : null
        ) : (
          <div
            className="flex flex-col items-center gap-1"
            title={`Strong Buy: ${p.analyst_rating.strong_buy}, Buy: ${p.analyst_rating.buy}, Hold: ${p.analyst_rating.hold}, Sell: ${p.analyst_rating.sell}, Strong Sell: ${p.analyst_rating.strong_sell}`}
          >
            <span
              className={`px-2 py-0.5 rounded text-xs font-medium ${getRatingBadgeColor(p.analyst_rating.consensus)}`}
            >
              {p.analyst_rating.consensus}
            </span>
            {/* The breakdown is rendered, not just in the `title=`, which is what makes
                it readable on a phone where no hover exists. */}
            <div className="text-[10px] text-muted-foreground flex gap-0.5">
              <span className="text-green-600 font-semibold">{p.analyst_rating.strong_buy}</span>
              <span>/</span>
              <span className="text-green-500 font-semibold">{p.analyst_rating.buy}</span>
              <span>/</span>
              <span className="text-yellow-600 font-semibold">{p.analyst_rating.hold}</span>
              <span>/</span>
              <span className="text-red-500 font-semibold">{p.analyst_rating.sell}</span>
              <span>/</span>
              <span className="text-red-600 font-semibold">{p.analyst_rating.strong_sell}</span>
            </div>
          </div>
        ),
    },
    {
      key: 'market_value',
      header: 'Market Value',
      shortHeader: 'Market Value',
      sortKey: 'market_value_eur',
      align: 'right',
      mobile: 'value',
      cell: (p) => (cannotValue(p) ? NO_VALUE : formatCurrency(p.market_value_eur)),
    },
    {
      key: 'gain_loss_percent',
      header: '%',
      shortHeader: 'Gain/Loss %',
      sortKey: 'gain_loss_percent',
      align: 'right',
      mobile: 'delta',
      tone: gainTone,
      cell: (p) => (cannotValue(p) ? NO_VALUE : formatPercent(p.gain_loss_percent)),
    },
    {
      key: 'quantity',
      header: 'Quantity',
      shortHeader: 'Quantity',
      sortKey: 'quantity',
      align: 'right',
      cell: (p) => p.quantity.toFixed(2),
    },
    {
      key: 'cost_basis',
      header: 'Cost Basis',
      shortHeader: 'Cost Basis',
      sortKey: 'cost_basis_eur',
      align: 'right',
      cell: (p) => formatCurrency(p.cost_basis_eur),
    },
    {
      key: 'gain_loss',
      header: 'Gain/Loss',
      shortHeader: 'Gain/Loss',
      sortKey: 'gain_loss_eur',
      align: 'right',
      tone: gainTone,
      cell: (p) => (cannotValue(p) ? NO_VALUE : formatCurrency(p.gain_loss_eur)),
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
      hint: {
        description:
          'Projected next-12-month dividend income over what this position cost. The ' +
          'same figure the Dividends tab shows, and the same definition the Yield on ' +
          'Cost card above uses. A dash means the holding distributes nothing — every ' +
          'accumulating ETF reads that way, correctly, because it reinvests internally ' +
          'instead of paying out.',
      },
      // `toFixed(2)` and an em dash, matching DividendsTab's `yoc` column and the KPI
      // card rather than this file's `formatPercent`/hyphen: it is the same number on a
      // third screen, and the hyphen belongs to the rating badge, where a lone dash in a
      // chip row says nothing. A detail row needs a value beside its label.
      cell: (p) => {
        const y = yocOf(p)
        return y != null ? `${y.toFixed(2)}%` : '—'
      },
    },
    // Carried by the desktop cells above as sub-lines, so they would be duplicated
    // there; on a phone they are the detail pairs that keep a dual-listed security
    // (ASML is two securities) tellable apart.
    {
      key: 'exchange',
      header: 'Exchange',
      shortHeader: 'Exchange',
      desktop: 'hide',
      cell: exchangeOf,
    },
    { key: 'isin', header: 'ISIN', shortHeader: 'ISIN', desktop: 'hide', cell: (p) => p.isin },
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
          aValue = a.description.toLowerCase()
          bValue = b.description.toLowerCase()
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
            Couldn't load positions — the backend didn't respond. It retries automatically.
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
            No positions found. Sync your IBKR data to see your holdings.
          </div>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Positions ({positions.length})</CardTitle>
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
            ({unpriced.map((p) => p.symbol).join(', ')}). Their value, gain and weight
            show a dash rather than a figure — the backend has no cached price, or no FX
            rate for the currency it trades in. Check the market-data sync's warnings and
            that security's <code>ticker_mappings</code> row. Totals elsewhere on this
            page exclude them too.
          </div>
        )}
        {cashTracked && totalMarketValue > 0 && (
          <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm">
            <span className="font-medium">Cash (uninvested)</span>
            <span className="tabular-nums">
              {formatCurrency(cashAmount)}
              <span className="ml-2 text-muted-foreground">
                {((cashAmount / totalMarketValue) * 100).toFixed(2)}% of the account
              </span>
            </span>
            <span className="w-full text-xs text-muted-foreground">
              Not a holding, so it has no row below — but it is part of the account, so
              every weight in this table is a share of holdings <em>plus</em> this
              {cashCaveat(cash?.cash_source) ? `. The balance is ${cashCaveat(cash?.cash_source)}` : ''}.
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
