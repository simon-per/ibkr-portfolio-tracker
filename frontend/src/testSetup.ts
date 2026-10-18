/**
 * Runs before every test file (`test.setupFiles` in vite.config.ts).
 *
 * Testing Library's async queries — `findBy*`, `waitFor` — give up after **1 s** by
 * default. A component test that waits on a mocked fetch, a lazy chunk and a re-render
 * takes well under that on a quiet machine and over it when the CPU is shared: the suite
 * failed one test in one of nine runs that overlapped the backend suite (2026-10-17), and
 * the slowest component test measured 3 s under that load. 4 s keeps a real absence
 * failing with Testing Library's own "Unable to find…" message, inside vitest's 5 s
 * per-test timeout, instead of a race against the scheduler.
 *
 * Only in jsdom files: the node-environment majority has no DOM and never imports it.
 */
if (typeof document !== 'undefined') {
  const { configure } = await import('@testing-library/react')
  configure({ asyncUtilTimeout: 4000 })

  // jsdom has no ResizeObserver, and `ScrollableTable`, Recharts' `ResponsiveContainer` and
  // the Fundamentals history page all construct one. A browser has had it since 2020, so this
  // is a gap in the test environment, not something a component should guard against. It
  // was copied into ~27 test files in two styles; a test that needs to watch the observer
  // (`renderIdentity.test.tsx`) still stubs its own with `vi.stubGlobal`.
  // `matchMedia` stays unstubbed on purpose: its absence is the desktop fallback every
  // component test relies on (docs/frontend.md, *The mobile layout*).
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
}
