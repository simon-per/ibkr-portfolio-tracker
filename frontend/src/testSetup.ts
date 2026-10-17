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
}
