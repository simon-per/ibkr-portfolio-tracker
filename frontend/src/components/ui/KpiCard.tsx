/**
 * One KPI: a label, a value, and optionally an icon, a delta, a sparkline and a footnote.
 *
 * Extracted because sixteen cards across four files each hand-rolled the same
 * `CardHeader` + `CardTitle className="text-sm font-medium"` + `CardContent` + value +
 * footnote shape, with four different ideas of what an absent value looks like (an em
 * dash, `N/A`, a blank, a `0`). The dash-for-absent rule lives here now — pass
 * `value={null}` — and a caller cannot accidentally colour a missing number as good news.
 *
 * Two renderings from one component (2026-09-13): the default is a standalone card; `tile`
 * drops the card chrome so a `KpiPanel` can seat six of them in one bordered panel with
 * hairline dividers. The Performance tab's two metric rows used to be twelve separate
 * boxes under five more — seventeen identical rectangles before the first chart — and the
 * panel is what makes the hero row read as the headline and the rest as its detail.
 *
 * The design-foundation pass (2026-10-17) added the fintech furniture, every piece optional
 * so no existing call site moved: `icon` as a lucide component in a tinted well, `delta` as a
 * `DeltaChip` pill beside the value, and `sparkline` under it. docs/frontend.md, *Design
 * foundation*, has the API and when to use which.
 */
