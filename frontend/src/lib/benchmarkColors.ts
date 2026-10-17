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
  '#7a3e9d', // purple   S&P 500
  '#a61e4d', // crimson  Nasdaq
  '#a9710f', // ochre    MSCI World
  '#c2378f', // berry
  '#7f4f24', // brown
  '#e34948', // red
  '#d6336c', // rose
  '#b5452f', // rust
]

/**
 * The value chart's own lines, which no benchmark may share a hue with.
 *
 * Slot 0 of the old benchmark palette was the portfolio line's own blue and slot 5 the
 * Cash line's teal, so comparing against the S&P 500 drew it in the portfolio's colour,
 * and Cash and Profit/Loss (teal beside emerald, ΔE 5 for full colour vision) could not be
 * told apart either. Value and Cash are the app's validated viz hues (the old
 * `--viz-series-1` and `-4`, kept here as hexes), Profit/Loss a green stepped to clear both and the neutral Money In. The benchmarks were then picked by
 * a greedy max-separation search against them — the first three, which the picker's
 * three-at-a-time cap makes the likeliest trio, clear ΔE 15 from each other and from every
 * line here; the rest keep clear of the portfolio's lines and lean on their dashes and
 * names. `benchmarkColors.test.ts` measures both.
 */
export const PORTFOLIO_SERIES_COLORS = {
  value: '#2a78d6',
  moneyIn: '#94a3b8',
  cash: '#eda100',
  profit: '#2f9e5b',
} as const

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
