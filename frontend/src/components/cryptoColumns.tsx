import type { Column } from '@/components/ui/DataTable'
import type { CryptoHoldingItem } from '@/lib/api'
import { formatQuantity } from '@/lib/utils'

export type CryptoSortColumn = 'symbol' | 'value' | 'change' | 'unrealized' | 'weight'

/** What a figure CoinStats did not report shows. Never a zero: a 0.00 claims a value. */
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
 * Every figure is in the base currency already. An **unpriced** coin — CoinStats has no
 * price for it — shows a dash for price, value and weight and says so in a badge; valuing
 * it at 0.00 would publish a fabricated total loss on the one screen someone opens to see
 * which coin. Cost, average buy and P&L are CoinStats' USD figures at one day's rate; the
 * overview prints that caveat above the table, where it applies to every row at once.
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
            {h.is_fiat && (
              <span className={BADGE} title="A fiat balance held on an exchange">cash</span>
            )}
            {h.status === 'unpriced' && (
              <span
                className="ml-1.5 rounded border border-amber-300/60 bg-amber-50 px-1 py-0.5 text-[10px] font-medium text-amber-800 align-middle dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
                title="CoinStats has no price for this coin, so it is left out of every total rather than valued at zero."
              >
                unpriced
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
      cell: h => price(h.price),
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
      header: '24h',
      shortHeader: '24h',
      sortKey: 'change',
      align: 'right',
      mobile: 'delta',
      cellClassName: 'tabular-nums',
      tone: h => signedTone(h.change_24h_pct),
      cell: h => pct(h.change_24h_pct),
    },
    {
      key: 'avg_buy',
      header: 'Avg buy',
      align: 'right',
      cellClassName: 'tabular-nums',
      hint: {
        description:
          "CoinStats' average purchase price, in USD as CoinStats keeps it, converted at the snapshot's rate.",
      },
      cell: h => price(h.avg_buy),
    },
    {
      key: 'unrealized',
      header: 'Unrealized P&L',
      shortHeader: 'Unrealized P&L',
      sortKey: 'unrealized',
      align: 'right',
      cellClassName: 'tabular-nums',
      tone: h => signedTone(h.unrealized_pl),
      hint: {
        description:
          "CoinStats' figure: current value less cost basis, measured in USD and converted at the snapshot's rate.",
      },
      cell: (h, view) => {
        const amount = money(h.unrealized_pl)
        if (h.unrealized_pl_pct == null) return amount
        return view === 'table' ? (
          <>
            <div>{amount}</div>
            <div className="text-xs">{pct(h.unrealized_pl_pct)}</div>
          </>
        ) : (
          `${amount} (${pct(h.unrealized_pl_pct)})`
        )
      },
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
          "Share of CoinStats' whole portfolio total. Not rescaled: what CoinStats totals but does not list stays out of every row.",
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
      case 'change': return h.change_24h_pct
      case 'unrealized': return h.unrealized_pl
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
