import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from 'recharts'
import { api } from '@/lib/api'
import { positionsQuery, summaryQuery } from '@/lib/queryCache'
import { cashIsTracked, summaryCash } from '@/lib/portfolioCash'
import type { AllocationCategory } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { Blocks, Globe, RefreshCw, Shapes, X, type LucideIcon } from 'lucide-react'
import { RebalanceCard } from './RebalanceCard'
import { CurrencyExposureCard } from './CurrencyExposureCard'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import { CompanyLink } from './CompanyLink'
import { SectionHeader } from '@/components/ui/SectionHeader'
import {
  allocationSectorPaint, assetTypePaint, regionPaint, type SectorPaint,
} from '@/lib/sectorColors'
import { TONE_CHIP } from '@/lib/tone'
import { displayName } from '@/lib/securityName'

/**
 * Colours come from `lib/sectorColors.ts` — palette slots by a fixed identity order per
 * breakdown, shared with the look-through treemap so one sector is one colour across the app.
 *
 * The buckets arrive final. Since 2026-11-08 the server rolls Sectors and Regions up from the
 * look-through's decomposition, already in the canonical sector names (`sector_taxonomy`) and
 * regions (`region_taxonomy`), largest first. This file used to rename sectors and fold
 * countries into regions itself — a third copy of both maps, and one that sent every country
 * it did not know to `Other`.
 */

/**
 * The "nothing loaded" allocation, as one module-level object rather than a fresh `{}` per
 * render. `AllocationBreakdown` resets its drill-down whenever its `allocation` prop changes
 * identity, so a literal here would close the panel on every parent re-render.
 */
const NO_ALLOCATION: Record<string, AllocationCategory> = {}

/** The drill-down's five columns, described once for both renderings. */
type DrillDownPosition = AllocationCategory['positions'][number]

/** The holding's logo or monogram, where the row names a security. */
const drillDownAvatar = (pos: DrillDownPosition, className: string) =>
  pos.security_id != null ? (
    <TickerAvatar symbol={pos.symbol} securityId={pos.security_id} className={className} />
  ) : null

