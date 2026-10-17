import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { IconChip } from '@/components/ui/IconChip'
import type { Tone } from '@/lib/tone'
import { cn } from '@/lib/utils'

/**
 * A heading for a block *inside* the Dividends card — the rolling chart, Per year, Expected
 * next, the position table. One step below `SectionHeader` (the card's own title): the small
 * well and `text-sm`, so the card keeps one headline and its parts read as parts.
 */
export function DividendSubheading({
  title, icon, iconTone, aside, className,
}: {
  title: ReactNode
  icon: LucideIcon
  iconTone?: Tone
  /** Right-aligned: a figure or a short state. Wraps under the title on a phone. */
  aside?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-wrap items-center justify-between gap-x-3 gap-y-1', className)}>
      <div className="flex min-w-0 items-center gap-2">
        <IconChip icon={icon} tone={iconTone} />
        <h4 className="text-sm font-semibold tracking-tight">{title}</h4>
      </div>
      {aside !== undefined && <div className="flex min-w-0 flex-wrap items-center gap-2">{aside}</div>}
    </div>
  )
}
