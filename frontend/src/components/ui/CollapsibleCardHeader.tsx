import { ChevronRight } from 'lucide-react'
import { CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
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
        // Same padding token as CardHeader, since this button *is* the header's
        // padded box — hardcoding `p-6` here would leave one collapsible card at
        // desktop padding on a phone while every other card shrank.
        className="flex w-full select-none items-center gap-2 rounded-lg p-[var(--card-padding)] text-left ring-offset-background transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <ChevronRight
          aria-hidden="true"
          className={cn(
            'h-5 w-5 shrink-0 text-muted-foreground transition-transform duration-200',
            open && 'rotate-90'
          )}
        />
        <div className="min-w-0 space-y-1.5">
          <CardTitle>{title}</CardTitle>
          {description !== undefined && <CardDescription>{description}</CardDescription>}
        </div>
      </button>
    </CardHeader>
  )
}
