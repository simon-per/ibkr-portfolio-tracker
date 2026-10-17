/**
 * A box the viewer can make taller or shorter by dragging its bottom edge.
 *
 * Pointer events with capture rather than mouse events, so a finger drags it too, and
 * the drag keeps following the pointer once it leaves the handle. The handle is a
 * focusable `separator`, so the arrow keys resize it as well (down grows, matching the
 * drag). The height is a per-viewer convenience, stored through `lib/storage.ts`, and
 * written once when a drag ends rather than on every frame of it.
 */
import { useCallback, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent } from 'react'
import { readStored, writeStored } from './storage'

export interface ResizableHeightOptions {
  storageKey: string
  initial: number
  min: number
  max: number
  /** Pixels per arrow-key press. */
  step?: number
}

export function clampHeight(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)))
}

/** The stored height, or `fallback` when none is stored or it is not a number. */
export function storedHeight(key: string, fallback: number, min: number, max: number): number {
  const raw = readStored(key)
  const parsed = raw === null ? NaN : Number(raw)
  return clampHeight(Number.isFinite(parsed) ? parsed : fallback, min, max)
}

export function useResizableHeight({
  storageKey, initial, min, max, step = 20,
}: ResizableHeightOptions) {
  const [height, setHeight] = useState(() => storedHeight(storageKey, initial, min, max))
  const drag = useRef<{ startY: number; startHeight: number } | null>(null)

  const commit = useCallback((value: number) => {
    const next = clampHeight(value, min, max)
    setHeight(next)
    writeStored(storageKey, String(next))
  }, [storageKey, min, max])

  const end = (e: PointerEvent<HTMLElement>) => {
    const d = drag.current
    if (!d) return
    drag.current = null
    if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
    commit(d.startHeight + e.clientY - d.startY)
  }

  const handleProps = {
    role: 'separator' as const,
    'aria-orientation': 'horizontal' as const,
    'aria-valuemin': min,
    'aria-valuemax': max,
    'aria-valuenow': height,
    tabIndex: 0,
    onPointerDown: (e: PointerEvent<HTMLElement>) => {
      e.preventDefault()
      e.currentTarget.setPointerCapture?.(e.pointerId)
      drag.current = { startY: e.clientY, startHeight: height }
    },
    onPointerMove: (e: PointerEvent<HTMLElement>) => {
      const d = drag.current
      if (d) setHeight(clampHeight(d.startHeight + e.clientY - d.startY, min, max))
    },
    onPointerUp: end,
    onPointerCancel: end,
    onKeyDown: (e: KeyboardEvent<HTMLElement>) => {
      const to = {
        ArrowDown: height + step,
        ArrowUp: height - step,
        Home: min,
        End: max,
      }[e.key]
      if (to === undefined) return
      e.preventDefault()
      commit(to)
    },
  }

  return { height, handleProps }
}
