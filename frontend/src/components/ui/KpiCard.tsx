/**
 * One KPI: a label, a value, an optional icon and an optional footnote.
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
 */
import type { ReactNode } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

export type KpiTone = 'neutral' | 'positive' | 'negative' | 'warning' | 'muted'

const TONE_CLASS: Record<KpiTone, string> = {
  neutral: '',
  positive: 'text-green-600 dark:text-green-400',
  negative: 'text-red-600 dark:text-red-400',
  warning: 'text-yellow-600 dark:text-yellow-400',
  muted: 'text-muted-foreground',
}

/**
 * `text-lg` below `sm`: two cards share 358px, and "CHF 48,561.36" is ~182px in `text-2xl`.
 * The hero keeps its size because it has the whole row to itself.
 */
const VALUE_CLASS = 'text-lg font-semibold tracking-tight tabular-nums sm:text-2xl'
const HERO_VALUE_CLASS = 'text-2xl font-semibold tracking-tight tabular-nums sm:text-3xl'
const LABEL_CLASS = 'text-xs font-medium text-muted-foreground'

export const ABSENT = '—'

export interface KpiCardProps {
  label: ReactNode
  /**
   * `null`/`undefined` renders {@link ABSENT} in the muted tone and ignores `tone`, so a
   * caller cannot accidentally colour a missing number as if it were good or bad news.
   */
  value: ReactNode
  tone?: KpiTone
  icon?: ReactNode
  /** Footnote, wrapped in the standard muted line. */
  sub?: ReactNode
  /** Arbitrary content in place of `sub` — a `DeltaChip`, say, that must keep its own colours. */
  footer?: ReactNode
  /**
   * Full width in a 2-up phone grid and two columns on desktop, and never shrinks its
   * value to `text-lg`. For the one figure the page is actually about; the rest tile
   * underneath it.
   */
  hero?: boolean
  /** Render without card chrome, for use inside a {@link KpiPanel}. */
  tile?: boolean
  /** Native tooltip, for a figure that needs a caveat the footnote has no room for. */
  title?: string
  className?: string
}

export function KpiCard({
  label, value, tone = 'neutral', icon, sub, footer, hero, tile, title, className,
}: KpiCardProps) {
  const absent = value == null
  const body = (
    <>
      <div
        className={cn(
          hero ? HERO_VALUE_CLASS : VALUE_CLASS,
          TONE_CLASS[absent ? 'muted' : tone],
        )}
      >
        {absent ? ABSENT : value}
      </div>
      {footer}
      {sub != null && <p className="mt-1 text-xs leading-snug text-muted-foreground">{sub}</p>}
    </>
  )

  if (tile) {
    return (
      <div className={cn('flex flex-col gap-2 p-4 sm:p-5', hero && 'col-span-2', className)} title={title}>
        <div className="flex items-center justify-between gap-2">
          <span className={LABEL_CLASS}>{label}</span>
          {icon && <span className="opacity-70">{icon}</span>}
        </div>
        {body}
      </div>
    )
  }

  return (
    <Card className={cn(hero && 'col-span-2', className)} title={title}>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className={LABEL_CLASS}>{label}</CardTitle>
        {icon && <span className="opacity-70">{icon}</span>}
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
  children, columns = 6, className,
}: { children: ReactNode; columns?: 4 | 5 | 6; className?: string }) {
  const cols = {
    4: 'md:grid-cols-2 lg:grid-cols-4',
    5: 'md:grid-cols-3 lg:grid-cols-5',
    6: 'md:grid-cols-3 lg:grid-cols-6',
  }[columns]
  return (
    <div className={cn('overflow-hidden rounded-xl border border-border/70 bg-border/60', className)}>
      <div className={cn('grid grid-cols-2 gap-px [&>*]:bg-card', cols)}>{children}</div>
    </div>
  )
}
