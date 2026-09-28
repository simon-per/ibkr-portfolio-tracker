import type { ReactNode } from 'react'
import { Button } from './button'
import { cn } from '@/lib/utils'

/**
 * A row of mutually related toggle buttons: `role="group"` plus `aria-pressed`.
 *
 * Extracted on 2026-09-28 when the header's Stocks | Crypto switch would have been the
 * sixth hand-rolled copy — Activity (twice), Analytics (twice), Dividends and Look-through
 * each carried one, and one of the six had already lost its `aria-pressed`. The scan in
 * `SegmentedControl.test.tsx` keeps a seventh from appearing.
 *
 * **Never tab roles.** These switch what a panel shows, they do not own a tab panel, and
 * the e2e suite asserts exactly one `tablist` on the page (the section strip).
 *
 * Two looks, both already in the app:
 *   segmented  one bordered control with a filled active segment (Dividends' Monthly | TTM)
 *   buttons    a row of small outline buttons, filled when active (the range pickers)
 *
 * Single-select by default (`value` + `onChange`); `isSelected` + `onToggle` make it a set
 * of independent toggles, which is what Activity's event-type filter is.
 */
export interface SegmentedOption<T extends string | number> {
  value: T
  label: ReactNode
  /** The accessible name, where the visible label is an icon or hidden at some widths. */
  ariaLabel?: string
  title?: string
  className?: string
}

interface CommonProps<T extends string | number> {
  /** Names the group for assistive technology; a `role="group"` needs one. */
  label: string
  options: readonly SegmentedOption<T>[]
  variant?: 'segmented' | 'buttons'
  className?: string
  buttonClassName?: string
}

interface SingleSelectProps<T extends string | number> extends CommonProps<T> {
  value: T
  onChange: (value: T) => void
  isSelected?: undefined
  onToggle?: undefined
}

interface ToggleSetProps<T extends string | number> extends CommonProps<T> {
  isSelected: (value: T) => boolean
  onToggle: (value: T) => void
  value?: undefined
  onChange?: undefined
}

export type SegmentedControlProps<T extends string | number> =
  | SingleSelectProps<T>
  | ToggleSetProps<T>

const SEGMENT_CLASS =
  'h-8 rounded px-3 text-sm font-medium transition-colors ring-offset-background ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1'

export function SegmentedControl<T extends string | number>(props: SegmentedControlProps<T>) {
  const { label, options, variant = 'segmented', className, buttonClassName } = props
  const pressed = (value: T) =>
    props.isSelected ? props.isSelected(value) : props.value === value
  const choose = (value: T) => {
    if (props.onToggle) props.onToggle(value)
    else props.onChange?.(value)
  }

  if (variant === 'buttons') {
    return (
      <div role="group" aria-label={label} className={cn('flex gap-1', className)}>
        {options.map(option => {
          const active = pressed(option.value)
          return (
            <Button
              key={String(option.value)}
              type="button"
              size="sm"
              variant={active ? 'default' : 'outline'}
              aria-pressed={active}
              aria-label={option.ariaLabel}
              title={option.title}
              onClick={() => choose(option.value)}
              className={cn(buttonClassName, option.className)}
            >
              {option.label}
            </Button>
          )
        })}
      </div>
    )
  }

  return (
    <div
      role="group"
      aria-label={label}
      className={cn('inline-flex rounded-md border border-input p-0.5', className)}
    >
      {options.map(option => {
        const active = pressed(option.value)
        return (
          <button
            key={String(option.value)}
            type="button"
            aria-pressed={active}
            aria-label={option.ariaLabel}
            title={option.title}
            onClick={() => choose(option.value)}
            className={cn(
              SEGMENT_CLASS,
              active ? 'bg-primary text-primary-foreground' : 'hover:bg-accent',
              buttonClassName,
              option.className,
            )}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}
