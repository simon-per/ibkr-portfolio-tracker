import { CHART_OTHER, chartColor } from './chartPalette'

/**
 * A categorical fill plus the ink that reads on it, for a chart that writes a label onto its
 * own marks (the look-through treemap's tiles, the composition bar's segments).
 *
 * `chartPalette.ts` hands out fills only; a label needs one more decision per slot. **The ink
 * is per slot, never per theme**: white clears 3:1 on seven slots in both themes, and the
 * three light ones (lime, sky, amber) take near-black, which clears 5:1 on them in both. One
 * ink per slot means no theme is read in JavaScript, exactly as with the fills.
 * `chartPaint.test.ts` re-measures every pair out of `index.css`, so a palette re-step that
 * makes a label unreadable fails there rather than on a screen nobody is looking at.
 *
 * The three neutrals are the tails and structural blocks a chart draws beside its hues: the
 * palette's Other, and the same grey a step toward the card (fainter) and a step toward the
 * ink (stronger). Mixed from `--chart-other` with `color-mix`, so they follow the theme with
 * no token of their own — and stay three distinguishable meanings rather than three uses of
 * one grey (docs/lookthrough.md, *every fill carries its own ink*).
 */

export interface ChartPaint {
  fill: string
  ink: string
}

/** Label ink for the three light slots. Near-black, fixed: it must not flip with the theme. */
export const DARK_LABEL_INK = '#020817'
/** Label ink for every other slot. */
export const LIGHT_LABEL_INK = '#ffffff'

/** 0-based slots whose fill is too light for white: lime, sky, amber. */
export const DARK_INK_SLOTS: readonly number[] = [4, 5, 7]

/** Theme-following ink for the neutrals (dark on the light theme's greys, light on the dark's). */
export const NEUTRAL_INK = 'hsl(var(--foreground))'

/** Slot `index`'s fill and ink; past the last slot, the Other neutral. */
export function chartPaint(index: number): ChartPaint {
  const fill = chartColor(index)
  if (fill === CHART_OTHER) return NEUTRAL_PAINT.tail
  return { fill, ink: DARK_INK_SLOTS.includes(index) ? DARK_LABEL_INK : LIGHT_LABEL_INK }
}

/**
 * How each neutral is mixed: `pct` of `--chart-other` into the named surface token.
 * Exported so the measuring test recomputes exactly what the browser paints.
 */
export const NEUTRAL_MIX = {
  faint: { toward: 'card', pct: 55 },
  strong: { toward: 'foreground', pct: 70 },
} as const

const mixed = (key: keyof typeof NEUTRAL_MIX) =>
  `color-mix(in srgb, ${CHART_OTHER} ${NEUTRAL_MIX[key].pct}%, hsl(var(--${NEUTRAL_MIX[key].toward})))`

export type NeutralRole = 'tail' | 'faint' | 'strong'

export const NEUTRAL_PAINT: Record<NeutralRole, ChartPaint> = {
  tail: { fill: CHART_OTHER, ink: NEUTRAL_INK },
  faint: { fill: mixed('faint'), ink: NEUTRAL_INK },
  strong: { fill: mixed('strong'), ink: NEUTRAL_INK },
}
