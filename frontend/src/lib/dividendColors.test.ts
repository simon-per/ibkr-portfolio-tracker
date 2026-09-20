/// <reference types="node" />
// `tsconfig.app.json` scopes types to `vite/client`, and this file is the one place that
// reads a source file off disk. Vite's `?raw` is not an option: vitest replaces CSS imports
// with empty strings unless CSS processing is on, so the palette would measure as absent
// and every assertion below would pass vacuously.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  dividendColor, hasIdentity, OTHER_COLOR, SERIES_HUES, SERIES_IDENTITIES, SERIES_MUTED,
} from './dividendColors'
import { OTHER } from './dividendChart'

/**
 * Two questions, both asked of the family rather than of an instance.
 *
 * **Can a view repaint a holding?** `dividendColor` takes the symbol and the server's
 * `stack_order` and nothing else, so the answer is structural — but a future edit could
 * hand it the visible set again, which is exactly how the old version worked. The first
 * block pins the rank-only contract; `dividendChart.test.ts` asks the same question end to
 * end across ranges.
 *
 * **Can every slot be told apart?** The separation used to be a measurement written in a
 * docblock, true of the fixed order the palette shipped with. Colour follows the holding
 * now, so any two slots can sit side by side and the pairlist is all-pairs — a claim worth
 * enforcing rather than restating. So this reads the real values out of `index.css`,
 * including the card surfaces, and measures them: a re-step that breaks the guarantee fails
 * here instead of shipping.
 *
 * Tritanopia is deliberately absent from `CVD`. No eight-colour palette on record clears it
 * on an all-pairs list — Okabe-Ito measures 2.9, Tol muted 3.6 — and the palette this one
 * replaced did not either. Asserting it would fail permanently and teach the next reader to
 * skip the file.
 */

const CSS = readFileSync(fileURLToPath(new URL('../index.css', import.meta.url)), 'utf8')

/**
 * The bars the hexes in `index.css` are stepped to clear.
 *
 * `HUE_BAR` is 9.4 rather than a rounder number because 9.46 is the ceiling: it is dark
 * `--viz-sector-2` against `--viz-sector-4`, two hues this palette borrows and may not move
 * without changing the Allocation tab. Light reaches 9.82.
 */
const HUE_BAR = 9.4          // hue vs hue, and hue vs the Other grey
const RAMP_HUE_BAR = 8.4     // a muted step vs any hue, and vs the Other grey
const RAMP_GAP = 6.0         // a muted step vs its neighbour
const SURFACE_RATIO = 3.0    // a muted step against the card it is drawn on
const CVD_CHROMA_FLOOR = 14  // below this a hue IS a neutral, to that reader

type Rgb = [number, number, number]
type Theme = 'light' | 'dark'

/**
 * The `theme`-th value of a custom property.
 *
 * `:root` and `.dark` each appear several times in `index.css`, so the blocks are found by
 * the property itself rather than by selector: light is declared first, dark second.
 */
function cssVar(theme: Theme, name: string): string {
  const found = [...CSS.matchAll(new RegExp(`--${name}:\\s*([^;]+);`, 'g'))].map((m) => m[1].trim())
  if (found.length !== 2) {
    throw new Error(`index.css declares --${name} ${found.length} times, expected 2`)
  }
  return found[theme === 'light' ? 0 : 1]
}

function hues(theme: Theme): string[] {
  return Array.from({ length: SERIES_HUES }, (_, i) => cssVar(theme, `viz-series-${i + 1}`))
}

function mutedSteps(theme: Theme): string[] {
  return Array.from({ length: SERIES_MUTED }, (_, i) => cssVar(theme, `viz-series-muted-${i + 1}`))
}

/** `--card` is stored as an HSL triple for Tailwind; the fills are measured against it. */
function cardSurface(theme: Theme): Rgb {
  const [h, s, l] = cssVar(theme, 'card').split(/\s+/).map((part) => Number(part.replace('%', '')))
  const sat = s / 100
  const light = l / 100
  const c = (1 - Math.abs(2 * light - 1)) * sat
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1))
  const m = light - c / 2
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
    : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x]
  return [r + m, g + m, b + m]
}

// ---------------------------------------------------------------- colour maths
const hexToRgb = (hex: string): Rgb =>
  [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255) as Rgb

const toLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)

function relativeLuminance([r, g, b]: Rgb): number {
  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b)
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

function toLab([r, g, b]: Rgb): [number, number, number] {
  const [R, G, B] = [toLinear(r), toLinear(g), toLinear(b)]
  const x = (0.4124564 * R + 0.3575761 * G + 0.1804375 * B) / 0.95047
  const y = 0.2126729 * R + 0.7151522 * G + 0.072175 * B
  const z = (0.0193339 * R + 0.119192 * G + 0.9503041 * B) / 1.08883
  const f = (t: number) => (t > 216 / 24389 ? Math.cbrt(t) : (841 / 108) * t + 4 / 29)
  const [fx, fy, fz] = [f(x), f(y), f(z)]
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)]
}

