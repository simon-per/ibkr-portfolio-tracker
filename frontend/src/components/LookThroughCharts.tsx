import type { ComponentProps } from 'react'
import { Treemap, ResponsiveContainer, Tooltip } from 'recharts'
import type {
  ExposureTile,
  PartitionSegment,
  TileRole,
} from '@/lib/lookthroughChart'

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

/** Fill per role. The hues are chosen and validated in `index.css`; this only names them. */
const ROLE_FILL: Record<TileRole, string> = {
  direct: 'var(--viz-direct)',
  both: 'var(--viz-both)',
  via_funds: 'var(--viz-via-funds)',
  other_companies: 'var(--viz-other)',
  unattributed: 'var(--viz-unattributed)',
}

/**
 * Deliberately not the composition bar's wording, though the hues are shared.
 *
 * The bar splits *value* and the treemap classifies *companies*, so "Held directly" would
 * mean two different things forty pixels apart: a share of the book in one and a company
 * whose exposure has no fund component in the other. A company held both ways contributes to
 * both bar segments while occupying exactly one tile — which is only legible if the two
 * legends do not appear to be naming the same thing.
 */
const ROLE_LABEL: Record<TileRole, string> = {
  direct: 'Direct only',
  both: 'Direct + funds',
  via_funds: 'Funds only',
  other_companies: 'Smaller companies',
  unattributed: 'Not attributed',
}

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
 * Recharts hands the datum's own fields to `content` as props, alongside the geometry.
 * `depth` distinguishes the root rectangle from the leaves.
 */
interface TileContentProps {
  x?: number
  y?: number
  width?: number
  height?: number
  depth?: number
  name?: string
  pct?: number
  role?: TileRole
}

function TileContent(props: TileContentProps) {
  const { x = 0, y = 0, width = 0, height = 0, depth = 0, name = '', pct = 0, role } = props
  if (depth !== 1 || !role) return null

  // Two thresholds rather than one: a tile can be wide enough for a percentage and still too
  // short for a name above it, and a clipped name is worse than no name.
  const showPct = width > 44 && height > 22
  const showName = width > 70 && height > 38
  // SVG text cannot be measured before it is laid out, so the fit is estimated — and the
  // estimate has to assume the worst glyphs, because company names arrive SHOUTED from IBKR
  // ("NU HOLDINGS LTD/CAYMAN ISL-A"). At 12px/600 an uppercase advance runs ~0.63em against
  // ~0.52em for mixed case; budgeting for the latter put "ARISTA NETWOR…" over both edges of
  // its own tile. Under-filling costs a character, overflowing costs the tile boundary.
  const UPPERCASE_ADVANCE_PX = 7.6
  const maxChars = Math.floor((width - 14) / UPPERCASE_ADVANCE_PX)

  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill={ROLE_FILL[role]}
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
          fill="hsl(var(--foreground))"
          fontSize={12}
          fontWeight={600}
          className="pointer-events-none"
        >
          {name.length > maxChars ? `${name.slice(0, Math.max(1, maxChars - 1))}…` : name}
        </text>
      )}
      {showPct && (
        <text
          x={x + width / 2}
          y={y + height / 2 + (showName ? 8 : 0)}
          textAnchor="middle"
          dominantBaseline="central"
          fill="hsl(var(--foreground))"
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
 * Company exposure as area, including the parts that are not companies.
 *
 * The grey tiles are the point as much as the coloured ones: without them the chart would
 * fill its card with the ~79% of the book that could be attributed and draw it as the whole,
 * which is the renormalisation this feature refuses everywhere else.
 */
export function ExposureTreemap({
  tiles,
  selected,
  onSelect,
  formatCurrency,
}: {
  tiles: ExposureTile[]
  selected: string | null
  onSelect: (companyKey: string) => void
  formatCurrency: (value: number) => string
}) {
  if (tiles.length === 0) return null

  // Only the roles actually on screen, so an all-direct portfolio does not advertise two
  // categories it has no tiles for.
  const roles = Array.from(new Set(tiles.map((tile) => tile.role)))

  return (
    <div className="space-y-3">
      <div className={`w-full min-w-0 ${TREEMAP_HEIGHT}`}>
        <ResponsiveContainer width="100%" height="100%">
          <Treemap
            /* Recharts declares `data` as an open index-signature record; `ExposureTile` is a
               closed shape, which is stricter rather than incompatible. */
            data={tiles as unknown as ComponentProps<typeof Treemap>['data']}
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
                    <p className="text-muted-foreground">{ROLE_LABEL[tile.role]}</p>
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

      {/* Identity is never colour alone: the legend names every role on the chart. */}
      <ul className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
        {roles.map((role) => (
          <li key={role} className="flex items-center gap-1.5">
            <Swatch fill={ROLE_FILL[role]} />
            {ROLE_LABEL[role]}
          </li>
        ))}
      </ul>
    </div>
  )
}
