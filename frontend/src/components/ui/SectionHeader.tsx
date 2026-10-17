import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { IconChip } from '@/components/ui/IconChip'
import type { Tone } from '@/lib/tone'
import { cn } from '@/lib/utils'

interface SectionHeaderProps {
  title: ReactNode
  /** A lucide component, drawn in a tinted well before the title. */
  icon?: LucideIcon
  /** The well's tint; neutral (brand blue) by default. */
  iconTone?: Tone
  /** One short line under the title: a figure or a state, never an explanation. */
  description?: ReactNode
  /**
   * Right-aligned: the section's headline figure, a `SegmentedControl`, a button. Wraps under
   * the title on a phone rather than squeezing it.
   */
  aside?: ReactNode
  /**
   * `card` (default) is a `CardHeader`, padded like every other card header. `bare` is the
   * same row with no padding, for a heading above a grid of cards rather than inside one.
   */
  variant?: 'card' | 'bare'
  className?: string
}

/**
 * Icon + title + optional description + optional right-aligned figure or actions: the one
 * header for a card or a section. `CollapsibleCardHeader` draws the same left half, so an
 * open/close card and a static one line up.
 */
export function SectionHeader({
  title, icon, iconTone, description, aside, variant = 'card', className,
}: SectionHeaderProps) {
  const row = (
    <>
      <div className="flex min-w-0 items-center gap-3">
        {icon && <IconChip icon={icon} tone={iconTone} size="md" />}
        <div className="min-w-0 space-y-1">
          <CardTitle>{title}</CardTitle>
          {description !== undefined && <CardDescription>{description}</CardDescription>}
        </div>
      </div>
      {aside !== undefined && (
        <div className="flex min-w-0 shrink-0 flex-wrap items-center gap-2 sm:justify-end">{aside}</div>
      )}
    </>
  )
  const layout = 'flex flex-row flex-wrap items-center justify-between gap-x-4 gap-y-3 space-y-0'
  if (variant === 'bare') return <div className={cn(layout, className)}>{row}</div>
  return <CardHeader className={cn(layout, className)}>{row}</CardHeader>
}
