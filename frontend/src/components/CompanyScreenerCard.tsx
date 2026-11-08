import { useId, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, ListFilter } from 'lucide-react'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { IconChip } from '@/components/ui/IconChip'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import type { CompanyScreenerRow } from '@/lib/api'
import { compareNullsLast } from '@/lib/nullsLast'
import { companyScreenerQuery } from '@/lib/queryCache'
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
import { CompanyLink } from './CompanyLink'

type FigureKey =
  | 'market_cap' | 'pe_ratio' | 'pe_median_5y' | 'pe_vs_median' | 'pfcf_vs_median'
  | 'ps_vs_median' | 'revenue_cagr_5y' | 'eps_cagr_5y' | 'fcf_margin' | 'roe'
  | 'dividend_yield' | 'raise_years'
export type ScreenerSortKey = 'symbol' | FigureKey

type Shown = 'all' | 'held'
const SHOWN_OPTIONS: { value: Shown; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'held', label: 'Held' },
]

const figure = (row: CompanyScreenerRow, key: FigureKey): number | null => row[key] ?? null

const signed = (v: number | null) => (v == null ? ABSENT : formatSigned(v, (n) => formatFractionPercent(n)))
const growthTone = (v: number | null) => {
  if (v == null || Math.abs(v) < 0.0005) return undefined
  return v > 0 ? TONE_TEXT.positive : TONE_TEXT.negative
}

const companyRef = (row: CompanyScreenerRow) => ({
  securityId: row.security_id,
  isin: row.isin,
  ticker: row.symbol,
})

const companyAvatar = (row: CompanyScreenerRow, className: string) => (
  <TickerAvatar
    symbol={row.symbol}
    securityId={row.security_id ?? undefined}
    isin={row.isin ?? undefined}
    className={className}
  />
)

/**
 * The screener's columns: each company's overview figures in one line, its P/E, P/FCF and P/S
 * against its own 5-year medians, growth, quality and dividend record. Money in the company's
 * reporting currency. Module-level, so `tableFamily.test.tsx` holds it to the shared conventions.
 */
export function screenerColumns(): Column<CompanyScreenerRow, ScreenerSortKey>[] {
  const numeric = (
    key: FigureKey,
    header: string,
    shortHeader: string,
    print: (v: number | null, row: CompanyScreenerRow) => string,
    extra: Partial<Column<CompanyScreenerRow, ScreenerSortKey>> = {},
  ): Column<CompanyScreenerRow, ScreenerSortKey> => ({
    key,
    header,
    shortHeader,
    sortKey: key,
    align: 'right',
    cell: (row) => print(figure(row, key), row),
    ...extra,
  })
  const multiple = (v: number | null) => (v == null ? ABSENT : formatMultiple(v))
  const percent = (v: number | null) => (v == null ? ABSENT : formatFractionPercent(v))
  return [
    {
      key: 'symbol',
      header: 'Company',
      shortHeader: 'Company',
      sortKey: 'symbol',
      mobile: 'title',
      avatar: companyAvatar,
      cell: (row, view) =>
        view === 'table' ? (
          <CompanyLink {...companyRef(row)} className="flex min-w-0 items-center gap-2">
            {companyAvatar(row, 'h-7 w-7 text-[10px]')}
            <span className="min-w-0">
              <span className="block font-medium">{row.symbol}</span>
              <span className="block truncate text-xs text-muted-foreground">{displayName(row.name)}</span>
            </span>
          </CompanyLink>
        ) : (
          <CompanyLink {...companyRef(row)}>{row.symbol}</CompanyLink>
        ),
    },
    {
      key: 'name',
      header: 'Name',
      desktop: 'hide',
      mobile: 'meta',
      cell: (row) => displayName(row.name),
    },
    numeric('market_cap', 'Mkt cap', 'Market cap', (v, row) =>
      v == null || !row.currency ? ABSENT : formatCompactMoney(v, row.currency)),
    numeric('pe_ratio', 'P/E', 'P/E', multiple, { mobile: 'value' }),
    numeric('pe_median_5y', 'P/E 5Y', 'P/E 5Y median', multiple, {
      cellClassName: 'text-muted-foreground',
      hint: { description: 'Its own 5-year median' },
    }),
    numeric('pe_vs_median', 'vs 5Y', 'P/E vs 5Y median', signed, {
      mobile: 'delta',
      hint: { description: 'P/E against its 5-year median' },
    }),
    numeric('pfcf_vs_median', 'P/FCF vs 5Y', 'P/FCF vs 5Y median', signed, {
      hint: { description: 'P/FCF against its 5-year median' },
    }),
    numeric('ps_vs_median', 'P/S vs 5Y', 'P/S vs 5Y median', signed, {
      hint: { description: 'P/S against its 5-year median' },
    }),
    numeric('revenue_cagr_5y', 'Rev 5Y', 'Revenue 5Y growth', signed, {
      tone: (row) => growthTone(row.revenue_cagr_5y),
      hint: { description: 'Revenue, 5-year CAGR' },
    }),
    numeric('eps_cagr_5y', 'EPS 5Y', 'EPS 5Y growth', signed, {
      tone: (row) => growthTone(row.eps_cagr_5y),
      hint: { description: 'Diluted EPS, 5-year CAGR' },
    }),
    numeric('fcf_margin', 'FCF margin', 'FCF margin', percent),
    numeric('roe', 'ROE', 'Return on equity', percent),
    numeric('dividend_yield', 'Yield', 'Dividend yield', (v) =>
      v == null ? ABSENT : formatFractionPercent(v, 2)),
    numeric('raise_years', 'Streak', 'Raise streak', (v) => (v == null ? ABSENT : `${v}y`), {
      cellClassName: 'text-muted-foreground',
      hint: { description: 'Years in a row raised' },
    }),
  ]
}

