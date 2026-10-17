import { useCallback, useState } from 'react'

/**
 * `localStorage`, behind one guard.
 *
 * Private-mode Safari and some embedded webviews throw on any access to the object, and
 * a full quota throws on write. Three call sites had grown a try/catch of their own
 * (`getApiKey`, `readTargets`, `readSelectedBenchmarks`) while five more read and wrote
 * bare — so whether a stored preference could take a tab down depended on which tab had
 * stored it. One implementation, so the next preference cannot be the sixth unguarded
 * one; the source scan in `storage.test.ts` holds the line.
 *
 * Shape validation stays with the caller: these return what the browser stored, or
 * `null`, and say nothing about whether it parses. `readTargets` and
 * `readSelectedBenchmarks` keep their own checks for well-formed JSON of the wrong shape,
 * which no storage guard can see.
 */
export function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

/** Stores `value`, or removes the key when `value` is `null`. Never throws. */
export function writeStored(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch {
    // Nothing to do: the preference simply does not survive the session.
  }
}

/** Where `useRememberedOpen` keeps a section's state: one key per section, `1` or `0`. */
export const SECTION_OPEN_PREFIX = 'section-open:'

/**
 * A collapsible section's open state, remembered on this browser.
 *
 * Every collapsible card started closed on each visit, so a reader who always wants the
 * monthly-returns grid open paid a click for it every time — a per-viewer convenience,
 * which is exactly what `localStorage` is for here. Anything but a stored `1`/`0` is the
 * section's own default, so a corrupt value cannot open or close anything.
 */
export function useRememberedOpen(
  section: string,
  initial: boolean | (() => boolean) = false,
): [boolean, (open: boolean | ((prev: boolean) => boolean)) => void] {
  const key = `${SECTION_OPEN_PREFIX}${section}`
  const [open, setOpen] = useState<boolean>(() => {
    const stored = readStored(key)
    if (stored === '1') return true
    if (stored === '0') return false
    return typeof initial === 'function' ? initial() : initial
  })
  const update = useCallback(
    (next: boolean | ((prev: boolean) => boolean)) => {
      setOpen((prev) => {
        const value = typeof next === 'function' ? next(prev) : next
        writeStored(key, value ? '1' : '0')
        return value
      })
    },
    [key],
  )
  return [open, update]
}
