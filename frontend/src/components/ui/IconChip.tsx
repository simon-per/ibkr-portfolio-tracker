import type { LucideIcon } from 'lucide-react'
import { TONE_CHIP, type Tone } from '@/lib/tone'
import { cn } from '@/lib/utils'

interface IconChipProps {
  icon: LucideIcon
  /** The well's tint; neutral is the brand blue (see `lib/tone.ts`). */
  tone?: Tone
  /** `sm` sits beside a KPI label, `md` beside a section title. */
  size?: 'sm' | 'md'
  className?: string
}

/**
 * A lucide glyph in a small tinted, rounded well: the one icon treatment for KPI labels and
 * section titles. Decorative by construction (`aria-hidden`) — the label or title beside it
 * carries the meaning, so an icon never becomes the only name a figure has.
 */
export function IconChip({ icon: Icon, tone = 'neutral', size = 'sm', className }: IconChipProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'inline-grid shrink-0 place-items-center',
        size === 'sm' ? 'h-7 w-7 rounded-lg' : 'h-8 w-8 rounded-[0.625rem]',
        TONE_CHIP[tone],
        className,
      )}
    >
      <Icon className={size === 'sm' ? 'h-3.5 w-3.5' : 'h-4 w-4'} strokeWidth={2.25} />
    </span>
  )
}
