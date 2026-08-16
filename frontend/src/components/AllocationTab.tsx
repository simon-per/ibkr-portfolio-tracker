import { useState, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Treemap, ResponsiveContainer, Tooltip } from 'recharts'
import { api } from '@/lib/api'
import type { AllocationCategory } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { RefreshCw, X } from 'lucide-react'
import { RebalanceCard } from './RebalanceCard'
import { CurrencyExposureCard } from './CurrencyExposureCard'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { allocationSectorPaint, neutralPaint, type SectorPaint } from '@/lib/sectorColors'

/**
 * Sector colours now come from `lib/sectorColors.ts`, shared with the Look-through treemap so
 * one sector is one colour across the app.
 *
 * The eighteen-entry map that used to live here was measurably broken: `#8b5cf6` Communications
 * against `#a855f7` Basic Materials scored **ΔE 0.3 under protanopia and 5.1 with full colour
 * vision** — indistinguishable, on the chart whose only job is to distinguish. Half its keys
 * were dead anyway, because `mergeAllocation` renames `Information Technology` to `Technology`
 * *before* the colour lookup runs, so the GICS spellings were never reached.
 */
const REGION_COLORS: Record<string, string> = {
  'North America': '#3b82f6',
  'United States': '#3b82f6',
  'US': '#3b82f6',
  'USA': '#3b82f6',
  'Europe': '#10b981',
  'Asia Pacific': '#f59e0b',
  'Emerging Markets': '#ef4444',
  'Latin America': '#8b5cf6',
  'Middle East & Africa': '#f97316',
  'Canada': '#06b6d4',
  'China': '#ec4899',
  'Japan': '#eab308',
  'South Korea': '#14b8a6',
  'Korea': '#14b8a6',
  'United Kingdom': '#22c55e',
  'UK': '#22c55e',
  'Germany': '#84cc16',
  'Switzerland': '#a855f7',
  'Ireland': '#6366f1',
}

/** Two real classes plus the bucket for "nobody has run the allocation sync against this yet". */
const ASSET_TYPE_COLORS: Record<string, string> = {
  Stock: 'var(--viz-sector-1)',
  ETF: 'var(--viz-sector-4)',
}

// Normalize sector names for consistency
const SECTOR_MAPPING: Record<string, string> = {
  'Information Technology': 'Technology',
  'Tech': 'Technology',
  'IT': 'Technology',
  'Financial Services': 'Financials',
  'Finance': 'Financials',
  'Health Care': 'Healthcare',
  'HealthCare': 'Healthcare',
  'Consumer Discretionary': 'Consumer Cyclical',
  'Consumer Staples': 'Consumer Defensive',
  'Communication Services': 'Communications',
  'Telecommunications': 'Communications',
  'Materials': 'Basic Materials',
}

const GEOGRAPHIC_MAPPING: Record<string, string> = {
  'US': 'North America', 'USA': 'North America', 'United States': 'North America',
  'Canada': 'North America', 'Mexico': 'North America',
  'Europe': 'Europe', 'European Union': 'Europe', 'EU': 'Europe',
  'Germany': 'Europe', 'France': 'Europe', 'UK': 'Europe', 'United Kingdom': 'Europe',
  'Switzerland': 'Europe', 'Netherlands': 'Europe', 'Ireland': 'Europe',
  'Spain': 'Europe', 'Italy': 'Europe', 'Sweden': 'Europe', 'Norway': 'Europe',
  'Denmark': 'Europe', 'Finland': 'Europe', 'Austria': 'Europe', 'Belgium': 'Europe', 'Poland': 'Europe',
  'China': 'Asia Pacific', 'Japan': 'Asia Pacific', 'South Korea': 'Asia Pacific', 'Korea': 'Asia Pacific',
  'India': 'Asia Pacific', 'Taiwan': 'Asia Pacific', 'Hong Kong': 'Asia Pacific',
  'Singapore': 'Asia Pacific', 'Australia': 'Asia Pacific', 'New Zealand': 'Asia Pacific',
  'Thailand': 'Asia Pacific', 'Malaysia': 'Asia Pacific', 'Indonesia': 'Asia Pacific',
  'Saudi Arabia': 'Middle East & Africa', 'UAE': 'Middle East & Africa', 'Israel': 'Middle East & Africa',
  'South Africa': 'Middle East & Africa',
  'Brazil': 'Latin America', 'Argentina': 'Latin America', 'Chile': 'Latin America',
}

