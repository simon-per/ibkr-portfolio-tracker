import { useId, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, Users } from 'lucide-react'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { IconChip } from '@/components/ui/IconChip'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import type { CompanyFinancialsKeyStats, CompanyPeer } from '@/lib/api'
import { compareNullsLast } from '@/lib/nullsLast'
import { CompanyLink } from './CompanyLink'
import { companyPeersQuery } from '@/lib/queryCache'
import { displayName } from '@/lib/securityName'
import { TONE_TEXT } from '@/lib/tone'
import {
  ABSENT,
  cn,
  formatCompactMoney,
  formatFractionPercent,
  formatMultiple,
  formatSigned,
} from '@/lib/utils'

type FigureKey = 'market_cap' | 'revenue' | 'revenue_yoy' | 'operating_margin' | 'fcf_margin'
  | 'pe_ratio' | 'ps_ratio' | 'ev_ebit'
export type PeerSortKey = 'symbol' | FigureKey

const figure = (row: CompanyPeer, key: FigureKey): number | null =>
  (row.key_stats as CompanyFinancialsKeyStats)[key] ?? null

const money = (row: CompanyPeer, key: FigureKey) => {
  const v = figure(row, key)
  return v == null || !row.currency ? ABSENT : formatCompactMoney(v, row.currency)
}
const percent = (row: CompanyPeer, key: FigureKey) => {
  const v = figure(row, key)
  return v == null ? ABSENT : formatFractionPercent(v)
}
const multiple = (row: CompanyPeer, key: FigureKey) => {
  const v = figure(row, key)
  return v == null ? ABSENT : formatMultiple(v)
}

/** A peer through the one lookup every link uses: every peer is on the tab's list. */
const peerRef = (row: CompanyPeer) => ({
  securityId: row.security_id,
  isin: row.isin,
  ticker: row.symbol,
})

const peerAvatar = (row: CompanyPeer, className: string) => (
  <TickerAvatar
    symbol={row.symbol}
    securityId={row.security_id ?? undefined}
    isin={row.isin ?? undefined}
    className={className}
  />
)

/**
 * The Peers card's columns (review item R1): the overview's key figures of each company the
 * tab lists in the same SEC industry, each in its own reporting currency. Module-level, so
 * `tableFamily.test.tsx` holds it to the shared conventions.
 */
export function peerColumns(): Column<CompanyPeer, PeerSortKey>[] {
  const numeric = (
    key: FigureKey,
    header: string,
    print: (row: CompanyPeer, key: FigureKey) => string,
    extra: Partial<Column<CompanyPeer, PeerSortKey>> = {},
  ): Column<CompanyPeer, PeerSortKey> => ({
    key,
    header,
    shortHeader: header,
    sortKey: key,
    align: 'right',
    cell: (row) => print(row, key),
    ...extra,
  })
  return [
    {
      key: 'symbol',
      header: 'Company',
      shortHeader: 'Company',
      sortKey: 'symbol',
      mobile: 'title',
      avatar: peerAvatar,
      cell: (row, view) =>
        view === 'table' ? (
          <CompanyLink {...peerRef(row)} className="flex min-w-0 items-center gap-2">
            {peerAvatar(row, 'h-7 w-7 text-[10px]')}
            <span className="min-w-0">
              <span className="block font-medium">{row.symbol}</span>
              <span className="block truncate text-xs text-muted-foreground">{displayName(row.name)}</span>
            </span>
          </CompanyLink>
        ) : (
          <CompanyLink {...peerRef(row)}>{row.symbol}</CompanyLink>
        ),
    },
    {
      key: 'name',
      header: 'Name',
      desktop: 'hide',
      mobile: 'meta',
      cell: (row) => displayName(row.name),
    },
    numeric('market_cap', 'Mkt cap', money, { mobile: 'value' }),
    numeric('revenue', 'Revenue', money),
    numeric(
      'revenue_yoy',
      'Rev YoY',
      (row, key) => {
        const v = figure(row, key)
        return v == null ? ABSENT : formatSigned(v, (n) => formatFractionPercent(n))
      },
      {
        mobile: 'delta',
        tone: (row) => {
          const v = figure(row, 'revenue_yoy')
          if (v == null || Math.abs(v) < 0.0005) return undefined
          return v > 0 ? TONE_TEXT.positive : TONE_TEXT.negative
        },
      },
    ),
    numeric('operating_margin', 'Op margin', percent),
    numeric('fcf_margin', 'FCF margin', percent),
    numeric('pe_ratio', 'P/E', multiple),
    numeric('ps_ratio', 'P/S', multiple),
    numeric('ev_ebit', 'EV/EBIT', multiple),
  ]
}

/**
 * The companies the tab lists in this one's SEC industry, with the overview's key figures
 * (review item R1). Collapsed: it asks the server only once opened. The company itself
 * leads until a column is sorted.
 */
export function CompanyPeersCard({ cik, industry }: { cik: number; industry: string }) {
  const [open, setOpen] = useState(false)
  const contentId = useId()
  const peers = useQuery({ ...companyPeersQuery(cik), enabled: open })
  const [sortKey, setSortKey] = useState<PeerSortKey | null>(null)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const columns = useMemo(() => peerColumns(), [])

  const onSort = (key: PeerSortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir(key === 'symbol' ? 'asc' : 'desc')
    }
  }

  const rows = useMemo(() => {
    const served = peers.data?.peers ?? []
    if (sortKey == null) return served
    const value = (row: CompanyPeer) => (sortKey === 'symbol' ? row.symbol : figure(row, sortKey))
    return [...served].sort(
      (a, b) => compareNullsLast(value(a), value(b), sortDir) || a.symbol.localeCompare(b.symbol),
    )
  }, [peers.data, sortKey, sortDir])

  const sic = peers.data?.sic
  const code = sic == null ? null : peers.data?.widened ? `SIC ${Math.floor(sic / 10)}x` : `SIC ${sic}`

  return (
    <section aria-label="Peers" className="rounded-xl border border-border/70 bg-card">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={contentId}
        className="flex w-full select-none flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl px-4 py-3 text-left ring-offset-background transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <ChevronRight
          aria-hidden="true"
          className={cn('-ml-1 h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200', open && 'rotate-90')}
        />
        <IconChip icon={Users} />
        <span className="text-sm font-semibold">Peers</span>
        <span className="min-w-0 truncate text-xs text-muted-foreground">{industry}</span>
        {code && <span className="text-xs tabular-nums text-muted-foreground">{code}</span>}
      </button>
      {open && (
        <div id={contentId} className="px-4 pb-3" data-testid="company-peers">
          {peers.isError ? (
            <p className="py-4 text-sm text-muted-foreground">Couldn't load the peers.</p>
          ) : !peers.data ? (
            <div className="h-24 animate-pulse rounded-lg bg-muted" />
          ) : rows.length < 2 ? (
            <p className="py-4 text-sm text-muted-foreground">No other company in this industry.</p>
          ) : (
            <DataTable
              rows={rows}
              columns={columns}
              getRowKey={(row) => row.cik}
              label="Peers table"
              density="normal"
              minWidthClassName="min-w-[48rem]"
              pinFirstColumn
              sort={{ column: sortKey, direction: sortDir, onSort }}
            />
          )}
        </div>
      )}
    </section>
  )
}
