import type { ComponentProps } from 'react'
import { Treemap, ResponsiveContainer, Tooltip } from 'recharts'
import type {
  ExposureGroup,
  ExposureTile,
  PartitionSegment,
} from '@/lib/lookthroughChart'
import { sectorPaint } from '@/lib/sectorColors'

/**
 * The two look-through charts. Their arithmetic lives in `lib/lookthroughChart.ts`; this file
 * is only how it is drawn.
 *
 * **Why a treemap and not a pie.** The company ranking is ~50 rows spanning three orders of
 * magnitude — the top holding is several hundred times the smallest. A pie asks the reader to
 * compare angles, which fails past about six slices and fails completely on a distribution
 * this skewed; a treemap encodes the same quantity as area, keeps the long tail visible as
 * texture, and is the form every broker uses for exactly this question. The composition bar
 * below is where a part-to-whole *is* the job, and even there a horizontal stacked bar beats a
 * ring: three segments with long names, read left to right, at 390px.
 *
 * Neither chart is the only route to a value. The company table repeats every tile and the
 * legend carries every segment's figure, so nothing here is gated behind a hover — which on a
 * touch device would mean gated behind nothing at all.
 */

const SEGMENT_FILL: Record<PartitionSegment['key'], string> = {
  direct: 'var(--viz-direct)',
  via_funds: 'var(--viz-via-funds)',
  unattributed: 'var(--viz-unattributed)',
}

/** Chart height as a module constant so the chart and its empty state cannot drift apart. */
const TREEMAP_HEIGHT = 'h-[280px] sm:h-[440px]'

function Swatch({ fill }: { fill: string }) {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-2.5 w-2.5 shrink-0 rounded-[2px]"
      style={{ backgroundColor: fill }}
    />
  )
}

/**
 * Every euro in the portfolio, split by how the look-through reaches it.
 *
 * A plain flex row rather than a Recharts bar: one stacked bar has no axis, no scale and no
 * hover geometry worth a chart library, and a div reflows at 390px for free where a
 * `ResponsiveContainer` would need a height guessed in advance.
 */