/**
 * Viénot, Brettel & Mollon (1999) on gamma-encoded sRGB — the model the `--viz-*` docblocks
 * quote: `#3b82f6` against `#8b5cf6` measures 1.1 deuteran here, against the 1.3 recorded in
 * `docs/lookthrough.md`.
 */
function simulate([r, g, b]: Rgb, kind: 'normal' | 'protan' | 'deutan'): Rgb {
  if (kind === 'normal') return [r, g, b]
  let L = 17.8824 * r + 43.5161 * g + 4.11935 * b
  let M = 3.45565 * r + 27.1554 * g + 3.86714 * b
  const S = 0.0299566 * r + 0.184309 * g + 1.46709 * b
  if (kind === 'protan') L = 2.02344 * M - 2.52581 * S
  else M = 0.494207 * L + 1.24827 * S
  return [
    0.0809444479 * L - 0.130504409 * M + 0.116721066 * S,
    -0.0102485335 * L + 0.0540193266 * M - 0.113614708 * S,
    -0.000365296938 * L - 0.00412161469 * M + 0.693511405 * S,
  ].map((c) => Math.min(1, Math.max(0, c))) as Rgb
}

function deltaE2000(l1: number[], l2: number[]): number {
  const [L1, a1, b1] = l1
  const [L2, a2, b2] = l2
  const C1 = Math.hypot(a1, b1)
  const C2 = Math.hypot(a2, b2)
  const Cb = (C1 + C2) / 2
  const G = 0.5 * (1 - Math.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
  const a1p = (1 + G) * a1
  const a2p = (1 + G) * a2
  const C1p = Math.hypot(a1p, b1)
  const C2p = Math.hypot(a2p, b2)
  const rad = Math.PI / 180
  const h1p = (Math.atan2(b1, a1p) / rad + 360) % 360
  const h2p = (Math.atan2(b2, a2p) / rad + 360) % 360
  const dLp = L2 - L1
  const dCp = C2p - C1p
  const dhp = C1p * C2p === 0 ? 0
    : Math.abs(h2p - h1p) <= 180 ? h2p - h1p
      : h2p - h1p > 180 ? h2p - h1p - 360 : h2p - h1p + 360
  const dHp = 2 * Math.sqrt(C1p * C2p) * Math.sin((dhp * rad) / 2)
  const Lbp = (L1 + L2) / 2
  const Cbp = (C1p + C2p) / 2
  const hbp = C1p * C2p === 0 ? h1p + h2p
    : Math.abs(h1p - h2p) <= 180 ? (h1p + h2p) / 2
      : h1p + h2p < 360 ? (h1p + h2p + 360) / 2 : (h1p + h2p - 360) / 2
  const T = 1 - 0.17 * Math.cos((hbp - 30) * rad) + 0.24 * Math.cos(2 * hbp * rad)
    + 0.32 * Math.cos((3 * hbp + 6) * rad) - 0.2 * Math.cos((4 * hbp - 63) * rad)
  const dTheta = 30 * Math.exp(-(((hbp - 275) / 25) ** 2))
  const Rc = 2 * Math.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7))
  const Sl = 1 + (0.015 * (Lbp - 50) ** 2) / Math.sqrt(20 + (Lbp - 50) ** 2)
  const Sc = 1 + 0.045 * Cbp
  const Sh = 1 + 0.015 * Cbp * T
  const Rt = -Math.sin(2 * dTheta * rad) * Rc
  return Math.sqrt(
    (dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh),
  )
}

const CVD = ['normal', 'protan', 'deutan'] as const

/** The worst a pair looks to any of the vision types above. */
function separation(a: string, b: string): number {
  return Math.min(...CVD.map((kind) =>
    deltaE2000(toLab(simulate(hexToRgb(a), kind)), toLab(simulate(hexToRgb(b), kind)))))
}

/** The smallest chroma a colour keeps under CVD. Near zero means "a neutral, to them". */
function survivingChroma(hex: string): number {
  return Math.min(...CVD.map((kind) => {
    const [, a, b] = toLab(simulate(hexToRgb(hex), kind))
    return Math.hypot(a, b)
  }))
}

