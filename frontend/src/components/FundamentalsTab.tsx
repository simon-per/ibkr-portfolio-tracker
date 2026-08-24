import { useState, useMemo, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { formatMarketCap } from '@/lib/utils'
import { api } from '@/lib/api'
import type { FundamentalMetrics, EarningsCalendarItem, EarningsHistoryItem } from '@/lib/api'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { useIsSideBySide } from '@/lib/useMediaQuery'
import { RefreshCw } from 'lucide-react'

type SortKey = 'symbol' | 'trailing_pe' | 'forward_pe' | 'peg_ratio' | 'price_to_sales' |
  'revenue_growth' | 'earnings_growth' | 'fwd_revenue_growth' | 'fwd_eps_growth' | 'profit_margins' | 'market_cap'
type SortDir = 'asc' | 'desc'

function formatPct(value: number | null): string {
  if (value === null) return '-'
  return `${(value * 100).toFixed(1)}%`
}

function formatRatio(value: number | null): string {
  if (value === null) return '-'
  return value.toFixed(1)
}

// Color coding helpers
function peColor(value: number | null): string {
  if (value === null) return ''
  if (value < 15) return 'text-green-600 dark:text-green-400'
  if (value < 25) return 'text-yellow-600 dark:text-yellow-400'
  return 'text-red-600 dark:text-red-400'
}

function pegColor(value: number | null): string {
  if (value === null) return ''
  if (value < 1) return 'text-green-600 dark:text-green-400'
  if (value < 2) return 'text-yellow-600 dark:text-yellow-400'
  return 'text-red-600 dark:text-red-400'
}

function growthColor(value: number | null): string {
  if (value === null) return ''
  if (value > 0.15) return 'text-green-600 dark:text-green-400'
  if (value > 0) return 'text-yellow-600 dark:text-yellow-400'
  return 'text-red-600 dark:text-red-400'
}

function marginColor(value: number | null): string {
  if (value === null) return ''
  if (value > 0.20) return 'text-green-600 dark:text-green-400'
  if (value > 0.10) return 'text-yellow-600 dark:text-yellow-400'
  return 'text-red-600 dark:text-red-400'
}

function surpriseColor(result: string | null): string {
  if (result === 'Beat') return 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300'
  if (result === 'Miss') return 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300'
  if (result === 'Met') return 'bg-yellow-100 dark:bg-yellow-900/40 text-yellow-700 dark:text-yellow-300'
  return 'bg-muted text-muted-foreground'
}

// Sort comparator that handles nulls (nulls last)
function compareValues(a: number | string | null, b: number | string | null, dir: SortDir): number {
  if (a === null && b === null) return 0
  if (a === null) return 1
  if (b === null) return -1
  if (a < b) return dir === 'asc' ? -1 : 1
  if (a > b) return dir === 'asc' ? 1 : -1
  return 0
}

function EtfSection({ etfs }: { etfs: FundamentalMetrics[] }) {
  if (etfs.length === 0) return null
  return (
    <div className="mt-6 pt-4 border-t">
      <h3 className="text-sm font-medium text-muted-foreground mb-3">
        ETF Holdings ({etfs.length}) — fundamental metrics not applicable
      </h3>
      <div className="flex flex-wrap gap-2">
        {etfs.map((etf) => (
          <div
            key={etf.security_id}
            className="inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm bg-muted/30"
          >
            <span className="text-xs bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 px-1.5 py-0.5 rounded font-medium">
              ETF
            </span>
            <span className="font-medium">{etf.symbol}</span>
            <span className="text-xs text-muted-foreground truncate max-w-[160px]">{etf.description}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * The fundamentals metrics, described once for both renderings.
 *
 * Nothing here is a "headline" figure the way a market value is, so market cap takes
 * the card's value slot and there is no delta — the shape DataTable has to tolerate
 * rather than fake.
 *
 * The five `title=` explanations that used to sit on the headers are `hint`s now, so
 * they reach the phone through the glossary instead of dying with the hover.
 */
function metricsColumns(): Column<FundamentalMetrics, SortKey>[] {
  const peg = (row: FundamentalMetrics) =>
    row.peg_ratio ??
    (row.trailing_pe && row.fwd_eps_growth && row.fwd_eps_growth > 0
      ? row.trailing_pe / (row.fwd_eps_growth * 100)
      : null)

  return [
    {
      key: 'symbol',
      header: 'Symbol',
      shortHeader: 'Symbol',
      sortKey: 'symbol',
      mobile: 'title',
      cell: (row, view) =>
        view === 'table' ? (
          <>
            <div className="font-medium">{row.symbol}</div>
            <div className="text-xs text-muted-foreground truncate max-w-[180px]">{row.description}</div>
          </>
        ) : (
          row.symbol
        ),
    },
    {
      key: 'description',
      header: 'Description',
      shortHeader: 'Description',
      desktop: 'hide',
      mobile: 'meta',
      cell: (row) => row.description,
    },
    {
      key: 'market_cap',
      header: 'Mkt Cap',
      shortHeader: 'Market cap',
      sortKey: 'market_cap',
      align: 'right',
      mobile: 'value',
      cell: (row) => (
        <>
          {formatMarketCap(row.market_cap)}
          {row.data_currency ? (
            <span className="text-muted-foreground text-xs ml-1">{row.data_currency}</span>
          ) : (
            ''
          )}
        </>
      ),
    },
    {
      key: 'trailing_pe',
      header: 'P/E',
      shortHeader: 'P/E',
      sortKey: 'trailing_pe',
      align: 'right',
      tone: (row) => peColor(row.trailing_pe),
      cell: (row) => formatRatio(row.trailing_pe),
    },
    {
      key: 'forward_pe',
      header: 'Fwd P/E',
      shortHeader: 'Fwd P/E',
      sortKey: 'forward_pe',
      align: 'right',
      tone: (row) => peColor(row.forward_pe),
      cell: (row) => formatRatio(row.forward_pe),
    },
    {
      key: 'peg_ratio',
      header: 'PEG',
      shortHeader: 'PEG',
      sortKey: 'peg_ratio',
      align: 'right',
      tone: (row) => pegColor(peg(row)),
      hint: {
        description:
          'P/E divided by forward EPS growth rate (analyst consensus next-12M estimate). Below 1 = potentially undervalued.',
      },
      cell: (row) => formatRatio(peg(row)),
    },
    {
      key: 'price_to_sales',
      header: 'P/S',
      shortHeader: 'P/S',
      sortKey: 'price_to_sales',
      align: 'right',
      cell: (row) => formatRatio(row.price_to_sales),
    },
    {
      key: 'revenue_growth',
      header: 'Rev Growth',
      shortHeader: 'Revenue growth',
      sortKey: 'revenue_growth',
      align: 'right',
      tone: (row) => growthColor(row.revenue_growth),
      hint: {
        description:
          'TTM (trailing twelve months) revenue growth: sum of 4 most recent quarters vs prior 4 quarters. Falls back to Yahoo Finance revenueGrowth if fewer than 8 quarters available.',
      },
      cell: (row) => formatPct(row.revenue_growth),
    },
    {
      key: 'earnings_growth',
      header: 'EPS Growth',
      shortHeader: 'EPS growth',
      sortKey: 'earnings_growth',
      align: 'right',
      tone: (row) => growthColor(row.earnings_growth),
      hint: {
        description:
          "TTM (trailing twelve months) EPS growth: sum of 4 most recent quarters vs prior 4 quarters. Falls back to Yahoo Finance earningsGrowth if fewer than 8 quarters available. Note: PEG uses Yahoo's separate 5-year analyst estimate, not this figure.",
      },
      cell: (row) => formatPct(row.earnings_growth),
    },
    {
      key: 'fwd_revenue_growth',
      header: 'Fwd Rev',
      shortHeader: 'Fwd revenue growth',
      sortKey: 'fwd_revenue_growth',
      align: 'right',
      tone: (row) => growthColor(row.fwd_revenue_growth),
      hint: { description: 'Next fiscal year revenue growth analyst consensus estimate.' },
      cell: (row) => formatPct(row.fwd_revenue_growth),
    },
    {
      key: 'fwd_eps_growth',
      header: 'Fwd EPS',
      shortHeader: 'Fwd EPS growth',
      sortKey: 'fwd_eps_growth',
      align: 'right',
      tone: (row) => growthColor(row.fwd_eps_growth),
      hint: { description: 'Next fiscal year EPS growth analyst consensus estimate.' },
      cell: (row) => formatPct(row.fwd_eps_growth),
    },
    {
      key: 'profit_margins',
      header: 'Margins',
      shortHeader: 'Margins',
      sortKey: 'profit_margins',
      align: 'right',
      tone: (row) => marginColor(row.profit_margins),
      cell: (row) => formatPct(row.profit_margins),
    },
  ]
}

function MetricsTable({ data }: { data: FundamentalMetrics[] }) {
  const [sortKey, setSortKey] = useState<SortKey>('symbol')
  const [sortDir, setSortDir] = useState<SortDir>('asc')

  const stocks = useMemo(() => data.filter((d) => d.quote_type !== 'ETF'), [data])
  const etfs = useMemo(() => data.filter((d) => d.quote_type === 'ETF'), [data])

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir(key === 'symbol' ? 'asc' : 'desc')
    }
  }

  const sorted = useMemo(() => {
    return [...stocks].sort((a, b) => compareValues(a[sortKey], b[sortKey], sortDir))
  }, [stocks, sortKey, sortDir])

  const columns = useMemo(() => metricsColumns(), [])

  return (
    <>
      <DataTable
        rows={sorted}
        columns={columns}
        getRowKey={(row) => row.security_id}
        label="Fundamentals table"
        density="normal"
        minWidthClassName="min-w-[52rem]"
        sort={{ column: sortKey, direction: sortDir, onSort: handleSort }}
      />
      <EtfSection etfs={etfs} />
    </>
  )
}

/** Ten cards is a page you can thumb through; the measured fit is a desktop nicety. */
const DEFAULT_HISTORY_PAGE_SIZE = 10

/** Row and chrome heights of the DESKTOP table, in px. Only valid for that rendering. */
const HISTORY_ROW_H = 37
const HISTORY_THEAD_H = 37
const HISTORY_PAGINATION_H = 48
const HISTORY_CONTENT_PAD = 24

function EarningsCalendar({ data }: { data: EarningsCalendarItem[] }) {
  const PAGE_SIZE = 10
  const [page, setPage] = useState(0)

  // Reset to first page when data changes
  useEffect(() => { setPage(0) }, [data])

  if (data.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-4 text-center">
        No upcoming earnings dates found.
      </p>
    )
  }

  // Group by date
  const grouped = data.reduce<Record<string, EarningsCalendarItem[]>>((acc, item) => {
    const dateStr = new Date(item.earnings_date).toLocaleDateString('en-US', {
      weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
    })
    if (!acc[dateStr]) acc[dateStr] = []
    acc[dateStr].push(item)
    return acc
  }, {})

  const entries = Object.entries(grouped)
  const totalPages = Math.ceil(entries.length / PAGE_SIZE)
  const pageEntries = entries.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  return (
    <div className="flex flex-col flex-1">
      <div className="flex-1 space-y-3">
        {pageEntries.map(([dateStr, items]) => (
          <div key={dateStr}>
            <h4 className="text-sm font-semibold text-muted-foreground mb-1">{dateStr}</h4>
            <div className="flex flex-wrap gap-2">
              {items.map((item) => (
                <div
                  key={`${item.security_id}-${item.earnings_date}`}
                  className="inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm"
                >
                  <span className="font-medium">{item.symbol}</span>
                  {item.eps_estimate !== null && (
                    <span className="text-muted-foreground">
                      Est: ${item.eps_estimate.toFixed(2)}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm text-muted-foreground mt-auto pt-3">
          <Button variant="ghost" size="sm" onClick={() => setPage(p => p - 1)} disabled={page === 0}>
            ← Prev
          </Button>
          <span>Page {page + 1} of {totalPages}</span>
          <Button variant="ghost" size="sm" onClick={() => setPage(p => p + 1)} disabled={page >= totalPages - 1}>
            Next →
          </Button>
        </div>
      )}
    </div>
  )
}

/**
 * The earnings-history columns. A module constant rather than a factory: nothing here
 * closes over a hook, which is exactly why it can be one.
 */
const EARNINGS_COLUMNS: Column<EarningsHistoryItem>[] = [
  {
    key: 'symbol',
    header: 'Symbol',
    shortHeader: 'Symbol',
    mobile: 'title',
    cellClassName: 'font-medium',
    cell: (row) => row.symbol,
  },
  {
    key: 'date',
    header: 'Date',
    shortHeader: 'Date',
    mobile: 'meta',
    cellClassName: 'text-muted-foreground',
    cell: (row) =>
      new Date(row.earnings_date).toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      }),
  },
  {
    key: 'surprise',
    header: 'Surprise',
    shortHeader: 'Surprise',
    align: 'right',
    mobile: 'value',
    cell: (row) => (row.surprise_percent !== null ? `${row.surprise_percent.toFixed(1)}%` : '-'),
  },
  {
    key: 'result',
    header: 'Result',
    shortHeader: 'Result',
    align: 'center',
    mobile: 'badge',
    cell: (row) =>
      row.beat_or_miss ? (
        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${surpriseColor(row.beat_or_miss)}`}>
          {row.beat_or_miss}
        </span>
      ) : null,
  },
  {
    key: 'eps_estimate',
    header: 'EPS Est.',
    shortHeader: 'EPS Est.',
    align: 'right',
    cell: (row) => (row.eps_estimate !== null ? `$${row.eps_estimate.toFixed(2)}` : '-'),
  },
  {
    key: 'reported_eps',
    header: 'Reported',
    shortHeader: 'Reported',
    align: 'right',
    cell: (row) => (row.reported_eps !== null ? `$${row.reported_eps.toFixed(2)}` : '-'),
  },
]

function EarningsSurpriseHistory({ data, pageSize = 10 }: { data: EarningsHistoryItem[], pageSize?: number }) {
  const [page, setPage] = useState(0)
  useEffect(() => { setPage(0) }, [pageSize])
  const totalPages = Math.ceil(data.length / pageSize)
  const pageData = data.slice(page * pageSize, (page + 1) * pageSize)

  if (data.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-4 text-center">
        No earnings history found. Sync fundamentals data first.
      </p>
    )
  }

  return (
    <div className="flex flex-col flex-1">
      <div className="flex-1">
        <DataTable
          rows={pageData}
          columns={EARNINGS_COLUMNS}
          getRowKey={(row, idx) => `${row.security_id}-${row.earnings_date}-${idx}`}
          label="Earnings surprise history"
          density="normal"
        />
      </div>
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm text-muted-foreground mt-auto pt-3">
          <Button variant="ghost" size="sm" onClick={() => setPage(p => p - 1)} disabled={page === 0}>
            ← Prev
          </Button>
          <span>Page {page + 1} of {totalPages}</span>
          <Button variant="ghost" size="sm" onClick={() => setPage(p => p + 1)} disabled={page >= totalPages - 1}>
            Next →
          </Button>
        </div>
      )}
    </div>
  )
}

export function FundamentalsTab() {
  const queryClient = useQueryClient()
  const [syncStartedAt, setSyncStartedAt] = useState<Date | null>(null)
  const isSyncing = syncStartedAt !== null

  // Each query captures isError: every empty state on this tab used to read as "no
  // data yet — click Sync Now", which is a statement about the *portfolio*. When the
  // backend is down it is a statement about the backend, and telling a user to trigger
  // a sync that cannot succeed is worse than saying nothing.
  const { data: fundamentals, isLoading: metricsLoading, isError: metricsError } = useQuery({
    queryKey: ['fundamentals', 'portfolio'],
    queryFn: () => api.getPortfolioFundamentals(),
  })

  const { data: upcomingEarnings, isLoading: earningsLoading, isError: earningsError } = useQuery({
    queryKey: ['fundamentals', 'earnings', 'upcoming'],
    queryFn: () => api.getUpcomingEarnings(90),
  })

  const { data: earningsHistory, isLoading: historyLoading, isError: historyError } = useQuery({
    queryKey: ['fundamentals', 'earnings', 'history'],
    queryFn: () => api.getEarningsHistory(365),
  })

  const { data: status } = useQuery({
    queryKey: ['fundamentals', 'status'],
    queryFn: () => api.getFundamentalsStatus(),
    refetchInterval: isSyncing ? 5000 : false,
  })

  // Detect when background sync completes: newest_update advances past syncStartedAt
  useEffect(() => {
    if (!isSyncing || !status || !syncStartedAt) return
    const newestUpdate = status.newest_update ? new Date(status.newest_update) : null
    if (newestUpdate && newestUpdate > syncStartedAt) {
      setSyncStartedAt(null)
      queryClient.invalidateQueries({ queryKey: ['fundamentals'] })
    }
  }, [status, isSyncing, syncStartedAt, queryClient])

  const handleSyncSuccess = (data: { status: string }) => {
    if (data.status === 'started') {
      setSyncStartedAt(new Date())
    } else {
      queryClient.invalidateQueries({ queryKey: ['fundamentals'] })
    }
  }

  const syncMutation = useMutation({
    mutationFn: () => api.syncFundamentals(false),
    onSuccess: handleSyncSuccess,
  })

  const forceRefreshMutation = useMutation({
    mutationFn: () => api.syncFundamentals(true),
    onSuccess: handleSyncSuccess,
  })

  const leftCardRef = useRef<HTMLDivElement>(null)
  const rightHeaderRef = useRef<HTMLDivElement>(null)
  const [historyPageSize, setHistoryPageSize] = useState(DEFAULT_HISTORY_PAGE_SIZE)

  // The earnings grid is `lg:grid-cols-2`, so the two cards only sit side by side at
  // >=1024px. Below that they stack and the left card's height says nothing about the
  // space this one has — the page size became whatever Upcoming Earnings happened to
  // be tall, which on a phone is unrelated to anything. The row heights below are the
  // DESKTOP table's, and below `sm` the rows are cards roughly twice as tall, so the
  // arithmetic was wrong twice over there.
  const sideBySide = useIsSideBySide()

  useEffect(() => {
    if (!sideBySide) {
      setHistoryPageSize(DEFAULT_HISTORY_PAGE_SIZE)
      return
    }
    const leftCard = leftCardRef.current
    if (!leftCard) return
    const update = () => {
      const leftH = leftCard.offsetHeight
      const rightHeaderH = rightHeaderRef.current?.offsetHeight ?? 0
      const available =
        leftH - rightHeaderH - HISTORY_CONTENT_PAD - HISTORY_THEAD_H - HISTORY_PAGINATION_H
      setHistoryPageSize(Math.max(1, Math.floor(available / HISTORY_ROW_H)))
    }
    // Known and deliberately not fixed here: above `lg` the grid stretches both cards
    // to the row height, so a larger page size grows this card, grows the row, grows
    // leftCard.offsetHeight and feeds back in. It is bounded by data.length, but it is
    // a loop waiting for the right data.
    const ro = new ResizeObserver(update)
    ro.observe(leftCard)
    return () => ro.disconnect()
  }, [sideBySide])

  const needsSync = status && status.securities_without_data > 0

  return (
    <div className="space-y-6">
      {/* Sync Status */}
      {needsSync && (
        <Card className="border-yellow-200 dark:border-yellow-800 bg-yellow-50 dark:bg-yellow-950">
          <CardContent className="pt-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-yellow-800 dark:text-yellow-200">
                  Fundamentals data needs updating
                </p>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
                  {status.securities_without_data} securities missing fundamental data
                  {status.stale_metrics > 0 && `, ${status.stale_metrics} stale`}
                </p>
              </div>
              <Button
                onClick={() => syncMutation.mutate()}
                disabled={syncMutation.isPending || isSyncing}
                variant="outline"
              >
                {syncMutation.isPending || isSyncing ? (
                  <>
                    <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                    Syncing...
                  </>
                ) : (
                  <>
                    <RefreshCw className="mr-2 h-4 w-4" />
                    Sync Now
                  </>
                )}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {isSyncing && (
        <Card className="border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950">
          <CardContent className="pt-6">
            <div className="flex items-center gap-2">
              <RefreshCw className="h-4 w-4 animate-spin text-blue-600 dark:text-blue-400" />
              <p className="text-sm text-blue-800 dark:text-blue-200">
                Sync running in background — data will update automatically when complete.
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {syncMutation.isError && (
        <Card className="border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950">
          <CardContent className="pt-6">
            <p className="text-sm text-red-800 dark:text-red-200">
              Sync failed: {syncMutation.error.message}
            </p>
          </CardContent>
        </Card>
      )}

      {/* Growth Metrics Table */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-2xl">Growth Metrics</CardTitle>
              <CardDescription>
                Valuation ratios, growth rates, and margins for all holdings
              </CardDescription>
            </div>
            {!needsSync && (
              <Button
                onClick={() => forceRefreshMutation.mutate()}
                disabled={forceRefreshMutation.isPending || isSyncing}
                variant="outline"
                size="sm"
              >
                {forceRefreshMutation.isPending || isSyncing ? (
                  <>
                    <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                    Syncing...
                  </>
                ) : (
                  <>
                    <RefreshCw className="mr-2 h-4 w-4" />
                    Refresh
                  </>
                )}
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent>
          {metricsLoading ? (
            <div className="flex items-center justify-center h-40 text-muted-foreground">Loading...</div>
          ) : metricsError ? (
            <div className="flex items-center justify-center h-40 text-center text-muted-foreground">
              Couldn't load fundamentals — the backend didn't respond. It retries automatically.
            </div>
          ) : fundamentals && fundamentals.length > 0 ? (
            <MetricsTable data={fundamentals} />
          ) : (
            <div className="flex items-center justify-center h-40 text-muted-foreground">
              No data available. Click "Sync Now" to fetch fundamental data.
            </div>
          )}
        </CardContent>
      </Card>

      {/* Earnings Section - Side by Side */}
      {/* `[&>*]:min-w-0` because a grid item defaults to `min-width: auto`, so it
          refuses to shrink below its content's min-content width — one wide table
          inside made this single-column track 392px in a 358px page and pushed the
          whole document sideways. The track, not the card, is what has to yield. */}
      <div className="grid gap-6 [&>*]:min-w-0 lg:grid-cols-2">
        {/* Upcoming Earnings */}
        <Card ref={leftCardRef} className="flex flex-col">
          <CardHeader>
            <CardTitle>Upcoming Earnings</CardTitle>
            <CardDescription>Next 90 days of earnings reports</CardDescription>
          </CardHeader>
          <CardContent className="flex-1 flex flex-col">
            {earningsLoading ? (
              <div className="flex items-center justify-center h-20 text-muted-foreground">Loading...</div>
            ) : earningsError ? (
              <div className="flex items-center justify-center h-20 text-center text-muted-foreground">
                Couldn't load upcoming earnings — the backend didn't respond.
              </div>
            ) : (
              <EarningsCalendar data={upcomingEarnings || []} />
            )}
          </CardContent>
        </Card>

        {/* Earnings Surprise History */}
        <Card className="flex flex-col">
          <CardHeader ref={rightHeaderRef}>
            <CardTitle>Earnings Surprise History</CardTitle>
            <CardDescription>Past year of earnings beats and misses</CardDescription>
          </CardHeader>
          <CardContent className="flex-1 flex flex-col">
            {historyLoading ? (
              <div className="flex items-center justify-center h-20 text-muted-foreground">Loading...</div>
            ) : historyError ? (
              <div className="flex items-center justify-center h-20 text-center text-muted-foreground">
                Couldn't load earnings history — the backend didn't respond.
              </div>
            ) : (
              <EarningsSurpriseHistory data={earningsHistory || []} pageSize={historyPageSize} />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
