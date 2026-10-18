import { useCallback, useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'

interface ScrollableTableProps {
  children: React.ReactNode
  /** Names the scroll region for screen readers, e.g. "Watchlist table". */
  label: string
  className?: string
  /**
   * The table pins its first column (`position: sticky`). The left fade would then lie
   * over the pinned cells, so it is dropped; the pinned column draws its own edge, keyed
   * off the scroller's `data-scrolled` attribute.
   */
  pinnedStart?: boolean
}

/**
 * A horizontally scrollable table container that says so.
 *
 * `overflow-x-auto` alone hides content with no cue: the dividends position table runs
 * ten columns and Watchlist eighteen, so on a laptop the right-hand columns simply are
 * not there and nothing suggests otherwise. An edge fade appears on whichever side has
 * content beyond it and disappears when there is none — the affordance is only useful
 * while it is true, and a permanent gradient on a wide screen is just decoration.
 *
 * It is also a real scroll region: `tabIndex={0}` plus `role="region"` make it
 * reachable and scrollable by keyboard, which an `overflow-x-auto` div is not.
 */
export function ScrollableTable({ children, label, className, pinnedStart = false }: ScrollableTableProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [edges, setEdges] = useState({ left: false, right: false })

  const measure = useCallback(() => {
    const el = ref.current
    if (!el) return
    const maxScroll = el.scrollWidth - el.clientWidth
    // A pixel of slack: fractional layout widths otherwise leave the fade stuck on at
    // full width, which is exactly the "decoration" case this avoids.
    const next = { left: el.scrollLeft > 1, right: el.scrollLeft < maxScroll - 1 }
    // Compared before setting: the scroll handler fires continuously during a drag,
    // and a fresh object every time would re-render the whole table on each event
    // even though the fades only change at the two ends.
    setEdges(prev => (prev.left === next.left && prev.right === next.right ? prev : next))
  }, [])

  useEffect(() => {
    const el = ref.current
    if (!el) return

    measure()
    // Both are needed: scroll for the user moving within the table, resize for the
    // window changing or the data arriving and changing scrollWidth under it.
    el.addEventListener('scroll', measure, { passive: true })
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    for (const child of Array.from(el.children)) observer.observe(child)
    // Attached once per mount, not per render: `children` is a new element on every parent
    // render, and depending on it rebuilt both listeners and forced a layout each time. A
    // child the parent swaps out (table for empty state) is picked up here instead.
    const mutations = new MutationObserver((records) => {
      for (const record of records) {
        for (const node of Array.from(record.addedNodes)) {
          if (node instanceof Element) observer.observe(node)
        }
        for (const node of Array.from(record.removedNodes)) {
          if (node instanceof Element) observer.unobserve(node)
        }
      }
      measure()
    })
    mutations.observe(el, { childList: true })

    return () => {
      el.removeEventListener('scroll', measure)
      observer.disconnect()
      mutations.disconnect()
    }
  }, [measure])

  return (
    <div className="relative">
      <div
        ref={ref}
        tabIndex={0}
        role="region"
        aria-label={`${label} (scrolls horizontally)`}
        data-scrolled={edges.left || undefined}
        // `relative` makes the scroller the containing block of any absolute descendant
        // (an `sr-only` header span is one): otherwise it resolves to the wrapper outside
        // the scroller, sits at the table's far edge unclipped, and widens the page.
        className={cn(
          'relative overflow-x-auto rounded-sm ring-offset-background',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
          className
        )}
      >
        {children}
      </div>

      {/* pointer-events-none so the fades never intercept a click on a cell beneath. */}
      {edges.left && !pinnedStart && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r from-card to-transparent"
        />
      )}
      {edges.right && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-card to-transparent"
        />
      )}
    </div>
  )
}
