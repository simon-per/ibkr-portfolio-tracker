import { useState, useMemo } from 'react'
import type { Position } from '@/lib/api'
import { formatPercent, formatQuantity } from '@/lib/utils'
import { isUnpriced } from '@/lib/positionValuation'
import { getRatingScore } from '@/lib/analystRating'
import { compareNullsLast, type SortDirection } from '@/lib/nullsLast'
import { cashIsTracked, inBookCash } from '@/lib/portfolioCash'
import type { CashSource } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { Briefcase, Wallet } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { IconChip } from '@/components/ui/IconChip'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import { displayName } from '@/lib/securityName'
import { weightDenominator } from '@/lib/performanceInsights'

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

/**
 * The return cell: a dash for an unvalued holding, and for a zero cost basis (a stock
 * distribution), where the server sends `null` — `formatPercent(null)` would print the
 * `0.00%` the server stopped sending.
 */
const returnOf = (p: Position): string =>
  cannotValue(p) || p.gain_loss_percent == null ? NO_VALUE : formatPercent(p.gain_loss_percent)

const gainTone = (p: Position) =>
  cannotValue(p) ? 'text-muted-foreground' : p.gain_loss_eur >= 0 ? 'text-gain' : 'text-loss'

/**
 * A row's share of the account, or `null` with no weight to compute — a 0.00% would read
 * as "a negligible part of the book", which is a claim, and `RebalanceCard`'s Current
 * column already prints a dash for the same row. The cell and the sort both read this.
 */
const weightOf = (p: Position, totalMarketValue: number): number | null =>
  cannotValue(p) || totalMarketValue <= 0 ? null : (p.market_value_eur / totalMarketValue) * 100

/**
 * What a column sorts by: what its cell shows, with a dash as `null`.
 *
 * An unvalued holding arrives with `market_value_eur: 0`, `gain_loss_eur: -cost` and
 * `gain_loss_percent: -100`. Its cells print dashes; sorting it by those stand-ins ranked it
 * the cheapest holding and the worst loser. Likewise an absent yield on cost was `-1`, so
 * ascending listed every non-payer first. `compareNullsLast` puts both last either way.
 *
 * The rating keeps `getRatingScore`'s unrated sentinel, so unrated rows lead ascending
 * here — pinned on purpose by `ratingSortFamily.test.tsx`.
 */
function positionSortValue(
  p: Position,
  column: SortColumn,
  deps: { totalMarketValue: number; yieldOnCost: Map<number, number | null> },
): number | string | null {
  switch (column) {
    case 'symbol': return p.symbol
    case 'description': return displayName(p.description) || p.symbol
    case 'rating': return getRatingScore(p.analyst_rating?.consensus)
    case 'quantity': return p.quantity
    case 'cost_basis_eur': return p.cost_basis_eur
    case 'market_value_eur': return cannotValue(p) ? null : p.market_value_eur
    case 'gain_loss_eur': return cannotValue(p) ? null : p.gain_loss_eur
    case 'gain_loss_percent': return cannotValue(p) ? null : p.gain_loss_percent
    case 'portfolio_percent': return weightOf(p, deps.totalMarketValue)
    case 'yield_on_cost': return deps.yieldOnCost.get(p.security_id) ?? null
  }
}

/** A new column starts in the direction a reader wants first: names A–Z, figures biggest. */
const initialDirection = (column: SortColumn): SortDirection =>
  column === 'symbol' || column === 'description' ? 'asc' : 'desc'

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
  /** The largest row's weight, which the weight bars scale to. Absent: bars scale to 100%. */
  maxWeightPct?: number | null
}): Column<Position, SortColumn>[] {
  const { formatCurrency, totalMarketValue, yieldOnCost, maxWeightPct } = deps
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
            <TickerAvatar symbol={p.symbol} securityId={p.security_id} />
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
            {/* Inline, so the title line still truncates as one run of text. */}
            <TickerAvatar
              symbol={p.symbol}
              securityId={p.security_id}
              className="mr-2 h-6 w-6 align-middle text-[9px]"
            />
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
      cell: returnOf,
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
            <div className="text-xs">{returnOf(p)}</div>
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
      cell: (p, view) => {
        const w = weightOf(p, totalMarketValue)
        if (w === null) return NO_VALUE
        const text = `${w.toFixed(2)}%`
        if (view !== 'table') return text
        // A slim bar under the figure, scaled to the largest row so the biggest holding
        // spans the cell. Decorative: the figure above it is the value.
        const scale = maxWeightPct && maxWeightPct > 0 ? maxWeightPct : 100
        return (
          <div className="ml-auto flex w-16 flex-col items-end gap-1">
            <span>{text}</span>
            <span aria-hidden="true" className="block h-1 w-full overflow-hidden rounded-full bg-muted">
              <span
                className="block h-full rounded-full bg-info/70"
                style={{ width: `${Math.min(100, (w / scale) * 100)}%` }}
              />
            </span>
          </div>
        )
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
  // A debit is still shown as a balance, but it is not in the denominator, so it has no share.
  const cashInBook = inBookCash(cash)
  // `weightDenominator` is shared with the Performance tab's "Largest holding" tile, so the
  // tile's weight and this row's cannot disagree.
  const totalMarketValue = useMemo(() => weightDenominator(positions ?? [], cash), [positions, cash])

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
      setSortColumn(column)
      setSortDirection(initialDirection(column))
    }
  }

  const sortedPositions = useMemo(() => {
    if (!positions || positions.length === 0) return []
    const deps = { totalMarketValue, yieldOnCost }
    // Ties fall back to the symbol, then the security, so equal rows cannot swap between
    // renders — the tie-break `lib/dividendTable.ts` uses.
    return [...positions].sort((a, b) =>
      compareNullsLast(
        positionSortValue(a, sortColumn, deps), positionSortValue(b, sortColumn, deps), sortDirection,
      ) || a.symbol.localeCompare(b.symbol) || a.security_id - b.security_id,
    )
    // `totalMarketValue` and `yieldOnCost` are read by the weight and yield keys, so they
    // belong here.
  }, [positions, sortColumn, sortDirection, totalMarketValue, yieldOnCost])

  const maxWeightPct = useMemo(() => {
    if (totalMarketValue <= 0) return null
    const top = (positions ?? []).reduce(
      (m, p) => (cannotValue(p) ? m : Math.max(m, p.market_value_eur)), 0,
    )
    return (top / totalMarketValue) * 100
  }, [positions, totalMarketValue])

  const columns = useMemo(
    () => positionColumns({ formatCurrency, totalMarketValue, yieldOnCost, maxWeightPct }),
    [formatCurrency, totalMarketValue, yieldOnCost, maxWeightPct]
  )

  if (isLoading) {
    return (
      <Card>
        <SectionHeader title="Positions" icon={Briefcase} />
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
        <SectionHeader title="Positions" icon={Briefcase} />
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
        <SectionHeader title="Positions" icon={Briefcase} />
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
      <SectionHeader
        title={<>Positions <span className="font-normal text-muted-foreground">{positions.length}</span></>}
        icon={Briefcase}
      />
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
          <div className="mb-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-xl bg-raised px-3 py-2.5 text-sm">
            <span className="flex items-center gap-3 font-medium">
              <IconChip icon={Wallet} />
              Cash
            </span>
            <span className="tabular-nums">
              {formatCurrency(cashAmount)}
              {cashInBook > 0 && (
                <span className="ml-2 text-muted-foreground">
                  {((cashInBook / totalMarketValue) * 100).toFixed(2)}%
                </span>
              )}
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
