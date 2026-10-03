import type { Column } from '@/components/ui/DataTable'
import type { CryptoHoldingItem } from '@/lib/api'
import { formatQuantity } from '@/lib/utils'

export type CryptoSortColumn = 'symbol' | 'value' | 'change' | 'weight'

/** What an unknown figure shows. Never a zero: a 0.00 claims a value. */
const ABSENT = '—'

const signedTone = (value: number | null) =>
  value == null ? 'text-muted-foreground' : value >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'

const pct = (value: number | null) =>
  value == null ? ABSENT : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`

const BADGE =
  'ml-1.5 rounded bg-muted px-1 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground align-middle'

/**
 * The crypto holdings table, described once for both renderings (see `ui/DataTable`).
 *
 * A factory for the reason `positionColumns` is one: the cells close over the currency
 * formatters, and the table-family test imports the exact array the view renders.
 *
 * Every figure is in the base currency already; prices are CoinGecko's. An **unpriced**
 * coin (CoinStats has no price) or one with **no price** that day (CoinGecko has none)
 * shows a dash for price, value and weight and says so in a badge; valuing it at 0.00
 * would publish a fabricated total loss on the one screen someone opens to see which
 * coin. A stablecoin valued at its fixed peg says so beside the price.
 */
export function cryptoHoldingColumns(deps: {
  formatCurrency: (value: number) => string
  formatPrice: (value: number) => string
}): Column<CryptoHoldingItem, CryptoSortColumn>[] {
  const { formatCurrency, formatPrice } = deps
  const money = (value: number | null) => (value == null ? ABSENT : formatCurrency(value))
  const price = (value: number | null) => (value == null ? ABSENT : formatPrice(value))

  return [
    {
      key: 'coin',
      header: 'Coin',
      shortHeader: 'Coin',
      sortKey: 'symbol',
      mobile: 'title',
      cell: (h, view) => {
        const tags = (
          <>
            {h.status === 'unpriced' && (
              <span
                className="ml-1.5 rounded border border-amber-300/60 bg-amber-50 px-1 py-0.5 text-[10px] font-medium text-amber-800 align-middle dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
                title="CoinStats has no price for this coin, so it is left out of every total rather than valued at zero."
              >
                unpriced
              </span>
            )}
            {h.status === 'no_price' && (
              <span
                className="ml-1.5 rounded border border-amber-300/60 bg-amber-50 px-1 py-0.5 text-[10px] font-medium text-amber-800 align-middle dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
                title="CoinGecko has no price for this coin today, so the total is unknown rather than understated."
              >
                no price
              </span>
            )}
          </>
        )
        const symbol = h.symbol ?? h.coin_id
        return view === 'table' ? (
          <>
            <div className="font-medium">
              {symbol}
              {tags}
            </div>
            {h.name && h.name !== h.symbol && (
              <div className="text-xs text-muted-foreground">{h.name}</div>
            )}
          </>
        ) : (
          <>
            {symbol}
            {tags}
          </>
        )
      },
    },
    {
      key: 'name',
      header: 'Name',
      desktop: 'hide',
      mobile: 'meta',
      cell: h => h.name ?? '',
    },
    {
      key: 'quantity',
      header: 'Quantity',
      align: 'right',
      cellClassName: 'tabular-nums',
      cell: h => formatQuantity(h.quantity),
    },
    {
      key: 'price',
      header: 'Price',
      align: 'right',
      cellClassName: 'tabular-nums',
      cell: h => (
        <>
          {price(h.price)}
          {h.price_source === 'peg' && <span className={BADGE}>peg</span>}
        </>
      ),
    },
    {
      key: 'value',
      header: 'Value',
      shortHeader: 'Value',
      sortKey: 'value',
      align: 'right',
      mobile: 'value',
      cellClassName: 'tabular-nums font-medium',
      cell: h => money(h.value),
    },
    {
      key: 'change',
      header: 'Today',
      shortHeader: 'Today',
      sortKey: 'change',
      align: 'right',
      mobile: 'delta',
      cellClassName: 'tabular-nums',
      tone: h => signedTone(h.change_today_pct),
      hint: { description: "Price move since the previous day's close (00:00 UTC)." },
      cell: h => pct(h.change_today_pct),
    },
    {
      key: 'weight',
      header: 'Weight',
      shortHeader: 'Weight',
      sortKey: 'weight',
      align: 'right',
      cellClassName: 'tabular-nums',
      hint: {
        description:
          "Share of the book's total: the priced coins at CoinGecko's price. Exchange cash and DeFi sit beside the total, not in it.",
      },
      cell: h => (h.weight_pct == null ? ABSENT : `${h.weight_pct.toFixed(2)}%`),
    },
  ]
}

/** Sort for the table: nulls last whichever way, then by symbol so ties are stable. */
export function sortCryptoHoldings(
  rows: readonly CryptoHoldingItem[],
  column: CryptoSortColumn | null,
  direction: 'asc' | 'desc',
): CryptoHoldingItem[] {
  if (!column) return [...rows]
  const value = (h: CryptoHoldingItem): number | string | null => {
    switch (column) {
      case 'symbol': return (h.symbol ?? h.coin_id).toUpperCase()
      case 'value': return h.value
      case 'change': return h.change_today_pct
      case 'weight': return h.weight_pct
    }
  }
  const sign = direction === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const va = value(a)
    const vb = value(b)
    if (va == null && vb == null) return 0
    if (va == null) return 1
    if (vb == null) return -1
    if (va < vb) return -1 * sign
    if (va > vb) return 1 * sign
    return (a.symbol ?? a.coin_id).localeCompare(b.symbol ?? b.coin_id)
  })
}