function normalize(name: string, mapping: Record<string, string>): string {
  return mapping[name] || name
}

/**
 * Colour by identity, never by row position.
 *
 * This used to fall back to `FALLBACK_COLORS[index % 15]`, so any category the map did not
 * cover — `Unknown` on the sector and region charts among them — changed colour whenever the
 * ordering changed. A reader who learned "Unknown is orange" was misled by the next sync.
 */
function mappedColor(colorMap: Record<string, string>) {
  return (name: string): SectorPaint => {
    const fill = colorMap[name]
    // The mapped region and asset-type hexes are all mid-to-dark, so white carries them; the
    // neutral ramp supplies its own ink for the same reason.
    return fill ? { fill, ink: '#ffffff' } : neutralPaint(name)
  }
}

// Custom treemap content renderer
interface TreemapContentProps {
  x: number
  y: number
  width: number
  height: number
  name: string
  percentage: number
  fill: string
  ink: string
  depth: number
}

function CustomTreemapContent(props: TreemapContentProps) {
  const { x, y, width, height, name, percentage, fill, ink, depth } = props
  if (depth !== 1) return null

  const showLabel = width > 60 && height > 30
  const showPct = width > 40 && height > 20

  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill={fill}
        stroke="hsl(var(--background))"
        strokeWidth={2}
        rx={4}
        className="cursor-pointer transition-opacity hover:opacity-80"
      />
      {showLabel && (
        <text
          x={x + width / 2}
          y={y + height / 2 - (showPct ? 8 : 0)}
          textAnchor="middle"
          dominantBaseline="central"
          fill={ink}
          fontSize={Math.min(14, width / 8)}
          fontWeight={600}
          className="pointer-events-none"
        >
          {name.length > width / 8 ? name.slice(0, Math.floor(width / 8)) + '...' : name}
        </text>
      )}
      {showPct && (
        <text
          x={x + width / 2}
          y={y + height / 2 + 12}
          textAnchor="middle"
          dominantBaseline="central"
          fill={ink}
          opacity={0.85}
          fontSize={Math.min(12, width / 9)}
          className="pointer-events-none"
        >
          {percentage.toFixed(1)}%
        </text>
      )}
    </g>
  )
}

// Merge allocation categories that share the same normalized name
function mergeAllocation(
  raw: Record<string, AllocationCategory>,
  mapping: Record<string, string>,
): Record<string, AllocationCategory> {
  const merged: Record<string, AllocationCategory> = {}

  for (const [name, cat] of Object.entries(raw)) {
    const normalized = normalize(name, mapping)
    if (!merged[normalized]) {
      merged[normalized] = { percentage: 0, market_value_eur: 0, positions: [] }
    }
    merged[normalized].percentage += cat.percentage
    merged[normalized].market_value_eur += cat.market_value_eur

    // Merge positions, combining same symbols
    for (const pos of cat.positions) {
      const existing = merged[normalized].positions.find(p => p.symbol === pos.symbol)
      if (existing) {
        existing.weight += pos.weight
        existing.market_value_eur += pos.market_value_eur
      } else {
        merged[normalized].positions.push({ ...pos })
      }
    }
  }

  // Sort by percentage descending
  const sorted = Object.entries(merged).sort((a, b) => b[1].percentage - a[1].percentage)
  const result: Record<string, AllocationCategory> = {}
  for (const [k, v] of sorted) {
    v.positions.sort((a, b) => b.weight - a.weight)
    result[k] = v
  }
  return result
}