export function CompositionBar({
  segments,
  formatCurrency,
}: {
  segments: PartitionSegment[]
  formatCurrency: (value: number) => string
}) {
  if (segments.length === 0) return null

  const summary = segments
    .map((segment) => `${segment.label} ${segment.pct.toFixed(1)}%`)
    .join(', ')

  return (
    <div className="space-y-3">
      {/* `overflow-hidden` + `gap` gives rounded outer ends and a 2px surface gap between
          segments — a separating gap rather than a border drawn around each fill. */}
      <div
        role="img"
        aria-label={`Portfolio composition: ${summary}`}
        className="flex h-7 w-full gap-[2px] overflow-hidden rounded sm:h-8"
      >
        {segments.map((segment) => (
          <div
            key={segment.key}
            className="flex min-w-0 items-center justify-center"
            style={{
              width: `${segment.pct}%`,
              backgroundColor: SEGMENT_FILL[segment.key],
            }}
            title={`${segment.label}: ${formatCurrency(segment.value)} (${segment.pct.toFixed(1)}%)`}
          >
            {/* Only label a segment wide enough to hold the text. Below that the legend
                carries it, rather than clipping "12.3%" to "1". */}
            {segment.pct >= 12 && (
              <span
                className="truncate px-1 text-[11px] font-semibold"
                style={{ color: 'hsl(var(--foreground))' }}
              >
                {segment.pct.toFixed(1)}%
              </span>
            )}
          </div>
        ))}
      </div>

      <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-3">
        {segments.map((segment) => (
          <div key={segment.key} className="min-w-0 text-xs">
            <dt className="flex items-center gap-1.5 font-medium">
              <Swatch fill={SEGMENT_FILL[segment.key]} />
              <span className="truncate">{segment.label}</span>
              <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">
                {segment.pct.toFixed(1)}%
              </span>
            </dt>
            <dd className="mt-0.5 pl-4 text-muted-foreground">
              <span className="tabular-nums">{formatCurrency(segment.value)}</span>
              <span className="block">{segment.hint}</span>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

/**
 * Recharts hands the datum's own fields to `content` as props, alongside the geometry, and
 * `depth` says which level it is drawing: 1 for a sector group, 2 for a company inside it.
 *
 * **Only depth 2 is drawn, and that is a finding rather than a shortcut.** The first version
 * painted a frame and a header strip at depth 1 to delimit each sector. Neither ever appeared:
 * Recharts paints a parent before its children and the children tile the parent exactly, so
 * both were covered the instant the companies rendered. It took a screenshot to notice — no
 * unit test can see paint order, and the chart looked plausible without them.
 *
 * So the grouping is carried by adjacency and fill, and each group's name and share by the
 * legend underneath, which nothing can paint over.
 */
interface TileContentProps {
  x?: number
  y?: number
  width?: number
  height?: number
  depth?: number
  name?: string
  pct?: number
  paintKey?: string
  root?: { paintKey?: string }
}

// SVG text cannot be measured before it is laid out, so the fit is estimated — and the estimate
// has to assume the worst glyphs, because company names arrive SHOUTED from IBKR ("NU HOLDINGS
// LTD/CAYMAN ISL-A"). At 12px/600 an uppercase advance runs ~0.63em against ~0.52em for mixed
// case; budgeting for the latter put "ARISTA NETWOR…" over both edges of its own tile.
// Under-filling costs a character, overflowing costs the tile boundary.
const UPPERCASE_ADVANCE_PX = 7.6

function truncate(name: string, width: number, pad: number): string {
  const maxChars = Math.floor((width - pad) / UPPERCASE_ADVANCE_PX)
  if (maxChars < 2) return ''
  return name.length > maxChars ? `${name.slice(0, maxChars - 1)}…` : name
}

function TileContent(props: TileContentProps) {
  const { x = 0, y = 0, width = 0, height = 0, depth = 0, name = '', pct = 0 } = props
  if (width <= 0 || height <= 0) return null
  if (depth !== 2) return null

  // Recharts hands a leaf its ancestor as `root`, which is how a company knows which sector
  // painted it — the leaf carries no paint key of its own, because the group owns that decision.
  const paint = sectorPaint(props.root?.paintKey ?? '')
  // Two thresholds rather than one: a tile can be wide enough for a percentage and still too
  // short for a name above it, and a clipped name is worse than no name.
  const showPct = width > 44 && height > 22
  const showName = width > 70 && height > 38

  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill={paint.fill}
        stroke="hsl(var(--card))"
        strokeWidth={2}
        rx={4}
        className="cursor-pointer transition-opacity hover:opacity-80"
      />
      {showName && (
        <text
          x={x + width / 2}
          y={y + height / 2 - 8}
          textAnchor="middle"
          dominantBaseline="central"
          fill={paint.ink}
          fontSize={12}
          fontWeight={600}
          className="pointer-events-none"
        >
          {truncate(name, width, 14)}
        </text>
      )}
      {showPct && (
        <text
          x={x + width / 2}
          y={y + height / 2 + (showName ? 8 : 0)}
          textAnchor="middle"
          dominantBaseline="central"
          fill={paint.ink}
          fontSize={11}
          opacity={0.85}
          className="pointer-events-none"
        >
          {pct.toFixed(pct >= 1 ? 1 : 2)}%
        </text>
      )}
    </g>
  )
}

/**
 * Company exposure as area, grouped by sector, including the parts that are not companies.
 *
 * The grey blocks are the point as much as the coloured ones: without them the chart would fill
 * its card with the ~79% of the book that could be attributed and draw it as the whole, which is
 * the renormalisation this feature refuses everywhere else.
 */
export function ExposureTreemap({
  groups,
  selected,
  onSelect,
  formatCurrency,
}: {
  groups: ExposureGroup[]
  selected: string | null
  onSelect: (companyKey: string) => void
  formatCurrency: (value: number) => string
}) {
  if (groups.length === 0) return null

  return (
    <div className="space-y-3">
      <div className={`w-full min-w-0 ${TREEMAP_HEIGHT}`}>
        <ResponsiveContainer width="100%" height="100%">
          <Treemap
            data={groups as unknown as ComponentProps<typeof Treemap>['data']}
            dataKey="size"
            aspectRatio={4 / 3}
            isAnimationActive={false}
            content={<TileContent />}
            onClick={(node: unknown) => {
              const key = (node as { companyKey?: string | null })?.companyKey
              if (key) onSelect(key)
            }}
          >
            <Tooltip
              content={({ payload }) => {
                if (!payload || payload.length === 0) return null
                const tile = payload[0].payload as ExposureTile
                return (
                  <div className="rounded-lg border border-border bg-card px-3 py-2 text-sm shadow-lg">
                    <p className="font-semibold">{tile.name}</p>
                    <p className="text-muted-foreground">
                      {formatCurrency(tile.value)} · {tile.pct.toFixed(2)}% of the portfolio
                    </p>
                    {/* The company's REAL sector, which for a folded row is not the group it was
                        drawn in — otherwise the fold would read as missing data. */}
                    {tile.sector && <p className="text-muted-foreground">{tile.sector}</p>}
                    {tile.companyKey && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        {selected === tile.companyKey
                          ? 'Shown below'
                          : 'Click for the fund breakdown'}
                      </p>
                    )}
                  </div>
                )
              }}
            />
          </Treemap>
        </ResponsiveContainer>
      </div>

      {/*
        Identity is never colour alone, and here the legend is the ONLY place a group is named:
        an in-chart header cannot survive Recharts' paint order (see `TileContent`). It carries
        the share too, which is what a header would have said.
      */}
      <ul className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
        {groups.map((group) => (
          <li key={group.key} className="flex items-center gap-1.5">
            <Swatch fill={sectorPaint(group.paintKey).fill} />
            <span>{group.name}</span>
            <span className="tabular-nums opacity-80">{group.pct.toFixed(1)}%</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
