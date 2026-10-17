import { ChevronDown, type LucideIcon } from 'lucide-react'
import { CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { IconChip } from '@/components/ui/IconChip'
import type { Tone } from '@/lib/tone'
import { cn } from '@/lib/utils'

interface CollapsibleCardHeaderProps {
  open: boolean
  onToggle: () => void
  title: React.ReactNode
  /** Collapsed summary. Shown in both states — it is the card's subtitle, not a peek. */
  description?: React.ReactNode
  /**
   * `id` of the element this header expands. Required rather than optional: without it
   * `aria-controls` cannot be emitted, and a screen reader is told a control exists but
   * not what it operates. Put the same value on the `<CardContent>`.
   */
  contentId: string
  /** A lucide component in a tinted well before the title, as `SectionHeader` draws it. */
  icon?: LucideIcon
  iconTone?: Tone
  /**
   * A figure before the chevron (the section's total, say). Text only: it sits inside the
   * toggle button, and a control nested in a button is unreachable.
   */
  aside?: React.ReactNode
  className?: string
}

/**
 * The chevron + title + description header that toggles a card open.
 *
 * Four cards hand-rolled this identically with `onClick` on `<CardHeader>` — a plain
 * `<div>`, so Monthly Returns, Money In per Month, Dividend Income and Performance
 * Attribution could not be opened without a mouse at all: no `tabIndex`, no key
 * handler, no `role`, and nothing announcing that they collapse.
 *
 * A real `<button>` fixes all of that at once — Tab reaches it, Enter and Space
 * activate it, and the focus ring comes from the same `focus-visible` classes the rest
 * of the app uses. `PositionsList`'s sortable headers already took this approach; this
 * is the same move for card headers.
 */
export function CollapsibleCardHeader({
  open,
  onToggle,
  title,
  description,
  contentId,
  icon,
  iconTone,
  aside,
  className,
}: CollapsibleCardHeaderProps) {
  return (
    // The padding moves onto the button so the whole header area stays clickable and
    // the focus ring traces the header rather than a smaller inner box.
    <CardHeader className={cn('p-0', className)}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={contentId}
        // Same horizontal token as CardHeader, since this button *is* the header's
        // padded box — hardcoding `px-6` here would leave one collapsible card at
        // desktop padding on a phone while every other card shrank. Vertically a
        // closed row is a list item, not a panel, so it sits a little tighter.
        // Title and summary lead; the chevron sits at the far edge in a quiet round well,
        // the way a disclosure row reads in most finance apps.
        className="group flex w-full select-none items-center justify-between gap-3 rounded-2xl px-[var(--card-padding)] py-4 text-left sm:py-5 ring-offset-background transition-colors hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <div className="flex min-w-0 items-center gap-3">
          {icon && <IconChip icon={icon} tone={iconTone} size="md" />}
          <div className="min-w-0 space-y-1">
            <CardTitle>{title}</CardTitle>
            {description !== undefined && <CardDescription>{description}</CardDescription>}
          </div>
        </div>
        {aside !== undefined && (
          <span className="ml-auto shrink-0 text-sm font-semibold tabular-nums">{aside}</span>
        )}
        <span
          aria-hidden="true"
          className="grid h-8 w-8 shrink-0 place-items-center rounded-full border border-border/80 text-muted-foreground transition-colors group-hover:bg-muted group-hover:text-foreground"
        >
          <ChevronDown
            className={cn('h-4 w-4 transition-transform duration-200', open && 'rotate-180')}
          />
        </span>
      </button>
    </CardHeader>
  )
}
