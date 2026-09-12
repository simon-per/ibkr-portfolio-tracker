import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { getRatingScore } from '@/lib/analystRating'
import { DataTable } from '@/components/ui/DataTable'
import { watchlistColumns, type WatchlistSortColumn } from './watchlistColumns'
import type { WatchlistItem } from '@/lib/api'
import { RefreshCw, Plus, Trash2, Pencil } from 'lucide-react'

type SortColumn = WatchlistSortColumn
type SortDirection = 'asc' | 'desc'

export function WatchlistTab() {
  const queryClient = useQueryClient()
  const formatBase = useFormatCurrency()
  // Per-row data_currency via the shared formatter's override; a row with no
  // currency falls back to the base currency rather than a hardcoded USD.
  const formatCurrency = (value: number | null, currency: string | null): string =>
    value === null ? '-' : formatBase(value, currency ?? undefined)
  const [tickerInput, setTickerInput] = useState('')
  const [sortColumn, setSortColumn] = useState<SortColumn>('buy_score')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')
  // `isError` captured, not dropped. Without it the only non-loading fallback was the
  // empty state, so a backend that is redeploying or 502-ing rendered "No stocks on your
  // watchlist yet" — a confident claim about the user's own data manufactured out of an
  // outage, followed by a call to action whose POST would also fail. CLAUDE.md states
  // this rule as spanning every panel: `undefined` means *not loaded*, an empty array
  // means *nothing held*, and collapsing the two lets a panel build an answer out of a
  // failure. `FundamentalsTab` one tab over captures it on all three of its queries.
  const { data: items, isLoading, isError } = useQuery({
    queryKey: ['watchlist'],
    queryFn: () => api.getWatchlist(),
    staleTime: 5 * 60 * 1000,
  })

  const addMutation = useMutation({
    mutationFn: (ticker: string) => api.addToWatchlist(ticker),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
      setTickerInput('')
    },
  })

  const removeMutation = useMutation({
    mutationFn: (id: number) => api.removeFromWatchlist(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
    },
  })

  const syncMutation = useMutation({
    mutationFn: () => api.syncWatchlist(true),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
    },
  })

  // Inline editing state
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editNotes, setEditNotes] = useState('')
  const [editTargetPrice, setEditTargetPrice] = useState('')

  const updateMutation = useMutation({
    mutationFn: ({ id, notes, targetPrice }: { id: number; notes: string; targetPrice: number | undefined }) =>
      api.updateWatchlistItem(id, notes || undefined, targetPrice),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
      setEditingId(null)
    },
  })

  const handleAdd = (e: React.FormEvent) => {
    e.preventDefault()
    const ticker = tickerInput.trim().toUpperCase()
    if (ticker) {
      addMutation.mutate(ticker)
    }
  }

  const handleSort = (column: SortColumn) => {
    if (sortColumn === column) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc')
    } else {
      setSortColumn(column)
      setSortDirection('desc')
    }
  }

  const sortedItems = useMemo(() => {
    if (!items) return []
    return [...items].sort((a, b) => {
      const aVal = a[sortColumn]
      const bVal = b[sortColumn]
      if (aVal === null && bVal === null) return 0
      if (aVal === null) return 1
      if (bVal === null) return -1
      // A rating is an enum, not a word: `localeCompare` ranked `strong_sell` above
      // `strong_buy` because "se" sorts after "bu", so descending led with the worst.
      // Scored on the shared conviction scale instead, so descending means strongest
      // first — the order the positions table gives the same ratings.
      if (sortColumn === 'analyst_rating') {
        const diff = getRatingScore(aVal as string) - getRatingScore(bVal as string)
        return sortDirection === 'asc' ? diff : -diff
      }
      if (typeof aVal === 'string' && typeof bVal === 'string') {
        return sortDirection === 'asc'
          ? aVal.localeCompare(bVal)
          : bVal.localeCompare(aVal)
      }
      return sortDirection === 'asc'
        ? (aVal as number) - (bVal as number)
        : (bVal as number) - (aVal as number)
    })
  }, [items, sortColumn, sortDirection])

  const columns = useMemo(() => watchlistColumns({ formatCurrency }), [formatCurrency])

  /**
   * Edit and delete, as one render function used by both the table and the cards —
   * so the popover, its state and the accessible names cannot diverge between them.
   */
  const rowActions = (item: WatchlistItem) => (
    <div className="flex items-center gap-0.5">
      <Popover
        open={editingId === item.id}
        onOpenChange={(open) => {
          if (open) {
            setEditNotes(item.notes || '')
            setEditTargetPrice(item.target_price !== null ? String(item.target_price) : '')
            setEditingId(item.id)
          } else {
            setEditingId(null)
          }
        }}
      >
        <PopoverTrigger asChild>
          {/* 28px was under any touch guideline, and the icon carried no accessible
              name at all. */}
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Edit notes and target price for ${item.symbol || item.yahoo_ticker}`}
            className="h-9 w-9 p-0 text-muted-foreground hover:text-foreground"
          >
            <Pencil className="h-4 w-4" />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-72" align="end">
          <div className="space-y-3">
            <div className="text-sm font-medium">{item.symbol || item.yahoo_ticker}</div>
            <div className="space-y-1.5">
              <label htmlFor={`wl-notes-${item.id}`} className="text-xs text-muted-foreground">
                Notes
              </label>
              <textarea
                id={`wl-notes-${item.id}`}
                rows={3}
                value={editNotes}
                onChange={(e) => setEditNotes(e.target.value)}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm resize-none"
                placeholder="Add notes..."
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor={`wl-tp-${item.id}`} className="text-xs text-muted-foreground">
                Target Price {item.data_currency ? `(${item.data_currency})` : ''}
              </label>
              <input
                id={`wl-tp-${item.id}`}
                type="number"
                step="0.01"
                value={editTargetPrice}
                onChange={(e) => setEditTargetPrice(e.target.value)}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                placeholder="0.00"
              />
            </div>
            {updateMutation.isError && (
              <p className="text-xs text-red-600 dark:text-red-400">Failed to save. Please try again.</p>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => setEditingId(null)}>
                Cancel
              </Button>
              <Button
                size="sm"
                disabled={updateMutation.isPending}
                onClick={() => {
                  const tp = editTargetPrice.trim() ? parseFloat(editTargetPrice) : undefined
                  updateMutation.mutate({ id: item.id, notes: editNotes, targetPrice: tp })
                }}
              >
                {updateMutation.isPending ? 'Saving...' : 'Save'}
              </Button>
            </div>
          </div>
        </PopoverContent>
      </Popover>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => removeMutation.mutate(item.id)}
        disabled={removeMutation.isPending}
        aria-label={`Remove ${item.symbol || item.yahoo_ticker} from the watchlist`}
        className="h-9 w-9 p-0 text-muted-foreground hover:text-red-600"
      >
        <Trash2 className="h-4 w-4" />
      </Button>
    </div>
  )

  return (
    <div className="space-y-6">
      {/* Add Stock + Sync Controls */}
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle>Watchlist</CardTitle>
            <Button
              variant="outline"
              size="sm"
              onClick={() => syncMutation.mutate()}
              disabled={syncMutation.isPending}
            >
              <RefreshCw className={`h-4 w-4 mr-2 ${syncMutation.isPending ? 'animate-spin' : ''}`} />
              {syncMutation.isPending ? 'Syncing...' : 'Refresh All'}
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleAdd} className="flex gap-2">
            <label htmlFor="watchlist-ticker" className="sr-only">
              Yahoo Finance ticker
            </label>
            <input
              id="watchlist-ticker"
              type="text"
              value={tickerInput}
              onChange={(e) => setTickerInput(e.target.value)}
              placeholder="Ticker, e.g. NVDA"
              className="min-w-0 flex-1 px-3 py-2 rounded-md border border-input bg-background text-sm"
            />
            <Button type="submit" disabled={addMutation.isPending || !tickerInput.trim()} size="sm">
              <Plus className="h-4 w-4 mr-1" />
              {addMutation.isPending ? 'Adding...' : 'Add'}
            </Button>
          </form>
          {/* The suffix rule was in the placeholder, where it was clipped mid-word at
              390px and — worse everywhere — vanished the moment you started typing,
              which is exactly when it matters. */}
          <p className="mt-2 text-xs text-muted-foreground">
            Yahoo Finance ticker. Non-US listings carry a suffix, e.g. <code>ASML.AS</code>.
          </p>

          {addMutation.isError && (
            <p className="text-sm text-red-600 dark:text-red-400 mt-2">
              {addMutation.error.message}
            </p>
          )}
          {syncMutation.isError && (
            <p className="text-sm text-red-600 dark:text-red-400 mt-2">
              Failed to sync watchlist. Please try again.
            </p>
          )}
          {syncMutation.isSuccess && (
            <p className="text-sm text-green-600 dark:text-green-400 mt-2">
              {syncMutation.data.message}
            </p>
          )}
        </CardContent>
      </Card>

      {/* Watchlist Table */}
      {isLoading ? (
        <Card>
          <CardContent className="py-8 text-center text-muted-foreground">Loading watchlist...</CardContent>
        </Card>
      ) : isError ? (
        <Card>
          <CardContent className="py-8 text-center text-muted-foreground">
            Couldn't load your watchlist — the backend didn't respond. It retries
            automatically. This is not an empty watchlist.
          </CardContent>
        </Card>
      ) : !sortedItems.length ? (
        <Card>
          <CardContent className="py-8 text-center text-muted-foreground">
            No stocks on your watchlist yet. Add a ticker above to get started.
          </CardContent>
        </Card>
      ) : (
        <Card>
          {removeMutation.isError && (
            <div className="px-4 pt-3">
              <p className="text-sm text-red-600 dark:text-red-400">Failed to remove item. Please try again.</p>
            </div>
          )}
          {/* `max-sm:px-4` restores the gutter the card list needs: `p-0` exists so
              the desktop table can bleed to the card edge, which a card list must not. */}
          <CardContent className="p-0 max-sm:px-4 max-sm:py-2">
            <DataTable
              rows={sortedItems}
              columns={columns}
              getRowKey={(i) => i.id}
              label="Watchlist table"
              density="roomy"
              minWidthClassName="min-w-[64rem]"
              // Four visible details, twelve behind the disclosure. Hiding them outright
              // would hide the data the watchlist is for; a collapsed card is ~120px, so
              // thirty rows is one honest vertical scroll instead of four sideways ones.
              detailLimit={4}
              sort={{ column: sortColumn, direction: sortDirection, onSort: handleSort }}
              rowActions={rowActions}
            />
          </CardContent>
        </Card>
      )}

    </div>
  )
}
