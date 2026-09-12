/**
 * Benchmark line colours, assigned by identity.
 *
 * Both the chart and the picker used to index this palette by the benchmark's position in
 * the *selection* (`selected.indexOf(key)`, `selected.map((key, i) => …)`), so deselecting
 * the first of three recoloured the other two — the reader who had learned "S&P is blue"
 * watched blue become the Nasdaq. Colouring by the benchmark's position in the full
 * `/benchmarks` list gives each index one colour for as long as the list is ordered the
 * same, whatever is selected. Same rule `AllocationTab`'s `mappedColor` states for
 * categories: colour by identity, never by row position.
 */
export const BENCHMARK_COLORS = [
  '#3b82f6', // blue
  '#ec4899', // pink
  '#f97316', // orange
  '#06b6d4', // cyan
  '#a855f7', // purple
  '#14b8a6', // teal
  '#ef4444', // red
  '#84cc16', // lime
]

/**
 * For a key the list does not carry — a stored selection naming a benchmark the backend
 * no longer offers, or the list not yet loaded. Grey says "unidentified"; borrowing slot
 * 0's blue would impersonate the first real benchmark.
 */
export const UNLISTED_BENCHMARK_COLOR = '#6b7280'

export function benchmarkColor(
  key: string,
  all: readonly { key: string }[] | undefined,
): string {
  const index = all?.findIndex((b) => b.key === key) ?? -1
  if (index < 0) return UNLISTED_BENCHMARK_COLOR
  return BENCHMARK_COLORS[index % BENCHMARK_COLORS.length]
}
