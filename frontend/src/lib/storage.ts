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