import { isValidElement, type ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { DeltaChip, type DeltaChipProps } from '@/components/DeltaChip'
import { IconChip } from '@/components/ui/IconChip'
import { Sparkline } from '@/components/ui/Sparkline'
import { TONE_TEXT, type Tone } from '@/lib/tone'
import { ABSENT, cn } from '@/lib/utils'

export type KpiTone = Tone

/**
 * `text-lg` below `sm`: two cards share 358px, and "CHF 48,561.36" is ~182px in `text-2xl`.
 * The hero keeps its size because it has the whole row to itself.
 */
/* No `tabular-nums` on a lone figure: Inter gives the hyphen a full figure width under it,
   which opened a gap in every negative value, and one figure has nothing to align with.
   `whitespace-nowrap`: a formatted figure carries an ASCII hyphen ("-CHF 57,244.07"), and a
   browser may break a line after a hyphen — in a narrow tile the sign sat alone above the
   amount. A panel too narrow for its figures takes fewer columns instead (`KpiPanel`). */
const VALUE_CLASS = 'whitespace-nowrap text-lg font-semibold leading-tight tracking-tight sm:text-[1.375rem]'
const HERO_VALUE_CLASS = 'whitespace-nowrap text-[1.75rem] font-semibold leading-tight tracking-tight sm:text-[2rem]'
const LABEL_CLASS = 'text-xs font-medium text-muted-foreground'

export interface KpiCardProps {
  label: ReactNode
  /**
   * `null`/`undefined` renders {@link ABSENT} in the muted tone and ignores `tone`, so a
   * caller cannot accidentally colour a missing number as if it were good or bad news.
   */
  value: ReactNode
  tone?: KpiTone
  /**
   * A lucide component (`icon={Wallet}`) renders in a tinted well before the label, in both
   * renderings. A rendered element (`icon={<Wallet className="h-4 w-4" />}`) is the older
   * form: drawn as-is at the card's top right, and not at all in a tile.
   */
  icon?: LucideIcon | ReactNode
  /** The well's tint. Defaults to `tone`, so a gain figure gets a green well. */
  iconTone?: KpiTone
  /**
   * A change beside the value, as a `DeltaChip` pill. `flatBand` defaults to 0 here — a KPI's
   * move is signal — where the chip's own default suits lumpy dividend series.
   */
  delta?: Omit<DeltaChipProps, 'variant'>
  /** A tiny trend under the value, oldest first. Gaps (null) break the line. */
  sparkline?: readonly (number | null | undefined)[]
  /** Footnote, wrapped in the standard muted line. */
  sub?: ReactNode
  /** Arbitrary content in place of `sub` — a `DeltaChip`, say, that must keep its own colours. */
  footer?: ReactNode
  /**
   * The emphasis level: full width in a 2-up phone grid and two columns on desktop, and never
   * shrinks its value to `text-lg`. For the one figure the page is actually about; the rest
   * tile underneath it.
   */
  hero?: boolean
  /** Render without card chrome, for use inside a {@link KpiPanel}. */
  tile?: boolean
  /** Native tooltip, for a figure that needs a caveat the footnote has no room for. */
  title?: string
  className?: string
}

/** A component to draw in a well, as opposed to an element someone already drew. */
function isIconComponent(icon: KpiCardProps['icon']): icon is LucideIcon {
  if (typeof icon === 'function') return true
  // lucide's icons are `forwardRef` objects: tagged with `$$typeof` like an element, but
  // not one. An array or a string is a ReactNode and stays on the legacy path.
  return typeof icon === 'object' && icon !== null && !Array.isArray(icon)
    && '$$typeof' in icon && !isValidElement(icon)
}

export function KpiCard({
  label, value, tone = 'neutral', icon, iconTone, delta, sparkline, sub, footer, hero, tile,
  title, className,
}: KpiCardProps) {
  const absent = value == null
  const chipIcon = isIconComponent(icon) ? icon : null
  const legacyIcon = chipIcon ? null : (icon as ReactNode)
  const chip = chipIcon && (
    <IconChip icon={chipIcon} tone={iconTone ?? (absent ? 'muted' : tone)} />
  )
  const labelRow = (
    <div className="flex min-w-0 items-center gap-2">
      {chip}
      <span className={cn(LABEL_CLASS, 'min-w-0')}>{label}</span>
    </div>
  )
  const body = (
    <>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <div
          data-kpi-value=""
          className={cn(
            hero ? HERO_VALUE_CLASS : VALUE_CLASS,
            TONE_TEXT[absent ? 'muted' : tone],
          )}
        >
          {absent ? ABSENT : value}
        </div>
        {delta && <DeltaChip flatBand={0} {...delta} variant="pill" />}
      </div>
      {sparkline && <Sparkline values={sparkline} className={cn('mt-1', hero ? 'h-12' : 'h-8')} />}
      {footer}
      {sub != null && <p className="mt-1 text-xs leading-snug text-muted-foreground">{sub}</p>}
    </>
  )

  if (tile) {
    return (
      <div className={cn('flex flex-col gap-1.5 p-4 sm:p-5', hero && 'col-span-2', className)} title={title}>
        {chipIcon ? <div className="mb-0.5">{labelRow}</div> : <span className={LABEL_CLASS}>{label}</span>}
        {body}
      </div>
    )
  }

  return (
    <Card className={cn(hero && 'col-span-2', className)} title={title}>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0 pb-2">
        {chipIcon ? (
          <CardTitle className="flex min-w-0 items-center gap-2 text-xs font-medium text-muted-foreground">
            {chip}
            <span className="min-w-0">{label}</span>
          </CardTitle>
        ) : (
          <CardTitle className={LABEL_CLASS}>{label}</CardTitle>
        )}
        {legacyIcon && <span className="opacity-70">{legacyIcon}</span>}
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  )
}

export function KpiCardSkeleton({ count, tile }: { count: number; tile?: boolean }) {
  return (
    <>
      {Array.from({ length: count }, (_, i) => (
        tile ? (
          <div key={i} className="flex flex-col gap-2 p-4 sm:p-5">
            <span className={LABEL_CLASS}>Loading...</span>
            <div className="h-7 animate-pulse rounded bg-muted" />
          </div>
        ) : (
          <Card key={i}>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className={LABEL_CLASS}>Loading...</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="h-7 animate-pulse rounded bg-muted" />
            </CardContent>
          </Card>
        )
      ))}
    </>
  )
}

/**
 * One bordered panel holding several `tile` KPIs, separated by hairlines rather than by
 * gaps and borders. `gap-px` over a border-coloured background is the divider trick: the
 * tiles paint the card colour and the 1px seams between them show the panel's background.
 * The phone keeps two columns, so a six-metric row is three rows of two.
 */
export function KpiPanel({
  children, columns = 6, fillLastRow, className,
}: {
  children: ReactNode
  columns?: 3 | 4 | 5 | 6
  /**
   * Let an odd last tile span the phone's two columns. A seam-divided panel shows its
   * background through an empty cell — a grey block, not whitespace — so a panel whose
   * tile count is odd asks for this. Off by default: a full-width notice inside the grid
   * (the XIRR alert) changes the parity, and the panel cannot see that.
   */
  fillLastRow?: boolean
  className?: string
}) {
  const cols = {
    3: 'md:grid-cols-3',
    4: 'md:grid-cols-2 lg:grid-cols-4',
    5: 'md:grid-cols-3 lg:grid-cols-5',
    // Six in a row only from `xl`, at `text-lg`: at `lg` a sixth of the page is ~120px of
    // tile, narrower than "-CHF 57,244.07" at any size that still reads as a headline.
    6: 'md:grid-cols-3 xl:grid-cols-6 xl:[&_[data-kpi-value]]:text-lg',
  }[columns]
  return (
    <div className={cn('overflow-hidden rounded-2xl border border-border/80 bg-border/70 shadow-card', className)}>
      <div
        className={cn(
          'grid grid-cols-2 gap-px [&>*]:bg-card',
          cols,
          fillLastRow && '[&>*:last-child:nth-child(odd)]:col-span-2 md:[&>*:last-child:nth-child(odd)]:col-span-1',
        )}
      >
        {children}
      </div>
    </div>
  )
}