export function drillDownColumns(deps: {
  formatCurrency: (v: number) => string
}): Column<AllocationCategory['positions'][number]>[] {
  const { formatCurrency } = deps
  return [
    {
      key: 'symbol',
      header: 'Symbol',
      shortHeader: 'Symbol',
      mobile: 'title',
      cellClassName: 'font-medium',
      avatar: drillDownAvatar,
      cell: (pos, view) =>
        view === 'table' && pos.security_id != null ? (
          <CompanyLink securityId={pos.security_id} className="block">
            <div className="flex items-center gap-2.5">
              {drillDownAvatar(pos, 'h-7 w-7 text-[10px]')}
              {pos.symbol}
            </div>
          </CompanyLink>
        ) : (
          <CompanyLink securityId={pos.security_id}>{pos.symbol}</CompanyLink>
        ),
    },
    {
      key: 'description',
      header: 'Description',
      shortHeader: 'Description',
      mobile: 'meta',
      cellClassName: 'text-muted-foreground max-w-[200px] truncate',
      cell: (pos) => displayName(pos.description),
    },
    {
      key: 'value',
      header: 'Value',
      shortHeader: 'Value',
      align: 'right',
      mobile: 'value',
      cell: (pos) => formatCurrency(pos.market_value_eur),
    },
    {
      key: 'weight',
      header: 'Weight',
      shortHeader: 'Weight',
      align: 'right',
      mobile: 'delta',
      cell: (pos) => `${pos.weight.toFixed(1)}%`,
    },
    {
      key: 'type',
      header: 'Type',
      shortHeader: 'Type',
      align: 'right',
      mobile: 'badge',
      cell: (pos) =>
        pos.is_etf_contribution ? (
          <span className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${TONE_CHIP.neutral}`}>
            ETF est.
          </span>
        ) : null,
    },
  ]
}

// Drill-down panel showing positions within a category
function DrillDownPanel({
  categoryName,
  category,
  fill,
  onClose,
}: {
  categoryName: string
  category: AllocationCategory
  fill: string
  onClose: () => void
}) {
  const formatCurrency = useFormatCurrency()
  return (
    <Card className="mt-4 bg-raised shadow-raised">
      <SectionHeader
        className="pb-3"
        title={
          <span className="flex items-center gap-2">
            <span aria-hidden="true" className="h-2.5 w-2.5 shrink-0 rounded-[3px]" style={{ backgroundColor: fill }} />
            {categoryName}
          </span>
        }
        description={
          <span className="tabular-nums">
            {category.percentage.toFixed(1)}% of portfolio · {formatCurrency(category.market_value_eur)}
          </span>
        }
        aside={
          <Button variant="ghost" size="icon" onClick={onClose} aria-label={`Close ${categoryName}`}>
            <X className="h-4 w-4" />
          </Button>
        }
      />
      <CardContent>
        <DataTable
          rows={category.positions}
          columns={drillDownColumns({ formatCurrency })}
          getRowKey={(pos) => pos.symbol}
          label={`${categoryName} positions`}
          density="normal"
        />
      </CardContent>
    </Card>
  )
}

/**
 * One breakdown: a donut, and beside it the legend that is also the way in — a row or a
 * slice opens the holdings behind that category.
 *
 * A donut rather than the treemap it replaced (2026-10-16): with a legend carrying every
 * share, the chart's job is the proportion at a glance, and a ring does that without the
 * unlabelled slivers a treemap leaves for anything under a few percent.
 */
function AllocationBreakdown({
  title,
  icon,
  allocation,
  colorFor,
  isLoading,
}: {
  title: string
  icon: LucideIcon
  allocation: Record<string, AllocationCategory>
  colorFor: (name: string) => SectorPaint
  isLoading: boolean
}) {
  const formatCurrency = useFormatCurrency()
  const [selected, setSelected] = useState<string | null>(null)

  // Clear the selection when the allocation data changes — adjusted while rendering,
  // React's pattern for state that follows a prop, not in an effect that renders twice.
  const [selectedIn, setSelectedIn] = useState(allocation)
  if (selectedIn !== allocation) {
    setSelectedIn(allocation)
    setSelected(null)
  }

  const entries = Object.entries(allocation).sort(([, a], [, b]) => b.percentage - a.percentage)
  if (isLoading) {
    return (
      <Card>
        <SectionHeader title={title} icon={icon} />
        <CardContent>
          <div className="flex h-64 items-center justify-center text-muted-foreground">Loading…</div>
        </CardContent>
      </Card>
    )
  }

  if (entries.length === 0) {
    return (
      <Card>
        <SectionHeader title={title} icon={icon} />
        <CardContent>
          <div className="flex h-64 items-center justify-center text-muted-foreground">
            No data yet.
          </div>
        </CardContent>
      </Card>
    )
  }

  const slices = entries.map(([name, cat]) => ({
    name,
    value: cat.percentage,
    market_value_eur: cat.market_value_eur,
    fill: colorFor(name).fill,
  }))
  const toggle = (name: string) => setSelected(selected === name ? null : name)
  const [leadName, lead] = entries[0]

  return (
    <Card>
      <SectionHeader title={title} icon={icon} />
      <CardContent>
        <div className="flex flex-col items-center gap-5">
          <div className="relative h-[190px] w-[190px] shrink-0">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={slices}
                  dataKey="value"
                  nameKey="name"
                  innerRadius="68%"
                  outerRadius="100%"
                  paddingAngle={slices.length > 1 ? 1.5 : 0}
                  stroke="none"
                  isAnimationActive={false}
                  onClick={(d: { name?: string }) => d?.name && toggle(d.name)}
                  className="cursor-pointer"
                >
                  {slices.map((s) => (
                    <Cell
                      key={s.name}
                      fill={s.fill}
                      opacity={selected && selected !== s.name ? 0.35 : 1}
                    />
                  ))}
                </Pie>
                <Tooltip
                  content={({ payload }) => {
                    if (!payload || payload.length === 0) return null
                    const d = payload[0].payload
                    return (
                      <div className="rounded-xl border border-border bg-card px-3 py-2 text-sm shadow-raised">
                        <p className="font-semibold">{d.name}</p>
                        <p className="tabular-nums text-muted-foreground">
                          {d.value?.toFixed(1)}% · {formatCurrency(d.market_value_eur)}
                        </p>
                      </div>
                    )
                  }}
                />
              </PieChart>
            </ResponsiveContainer>
            {/* The largest slice, named in the hole. */}
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
              {/* The legend's precision: "37%" in the middle beside "36.5%" in the
                  legend read as two different answers. */}
              <span className="text-2xl font-semibold tabular-nums">{lead.percentage.toFixed(1)}%</span>
              <span className="max-w-[7rem] truncate text-xs text-muted-foreground">{leadName}</span>
            </div>
          </div>

          <div className="w-full min-w-0 space-y-0.5">
            {entries.map(([name, cat]) => (
              <button
                key={name}
                onClick={() => toggle(name)}
                className={`flex min-h-10 w-full items-center gap-2.5 rounded-md px-2 text-left text-sm transition-colors sm:min-h-8 ${
                  selected === name ? 'bg-muted' : 'hover:bg-muted/60'
                }`}
              >
                <span
                  aria-hidden="true"
                  className="h-2.5 w-2.5 shrink-0 rounded-[3px]"
                  style={{ backgroundColor: colorFor(name).fill }}
                />
                <span className="min-w-0 flex-1 truncate">{name}</span>
                <span className="hidden text-xs tabular-nums text-muted-foreground sm:inline">
                  {formatCurrency(cat.market_value_eur)}
                </span>
                <span className="w-14 text-right font-semibold tabular-nums">{cat.percentage.toFixed(1)}%</span>
              </button>
            ))}
          </div>
        </div>

        {/* Drill-down panel */}
        {selected && allocation[selected] && (
          <DrillDownPanel
            categoryName={selected}
            category={allocation[selected]}
            fill={colorFor(selected).fill}
            onClose={() => setSelected(null)}
          />
        )}
      </CardContent>
    </Card>
  )
}

export function AllocationTab() {
  const queryClient = useQueryClient()

  const { data: allocation, isLoading, isError: allocationError } = useQuery({
    queryKey: ['allocation', 'portfolio'],
    queryFn: () => api.getPortfolioAllocation(),
  })

  const { data: status, isError: statusError } = useQuery({
    queryKey: ['allocation', 'status'],
    queryFn: () => api.getAllocationStatus(),
  })

  // The same key the Dashboard uses, so this is a cache hit rather than a
  // second request for data already on the page.
  const { data: positions, isLoading: positionsLoading, isError: positionsError } = useQuery(positionsQuery)
  // The rebalance plan's book total takes the account's cash, as Positions' weights do.
  const { data: summary } = useQuery(summaryQuery)
  const accountCash = useMemo(() => summaryCash(summary), [summary])

  const syncMutation = useMutation({
    mutationFn: () => api.syncAllocationData(false),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['allocation'] })
    },
  })

  const needsSync = status && status.securities_without_data > 0

  // The ETF bucket names the positions whose quote currency is not their economic
  // exposure. Derived from the allocation response already on this page rather
  // than a new field, since `Position` carries no asset type.
  const fundSymbols = useMemo(
    () =>
      new Set(
        Object.entries(allocation?.asset_type_allocation ?? {})
          .filter(([name]) => name.toUpperCase() === 'ETF')
          .flatMap(([, category]) => category.positions.map((p) => p.symbol)),
      ),
    [allocation],
  )

  // Passed through as the query cached them, so each keeps its identity across re-renders:
  // `AllocationBreakdown` resets its drill-down whenever its `allocation` prop changes
  // identity, and a fresh object per render once closed the open panel on every 60-second
  // `scheduler/status` refetch.
  const sectorAllocation = allocation?.sector_allocation ?? NO_ALLOCATION
  const geoAllocation = allocation?.geographic_allocation ?? NO_ALLOCATION
  const assetAllocation = allocation?.asset_type_allocation ?? NO_ALLOCATION

  return (
    <div className="space-y-6">
      {/* A fetch error must not impersonate empty allocation data — the
          treemaps below just render blank when the backend isn't answering. */}
      {(allocationError || statusError) && (
        <Card className="border-loss/30 bg-loss-soft">
          <CardContent className="pt-6">
            <p className="text-sm text-loss">
              Couldn't load allocation data — the server didn't respond. It retries automatically.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Completeness of all three breakdowns below. Above the charts and outside every
          collapsible, the rule MonthlyReturnsHeatmap and PerformanceAttribution both had
          to learn: the figure it qualifies is on screen either way.

          This one hides especially well. Every slice is labelled "% of portfolio" and the
          percentages still sum to 100, because they are shares of the value that could be
          *priced* — so an unvaluable holding is not a visible zero, it is an absence from a
          picture that looks complete. */}
      {cashIsTracked(allocation) && (allocation!.cash_eur ?? 0) < 0 && (
        <div
          role="alert"
          className="rounded-md border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500"
        >
          <span className="font-medium">
            The account carries a negative cash balance
          </span>{' '}
          , so these percentages are shares of the holdings alone.
        </div>
      )}

      {(allocation?.unpriced_holdings ?? 0) > 0 && (
        <div
          role="alert"
          className="rounded-md border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500"
        >
          <span className="font-medium">
            These charts exclude {allocation!.unpriced_holdings}{' '}
            {allocation!.unpriced_holdings === 1 ? 'holding' : 'holdings'} with no usable
            price
            {allocation!.unpriced_symbols?.length
              ? ` (${allocation!.unpriced_symbols.join(', ')})`
              : ''}
          </span>
          {' '}— percentages are a share of what could be valued.
        </div>
      )}

      {/* Sync Status */}
      {needsSync && (
        <Card className="border-warn/30 bg-warn-soft">
          <CardContent className="pt-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium text-warn">
                  {status.securities_without_data}{' '}
                  {status.securities_without_data === 1 ? 'holding has' : 'holdings have'} no sector or region yet
                </p>
              </div>
              <Button
                onClick={() => syncMutation.mutate()}
                disabled={syncMutation.isPending}
                variant="outline"
              >
                {syncMutation.isPending ? (
                  <>
                    <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                    Updating…
                  </>
                ) : (
                  <>
                    <RefreshCw className="mr-2 h-4 w-4" />
                    Update
                  </>
                )}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {syncMutation.isSuccess && (
        <Card className="border-gain/30 bg-gain-soft">
          <CardContent className="pt-6">
            <p className="text-sm text-gain">
              Updated {syncMutation.data.securities_updated}{' '}
              {syncMutation.data.securities_updated === 1 ? 'holding' : 'holdings'}
            </p>
          </CardContent>
        </Card>
      )}

      {syncMutation.isError && (
        <Card className="border-loss/30 bg-loss-soft">
          <CardContent className="pt-6">
            <p className="text-sm text-loss">
              Sync failed
              {syncMutation.error instanceof Error ? ` — ${syncMutation.error.message}` : ''}.
              Try again in a while.
            </p>
          </CardContent>
        </Card>
      )}

      {/* `[&>*]:min-w-0` because a grid item defaults to `min-width: auto`, so it
          refuses to shrink below its content's min-content width — one wide table
          inside made this single-column track 392px in a 358px page and pushed the
          whole document sideways. The track, not the card, is what has to yield. */}
      <div className="grid gap-6 [&>*]:min-w-0 lg:grid-cols-3">
        <AllocationBreakdown
          title="Sectors"
          icon={Shapes}
          allocation={sectorAllocation}
          colorFor={allocationSectorPaint}
          isLoading={isLoading}
        />
        <AllocationBreakdown
          title="Regions"
          icon={Globe}
          allocation={geoAllocation}
          colorFor={regionPaint}
          isLoading={isLoading}
        />
        <AllocationBreakdown
          title="Asset types"
          icon={Blocks}
          allocation={assetAllocation}
          colorFor={assetTypePaint}
          isLoading={isLoading}
        />
      </div>

      <CurrencyExposureCard
        positions={positions}
        fundSymbols={fundSymbols}
        isLoading={positionsLoading}
        isError={positionsError}
      />

      <RebalanceCard
        positions={positions}
        cash={accountCash}
        isLoading={positionsLoading}
        isError={positionsError}
      />
    </div>
  )
}