/** The drill-down's five columns, described once for both renderings. */
function drillDownColumns(deps: {
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
      cell: (pos) => pos.symbol,
    },
    {
      key: 'description',
      header: 'Description',
      shortHeader: 'Description',
      mobile: 'meta',
      cellClassName: 'text-muted-foreground max-w-[200px] truncate',
      cell: (pos) => pos.description,
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
          <span className="text-xs bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 px-1.5 py-0.5 rounded">
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
  onClose,
}: {
  categoryName: string
  category: AllocationCategory
  onClose: () => void
}) {
  const formatCurrency = useFormatCurrency()
  return (
    <Card className="mt-4 border-primary/20">
      <CardHeader className="pb-3">
        <div className="flex justify-between items-center">
          <div>
            <CardTitle className="text-lg">{categoryName}</CardTitle>
            <CardDescription>
              {category.percentage.toFixed(1)}% of portfolio · {formatCurrency(category.market_value_eur)}
            </CardDescription>
          </div>
          <Button variant="ghost" size="icon" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        </div>
      </CardHeader>
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

// Treemap section component
function AllocationTreemap({
  title,
  description,
  allocation,
  colorFor,
  isLoading,
}: {
  title: string
  description: string
  allocation: Record<string, AllocationCategory>
  colorFor: (name: string) => SectorPaint
  isLoading: boolean
}) {
  const formatCurrency = useFormatCurrency()
  const [selected, setSelected] = useState<string | null>(null)

  // Reset selection when allocation data changes
  useEffect(() => { setSelected(null) }, [allocation])

  const entries = Object.entries(allocation)
  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-2xl">{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-center h-80 text-muted-foreground">Loading...</div>
        </CardContent>
      </Card>
    )
  }

  if (entries.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-2xl">{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-center h-80 text-muted-foreground">
            No data available. Click "Sync Now" to fetch allocation data.
          </div>
        </CardContent>
      </Card>
    )
  }

  const treemapData = entries.map(([name, cat]) => ({
    name,
    size: cat.percentage,
    percentage: cat.percentage,
    market_value_eur: cat.market_value_eur,
    fill: colorFor(name).fill,
    ink: colorFor(name).ink,
  }))

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-2xl">{title}</CardTitle>
        <CardDescription>{description} · Click a section to see positions</CardDescription>
      </CardHeader>
      <CardContent>
        {/* Stacks below `sm`. Side by side, the 200px legend leaves the treemap ~86px
            of a 390px screen — and CustomTreemapContent suppresses every label under
            60x30, so the chart rendered as unlabelled colour blocks. Stacked, the
            treemap is full width and the labels come back on their own. */}
        <div className="flex flex-col gap-4 sm:flex-row sm:gap-6 sm:items-start">
          {/* Treemap */}
          <div className="h-[240px] w-full min-w-0 sm:h-[400px] sm:flex-1">
            <ResponsiveContainer width="100%" height="100%">
              <Treemap
                data={treemapData}
                dataKey="size"
                aspectRatio={4 / 3}
                isAnimationActive={false}
                content={<CustomTreemapContent x={0} y={0} width={0} height={0} name="" percentage={0} fill="" ink="#ffffff" depth={1} />}
                onClick={(node: any) => {
                  if (node?.name) {
                    setSelected(selected === node.name ? null : node.name)
                  }
                }}
              >
                <Tooltip
                  content={({ payload }) => {
                    if (!payload || payload.length === 0) return null
                    const d = payload[0].payload
                    return (
                      <div className="bg-card border border-border rounded-lg px-3 py-2 shadow-lg text-sm">
                        <p className="font-semibold">{d.name}</p>
                        <p className="text-muted-foreground">{d.percentage?.toFixed(1)}%</p>
                        <p className="text-muted-foreground">{formatCurrency(d.market_value_eur)}</p>
                      </div>
                    )
                  }}
                />
              </Treemap>
            </ResponsiveContainer>
          </div>

          {/* Legend */}
          <div className="w-full space-y-1.5 sm:w-[200px] sm:shrink-0 sm:pt-2">
            {entries.map(([name, cat]) => (
              <button
                key={name}
                onClick={() => setSelected(selected === name ? null : name)}
                className={`flex min-h-11 items-center gap-2 w-full text-left py-1 px-1.5 rounded transition-colors sm:min-h-0 ${
                  selected === name ? 'bg-muted' : 'hover:bg-muted/50'
                }`}
              >
                <div
                  style={{
                    width: '12px',
                    height: '12px',
                    borderRadius: '3px',
                    backgroundColor: colorFor(name).fill,
                    flexShrink: 0,
                  }}
                />
                <span className="text-sm truncate flex-1">{name}</span>
                <span className="text-sm font-semibold tabular-nums">{cat.percentage.toFixed(1)}%</span>
              </button>
            ))}
          </div>
        </div>

        {/* Drill-down panel */}
        {selected && allocation[selected] && (
          <DrillDownPanel
            categoryName={selected}
            category={allocation[selected]}
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
  const { data: positions, isLoading: positionsLoading, isError: positionsError } = useQuery({
    queryKey: ['portfolio', 'positions'],
    queryFn: () => api.getPositions(),
  })

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

  // Merge and normalize allocation data
  const sectorAllocation = allocation?.sector_allocation
    ? mergeAllocation(allocation.sector_allocation, SECTOR_MAPPING)
    : {}

  const geoAllocation = allocation?.geographic_allocation
    ? mergeAllocation(allocation.geographic_allocation, GEOGRAPHIC_MAPPING)
    : {}

  const assetAllocation = allocation?.asset_type_allocation || {}

  return (
    <div className="space-y-6">
      {/* A fetch error must not impersonate empty allocation data — the
          treemaps below just render blank when the backend isn't answering. */}
      {(allocationError || statusError) && (
        <Card className="border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950">
          <CardContent className="pt-6">
            <p className="text-sm text-red-800 dark:text-red-200">
              Couldn't load allocation data — the backend didn't respond. It retries automatically.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Sync Status */}
      {needsSync && (
        <Card className="border-yellow-200 dark:border-yellow-800 bg-yellow-50 dark:bg-yellow-950">
          <CardContent className="pt-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-yellow-800 dark:text-yellow-200">
                  Allocation data needs updating
                </p>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
                  {status.securities_without_data} securities missing allocation data
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

      {syncMutation.isSuccess && (
        <Card className="border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-950">
          <CardContent className="pt-6">
            <p className="text-sm text-green-800 dark:text-green-200">
              Sync successful! Updated {syncMutation.data.securities_updated} securities
            </p>
          </CardContent>
        </Card>
      )}

      {syncMutation.isError && (
        <Card className="border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950">
          <CardContent className="pt-6">
            <p className="text-sm text-red-800 dark:text-red-200">
              Sync failed
              {syncMutation.error instanceof Error ? ` — ${syncMutation.error.message}` : ''}.
              Yahoo may be rate-limiting; try again in a while.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Sector Breakdown - Full Width Treemap */}
      <AllocationTreemap
        title="Sector Breakdown"
        description="Portfolio allocation by sector"
        allocation={sectorAllocation}
        colorFor={allocationSectorPaint}
        isLoading={isLoading}
      />

      {/* Geographic and Asset Type side by side */}
      {/* `[&>*]:min-w-0` because a grid item defaults to `min-width: auto`, so it
          refuses to shrink below its content's min-content width — one wide table
          inside made this single-column track 392px in a 358px page and pushed the
          whole document sideways. The track, not the card, is what has to yield. */}
      <div className="grid gap-6 [&>*]:min-w-0 lg:grid-cols-2">
        <AllocationTreemap
          title="Geographic Breakdown"
          description="Portfolio allocation by region"
          allocation={geoAllocation}
          colorFor={mappedColor(REGION_COLORS)}
          isLoading={isLoading}
        />

        <AllocationTreemap
          title="Asset Type"
          description="Stocks vs ETFs"
          allocation={assetAllocation}
          colorFor={mappedColor(ASSET_TYPE_COLORS)}
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
        isLoading={positionsLoading}
        isError={positionsError}
      />
    </div>
  )
}