/**
 * Every company the tab lists in one sortable table: today's multiples against the company's
 * own history first, growth, quality and dividends after. Collapsed: it asks the server only
 * once opened, and it opens on the cheapest against their own 5-year P/E median.
 */
export function CompanyScreenerCard() {
  const [open, setOpen] = useState(false)
  const contentId = useId()
  const screener = useQuery({ ...companyScreenerQuery, enabled: open })
  const [shown, setShown] = useState<Shown>('all')
  const [sortKey, setSortKey] = useState<ScreenerSortKey>('pe_vs_median')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const columns = useMemo(() => screenerColumns(), [])

  const onSort = (key: ScreenerSortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir(key === 'symbol' ? 'asc' : 'desc')
    }
  }

  const rows = useMemo(() => {
    const served = (screener.data ?? []).filter((row) => shown === 'all' || row.held)
    const value = (row: CompanyScreenerRow) => (sortKey === 'symbol' ? row.symbol : figure(row, sortKey))
    return [...served].sort(
      (a, b) => compareNullsLast(value(a), value(b), sortDir) || a.symbol.localeCompare(b.symbol),
    )
  }, [screener.data, shown, sortKey, sortDir])

  return (
    <section aria-label="Screener" className="rounded-xl border border-border/70 bg-card">
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
        <IconChip icon={ListFilter} />
        <span className="text-sm font-semibold">Screener</span>
        {screener.data && (
          <span className="text-xs tabular-nums text-muted-foreground">{screener.data.length} companies</span>
        )}
      </button>
      {open && (
        <div id={contentId} className="space-y-3 px-4 pb-3" data-testid="company-screener">
          <SegmentedControl label="Show" value={shown} onChange={setShown} options={SHOWN_OPTIONS} />
          {screener.isError ? (
            <p className="py-4 text-sm text-muted-foreground">Couldn't load the screener.</p>
          ) : !screener.data ? (
            <div className="h-24 animate-pulse rounded-lg bg-muted" />
          ) : rows.length === 0 ? (
            <p className="py-4 text-sm text-muted-foreground">No company to show.</p>
          ) : (
            <DataTable
              rows={rows}
              columns={columns}
              getRowKey={(row) => row.cik}
              label="Screener table"
              density="normal"
              minWidthClassName="min-w-[64rem]"
              pinFirstColumn
              sort={{ column: sortKey, direction: sortDir, onSort }}
            />
          )}
        </div>
      )}
    </section>
  )
}