// ---------------------------------------------------------------- the assignment rule
describe("a holding's colour comes from its rank and nothing else", () => {
  const order = ['AAA', 'BBB', 'CCC', 'DDD', 'EEE', 'FFF', 'GGG', 'HHH',
                 'III', 'JJJ', 'KKK', 'LLL', 'MMM', 'NNN', 'OOO']

  it('gives every identity its own colour, and the fold the Other grey', () => {
    const colors = order.slice(0, SERIES_IDENTITIES).map((s) => dividendColor(s, order))
    expect(new Set(colors).size).toBe(SERIES_IDENTITIES)
    expect(colors).not.toContain(OTHER_COLOR)
    expect(dividendColor(OTHER, order)).toBe(OTHER_COLOR)
  })

  it('folds everything past the identities, and anything it has never seen', () => {
    expect(dividendColor(order[SERIES_IDENTITIES], order)).toBe(OTHER_COLOR)
    expect(dividendColor('NOPE', order)).toBe(OTHER_COLOR)
    expect(hasIdentity(order[SERIES_IDENTITIES - 1], order)).toBe(true)
    expect(hasIdentity(order[SERIES_IDENTITIES], order)).toBe(false)
    expect(hasIdentity('NOPE', order)).toBe(false)
  })

  it('takes the order and the symbol, and nothing about the current view', () => {
    // The old signature was (symbol, stackSymbols, palette), where `stackSymbols` was the
    // top eight of the selected range — which is exactly how a range change repainted the
    // chart. Arity is a crude pin, and it is the one that would catch the regression.
    expect(dividendColor.length).toBe(2)
    for (const symbol of order.slice(0, SERIES_IDENTITIES)) {
      expect(dividendColor(symbol, order)).toBe(dividendColor(symbol, [...order]))
    }
  })

  it('counts the slots the same way index.css fills them', () => {
    expect(SERIES_IDENTITIES).toBe(SERIES_HUES + SERIES_MUTED)
    expect(hues('light')).toHaveLength(SERIES_HUES)
    expect(mutedSteps('dark')).toHaveLength(SERIES_MUTED)
    // One past the last must not exist, or raising the count hands the new slot a colour
    // nothing ever measured — the failure the old MAX_SERIES pin was written for.
    expect(CSS).not.toContain(`--viz-series-${SERIES_HUES + 1}:`)
    expect(CSS).not.toContain(`--viz-series-muted-${SERIES_MUTED + 1}:`)
  })
})

// ---------------------------------------------------------------- the measurements
describe.each(['light', 'dark'] as const)('the %s palette clears its pairlist', (theme) => {
  const hue = hues(theme)
  const muted = mutedSteps(theme)
  const other = cssVar(theme, 'viz-series-other')
  const surface = cardSurface(theme)

  it('separates every hue from every other hue, and from the fold', () => {
    const all = [...hue, other]
    for (let i = 0; i < all.length; i++) {
      for (let j = i + 1; j < all.length; j++) {
        const value = separation(all[i], all[j])
        expect(value, `${all[i]} vs ${all[j]}`).toBeGreaterThanOrEqual(HUE_BAR)
      }
    }
  })

  it('keeps the muted steps apart from each other by lightness', () => {
    for (let i = 0; i < muted.length - 1; i++) {
      const gap = deltaE2000(toLab(hexToRgb(muted[i])), toLab(hexToRgb(muted[i + 1])))
      expect(gap, `${muted[i]} vs ${muted[i + 1]}`).toBeGreaterThanOrEqual(RAMP_GAP)
    }
  })

  it('keeps every muted step off every hue and off the fold', () => {
    for (const step of muted) {
      for (const against of [...hue, other]) {
        expect(separation(step, against), `${step} vs ${against}`)
          .toBeGreaterThanOrEqual(RAMP_HUE_BAR)
      }
    }
  })

  it('keeps the muted steps and the fold visible on the card they are drawn on', () => {
    // The case that fails silently: a thin de-emphasis segment against its own surface.
    // The ramp is low-chroma, so lightness is the only thing holding it up.
    for (const fill of [...muted, other]) {
      expect(contrast(hexToRgb(fill), surface), `${fill} on the card`)
        .toBeGreaterThanOrEqual(SURFACE_RATIO)
    }
  })

  it('does not let a hue collapse toward a neutral under CVD', () => {
    // What forbids a muted step its lightness band: a hue that projects to near-zero chroma
    // IS a neutral to that reader, whatever it looks like to everyone else.
    // `--viz-sector-3` is the documented exception — a fixed sector hue that does collapse,
    // which is why the ramp is stepped around the band it lands in rather than through it.
    const sectorPink = cssVar(theme, 'viz-sector-3')
    for (const c of hue) {
      if (c === sectorPink) continue
      expect(survivingChroma(c), `${c} under CVD`).toBeGreaterThanOrEqual(CVD_CHROMA_FLOOR)
    }
  })

  it('reuses the four validated sector hues rather than growing a second blue', () => {
    // Slots 1, 4, 5 and 6 ARE --viz-sector-1..4. Stated as a test because the whole re-step
    // was scoped around not moving them: if one drifts, the Allocation tab and the dividend
    // stack have quietly become two palettes that merely look alike.
    for (const [slot, sector] of [[1, 1], [4, 2], [5, 3], [6, 4]]) {
      expect(cssVar(theme, `viz-series-${slot}`)).toBe(cssVar(theme, `viz-sector-${sector}`))
    }
  })
})
